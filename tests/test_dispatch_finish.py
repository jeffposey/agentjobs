"""The scripted post-approval finish (task-241).

**Real git repositories, real worktrees, real conflicting rebases.** Every interesting
property of this feature is in what it does when a step cannot be completed, and a
mocked ``subprocess`` would assert that the right flags were passed while proving
nothing about whether a conflict is actually aborted or whether a branch survives one.
The task's own acceptance criteria say "demonstrated, not asserted" twice, and this file
is where that is discharged for the mechanism; the live demonstration on this
repository's own branch is on the task record.

The two failures these are really guarding are worth naming, because both are silent:

- **A half-finished merge that reads as either state.** The merge is irreversible and
  everything after it can fail, so the tests below check the *record* after a post-merge
  failure, not just the return value.
- **A finish reported for a delivery that never happened.** ``verify_live`` is given a
  real HTTP server that answers correctly and reports a stale commit, which is exactly
  what a server that was not restarted looks like: the port answers, and the answer is
  wrong.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest
import yaml

from agentjobs.actors import FINISHER, reserved_actors
from agentjobs.dispatch.config import FinishSettings
from agentjobs.dispatch.finish import (
    DECLINED,
    ESCALATED,
    FINISHED,
    POSTURE,
    Escalate,
    Plan,
    active_branches,
    finish_task,
    verify_live,
    worktree_paths,
)
from agentjobs.dispatch.phases import RUN_ID_ENV
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BranchStatus, DispatchPosture, Lifecycle, Outcome
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

GREEN_GATE = "import sys\nsys.exit(0)\n"
RED_GATE = "import sys\nprint('vitest failed')\nsys.exit(1)\n"


def git(root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def head(root: Path, ref: str = "HEAD") -> str:
    return git(root, "rev-parse", ref).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A clone on ``main``, a feature branch in its own worktree, and a task for it.

    Shaped like the real thing on purpose: the task records live inside the clone being
    merged into, which is the arrangement that makes the finisher's own commits land in
    the tree it is working -- and the reason ``commit_task_record`` exists at all.
    """
    root = tmp_path / "clone"
    (root / "tasks").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "src" / "agentjobs").mkdir(parents=True)
    git(tmp_path, "init", "--initial-branch=main", str(root))
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    (root / "scripts" / "check.py").write_text(GREEN_GATE, encoding="utf-8")
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "--", "scripts/check.py", "shared.txt")
    git(root, "commit", "-m", "init")

    branch = "feat/task-001-thing"
    worktree = tmp_path / "worktrees" / "wt"
    git(root, "worktree", "add", "-b", branch, str(worktree), "main")
    # git does not track empty directories, so the worktree has none of the tree above.
    # The default deliverable is deliberately *not* under a served prefix: most merges
    # do not change what a running server holds in memory, and that is the case where a
    # finish honestly needs no restart at all.
    (worktree / "docs").mkdir(parents=True, exist_ok=True)
    (worktree / "docs" / "feature.md").write_text("the deliverable\n", encoding="utf-8")
    git(worktree, "add", "--", "docs/feature.md")
    git(worktree, "commit", "-m", "docs: the deliverable")

    project = Project(id="demo", name="Demo", root=root)
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title="The deliverable",
        category="infrastructure",
        summary="A task with a branch waiting to be merged.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="claude")
    manager.update_task(task.id, actor="claude", branches=[{"name": branch, "status": "active"}])
    git(root, "add", "--", "tasks")
    git(root, "commit", "-m", "chore(tasks): the record")

    home = tmp_path / "home"
    home.mkdir()
    # `worktree_interpreter` asks Poetry, and a temp repository has no Poetry project.
    # The question this suite is asking is never "does Poetry answer" -- it is what the
    # sequence does with the answer -- so it is supplied. The real hazard the function
    # guards (resolving to a neighbouring checkout's environment) has no analogue here.
    monkeypatch.setattr(
        "agentjobs.dispatch.finish.worktree_interpreter",
        lambda path: Path(_interpreter()),
    )
    return {
        "root": root,
        "worktree": worktree,
        "branch": branch,
        "project": project,
        "manager": manager,
        "task_id": task.id,
        "home": home,
    }


def _interpreter() -> str:
    import sys

    return sys.executable


def settings(**overrides: Any) -> FinishSettings:
    base: Dict[str, Any] = {
        "enabled": True,
        "base_branch": "main",
        "gate_timeout_seconds": 300,
        # Two seconds rather than the two-minute default: every test that reaches
        # verification here is deciding whether the answer is right, not waiting for a
        # real server to boot, and the default made the file take five minutes.
        "verify_timeout_seconds": 2,
    }
    base.update(overrides)
    return FinishSettings(**base)


def add_served_change(world: Dict[str, Any]) -> None:
    """Put a change to code the server holds in memory on the branch.

    Separate from the fixture because it changes what finishing *means*: with served code
    in the merge, a restart stops being optional and a machine that cannot restart has to
    escalate rather than report a delivery.
    """
    worktree = world["worktree"]
    (worktree / "src" / "agentjobs").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "agentjobs" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(worktree, "add", "--", "src/agentjobs/feature.py")
    git(worktree, "commit", "-m", "feat: served code")


def merged_into(root: Path, branch: str, base: str = "main") -> bool:
    """Whether ``base`` contains ``branch``.

    The question every "nothing was merged" assertion below actually means. Comparing
    the base's tip against a value captured earlier does *not* answer it: the finisher
    commits its own escalation onto the base, so the tip legitimately moves even when
    nothing was merged.
    """
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", branch, base],
            capture_output=True,
        ).returncode
        == 0
    )


def run(world: Dict[str, Any], **overrides: Any) -> Any:
    return finish_task(
        manager=world["manager"],
        project=world["project"],
        task_id=world["task_id"],
        approver="Jeff Posey",
        home=world["home"],
        api_base=overrides.pop("api_base", "http://127.0.0.1:1"),
        settings=settings(**overrides),
    )


# ----- a version endpoint that can lie the way a stale server lies -------------


class _VersionServer:
    """A real HTTP server answering ``/api/version``, whose answer the test controls."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                body = json.dumps(outer.payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        # Port 0: the kernel picks. Nothing in this suite may bind a fixed port -- the
        # gate runs several of these at once across xdist workers.
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_VersionServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serving() -> Iterator[Any]:
    servers = []

    def start(payload: Dict[str, Any]) -> _VersionServer:
        server = _VersionServer(payload)
        server.__enter__()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.__exit__()


# ----- the happy path ---------------------------------------------------------


class TestTheCommonCase:
    def test_it_merges_closes_and_removes_the_worktree(self, world: Dict[str, Any]) -> None:
        """f1: approval on a clean, green branch finishes with no agent anywhere."""
        result = run(world)

        assert result.outcome == FINISHED, result.render()
        root = world["root"]
        assert merged_into(root, world["branch"])
        # --no-ff, so the merge commit has two parents and is itself the reviewable unit.
        parents = git(root, "rev-list", "--parents", "-n", "1", result.merge_commit).stdout
        assert len(parents.split()) == 3
        assert (root / "docs" / "feature.md").is_file()

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.lifecycle is Lifecycle.CLOSED
        assert task.outcome is Outcome.COMPLETED
        assert task.branches[0].status is BranchStatus.MERGED
        assert task.branches[0].merged_at is not None
        assert world["branch"] not in worktree_paths(root)
        assert not world["worktree"].exists()

    def test_the_merge_message_names_the_task_and_the_approver(self, world: Dict[str, Any]) -> None:
        result = run(world)
        assert result.merge_commit is not None
        message = git(world["root"], "log", "-1", "--format=%B", result.merge_commit).stdout
        assert world["task_id"] in message
        assert "Jeff Posey" in message
        assert "check.py" in message

    def test_the_finisher_is_the_actor_and_it_is_an_agent(self, world: Dict[str, Any]) -> None:
        """A merge nobody thought about must never clock a later dispatch as human."""
        run(world)
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert any(entry.actor == FINISHER for entry in task.log)
        assert not reserved_actors()[FINISHER].is_human

    def test_it_writes_a_finish_record_rather_than_a_run(self, world: Dict[str, Any]) -> None:
        """The saving is only measurable if a finish is not filed as another run."""
        result = run(world)
        assert (world["home"] / "finishes" / result.finish_id / "meta.yaml").is_file()
        runs = world["home"] / "runs"
        # `runs/.locks` is created by the per-task lock a finish shares with a run. What
        # must not exist is a *run*, which is what every runs-per-task figure counts.
        assert (
            not runs.exists()
            or [entry.name for entry in runs.iterdir() if entry.name != ".locks"] == []
        )

    def test_it_leaves_no_run_lock_behind(self, world: Dict[str, Any]) -> None:
        run(world)
        locks = world["home"] / "runs" / ".locks"
        assert not locks.exists() or not list(locks.iterdir())


# ----- declining: never a candidate, so nothing happens -----------------------


class TestDeclining:
    def test_a_task_with_no_active_branch_is_not_a_candidate(self, world: Dict[str, Any]) -> None:
        world["manager"].update_task(world["task_id"], actor="claude", branches=[])
        before = head(world["root"], "main")
        result = run(world)
        assert result.outcome == DECLINED
        assert result.reason == "no_active_branch"
        # A decline writes nothing at all, so here the tip really is untouched.
        assert head(world["root"], "main") == before
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open

    def test_two_active_branches_are_a_judgement_not_a_lookup(self, world: Dict[str, Any]) -> None:
        world["manager"].update_task(
            world["task_id"],
            actor="claude",
            branches=[
                {"name": world["branch"], "status": "active"},
                {"name": "feat/other", "status": "active"},
            ],
        )
        result = run(world)
        assert result.outcome == DECLINED
        assert result.reason == "several_active_branches"

    def test_the_feature_off_declines_without_touching_anything(
        self, world: Dict[str, Any]
    ) -> None:
        before = head(world["root"], "main")
        result = run(world, enabled=False)
        assert result.outcome == DECLINED
        assert result.reason == "not_enabled"
        assert head(world["root"], "main") == before

    def test_a_closed_task_is_not_finished_twice(self, world: Dict[str, Any]) -> None:
        run(world)
        second = run(world)
        assert second.outcome == DECLINED
        assert second.reason == "not_open"


# ----- escalating before the merge -------------------------------------------


class TestBeforeTheMerge:
    def test_a_conflicting_rebase_is_aborted_and_the_branch_is_unchanged(
        self, world: Dict[str, Any]
    ) -> None:
        """f3. The branch's tip is compared byte for byte with what it was."""
        root, worktree = world["root"], world["worktree"]
        # The same line of the same file, differently, on both sides.
        (worktree / "shared.txt").write_text("branch\n", encoding="utf-8")
        git(worktree, "commit", "-am", "branch edit")
        (root / "shared.txt").write_text("main\n", encoding="utf-8")
        git(root, "commit", "-am", "main edit")

        before_branch = head(root, world["branch"])

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "rebase_conflict"
        assert result.merge_commit is None
        assert head(root, world["branch"]) == before_branch, "the branch was not restored"
        assert not merged_into(root, world["branch"])
        # No rebase left in progress: the worktree is usable by whoever owns it.
        assert not (
            Path(git(root, "rev-parse", "--git-path", "rebase-merge").stdout.strip())
        ).exists()
        assert git(worktree, "status", "--porcelain").stdout.strip() == ""

    def test_a_conflicting_rebase_says_so_on_the_record_and_hands_the_ball_back(
        self, world: Dict[str, Any]
    ) -> None:
        (world["worktree"] / "shared.txt").write_text("branch\n", encoding="utf-8")
        git(world["worktree"], "commit", "-am", "branch edit")
        (world["root"] / "shared.txt").write_text("main\n", encoding="utf-8")
        git(world["root"], "commit", "-am", "main edit")

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.is_open
        assert task.ball is Ball.AGENT
        assert "Nothing was merged" in (task.ball_prompt or "")
        assert "rebase" in (task.ball_prompt or "")
        entry = task.log[-2]
        assert entry.data.get("merged") is False
        assert entry.data.get("finish_step") == "rebase"

    def test_a_red_gate_never_merges(self, world: Dict[str, Any]) -> None:
        """f4. The gate is failed on the branch, after a rebase that applied cleanly."""
        (world["worktree"] / "scripts" / "check.py").write_text(RED_GATE, encoding="utf-8")
        git(world["worktree"], "commit", "-am", "break the gate")

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "gate_failed"
        assert result.merge_commit is None
        assert not merged_into(world["root"], world["branch"])
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open
        assert "red gate never merges" in (task.ball_prompt or "")

    def test_a_red_gate_keeps_its_output_where_a_person_can_read_it(
        self, world: Dict[str, Any]
    ) -> None:
        (world["worktree"] / "scripts" / "check.py").write_text(RED_GATE, encoding="utf-8")
        git(world["worktree"], "commit", "-am", "break the gate")
        result = run(world)
        log = world["home"] / "finishes" / result.finish_id / "gate.log"
        assert log.is_file()
        assert "vitest failed" in log.read_text(encoding="utf-8")

    def test_a_clone_on_the_wrong_branch_is_never_checked_out_from_under_anyone(
        self, world: Dict[str, Any]
    ) -> None:
        git(world["root"], "checkout", "-b", "somebody-elses-work")
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "clone_not_on_base"
        assert git(world["root"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == (
            "somebody-elses-work"
        )

    def test_a_missing_worktree_escalates_rather_than_improvising_one(
        self, world: Dict[str, Any]
    ) -> None:
        git(world["root"], "worktree", "remove", str(world["worktree"]))
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "worktree_missing"

    def test_a_dirty_worktree_escalates_rather_than_rebasing_over_it(
        self, world: Dict[str, Any]
    ) -> None:
        (world["worktree"] / "docs" / "feature.md").write_text(
            "edited, not committed\n", encoding="utf-8"
        )
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "worktree_dirty"

    def test_uncommitted_work_in_the_clone_that_the_merge_would_overwrite(
        self, world: Dict[str, Any]
    ) -> None:
        """Somebody is working in the shared clone, on a tracked file this merge changes."""
        git(world["worktree"], "checkout", "-q", world["branch"])
        (world["worktree"] / "shared.txt").write_text("from the branch\n", encoding="utf-8")
        git(world["worktree"], "commit", "-am", "the branch edits a shared file")
        (world["root"] / "shared.txt").write_text("somebody is mid-edit\n", encoding="utf-8")

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "clone_dirty_in_merge"
        assert not merged_into(world["root"], world["branch"])
        # Untouched: their work is theirs, and nothing here reverts it to get a merge in.
        assert (world["root"] / "shared.txt").read_text(
            encoding="utf-8"
        ) == "somebody is mid-edit\n"

    def test_an_untracked_file_the_merge_would_overwrite_is_also_a_clash(
        self, world: Dict[str, Any]
    ) -> None:
        """git refuses this outright rather than overwriting, so it is checked for."""
        (world["root"] / "docs").mkdir(parents=True, exist_ok=True)
        (world["root"] / "docs" / "feature.md").write_text(
            "mine, not committed\n", encoding="utf-8"
        )
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "clone_dirty_in_merge"
        assert (world["root"] / "docs" / "feature.md").read_text(
            encoding="utf-8"
        ) == "mine, not committed\n"


# ----- escalating after the merge: the record must never be ambiguous ---------


class TestAfterTheMerge:
    def test_a_missing_restart_command_escalates_with_the_merge_stated(
        self, world: Dict[str, Any]
    ) -> None:
        """f5, and the ambiguity rule. The merge happened; the delivery did not."""
        add_served_change(world)
        result = run(world, restart=[])

        assert result.outcome == ESCALATED
        assert result.reason == "no_restart_command"
        assert result.merge_commit is not None
        assert merged_into(world["root"], world["branch"])

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.is_open, "closing before delivery would call this completed"
        assert task.outcome is None
        assert "The merge is done" in (task.ball_prompt or "")
        merge_entry = next(entry for entry in task.log if entry.data.get("finish_step") == "merge")
        assert merge_entry.data["merge_commit"] == result.merge_commit
        assert task.log[-2].data.get("merged") is True

    def test_a_merge_touching_no_served_code_needs_no_restart(self, world: Dict[str, Any]) -> None:
        """The other half of the same rule: an honest finish with nothing to restart."""
        result = run(world, restart=[])
        assert result.outcome == FINISHED, result.render()
        assert [step.skipped for step in result.steps if step.step == "restart"] == [True]
        assert [step.skipped for step in result.steps if step.step == "verify"] == [True]

    def test_a_failing_restart_command_escalates(self, world: Dict[str, Any]) -> None:
        result = run(world, restart=[_interpreter(), "-c", "import sys; sys.exit(3)"])
        assert result.outcome == ESCALATED
        assert result.reason == "restart_failed"
        assert result.merge_commit is not None

    def test_a_stale_server_is_not_a_finish(self, world: Dict[str, Any], serving: Any) -> None:
        """f5's sharpest case: the port answers, and the answer is the old commit."""
        stale = serving(
            {
                "source_commit": head(world["root"], "main"),  # pre-merge, deliberately
                "source_root": str(world["root"]),
                "started_at": "2020-01-01T00:00:00+00:00",
            }
        )
        result = run(
            world,
            restart=[_interpreter(), "-c", "pass"],
            verify_base=stale.base,
        )
        assert result.outcome == ESCALATED
        assert result.reason == "not_live"
        assert result.merge_commit is not None
        assert "already running" in result.detail
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open

    def test_the_wrong_checkout_serving_is_refused_without_waiting(
        self, world: Dict[str, Any], serving: Any, tmp_path: Path
    ) -> None:
        """A different clone answering on the address is not a slow restart."""
        elsewhere = serving(
            {
                "source_commit": "f" * 40,
                "source_root": str(tmp_path / "some-other-worktree"),
                "started_at": "2026-08-22T00:00:00+00:00",
            }
        )
        result = run(world, restart=[_interpreter(), "-c", "pass"], verify_base=elsewhere.base)
        assert result.outcome == ESCALATED
        assert result.reason == "wrong_checkout_serving"
        assert result.merge_commit is not None

    def test_a_verified_restart_finishes(
        self, world: Dict[str, Any], serving: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole delivery, including a server that comes back on the merged code."""
        add_served_change(world)
        server = serving({"source_commit": "pending", "source_root": str(world["root"])})
        import agentjobs.dispatch.finish as finish_module

        real_restart = finish_module.restart_server

        def restart(plan: Any, merged_paths: Any, settings_: Any, directory: Any) -> Any:
            # Stand in for a process coming back up on the merged tree. It reports the
            # base's tip, not the merge commit -- which is what a real restart reports,
            # because the finisher has already committed the task record on top of it.
            server.payload["source_commit"] = head(world["root"], "main")
            return real_restart(plan, merged_paths, settings_, directory)

        monkeypatch.setattr(finish_module, "restart_server", restart)
        result = run(world, restart=[_interpreter(), "-c", "pass"], verify_base=server.base)

        assert result.outcome == FINISHED, result.render()
        verify = next(step for step in result.steps if step.step == "verify")
        assert verify.ok and not verify.skipped
        assert "contains the merge" in verify.detail
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.lifecycle is Lifecycle.CLOSED

    def test_a_retry_after_an_escalation_finishes_its_own_merge(
        self, world: Dict[str, Any]
    ) -> None:
        """`agentjobs finish` re-run: the merge is already in, and that is not an error.

        `git merge --no-ff` of a branch already contained in the base exits **zero** and
        moves nothing, so this is the case a naive reading of the exit code gets wrong in
        the direction of closing a task on a merge that never happened.
        """
        add_served_change(world)
        first = run(world, restart=[])
        assert first.outcome == ESCALATED and first.merge_commit is not None

        second = run(world, restart=[_interpreter(), "-c", "pass"], verify_base=None)
        # Verification is skipped here only because there is no server; what matters is
        # that the second attempt recognised its own merge instead of refusing.
        assert second.reason != "already_merged"
        assert second.merge_commit == first.merge_commit

    def test_a_branch_somebody_else_merged_is_not_quietly_closed(
        self, world: Dict[str, Any]
    ) -> None:
        """An already-merged branch with nothing on the record is a person, not us."""
        git(world["root"], "merge", "--no-ff", "--no-edit", "-m", "by hand", world["branch"])
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "already_merged"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open


# ----- verify_live on its own -------------------------------------------------


class TestVerification:
    """Driven against a real repository, because containment is a git question."""

    def _plan(self, world: Dict[str, Any]) -> Plan:
        return Plan(
            root=world["root"],
            branch=world["branch"],
            worktree=world["worktree"],
            interpreter=Path(_interpreter()),
            base="main",
            branch_head_before=head(world["root"], world["branch"]),
            base_head_before=head(world["root"], "main"),
        )

    def test_a_skipped_restart_skips_verification_rather_than_faking_it(
        self, world: Dict[str, Any]
    ) -> None:
        step = verify_live(
            self._plan(world), head(world["root"], "main"), "http://127.0.0.1:1", restarted=False
        )
        assert step.ok and step.skipped

    def test_nothing_answering_is_not_a_finish(self, world: Dict[str, Any]) -> None:
        with pytest.raises(Escalate) as caught:
            verify_live(
                self._plan(world),
                head(world["root"], "main"),
                "http://127.0.0.1:1",
                restarted=True,
                timeout=0.4,
                sleep=0.05,
            )
        assert caught.value.reason == "not_live"

    def test_a_version_without_a_commit_cannot_prove_anything(
        self, world: Dict[str, Any], serving: Any
    ) -> None:
        server = serving({"source_root": str(world["root"]), "started_at": "now"})
        with pytest.raises(Escalate) as caught:
            verify_live(
                self._plan(world),
                head(world["root"], "main"),
                server.base,
                restarted=True,
                timeout=0.4,
                sleep=0.05,
            )
        assert caught.value.reason == "not_live"
        assert "source_commit" in caught.value.detail

    def test_a_descendant_of_the_merge_is_accepted(
        self, world: Dict[str, Any], serving: Any
    ) -> None:
        """The commit that matters is behind the reported one, which is the normal case."""
        merge_commit = head(world["root"], "main")
        (world["root"] / "later.txt").write_text("a later commit\n", encoding="utf-8")
        git(world["root"], "add", "--", "later.txt")
        git(world["root"], "commit", "-m", "something landed afterwards")
        server = serving(
            {
                "source_commit": head(world["root"], "main"),
                "source_root": str(world["root"]),
                "started_at": "2026-08-22T00:00:00+00:00",
            }
        )
        step = verify_live(
            self._plan(world), merge_commit, server.base, restarted=True, timeout=5, sleep=0.05
        )
        assert step.ok and not step.skipped

    def test_an_unrelated_commit_is_not_accepted(self, world: Dict[str, Any], serving: Any) -> None:
        server = serving(
            {
                "source_commit": head(world["root"], world["branch"]),
                "source_root": str(world["root"]),
            }
        )
        # The branch does not contain a merge that has not happened.
        (world["worktree"] / "docs" / "later.md").write_text("x\n", encoding="utf-8")
        git(world["worktree"], "add", "--", "docs/later.md")
        git(world["worktree"], "commit", "-m", "branch moves on")
        with pytest.raises(Escalate) as caught:
            verify_live(
                self._plan(world),
                head(world["worktree"]),
                server.base,
                restarted=True,
                timeout=0.4,
                sleep=0.05,
            )
        assert caught.value.reason == "not_live"


# ----- small readers ----------------------------------------------------------


class TestReaders:
    def test_active_branches_reads_only_the_open_ones(self, world: Dict[str, Any]) -> None:
        world["manager"].update_task(
            world["task_id"],
            actor="claude",
            branches=[
                {"name": "old", "status": "merged"},
                {"name": "gone", "status": "abandoned"},
                {"name": world["branch"], "status": "active"},
            ],
        )
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert active_branches(task) == [world["branch"]]

    def test_worktree_paths_finds_the_branch_wherever_it_is(self, world: Dict[str, Any]) -> None:
        found = worktree_paths(world["root"])
        assert found[world["branch"]].resolve() == world["worktree"].resolve()
        assert found["main"].resolve() == world["root"].resolve()


class TestTheUnexpected:
    """Whatever the sequence does not model still has to reach the record."""

    def test_an_unmodelled_failure_escalates_instead_of_crashing(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traceback here is a merge that happened with nobody left to say so."""
        import agentjobs.dispatch.finish as finish_module

        real_merge = finish_module.merge

        def merge_then_explode(plan: Any, task: Any, approver: str) -> str:
            # The merge really happens, and then something unmodelled goes wrong. That
            # ordering is the whole test: a failure *before* the merge is easy.
            real_merge(plan, task, approver)
            raise RuntimeError("something nobody thought about")

        monkeypatch.setattr(finish_module, "merge", merge_then_explode)

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "unexpected_error"
        assert "something nobody thought about" in result.detail
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.is_open
        assert task.ball is Ball.AGENT

    def test_a_decline_is_not_swallowed_as_an_unexpected_error(self, world: Dict[str, Any]) -> None:
        """The guard sits outside the sequence, so its own signals pass through it."""
        world["manager"].update_task(world["task_id"], actor="claude", branches=[])
        result = run(world)
        assert result.outcome == DECLINED
        assert result.reason == "no_active_branch"


class TestTheRecordWhileItRuns:
    """The gate is minutes long and silent; the record must not be."""

    def test_it_says_it_started_before_the_expensive_part(self, world: Dict[str, Any]) -> None:
        run(world)
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        started = [entry for entry in task.log if entry.data.get("finish_step") == "started"]
        assert len(started) == 1
        assert "Nothing is merged yet" in started[0].body
        assert world["branch"] in started[0].body
        # And it is written before the merge is, so a reader mid-gate sees the first and
        # not the second.
        merged_at = next(
            index
            for index, entry in enumerate(task.log)
            if entry.data.get("finish_step") == "merge"
        )
        assert task.log.index(started[0]) < merged_at

    def test_the_closing_entry_carries_the_evidence_not_just_the_claim(
        self, world: Dict[str, Any]
    ) -> None:
        run(world)
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        closing = task.log[-1]
        assert "verified live" in closing.body
        # Up to and including verification. `close` and `worktree` are absent because
        # this entry *is* the close -- and the body says so, rather than leaving a reader
        # to wonder whether two steps went missing.
        for step in ("preflight", "rebase", "gate", "merge", "rebuild", "restart", "verify"):
            assert step in closing.body
        assert "up to and including verification" in closing.body


def write_dispatch_config(
    world: Dict[str, Any], posture: str, *, max_posture: Optional[str] = None
) -> None:
    """A machine-local dispatch config that permits ``demo`` at ``posture``.

    Written into the world's own home rather than mocked, because the posture check is
    the whole point of the authority: reading it from the real configuration loader is
    what makes the test evidence that a *deployment* cannot merge unreviewed unless it
    said so.

    ``max_posture`` is the ceiling (task-308), and omitting it means the ceiling *is*
    ``posture`` -- which is what makes the clamp tests below read the way they do: a
    config with no ceiling of its own permits nothing wider than its default.
    """
    home = world["home"]
    home.mkdir(parents=True, exist_ok=True)
    project: Dict[str, Any] = {"enabled": True, "runner": "claude", "posture": posture}
    if max_posture is not None:
        project["max_posture"] = max_posture
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {"claude": {"argv": ["claude", "-p", "{prompt}"]}},
                "projects": {"demo": project},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def seed_run_record(
    world: Dict[str, Any],
    run_id: str,
    *,
    posture: str,
    source: str = "dispatch",
    ceiling: Optional[str] = None,
) -> Path:
    """A run directory shaped the way the dispatcher writes one (task-315).

    Real files rather than a patched reader: what is under test is that the finisher
    consults the run's *recorded* posture, and a stub would assert only that some
    function was called. The fields are the ones ``ResolvedPosture.as_data`` writes.
    """
    directory: Path = Path(world["home"]) / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": world["task_id"],
        "project_id": "demo",
        "mode": "session",
        "posture": posture,
        "posture_source": source,
        "status": "running",
        "started_at": "2026-08-25T08:00:00+00:00",
    }
    if ceiling is not None:
        meta["posture_ceiling"] = ceiling
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    return directory


def release(world: Dict[str, Any], **overrides: Any) -> Any:
    """A finish claiming the posture authority, as a dispatched autonomous run does."""
    return finish_task(
        manager=world["manager"],
        project=world["project"],
        task_id=world["task_id"],
        approver="run_abcd1234",
        home=world["home"],
        api_base=overrides.pop("api_base", "http://127.0.0.1:1"),
        settings=settings(**overrides),
        authority=POSTURE,
    )


class TestThePostureAuthority:
    """task-021. The merge an agent makes without a human, and what gates it.

    The property under test is not "autonomous can merge" -- it is that the *sanctioned
    path* cannot merge at a posture that did not release it. Containment against an
    agent that ignores its instructions is not on offer here and is not claimed:
    ``autonomous`` is ``bypassPermissions``, and a run that wanted to could merge by
    hand. What this guarantees is that the command an agent is told to run fails closed
    when the deployment did not ask for unreviewed merges.
    """

    def test_a_review_posture_declines_and_touches_nothing(self, world: Dict[str, Any]) -> None:
        write_dispatch_config(world, "auto")
        before = head(world["root"])

        result = release(world)

        assert result.outcome == DECLINED
        assert result.reason == "posture_requires_review"
        assert not merged_into(world["root"], world["branch"])
        assert head(world["root"]) == before
        # It says which posture and what to do instead, because the agent reading this
        # exit code has to decide what to do next from it alone.
        assert "auto" in result.detail
        assert "human/review" in result.detail

    def test_supervised_declines_for_the_same_reason(self, world: Dict[str, Any]) -> None:
        write_dispatch_config(world, "supervised")

        result = release(world)

        assert result.outcome == DECLINED
        assert result.reason == "posture_requires_review"

    def test_read_only_declines_too(self, world: Dict[str, Any]) -> None:
        """It has no branch by construction, but nothing here relies on that."""
        write_dispatch_config(world, "read_only")

        assert release(world).reason == "posture_requires_review"

    def test_an_autonomous_posture_merges(self, world: Dict[str, Any]) -> None:
        write_dispatch_config(world, "autonomous")

        result = release(world)

        assert result.outcome == FINISHED
        assert merged_into(world["root"], world["branch"])

    def test_a_red_gate_still_stops_it(self, world: Dict[str, Any]) -> None:
        """The objective floor. It is the authority, so it cannot be the soft half."""
        write_dispatch_config(world, "autonomous")
        (world["worktree"] / "scripts").mkdir(parents=True, exist_ok=True)
        (world["worktree"] / "scripts" / "check.py").write_text(RED_GATE, encoding="utf-8")
        git(world["worktree"], "add", "--", "scripts/check.py")
        git(world["worktree"], "commit", "-m", "chore: a red gate")

        result = release(world)

        assert result.outcome == ESCALATED
        assert result.reason == "gate_failed"
        assert not merged_into(world["root"], world["branch"])

    def test_the_record_says_no_human_reviewed_it(self, world: Dict[str, Any]) -> None:
        """ac-5's other half: the record must not read like an approved merge.

        A reader six months from now has no way to tell the two apart except by what is
        written here, and "Approved by ..." on a merge nobody approved is the worst
        possible sentence to leave behind.
        """
        write_dispatch_config(world, "autonomous")

        result = release(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        merge_entry = next(entry for entry in task.log if entry.data.get("finish_step") == "merge")
        assert "No human reviewed this merge" in merge_entry.body
        assert "autonomous" in merge_entry.body
        assert "run_abcd1234" in merge_entry.body
        assert "Approved by" not in merge_entry.body
        # And the same account is in the merge commit itself, where git will keep it
        # regardless of what happens to the task file. Named by its sha rather than by
        # HEAD: the finisher commits the closing task record onto the base afterwards,
        # so HEAD is that commit and not the merge.
        assert result.merge_commit
        message = subprocess.run(
            ["git", "-C", str(world["root"]), "log", "-1", "--format=%B", result.merge_commit],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        assert "No human reviewed this merge" in message

    def test_an_approval_still_says_a_person_approved_it(self, world: Dict[str, Any]) -> None:
        """The regression that matters: the default authority is unchanged."""
        result = run(world)

        assert result.outcome == FINISHED
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        merge_entry = next(entry for entry in task.log if entry.data.get("finish_step") == "merge")
        assert "Approved by Jeff Posey" in merge_entry.body
        assert "No human reviewed" not in merge_entry.body

    def test_a_project_dispatch_never_enabled_declines_rather_than_merging(
        self, world: Dict[str, Any]
    ) -> None:
        """No config at all is not a posture that releases anything."""
        result = release(world)

        assert result.outcome == DECLINED
        assert not merged_into(world["root"], world["branch"])


class TestThePostureThatDecides:
    """task-315. *Which* posture releases the merge -- the run's, not the project's.

    task-307 and task-308 made a run's posture a per-run fact: chosen at dispatch, or
    written on the task record, or defaulted by the project, most-specific-wins, capped
    by the machine's ceiling. The finisher went on reading the project default alone, so
    a run dispatched `autonomous` on a project configured `auto` was told in its own
    prompt that the merge gate was released and then refused by the command that prompt
    named. These fix the direction of that, and the two directions are not symmetric --
    the widening case is the bug, and the narrowing case is the one a careless fix
    breaks.
    """

    def test_a_run_dispatched_autonomous_merges_on_a_project_configured_auto(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sc-1: the reported failure, from the other side.

        Exactly the shape of task-298: project default `auto`, ceiling raised to
        `autonomous`, and a human who chose `autonomous` for this one dispatch.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        seed_run_record(world, "run_raised", posture="autonomous", source="dispatch")
        monkeypatch.setenv(RUN_ID_ENV, "run_raised")

        result = release(world)

        assert result.outcome == FINISHED
        assert merged_into(world["root"], world["branch"])

    def test_the_merge_says_the_posture_came_from_the_dispatch(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record must not send a reader to `dispatch.yaml` to find `auto` there.

        That file says `auto` and this merge happened without a review, so a record that
        only cited "the project" would read as a contradiction -- or worse, as evidence
        the gate had been bypassed.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        seed_run_record(world, "run_raised", posture="autonomous", source="dispatch")
        monkeypatch.setenv(RUN_ID_ENV, "run_raised")

        result = release(world)

        assert result.merge_commit, "nothing merged, so there is no message to read"
        message = git(world["root"], "log", "-1", "--format=%B", result.merge_commit).stdout
        assert "autonomous" in message
        assert "from the dispatch" in message
        assert "No human reviewed this merge" in message

    def test_a_run_dispatched_auto_declines_on_a_project_configured_autonomous(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sc-2, and the half a careless fix loses.

        Narrowing is a choice too. A human who picked `auto` for this dispatch on a
        project that defaults to `autonomous` has asked for a review, and reading the
        project default here would quietly overrule them -- in the direction that merges.
        """
        write_dispatch_config(world, "autonomous")
        seed_run_record(world, "run_narrowed", posture="auto", source="dispatch")
        monkeypatch.setenv(RUN_ID_ENV, "run_narrowed")
        before = head(world["root"])

        result = release(world)

        assert result.outcome == DECLINED
        assert result.reason == "posture_requires_review"
        assert not merged_into(world["root"], world["branch"])
        assert head(world["root"]) == before
        assert "auto" in result.detail
        assert "dispatch" in result.detail, "and where that posture came from"

    def test_a_posture_raised_on_a_run_record_cannot_exceed_the_ceiling(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sc-3. The run directory is machine-local, not sacred.

        An agent that can run a shell can write `~/.agentjobs/runs/<id>/meta.yaml` as
        easily as it can write its own task record. What stops that buying a merge is
        `max_posture`, which lives in a file nothing reachable over the network writes --
        so this is the test that the clamp is applied to *this* source and not only to
        the task record it was written for.
        """
        write_dispatch_config(world, "auto")  # no max_posture: the ceiling is `auto`
        seed_run_record(world, "run_forged", posture="autonomous", source="dispatch")
        monkeypatch.setenv(RUN_ID_ENV, "run_forged")

        result = release(world)

        assert result.outcome == DECLINED
        assert result.reason == "posture_requires_review"
        assert not merged_into(world["root"], world["branch"])
        assert "max_posture" in result.detail, "and it says what would have to change"

    def test_the_ceiling_is_read_now_not_as_the_run_recorded_it(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ceiling lowered after dispatch binds the merge that has not happened yet.

        The run wrote down the ceiling it started under, and that is history. The
        question at merge time is what this machine permits now.
        """
        write_dispatch_config(world, "auto")
        seed_run_record(
            world, "run_stale", posture="autonomous", source="dispatch", ceiling="autonomous"
        )
        monkeypatch.setenv(RUN_ID_ENV, "run_stale")

        assert release(world).reason == "posture_requires_review"

    def test_with_no_run_the_task_records_posture_decides(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sc-4: a person running the command from a shell.

        No run to read, so the posture resolves the way a dispatch of this task would --
        which is the coherent rule, and the one that makes a hand-run finish and a
        dispatched one agree about the same task.
        """
        monkeypatch.delenv(RUN_ID_ENV, raising=False)
        write_dispatch_config(world, "auto", max_posture="autonomous")
        world["manager"].update_task(
            world["task_id"], actor="claude", posture=DispatchPosture.AUTONOMOUS
        )

        result = release(world)

        assert result.outcome == FINISHED
        assert merged_into(world["root"], world["branch"])

    def test_a_task_records_posture_is_still_clamped(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The task record is a git-tracked file any agent can write. Same ceiling."""
        monkeypatch.delenv(RUN_ID_ENV, raising=False)
        write_dispatch_config(world, "auto")
        world["manager"].update_task(
            world["task_id"], actor="claude", posture=DispatchPosture.AUTONOMOUS
        )

        assert release(world).reason == "posture_requires_review"

    def test_a_run_id_naming_nothing_falls_back_rather_than_failing(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run directory that has been swept is not an authority and not a crash.

        It resolves as if there were no run at all, which for this world is the project
        default -- and that is the fail-closed direction.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        monkeypatch.setenv(RUN_ID_ENV, "run_thatneverexisted")

        assert release(world).reason == "posture_requires_review"

    def test_a_run_cannot_vouch_for_a_task_it_was_not_dispatched_against(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A posture is granted for one task, and it does not travel.

        The task id is an argument on the command line; the posture is not. Without this
        binding, a run authorised `autonomous` for its own task could name any other open
        task and merge that branch on an authority nobody granted for it.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        record = seed_run_record(world, "run_elsewhere", posture="autonomous", source="dispatch")
        meta = yaml.safe_load((record / "meta.yaml").read_text(encoding="utf-8"))
        meta["task_id"] = "task-999"
        (record / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
        monkeypatch.setenv(RUN_ID_ENV, "run_elsewhere")

        result = release(world)

        assert result.outcome == DECLINED
        assert result.reason == "posture_requires_review"
        assert not merged_into(world["root"], world["branch"])

    def test_an_unparseable_posture_on_a_run_record_is_not_a_claim(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Garbage in the meta resolves as absence, never as permission."""
        write_dispatch_config(world, "auto", max_posture="autonomous")
        seed_run_record(world, "run_junk", posture="wide-open", source="dispatch")
        monkeypatch.setenv(RUN_ID_ENV, "run_junk")

        assert release(world).reason == "posture_requires_review"
