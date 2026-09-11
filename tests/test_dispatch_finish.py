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
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import yaml

from agentjobs.actors import FINISHER, reserved_actors
from agentjobs.dispatch.config import FinishSettings
from agentjobs.dispatch.finish import (
    DECLINED,
    ESCALATED,
    FINISHED,
    POSTURE,
    SALIENT_LIMIT,
    Escalate,
    Plan,
    active_branches,
    delete_branch,
    failing_stage,
    failing_tests,
    finish_task,
    lead_with_the_cause,
    reachable_stages,
    verify_live,
    worktree_paths,
)
from agentjobs.dispatch.finish_status import read_finish_status
from agentjobs.dispatch.ledger import LockHolder
from agentjobs.dispatch.phases import RUN_ID_ENV
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    BranchStatus,
    DispatchPosture,
    Lifecycle,
    LogEntryType,
    Outcome,
)
from agentjobs.projects import Project, ProjectRegistry
from support import task_store

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

    Shaped like the real thing on purpose. The task record is a row rather than a file
    in the clone (task-402), so the finisher's own writes leave the tree it is merging
    into clean -- which is what the clean-tree checks below are now free to assert.
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

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    project = Project(id="demo", name="Demo", root=root)
    ProjectRegistry(home).add(root, project_id="demo")
    # Addressed by project id, so the rows go in the file the server would open for
    # this project rather than in one named from a directory. Seeding anywhere else is
    # invisible to every route the tests below drive.
    manager = TaskManager(task_store(root / "tasks", project_id="demo"))
    task = manager.create_task(
        title="The deliverable",
        category="infrastructure",
        summary="A task with a branch waiting to be merged.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="claude")
    manager.update_task(task.id, actor="claude", branches=[{"name": branch, "status": "active"}])
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


REPO_ROOT = Path(__file__).resolve().parents[1]

GATE_THAT_MOVES_THE_BASE = '''\
"""A stub gate that lands a real commit on the base while it is running."""

import pathlib
import subprocess
import sys

CLONE = pathlib.Path("{clone}")
RELATIVE = "{relative}"

if "--only" in sys.argv:
    # The catch-up re-run. It must not move the base again, or nothing would ever
    # converge and the test would be measuring the round limit instead of the feature.
    sys.exit({reduced_exit})

target = CLONE / RELATIVE
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("landed while the gate was running", encoding="utf-8")
subprocess.run(["git", "-C", str(CLONE), "add", "--", RELATIVE], check=True)
subprocess.run(
    ["git", "-C", str(CLONE), "commit", "-m", "chore: landed mid-gate"],
    check=True,
    capture_output=True,
)
sys.exit(0)
'''


def gate_that_moves_the_base(world: Dict[str, Any], relative: str, reduced_exit: int = 0) -> None:
    """Replace the stub gate with one that commits *relative* to main as it runs.

    A real commit at the real moment. Reaching into ``plan`` and rewriting a captured sha
    would test the comparison and prove nothing about the race, which is the whole
    subject here.
    """
    root = world["root"]
    (root / "scripts" / "check.py").write_text(
        GATE_THAT_MOVES_THE_BASE.format(
            clone=root.as_posix(), relative=relative, reduced_exit=reduced_exit
        ),
        encoding="utf-8",
    )
    git(root, "add", "--", "scripts/check.py")
    git(root, "commit", "-m", "chore: a gate that moves the base under itself")


def publish_gate_scope(world: Dict[str, Any]) -> None:
    """Give the test clone this repository's own classification table.

    The real file, copied, rather than a fixture's idea of one: the property under test
    is that the finish reuses ``--since-gate``'s judgement, and a hand-written table here
    could agree with the finish while both disagreed with the gate.
    """
    root = world["root"]
    shutil.copyfile(REPO_ROOT / "scripts" / "gate_scope.py", root / "scripts" / "gate_scope.py")
    git(root, "add", "--", "scripts/gate_scope.py")
    git(root, "commit", "-m", "chore: publish the gate scope table")


@pytest.fixture
def contended_runway(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A runway that is busy exactly once, so ``on_wait`` runs and then the lock is given.

    Standing up a second real finish to contend for it would be testing the lock, which
    ``test_dispatch_ledger`` already does. What is under test here is what the *waiting*
    finish writes and does not commit.
    """

    class _Lock:
        def release(self) -> None:
            return None

    def busy_once(home: Path, root: Path, **kwargs: Any) -> Any:
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(LockHolder(pid=1234, kind="runway", finish_id="fin_someoneelse"))
        return _Lock()

    monkeypatch.setattr("agentjobs.dispatch.finish.acquire_runway_lock", busy_once)
    yield None


def merged_into(root: Path, branch: str, base: str = "main") -> bool:
    """Whether ``base`` contains ``branch``.

    The question every "nothing was merged" assertion below actually means. Comparing
    the base's tip against a value captured earlier does *not* answer it: the finisher
    commits its own escalation onto the base, so the tip legitimately moves even when
    nothing was merged.

    **Only askable while the branch still exists**, which since task-293 means only on
    the paths that stop: a successful finish deletes the branch it merged. A missing ref
    is therefore a test error rather than a ``False`` -- ``git merge-base --is-ancestor``
    exits non-zero for "not contained" and for "no such ref" alike, so a deleted branch
    would read as "nothing was merged", which is the exact opposite of what it means.
    Successful paths ask `landed` instead.
    """
    assert (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode
        == 0
    ), f"{branch} no longer exists in {root}; ask `landed` about a finish that succeeded"
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", branch, base],
            capture_output=True,
        ).returncode
        == 0
    )


def escalation(task: Any) -> Any:
    """The entry the finish wrote about the step it stopped at.

    Found by its own ``finish_step`` marker rather than counted back from the end of the
    log. Since task-340 an escalation writes either two entries or three -- the second
    handoff exists exactly when no run could be started -- so an index that was right for
    one shape is silently wrong for the other, which is worse than either.
    """
    for entry in reversed(task.log):
        if entry.data.get("finish_step") and entry.data.get("finish_step") != "started":
            return entry
    raise AssertionError("no escalation entry on the record")


def landed(root: Path, result: Any, base: str = "main") -> bool:
    """Whether ``base`` contains this finish's merge commit.

    What `merged_into` asks, of the one name that survives a successful finish. Since
    task-293 the branch does not: retiring it is the last thing the sequence does.
    """
    assert result.merge_commit, result.render()
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", result.merge_commit, base],
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
        assert landed(root, result)
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
        # The other half of ENGINEERING.md step 5, missing until task-293.
        assert git(root, "branch", "--list", world["branch"]).stdout.strip() == ""

    def test_the_branch_is_deleted_and_the_step_table_says_so(self, world: Dict[str, Any]) -> None:
        """task-293: six merged branches accumulated in one night because nothing did this."""
        result = run(world)

        assert result.outcome == FINISHED, result.render()
        root = world["root"]
        assert git(root, "branch", "--list", world["branch"]).stdout.strip() == ""
        assert world["branch"] not in git(root, "branch", "--merged", "main").stdout
        step = next(entry for entry in result.steps if entry.step == "branch")
        assert step.ok and world["branch"] in step.detail and "deleted" in step.detail
        # And after the worktree, because a checked-out branch cannot be deleted.
        order = [entry.step for entry in result.steps]
        assert order.index("worktree") < order.index("branch")

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


# ----- deleting the branch, and the two cases where it must not ---------------


class TestRetiringTheBranch:
    """``git branch -d`` on real branches, because a refusal is the safety argument.

    Driven against ``delete_branch`` rather than through a whole finish, because the two
    interesting states -- an unmerged branch, and one still checked out -- are states a
    successful finish cannot be in. Constructing them through the sequence would mean
    faking the merge, and then the test would be about the fake.
    """

    def _plan(self, world: Dict[str, Any], branch: str) -> Plan:
        return Plan(
            root=world["root"],
            branch=branch,
            worktree=world["worktree"],
            interpreter=Path(_interpreter()),
            base="main",
            branch_head_before=head(world["root"], branch),
            base_head_before=head(world["root"], "main"),
        )

    def _unmerged(self, world: Dict[str, Any]) -> str:
        """A branch with a commit of its own that ``main`` does not contain."""
        root, worktree = world["root"], world["worktree"]
        branch = "feat/task-002-unmerged"
        git(root, "branch", branch, "main")
        spare = worktree.parent / "spare"
        git(root, "worktree", "add", str(spare), branch)
        (spare / "docs").mkdir(parents=True, exist_ok=True)
        (spare / "docs" / "unmerged.md").write_text("not in main\n", encoding="utf-8")
        git(spare, "add", "--", "docs/unmerged.md")
        git(spare, "commit", "-m", "docs: unmerged work")
        git(root, "worktree", "remove", str(spare))
        return branch

    def test_an_unmerged_branch_is_refused_rather_than_forced(self, world: Dict[str, Any]) -> None:
        """`-d`, never `-D`. A refusal means the assumption behind deleting it is wrong."""
        branch = self._unmerged(world)
        before = head(world["root"], branch)

        step = delete_branch(self._plan(world, branch))

        assert git(world["root"], "branch", "--list", branch).stdout.strip() != ""
        assert head(world["root"], branch) == before
        assert "refused" in step.detail and "never forced" in step.detail
        assert branch in step.detail

    def test_a_refusal_does_not_stop_the_finish(self, world: Dict[str, Any]) -> None:
        """The merge is in and the delivery is verified by then; a ref is not worth a wake."""
        step = delete_branch(self._plan(world, self._unmerged(world)))
        assert step.ok
        assert not step.skipped

    def test_a_branch_still_checked_out_is_left_alone_and_says_which_worktree(
        self, world: Dict[str, Any]
    ) -> None:
        """The ordering constraint, asked of git rather than assumed of the step above.

        A failed ``git worktree remove`` would otherwise come back here as a refusal
        phrased in terms of checkout, which reads exactly like the unmerged case above
        and means something entirely different.
        """
        git(world["root"], "merge", "--no-ff", "--no-edit", "-m", "by hand", world["branch"])

        step = delete_branch(self._plan(world, world["branch"]))

        assert git(world["root"], "branch", "--list", world["branch"]).stdout.strip() != ""
        assert "still checked out" in step.detail
        assert str(world["worktree"]) in step.detail

    def test_a_merged_branch_with_no_worktree_is_deleted(self, world: Dict[str, Any]) -> None:
        git(world["root"], "merge", "--no-ff", "--no-edit", "-m", "by hand", world["branch"])
        git(world["root"], "worktree", "remove", str(world["worktree"]))

        step = delete_branch(self._plan(world, world["branch"]))

        assert git(world["root"], "branch", "--list", world["branch"]).stdout.strip() == ""
        assert "deleted" in step.detail


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
        # Not `agent`: this world has no dispatch config, so the escalation could start
        # no run, and a ball on an agent that does not exist is the state task-340
        # removed. What the agent would have been handed is in the entry below.
        assert task.ball is Ball.HUMAN
        entry = escalation(task)
        assert "Nothing was merged" in entry.body
        assert "rebase" in entry.body
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
        assert "red gate never merges" in escalation(task).body

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


# ----- a base that moves while the gate runs (task-297) -----------------------


class TestCatchingUpWithAMovedBase:
    """The base moving mid-gate is the normal case here, not the exceptional one.

    Every session committed its task records to the base by design, and a gate takes
    minutes, so ``base_moved`` fired on nearly every finish on a busy evening -- each
    refusal costing a whole dispatched run, over other people's bookkeeping.

    **That cause is gone and the mechanism is not.** Records are rows, so no session
    commits one; task-380 removed the last of them from the checkout. What still lands on
    the base mid-gate is prose -- another branch merging a docs change -- and it costs a
    refusal for exactly the same reason, which is why these are now written around a
    documentation commit.

    **A real commit is landed on the base while the gate is running**, by the stub gate
    itself, because that is the only arrangement that tests the race rather than a
    simulation of it. The two tests that matter are the pair: an absorbable commit is
    caught up with and merged, a source commit is still refused.
    """

    def test_an_absorbable_commit_landing_mid_gate_is_caught_up_with_and_merged(
        self, world: Dict[str, Any]
    ) -> None:
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md")
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        assert landed(world["root"], result)
        # And the commit it raced with is still there -- catching up rebases onto it, it
        # does not step over it.
        assert (world["root"] / "docs" / "somebody-elses-note.md").is_file()

    def test_a_source_commit_landing_mid_gate_still_refuses(self, world: Dict[str, Any]) -> None:
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "src/agentjobs/somebody_elses_code.py")
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "base_moved"
        assert merged_into(world["root"], world["branch"]) is False
        # The escalation names what moved, so the woken session does not have to diff.
        assert "src/agentjobs/somebody_elses_code.py" in result.detail

    def test_the_step_table_says_it_caught_up_and_with_what(self, world: Dict[str, Any]) -> None:
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md")
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        rendered = "\n".join(step.render() for step in result.steps)
        assert "catch_up" in rendered
        assert "pytest" in rendered

    def test_a_quiet_base_still_reports_the_step_as_skipped(self, world: Dict[str, Any]) -> None:
        """Like a merge that needs no restart, the common case says so rather than vanishing.

        A step that disappears when it does nothing leaves the live view guessing what
        comes next, and `finish_status` derives "what is running now" from the last step
        it saw.
        """
        publish_gate_scope(world)
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        step = next(step for step in result.steps if step.step == "catch_up")
        assert step.skipped is True
        assert "did not move" in step.detail

    def test_an_unabsorbable_move_says_so_before_the_merge_refuses(
        self, world: Dict[str, Any]
    ) -> None:
        """The step table has to explain the refusal that follows it, not just precede it."""
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "src/agentjobs/somebody_elses_code.py")
        result = run(world)
        assert result.reason == "base_moved"
        step = next(step for step in result.steps if step.step == "catch_up")
        assert step.skipped is True
        assert "not absorbable" in step.detail

    def test_the_live_view_sees_the_catch_up_too(self, world: Dict[str, Any]) -> None:
        """`list.extend` does not go through `StepLog.append`, and `catch_up` returns a list.

        Without `StepLog.extend`, a caught-up finish would show the step in the table on
        the task and omit it from the page somebody is watching -- silently, which is the
        disagreement `StepLog` exists to prevent (task-321).
        """
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md")
        result = run(world)
        assert result.outcome == FINISHED, result.render()

        status = read_finish_status(world["home"], world["task_id"], "demo")
        assert status is not None
        assert [step.name for step in status.steps] == [step.step for step in result.steps]
        assert "catch_up" in [step.name for step in status.steps]

    def test_catching_up_writes_nothing_to_the_task_record(self, world: Dict[str, Any]) -> None:
        """The one step in this module that must stay silent.

        A progress note would be committed to the base, which would move the base, which
        is what it is catching up with. The evidence goes in the step table instead.
        """
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md")
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        task = world["manager"].get_task(world["task_id"])
        assert [entry for entry in task.log if entry.data.get("finish_step") == "catch_up"] == []

    def test_a_red_re_run_stops_the_merge(self, world: Dict[str, Any]) -> None:
        """The catch-up is a verification, so it has to be able to say no."""
        publish_gate_scope(world)
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md", reduced_exit=1)
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "catch_up_gate_failed"
        assert merged_into(world["root"], world["branch"]) is False

    def test_without_a_published_table_the_refusal_is_unconditional(
        self, world: Dict[str, Any]
    ) -> None:
        """The exemption is opt-in by the repository, in a file that goes through review.

        No ``scripts/gate_scope.py`` means nothing classifies anything, so even a
        task-record move refuses exactly as it did before this existed.
        """
        gate_that_moves_the_base(world, "docs/somebody-elses-note.md")
        result = run(world)
        assert result.outcome == ESCALATED
        assert result.reason == "base_moved"
        assert merged_into(world["root"], world["branch"]) is False


class TestWhatAChangedPathCanReach:
    """``reachable_stages`` delegates the judgement; these check it delegates correctly."""

    def test_prose_reaches_the_python_suite_and_nothing_else(self, world: Dict[str, Any]) -> None:
        publish_gate_scope(world)
        assert reachable_stages(world["root"], ["docs/agent-workflow.md"]) == ["pytest"]

    def test_a_retired_task_path_is_unclassified_and_so_denies_the_move(
        self, world: Dict[str, Any]
    ) -> None:
        """It used to reach pytest alone. task-380 removed the class, not the safety.

        Worth asserting rather than deleting: the direction of the change is what makes
        it safe. A path the table no longer claims refuses the catch-up and costs a
        merge, where claiming it wrongly would have cost coverage.
        """
        publish_gate_scope(world)
        assert reachable_stages(world["root"], ["tasks/agentjobs/task-001.yaml"]) is None

    def test_one_unclassified_path_denies_the_whole_move(self, world: Dict[str, Any]) -> None:
        publish_gate_scope(world)
        assert (
            reachable_stages(world["root"], ["docs/agent-workflow.md", "src/agentjobs/manager.py"])
            is None
        )

    def test_no_table_classifies_nothing(self, world: Dict[str, Any]) -> None:
        assert reachable_stages(world["root"], ["docs/agent-workflow.md"]) is None

    def test_a_table_that_will_not_import_classifies_nothing(self, world: Dict[str, Any]) -> None:
        (world["root"] / "scripts" / "gate_scope.py").write_text(
            "raise RuntimeError('half-written')\n", encoding="utf-8"
        )
        assert reachable_stages(world["root"], ["docs/agent-workflow.md"]) is None

    def test_a_base_that_moved_without_changing_a_path_is_not_absorbed(
        self, world: Dict[str, Any]
    ) -> None:
        publish_gate_scope(world)
        assert reachable_stages(world["root"], []) is None


class TestQueuingForTheRunwayIsSilentInGit:
    """The finisher's own contribution to the feedback loop, removed (task-297).

    ``Runway.take`` announces on the record when the runway is contended -- which is
    right, a task queued behind three others must not read as hung. What it must not do
    is *commit* that note: the finish it is queued behind is mid-gate, and a commit to
    the base is precisely what escalates that finish with ``base_moved``.

    Task-402 removed the whole class of commit rather than this one instance -- a record
    is a row, so nothing the finish writes to it touches git. The second case is kept
    and widened to say so: it is now the standing check that no record write has found
    its way back into the repository.
    """

    def test_the_note_is_written(self, world: Dict[str, Any], contended_runway: Any) -> None:
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        task = world["manager"].get_task(world["task_id"])
        queued = [entry for entry in task.log if entry.data.get("finish_step") == "runway_queued"]
        assert len(queued) == 1
        assert "merge runway" in queued[0].body

    def test_but_it_lands_no_commit_of_its_own_on_the_base(
        self, world: Dict[str, Any], contended_runway: Any
    ) -> None:
        before = git(world["root"], "rev-parse", "main").stdout.strip()
        result = run(world)
        assert result.outcome == FINISHED, result.render()
        subjects = git(world["root"], "log", "--format=%s", f"{before}..main").stdout.splitlines()
        # Nothing the finish wrote to the *record* reaches the base at all now
        # (task-402), so the assertion is the whole class rather than the one note:
        # only the merge commit and the branch's own work are here.
        assert [line for line in subjects if "chore(task" in line] == [], subjects
        assert [line for line in subjects if "queuing" in line] == [], subjects
        assert any(line.startswith("Merge branch") for line in subjects), subjects


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
        assert "The merge is done" in escalation(task).body
        merge_entry = next(entry for entry in task.log if entry.data.get("finish_step") == "merge")
        assert merge_entry.data["merge_commit"] == result.merge_commit
        assert escalation(task).data.get("merged") is True

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
        # Somebody is named, and it is a somebody who exists: no dispatch config here,
        # so no run could be started and the ball is a person's (task-340).
        assert task.ball is Ball.HUMAN
        assert "something nobody thought about" in escalation(task).body

    def test_the_record_says_where_it_threw_not_only_what_it_threw(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task-388: a type and a message do not place a failure; frames do.

        ``TypeError: Object of type datetime is not JSON serializable`` named no file, no
        line and no field, so working out which of the sequence's writers had thrown it
        meant reasoning backwards from which step had *not* run. The frames were in the
        spawn log the whole time and never reached the task, which is the only artefact
        the next reader is guaranteed to have.
        """
        import agentjobs.dispatch.finish as finish_module

        def restart_then_explode(*args: Any, **kwargs: Any) -> Any:
            raise TypeError("Object of type datetime is not JSON serializable")

        monkeypatch.setattr(finish_module, "restart_server", restart_then_explode)

        result = run(world)
        assert result.reason == "unexpected_error"

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        entry = escalation(task)
        # The function that raised, and the module it is in, in the entry a reader opens.
        assert "restart_then_explode" in entry.body
        assert "test_dispatch_finish.py" in entry.body
        assert "restart_then_explode" in entry.data["traceback"]
        # The prompt an agent is woken with stays the short ask; the frames are evidence.
        assert task.ball_prompt is not None
        assert "restart_then_explode" not in task.ball_prompt

    def test_the_frames_place_a_file_without_naming_a_home_directory(self) -> None:
        """This record has a public remote, so the path is cut at the package root."""
        from agentjobs.dispatch.finish import where_it_was_raised

        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            rendered = where_it_was_raised(error)

        assert "tests/test_dispatch_finish.py:" in rendered
        assert "C:" not in rendered and "/Users/" not in rendered

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

    def test_a_watcher_can_read_the_whole_step_table_off_the_record(
        self, world: Dict[str, Any]
    ) -> None:
        """Every step a real finish runs reaches the surface a page reads (task-321).

        Against a real repository and a real sequence rather than fabricated phase
        records, because what is being checked is the wiring: that `StepLog` is what the
        sequence appends to, that the reader's fixed order matches the order that
        actually happened, and that a step added to one and not the other shows up as a
        failure here rather than as a step the task page silently omits.
        """
        result = run(world)
        assert result.finished, result.render()

        status = read_finish_status(world["home"], world["task_id"], "demo")

        assert status is not None
        assert status.state == "finished"
        assert status.live is False
        assert status.merge_commit == result.merge_commit
        # The same steps, in the same order, as the table the finish wrote onto the task.
        assert [step.name for step in status.steps] == [step.step for step in result.steps]
        assert [step.name for step in status.steps][:4] == [
            "preflight",
            "runway",
            "rebase",
            "gate",
        ]

    def test_the_steps_are_readable_before_the_finish_has_ended(
        self, world: Dict[str, Any]
    ) -> None:
        """The point of recording each one: they are there while it is still going.

        The sequence is interrupted at the merge, so what this reads is a genuinely
        partial finish rather than a completed one with some records removed -- the
        distinction task-207 is the standing warning about.
        """
        import agentjobs.dispatch.finish as module

        def refuse(*args: Any, **kwargs: Any) -> str:
            raise Escalate("merge", "stopped_here", "Stopped on purpose.")

        original, module.merge = module.merge, refuse
        try:
            result = run(world)
        finally:
            module.merge = original
        assert result.outcome == "escalated"

        status = read_finish_status(world["home"], world["task_id"], "demo")

        assert status is not None
        names = [step.name for step in status.steps]
        assert names[:3] == ["preflight", "runway", "rebase"]
        # The gate ran and is recorded as having passed; the merge is where it stopped.
        assert "gate" in names
        assert status.steps[-1].state == "stopped"
        assert status.stopped_at == "merge"


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
        assert landed(world["root"], result)

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
        assert landed(world["root"], result)

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

    def test_a_child_of_an_epic_merges_on_the_posture_it_inherited(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task-316, at the far end of the chain it fixes.

        A child started by the epic walk records ``posture_source: epic``. This is the
        only test that the value the walk writes is one the finisher can act on -- an
        inherited posture that merged nothing would leave the walk stopping on its first
        child for a different reason than it used to, which is not an improvement.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        seed_run_record(world, "run_child", posture="autonomous", source="epic")
        monkeypatch.setenv(RUN_ID_ENV, "run_child")

        result = release(world)

        assert result.outcome == FINISHED
        assert landed(world["root"], result)

    def test_the_merge_says_the_posture_came_from_the_epic(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where a reader goes to find out who authorised an unreviewed merge.

        ``from the dispatch`` would send them to this task's own record looking for a
        click nobody made on it; ``from the project`` would send them to ``dispatch.yaml``
        to find ``auto``. The act is on the *parent's* record, and this is the word that
        says so.
        """
        write_dispatch_config(world, "auto", max_posture="autonomous")
        seed_run_record(world, "run_child", posture="autonomous", source="epic")
        monkeypatch.setenv(RUN_ID_ENV, "run_child")

        result = release(world)

        assert result.merge_commit, "nothing merged, so there is no message to read"
        message = git(world["root"], "log", "-1", "--format=%B", result.merge_commit).stdout
        assert "from the epic" in message
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
        assert landed(world["root"], result)

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


# --- which run is calling, when the environment may be lying (task-249) ---------------


def write_run_record(home: Path, run_id: str, *, task_id: str, live: bool = True, **extra) -> None:
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "task_id": task_id,
        "status": "running" if live else "finished",
        **extra,
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")


def hold_lock(home: Path, task_id: str, run_id: str) -> None:
    locks = home / "runs" / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{task_id}.lock").write_text(
        f"pid=1234 run={run_id} kind=dispatch started=2026-08-25T23:56:46+00:00",
        encoding="utf-8",
    )


class TestWhichRunIsCalling:
    """AGENTJOBS_RUN_ID is set on a launcher, and for --bg that is not the worker.

    A persistent daemon spawns the worker from its own environment, so a session can
    come up holding whichever run started that daemon. run_68ea396e came up as
    run_12b2675c -- fourteen hours old, another task -- and both consumers here read the
    wrong run: the posture resolution fell through to the project default and told an
    autonomous run that a human had to review work a human had already released, and the
    lock check would have refused it `locked` immediately afterwards.

    The repair is narrow by construction. It fires only on that exact signature, takes
    its replacement from the lock file rather than from a search, and can never widen an
    envelope -- released_posture re-applies the machine ceiling to whatever comes out.
    """

    def test_an_environment_naming_this_tasks_own_live_run_is_believed(
        self, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_3f8ec46f", task_id="task-249")

        assert (
            own_run_id(tmp_path, "task-249", environ={RUN_ID_ENV: "run_3f8ec46f"}) == "run_3f8ec46f"
        )

    def test_a_stale_id_is_replaced_by_the_run_holding_this_tasks_lock(
        self, tmp_path: Path
    ) -> None:
        """The task-316 incident, reproduced: a finished run against another task."""
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_12b2675c", task_id="task-269", live=False)
        write_run_record(tmp_path, "run_68ea396e", task_id="task-316")
        hold_lock(tmp_path, "task-316", "run_68ea396e")

        assert (
            own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_12b2675c"}) == "run_68ea396e"
        )

    def test_a_person_at_a_shell_is_not_treated_as_a_run(self, tmp_path: Path) -> None:
        """No variable means nothing dispatched this process, and their behaviour must
        not change: the lock is not a licence to adopt somebody else's authority."""
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_68ea396e", task_id="task-316")
        hold_lock(tmp_path, "task-316", "run_68ea396e")

        assert own_run_id(tmp_path, "task-316", environ={}) == ""

    def test_a_lock_held_by_a_finished_run_is_not_adopted(self, tmp_path: Path) -> None:
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_12b2675c", task_id="task-269", live=False)
        write_run_record(tmp_path, "run_old", task_id="task-316", live=False)
        hold_lock(tmp_path, "task-316", "run_old")

        assert (
            own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_12b2675c"}) == "run_12b2675c"
        )

    def test_a_lock_held_for_another_task_is_not_adopted(self, tmp_path: Path) -> None:
        """released_posture's own rule -- a run may only vouch for the task it was
        dispatched against -- applied here so the two cannot disagree."""
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_12b2675c", task_id="task-269", live=False)
        write_run_record(tmp_path, "run_elsewhere", task_id="task-999")
        hold_lock(tmp_path, "task-316", "run_elsewhere")

        assert (
            own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_12b2675c"}) == "run_12b2675c"
        )

    def test_with_no_lock_at_all_the_declared_id_stands(self, tmp_path: Path) -> None:
        from agentjobs.dispatch.finish import own_run_id

        assert (
            own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_12b2675c"}) == "run_12b2675c"
        )


class TestTheLeakedRunIsTheOneRunningOneLevelUp:
    """task-318: the leaked identity is a **live** run, and every test above uses a dead one.

    The walk case is not the daemon-started-yesterday case. A supervisor dispatches its
    children and is still running while they work, so the id a child comes up holding
    names a run that is live, privileged, and legitimately doing something -- it is just
    not this one. ``run_74dfbc1c`` (task-318) was dispatched by the walk driving
    ``run_1132ebf8`` (task-269), and that supervisor was live throughout.

    **``_run_vouching_for`` compares task ids on two sides, and the class above exercises
    only one of them.** In all six of its cases the leaked run is *finished*, so the
    liveness half alone rejects it and the comparison is never what decides the declared
    value; only ``test_a_lock_held_for_another_task_is_not_adopted`` makes it load-bearing,
    and that is the holder side. Here the leaked run is live, so the task id is the sole
    thing separating the supervisor from the child. Dropping the comparison turns three
    tests red -- that one and the two below -- rather than one.
    """

    def test_a_live_supervisors_id_is_replaced_by_the_run_holding_this_tasks_lock(
        self, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.finish import own_run_id

        # The supervisor: live, and dispatched against the epic rather than this child.
        write_run_record(tmp_path, "run_1132ebf8", task_id="task-269", live=True)
        write_run_record(tmp_path, "run_74dfbc1c", task_id="task-318", live=True)
        hold_lock(tmp_path, "task-318", "run_74dfbc1c")

        assert (
            own_run_id(tmp_path, "task-318", environ={RUN_ID_ENV: "run_1132ebf8"}) == "run_74dfbc1c"
        )

    def test_the_child_is_recognised_as_holding_its_own_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consequence, in the units task-302 and task-303 actually failed in.

        Both got as far as a green gate and were declined ``locked`` by the command
        their own prompts named. This is that decline, asserted not to happen.
        """
        from agentjobs.dispatch.finish import _own_run_holds_lock

        write_run_record(tmp_path, "run_1132ebf8", task_id="task-269", live=True)
        write_run_record(tmp_path, "run_74dfbc1c", task_id="task-318", live=True)
        hold_lock(tmp_path, "task-318", "run_74dfbc1c")
        monkeypatch.setenv(RUN_ID_ENV, "run_1132ebf8")

        assert _own_run_holds_lock(tmp_path, "task-318") is True

    def test_a_supervisor_holding_its_own_lock_is_not_adopted_by_a_child(
        self, tmp_path: Path
    ) -> None:
        """The direction that must not work, so the repair cannot be read as symmetric.

        A run wearing the supervisor's id may borrow the identity of whoever holds *this
        task's* lock. It may never borrow the supervisor's own authority over the epic:
        the lock consulted is always the one for the task being finished.
        """
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_1132ebf8", task_id="task-269", live=True)
        hold_lock(tmp_path, "task-269", "run_1132ebf8")
        # No lock on task-318 at all, and no run dispatched against it.

        assert (
            own_run_id(tmp_path, "task-318", environ={RUN_ID_ENV: "run_1132ebf8"}) == "run_1132ebf8"
        )

    def test_an_invented_identity_is_not_a_way_into_the_lock_holders_authority(
        self, tmp_path: Path
    ) -> None:
        """``_own_run_holds_lock``'s docstring promises this, and it is the property
        task-318's constraint names: *"A process that invents the variable matches no
        lock and gets the ordinary refusal."*

        The substitution exists to correct a **real** identity that arrived stale, and a
        leaked one is always real -- it is whichever run started the daemon, and that run
        has a record. A value naming no run at all is not that signature, so it is left
        alone and the ordinary refusals stand. Otherwise typing a nonsense value into the
        environment would buy strictly more than setting nothing does, which is the
        opposite of what a guard should do.
        """
        from agentjobs.dispatch.finish import own_run_id

        write_run_record(tmp_path, "run_74dfbc1c", task_id="task-318", live=True)
        hold_lock(tmp_path, "task-318", "run_74dfbc1c")

        assert own_run_id(tmp_path, "task-318", environ={RUN_ID_ENV: "run_nonsense"}) == (
            "run_nonsense"
        )


class TestAStaleIdentityDoesNotStripAuthority:
    """The consequence the repair above exists for, stated in the units that matter."""

    def test_the_dispatched_posture_survives_a_leaked_run_id(self, tmp_path: Path) -> None:
        from agentjobs.dispatch.config import Posture, ProjectDispatchSettings
        from agentjobs.dispatch.finish import own_run_id, released_posture

        # The leak: the environment names a finished run against another task.
        write_run_record(tmp_path, "run_12b2675c", task_id="task-269", live=False)
        # The truth: a live run dispatched against this task at `autonomous`.
        write_run_record(
            tmp_path,
            "run_68ea396e",
            task_id="task-316",
            posture="autonomous",
            posture_source="dispatch",
        )
        hold_lock(tmp_path, "task-316", "run_68ea396e")

        settings = ProjectDispatchSettings(
            project_id="agentjobs", posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS
        )
        resolved = released_posture(
            settings=settings,
            task_id="task-316",
            task_posture=None,
            home=tmp_path,
            run_id=own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_12b2675c"}),
        )

        assert resolved.posture is Posture.AUTONOMOUS

    def test_without_the_repair_the_same_input_falls_through_to_the_project(
        self, tmp_path: Path
    ) -> None:
        """What actually happened on task-316: `posture_requires_review`, on a task a
        human had already released. Pinned so the repair above cannot quietly regress."""
        from agentjobs.dispatch.config import Posture, ProjectDispatchSettings
        from agentjobs.dispatch.finish import released_posture

        write_run_record(tmp_path, "run_12b2675c", task_id="task-269", live=False)

        settings = ProjectDispatchSettings(
            project_id="agentjobs", posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS
        )
        resolved = released_posture(
            settings=settings,
            task_id="task-316",
            task_posture=None,
            home=tmp_path,
            run_id="run_12b2675c",
        )

        assert resolved.posture is Posture.AUTO

    def test_the_machine_ceiling_still_caps_what_a_recovered_run_may_claim(
        self, tmp_path: Path
    ) -> None:
        """Nothing recovered here can widen an envelope; the ceiling is re-applied from
        machine-local config whatever the run record says."""
        from agentjobs.dispatch.config import Posture, ProjectDispatchSettings
        from agentjobs.dispatch.finish import own_run_id, released_posture

        # The leaked identity is a real run, because a leaked one always is -- it is
        # whichever run started the daemon (task-318). This used to be a bare name with
        # no record behind it, which is the one shape recovery deliberately ignores.
        write_run_record(tmp_path, "run_stale", task_id="task-269", live=False)
        write_run_record(
            tmp_path,
            "run_68ea396e",
            task_id="task-316",
            posture="autonomous",
            posture_source="dispatch",
        )
        hold_lock(tmp_path, "task-316", "run_68ea396e")

        settings = ProjectDispatchSettings(
            project_id="agentjobs", posture=Posture.AUTO, max_posture=Posture.SUPERVISED
        )
        resolved = released_posture(
            settings=settings,
            task_id="task-316",
            task_posture=None,
            home=tmp_path,
            run_id=own_run_id(tmp_path, "task-316", environ={RUN_ID_ENV: "run_stale"}),
        )

        assert resolved.posture is Posture.SUPERVISED
        assert resolved.requested is Posture.AUTONOMOUS


# ----- the repository's one runway (task-223) ---------------------------------


class TestTheRunway:
    """One repository merges one branch at a time, and a finish holds the strip.

    **The property being protected is not "git can merge two branches".** It is that the
    commit which lands is the commit the gate verified. Without a runway, N concurrent
    finishers each rebase, gate and merge against a base the others are moving, and
    ``merge``'s ``base_moved`` check -- written for a rare race -- becomes the ordinary
    outcome, costing a dispatched run every time it fires.
    """

    def test_a_finish_takes_the_runway_and_gives_it_back(self, world: Dict[str, Any]) -> None:
        from agentjobs.dispatch.ledger import locks_root, runway_lock_name

        result = run(world)
        assert result.outcome == "finished", result.render()
        steps = {step.step: step for step in result.steps}
        assert "runway" in steps and steps["runway"].ok
        lock = locks_root(world["home"]) / f"{runway_lock_name(world['root'])}.lock"
        assert not lock.exists(), "a finished finish left the runway held"

    def test_the_runway_is_released_when_the_finish_stops(self, world: Dict[str, Any]) -> None:
        """An escalation must not strand the strip -- every other child would queue on it."""
        from agentjobs.dispatch.ledger import locks_root, runway_lock_name

        (world["worktree"] / "scripts" / "check.py").write_text(RED_GATE, encoding="utf-8")
        git(world["worktree"], "commit", "-am", "break the gate")

        result = run(world)
        assert result.outcome == "escalated", result.render()
        lock = locks_root(world["home"]) / f"{runway_lock_name(world['root'])}.lock"
        assert not lock.exists()

    def test_a_held_runway_makes_a_second_finish_queue_and_say_so(
        self, world: Dict[str, Any]
    ) -> None:
        """The queued finish writes to its own record rather than looking hung.

        A child third in line is otherwise indistinguishable from one that has stalled,
        which is the thing a person reading the dashboard has to be able to tell.
        """
        from agentjobs.dispatch.ledger import acquire_runway_lock

        held = acquire_runway_lock(world["home"], world["root"], finish_id="fin_held")
        try:
            result = run(world, runway_timeout_seconds=1)
        finally:
            held.release()

        assert result.outcome == "escalated"
        assert result.reason == "runway_busy"
        assert not merged_into(world["root"], world["branch"]), "it merged while queued"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        queued = [entry for entry in task.log if entry.data.get("finish_step") == "runway_queued"]
        assert queued, "a finish that queued said nothing about it on the record"
        assert "fin_held" in queued[-1].body

    def test_the_runway_is_keyed_on_the_checkout_not_the_project(self, tmp_path: Path) -> None:
        """Two projects over one clone share a ``main``, so they share a runway."""
        from agentjobs.dispatch.ledger import runway_lock_name

        root = tmp_path / "clone"
        root.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert runway_lock_name(root) == runway_lock_name(root)
        assert runway_lock_name(root) != runway_lock_name(other)
        # And one checkout spelled two ways is one runway, which on Windows includes case.
        assert runway_lock_name(root) == runway_lock_name(Path(str(root) + os.sep + "."))

    def test_a_second_claimant_waits_rather_than_being_refused(self, tmp_path: Path) -> None:
        """Contention on the runway means the queue is working, not that something is wrong.

        The opposite of the per-task lock, whose contention means two runs want one task.
        """
        from agentjobs.dispatch.ledger import RunLockTimeout, acquire_runway_lock

        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "repo"
        root.mkdir()
        first = acquire_runway_lock(home, root, finish_id="fin_a")
        seen: List[Any] = []
        with pytest.raises(RunLockTimeout) as refused:
            acquire_runway_lock(
                home, root, finish_id="fin_b", timeout=0.2, poll=0.05, on_wait=seen.append
            )
        assert seen and seen[0].finish_id == "fin_a"
        assert "runway" in str(refused.value)
        first.release()
        # And once it is free the next claimant takes it immediately.
        acquire_runway_lock(home, root, finish_id="fin_b", timeout=0.2).release()

    def test_releasing_does_not_delete_a_runway_somebody_else_reclaimed(
        self, tmp_path: Path
    ) -> None:
        """A shared lock released blind would let a third finish in beside the second."""
        from agentjobs.dispatch.ledger import (
            acquire_runway_lock,
            locks_root,
            read_lock_holder,
            runway_lock_name,
        )

        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "repo"
        root.mkdir()
        mine = acquire_runway_lock(home, root, finish_id="fin_a")
        path = locks_root(home) / f"{runway_lock_name(root)}.lock"
        # Somebody judged it stale and took it for themselves while we still held it.
        path.write_text("pid=1 run= kind=runway finish=fin_b", encoding="ascii")
        mine.release()
        holder = read_lock_holder(path)
        assert holder is not None and holder.finish_id == "fin_b"


# ----- what happens after it stops: somebody is always on the task -------------


DISPATCHABLE_CONFIG: Dict[str, Any] = {
    "project_name": "Demo",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


def make_dispatchable(
    world: Dict[str, Any],
    tmp_path: Path,
    *,
    auto_dispatch: bool = False,
    require_clean_tree: bool = False,
) -> Path:
    """Give the world a machine that *can* dispatch, with ``auto_dispatch`` off.

    Off is the configuration this whole class is about: it is what this repository runs,
    and until task-340 it was also what stopped an escalation starting anything. A test
    that turned it on to make a run appear would be asserting the old behaviour under a
    new name.

    The runner exits immediately. What is under test is whether a run is *started* and
    what it is attributed to, not what an agent does once it is running.
    """
    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (world["home"] / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {
                        "argv": [_interpreter(), str(runner), "{prompt}"],
                        "actor": "claude",
                    }
                },
                "projects": {
                    "demo": {
                        "enabled": True,
                        "runner": "fake",
                        "auto_dispatch": auto_dispatch,
                        "require_clean_tree": require_clean_tree,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    root = world["root"]
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(DISPATCHABLE_CONFIG), encoding="utf-8"
    )
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "-m", "chore: ignore the machine-local config")
    return runner


def approve(world: Dict[str, Any]) -> int:
    """The human act every finish descends from, written as a person would leave it.

    Returned rather than assumed to be the last entry: the point of the assertions below
    is that the run is attributed to *this* id and not to whatever the finisher wrote
    afterwards, and hard-coding the number would test the fixture.
    """
    manager: TaskManager = world["manager"]
    manager.add_log_entry(
        world["task_id"],
        actor="Jeff Posey",
        type=LogEntryType.NOTE,
        body="Approved. Merge it.",
    )
    task = manager.get_task(world["task_id"])
    assert task is not None
    return task.log[-1].id


def break_the_gate(world: Dict[str, Any], output: str = "vitest failed") -> None:
    """A branch whose gate goes red, in its own worktree."""
    script = f"import sys\nprint({output!r})\nsys.exit(1)\n"
    (world["worktree"] / "scripts" / "check.py").write_text(script, encoding="utf-8")
    git(world["worktree"], "commit", "-am", "break the gate")


PYTEST_SHAPED_GATE = """\
import sys

print("============================= test session starts =============================")
for i in range(40):
    print("  DeprecationWarning: something in a dependency is deprecated (%d)" % i)
print("FAILED tests/test_validate.py::TestRealCorpus::test_the_drift - AssertionError: drifted")
print("FAILED tests/test_other.py::test_two - assert 3 == 4")
for i in range(40):
    print("  DeprecationWarning: and again after it (%d)" % i)
print("\\nFailed at stage 'pytest'.", file=sys.stderr)
sys.exit(1)
"""


class TestTheEscalationStartsTheRepair:
    """task-340. An approval is the human act; a red gate is work for an agent.

    The incident is task-337 on 2026-09-05: an approved task whose finish went red at the
    gate, handed the ball to ``agent``/``work``, dispatched nobody because
    ``auto_dispatch`` was off, notified nobody, and sat there all evening. Every
    assertion in this class is one sentence of that incident.
    """

    def test_a_red_gate_starts_a_run_with_auto_dispatch_off(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """ac-1. The whole of it: no ``auto_dispatch``, no second click, a run exists."""
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "gate_failed"
        assert result.dispatched_run_id, result.render()
        assert result.escalation_dispatch == "dispatched"

    def test_the_run_is_attributed_to_the_humans_approval(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """Not to anything the finisher wrote, which is by then the newest entry."""
        make_dispatchable(world, tmp_path)
        approval = approve(world)
        break_the_gate(world)

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        dispatch = next(
            entry for entry in reversed(task.log) if entry.type is LogEntryType.DISPATCH
        )
        assert dispatch.data.get("caused_by") == approval
        assert dispatch.data.get("trigger") == "auto"

    def test_the_ball_stays_on_the_agent_that_was_started(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The half of ac-2 that is *not* a handoff: a run exists, so `agent` is true."""
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.ball is Ball.AGENT
        assert "scripts/check.py" in (task.ball_prompt or "")

    def test_switching_it_off_leaves_the_task_on_a_human_not_on_an_agent(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """ac-2. The knob exists, and turning it off does not recreate the incident."""
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        result = run(world, dispatch_on_escalation=False)

        assert result.dispatched_run_id is None
        assert result.escalation_dispatch == "escalation_dispatch_off"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open
        assert task.ball is Ball.HUMAN
        assert task.ball_reason is BallReason.DECISION
        assert "no agent was started" in (task.ball_prompt or "")
        assert "dispatch_on_escalation" in (task.ball_prompt or "")
        assert f"agentjobs finish {world['task_id']} --project demo" in (task.ball_prompt or "")

    def test_a_machine_that_cannot_dispatch_at_all_also_lands_on_a_human(
        self, world: Dict[str, Any]
    ) -> None:
        """No dispatch config whatsoever -- the shape every other test in this file has."""
        break_the_gate(world)

        result = run(world)

        assert result.escalation_dispatch == "dispatch_not_permitted"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.ball is Ball.HUMAN
        assert task.ball_reason is BallReason.DECISION

    def test_no_human_entry_is_a_human_decision_rather_than_a_silent_stop(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The approval cannot be found, so there is nothing to spend. Say so."""
        make_dispatchable(world, tmp_path)
        break_the_gate(world)

        result = run(world)

        assert result.dispatched_run_id is None
        assert result.escalation_dispatch == "no_human_entry"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.ball is Ball.HUMAN
        assert "no approval" in (task.ball_prompt or "").lower()

    def test_a_refused_dispatch_is_named_rather_than_swallowed(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """Somebody is working in the shared clone. The ball still names a real person.

        A dirty tree rather than a stubbed exception, because it is the refusal an
        escalation is most likely to meet in real life: the finish only runs when a task
        is ready to merge, and the clone it merges into is shared.
        """
        make_dispatchable(world, tmp_path, require_clean_tree=True)
        approve(world)
        break_the_gate(world)
        (world["root"] / "shared.txt").write_text(
            "somebody else is editing this\n", encoding="utf-8"
        )

        result = run(world)

        assert result.dispatched_run_id is None
        assert result.escalation_dispatch == "dispatch_refused"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.ball is Ball.HUMAN
        assert "uncommitted changes" in (task.ball_prompt or "")

    def test_the_escalation_prompt_reaches_the_run_that_was_started(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The prompt is the escalation's, not a fresh "work task-NNN".

        Asserted on the argv the dispatch recorded, because that is what the process was
        actually given. A cold start carries a pointer to the record by design
        (``PROMPT_STUB``), so what is checked here is that the record it points at holds
        the escalation -- and that the prompt names this task rather than describing some
        other work.
        """
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        dispatch = next(
            entry for entry in reversed(task.log) if entry.type is LogEntryType.DISPATCH
        )
        argv = dispatch.data.get("argv") or []
        prompt = "\n".join(str(word) for word in argv)
        assert task.id in prompt
        # And the record the prompt points at is the escalation, not the original brief.
        assert "scripts/check.py" in (task.ball_prompt or "")

    def test_the_prompt_names_the_branch_and_worktree_the_work_is_already_in(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """A cold session is otherwise told to take a *new* worktree. This is the fix."""
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        prompt = task.ball_prompt or ""
        assert world["branch"] in prompt
        assert str(world["worktree"]) in prompt
        assert "do not take a new one" in prompt


class TestLeadingWithTheCause:
    """ac-3. Which test failed, in the first screen -- not two hundred lines down."""

    def test_the_stage_and_the_failing_tests_come_before_the_noise(
        self, world: Dict[str, Any]
    ) -> None:
        (world["worktree"] / "scripts" / "check.py").write_text(
            PYTEST_SHAPED_GATE, encoding="utf-8"
        )
        git(world["worktree"], "commit", "-am", "a gate that fails like pytest does")

        result = run(world)

        assert result.reason == "gate_failed"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        body = escalation(task).body
        head = body[: body.index("Everything it did get through")]
        assert "`pytest` stage" in head
        assert "TestRealCorpus::test_the_drift" in head
        assert "AssertionError: drifted" in head
        assert "test_other.py::test_two" in head
        # The whole point: the warnings that used to fill the window are gone from the
        # record entirely, and the log on disk is where they still are.
        assert "DeprecationWarning" not in body
        assert "gate.log" in body

    def test_the_failing_test_is_in_the_first_lines_a_reader_sees(
        self, world: Dict[str, Any]
    ) -> None:
        """The dashboard shows the top of an entry, so position is the property."""
        (world["worktree"] / "scripts" / "check.py").write_text(
            PYTEST_SHAPED_GATE, encoding="utf-8"
        )
        git(world["worktree"], "commit", "-am", "a gate that fails like pytest does")

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        first = escalation(task).body.splitlines()[:6]
        assert any("TestRealCorpus::test_the_drift" in line for line in first), first

    def test_a_gate_that_names_no_test_still_shows_its_output(self, world: Dict[str, Any]) -> None:
        """Nothing to extract is the one case where the raw tail is the best answer."""
        break_the_gate(world, output="oxlint: 3 problems")

        run(world)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert "oxlint: 3 problems" in escalation(task).body

    def test_reading_the_stage_and_the_failures_out_of_gate_output(self) -> None:
        """The two readers, directly, including the shapes that must return nothing."""
        assert failing_stage("noise\nFailed at stage 'vitest'.\nmore noise") == "vitest"
        assert failing_stage("nothing said about a stage") is None
        assert failing_stage("Failed at stage 'unterminated") is None
        assert failing_tests("FAILED a::b - boom\nFAILED a::b - boom\nERROR c") == [
            "FAILED a::b - boom",
            "ERROR c",
        ]
        assert failing_tests("the word FAILED in the middle of a line") == []

    def test_a_flood_of_failures_is_capped_and_says_it_was(self, tmp_path: Path) -> None:
        output = "\n".join(f"FAILED tests/t.py::test_{i}" for i in range(40))
        rendered = lead_with_the_cause(
            output + "\nFailed at stage 'pytest'.", log=tmp_path / "gate.log"
        )
        assert rendered.count("FAILED") == SALIENT_LIMIT
        assert "and possibly more" in rendered
        assert "gate.log" in rendered


@pytest.fixture()
def remote(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A real ``RemoteTaskManager`` over a real cut-over project, through the real routes.

    Module level rather than inside the class it was written for (task-388), because
    task-390 needs the same thing for a different failure: the finish's escalation reached
    ``record_dispatch`` through one of these and got the refusal it is designed to give.
    Two classes, one arrangement -- the alternative was a second copy that would drift.
    """
    from starlette.testclient import TestClient

    from agentjobs.api.dependencies import reset_dependency_cache
    from agentjobs.api.main import app
    from agentjobs.client import TaskClient
    from agentjobs.remote_manager import RemoteTaskManager
    from agentjobs.store_factory import close_databases, mark_server_process

    monkeypatch.setenv("AGENTJOBS_HOME", str(world["home"]))
    # Both caches are process-global and both outlive a test: a handle to the
    # previous case's database, and the dependency wiring that resolved it.
    close_databases()
    reset_dependency_cache()

    # `agentjobs.api.main` declares the server on import, and the import is cached,
    # so only the first test in a session gets that declaration -- conftest's autouse
    # teardown zeroes it after every test. Without this the app answers the second
    # case with `StoreAccessError`, which would look like a product bug and is a
    # test-session artefact.
    mark_server_process()
    connection = TestClient(app)
    client = TaskClient("http://testserver", client=connection, project_id="demo")
    yield RemoteTaskManager(client, world["project"])
    connection.close()
    close_databases()
    reset_dependency_cache()


class TestWritingTheRecordOverTheService:
    """The manager a finish gets outside the server process.

    Every test above hands the sequence a local :class:`~agentjobs.manager.TaskManager`.
    That is not what a finish run from a shell gets: ``task_manager_for`` returns a
    :class:`~agentjobs.remote_manager.RemoteTaskManager` outside the server, so every
    patch the finish writes becomes an HTTP request body -- and the difference between a
    Python object and its JSON form, which a local manager simply does not have, is a
    ``TypeError`` in ``httpx``'s encoder.

    Task-388 is that failure: after a real merge, a real rebuild and a verified restart,
    `mark_branch_merged` died on ``Object of type datetime is not JSON serializable`` and
    every scripted finish on this project stopped one step short of closing its task. A
    files-only test cannot see it, which is precisely why there was not one.

    The transport is ASGI rather than a socket, so the request goes through routing, the
    capability gate and the real route -- the half that a stub manager would skip and the
    half this bug lived in.
    """

    def test_marking_the_branch_merged_survives_the_wire(
        self, world: Dict[str, Any], remote: Any
    ) -> None:
        """The write that stopped every finish on this project, over the real route."""
        from agentjobs.dispatch.finish import mark_branch_merged

        mark_branch_merged(remote, world["task_id"], world["branch"])

        task = remote.get_task(world["task_id"])
        assert task is not None
        merged = [entry for entry in task.branches if entry.name == world["branch"]]
        assert merged, "the branch must still be on the record"
        assert merged[0].status is BranchStatus.MERGED
        # The timestamp is the payload that could not cross: assert it arrived and was
        # stored as a time, not that the request merely returned.
        assert merged[0].merged_at is not None

    def test_a_branch_already_merged_is_carried_across_untouched(
        self, world: Dict[str, Any], remote: Any
    ) -> None:
        """The other datetime on that patch, which no new value passes through.

        An entry marked merged by an earlier finish already holds a ``merged_at``, and
        the patch re-sends every entry. So the round trip has to render a datetime it did
        not construct -- a case a fix confined to ``datetime.now()`` would still fail.
        """
        from agentjobs.dispatch.finish import mark_branch_merged

        remote.update_task(
            world["task_id"],
            actor="claude",
            branches=[
                {"name": "feat/older", "status": "merged", "merged_at": "2026-09-01T10:00:00Z"},
                {"name": world["branch"], "status": "active"},
            ],
        )
        mark_branch_merged(remote, world["task_id"], world["branch"])

        task = remote.get_task(world["task_id"])
        assert task is not None
        by_name = {entry.name: entry for entry in task.branches}
        assert by_name["feat/older"].merged_at is not None
        assert by_name[world["branch"]].status is BranchStatus.MERGED

    def test_a_finish_completes_end_to_end_on_the_backend_this_project_uses(
        self, world: Dict[str, Any], remote: Any
    ) -> None:
        """AC-2, as a test rather than as an observation of one production run.

        The whole sequence, with the remote manager: merge, close, remove the worktree,
        delete the branch. What task-388 broke was not the merge -- that always worked --
        but everything the finish writes *after* it, so a test that stops at the merge
        would still be green today.
        """
        result = finish_task(
            manager=remote,
            project=world["project"],
            task_id=world["task_id"],
            approver="Jeff Posey",
            home=world["home"],
            api_base="http://127.0.0.1:1",
            settings=settings(),
        )
        assert result.outcome == FINISHED, result.render()
        assert landed(world["root"], result)

        task = remote.get_task(world["task_id"])
        assert task is not None
        assert task.lifecycle is Lifecycle.CLOSED
        assert task.outcome is Outcome.COMPLETED
        merged = [entry for entry in task.branches if entry.name == world["branch"]]
        assert merged and merged[0].status is BranchStatus.MERGED
        assert merged[0].merged_at is not None
        assert not world["worktree"].exists()
        assert world["branch"] not in worktree_paths(world["root"])

    def test_it_writes_no_task_file_and_leaves_no_dirty_checkout(
        self, world: Dict[str, Any], remote: Any
    ) -> None:
        """The other half of AC-2, which the assertions above do not reach.

        A finish makes four or five writes to the record -- the branch, the closure, the
        delivery -- and every one of them used to land in a tracked file that somebody
        then had to commit. The observable that says they no longer do is ``git
        status``: nothing may appear under ``tasks/`` and nothing there may change.
        """
        listing = sorted(path.name for path in (world["root"] / "tasks").iterdir())

        result = finish_task(
            manager=remote,
            project=world["project"],
            task_id=world["task_id"],
            approver="Jeff Posey",
            home=world["home"],
            api_base="http://127.0.0.1:1",
            settings=settings(),
        )
        assert result.outcome == FINISHED, result.render()

        # Scoped to `tasks/` because this fixture's clone has no `.gitignore` for the
        # machine-local `.agentjobs/` a real project carries one for. The claim is about
        # the records, and naming the path makes that the claim.
        assert git(world["root"], "status", "--porcelain", "--", "tasks").stdout.strip() == ""
        assert sorted(path.name for path in (world["root"] / "tasks").iterdir()) == listing
        # ...and the writes really happened, in the store, so this is not green because
        # the finish did nothing.
        closed = remote.get_task(world["task_id"])
        assert closed is not None and closed.lifecycle is Lifecycle.CLOSED


# ----- the invariant, and the two roads that used to defeat it (task-390) ------


def make_session_dispatchable(
    world: Dict[str, Any], tmp_path: Path, *, require_clean_tree: bool = False
) -> Path:
    """``make_dispatchable``, but with a runner the poller can actually follow.

    The difference is ``mode: session``. ``make_dispatchable``'s runner exits the moment
    it is started, which is all its own tests need -- they ask whether a run was *begun*.
    Everything below is about what happens when a run **ends**, so the run has to be one
    something can observe ending, and that means the fake CLI the runner and poller
    suites already drive: session mode is defined operationally as a runner whose
    executable answers ``agents --json``.

    The project is registered as well, because the two things that re-ask the escalation's
    question -- the poller and ``resolve_deferred_escalation`` -- both resolve a project
    from the registry rather than being handed one.
    """
    from agentjobs.projects import ProjectError, ProjectRegistry

    from test_dispatch_runner import FAKE_CLI, write_script

    fake_cli = write_script(tmp_path / "fakecli.py", FAKE_CLI)
    (world["home"] / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "api_base": "http://127.0.0.1:1",
                "runners": {
                    "fake": {
                        "mode": "session",
                        "argv": [_interpreter(), str(fake_cli), "--bg", "{prompt}"],
                        "actor": "claude",
                    }
                },
                "projects": {
                    "demo": {
                        "enabled": True,
                        "runner": "fake",
                        "auto_dispatch": False,
                        "require_clean_tree": require_clean_tree,
                    }
                },
                "limits": {"session_stale_seconds": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    root = world["root"]
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(DISPATCHABLE_CONFIG), encoding="utf-8"
    )
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "-m", "chore: ignore the machine-local config")
    registry = ProjectRegistry(home=world["home"])
    try:
        registry.get("demo")
    except ProjectError:
        registry.add(root, project_id="demo")
    return fake_cli


def start_live_session(world: Dict[str, Any], caused_by: int) -> Any:
    """A real dispatched session run on the task, started the way dispatch starts one."""
    from agentjobs.dispatch.config import assert_dispatch_permitted
    from agentjobs.dispatch.runner import DispatchRunner

    runner = DispatchRunner(
        manager=world["manager"],
        resolution=assert_dispatch_permitted("demo", world["home"]),
        project_root=world["root"],
        home=world["home"],
    )
    return runner.start(
        world["manager"].get_task(world["task_id"]), actor="Jeff Posey", caused_by=caused_by
    )


def end_the_session(world: Dict[str, Any], fake_cli: Path) -> None:
    """Let the session go idle and have the poller settle it, as the real one does.

    Nothing here reaches into the run's record. The fake CLI reports the session done,
    ``poll_live_sessions`` finds it on disk exactly as the daemon would, and every
    consequence -- the terminal entry, the lock release, and whatever the settle then
    decides about a deferred escalation -- is the application's own.
    """
    from agentjobs.dispatch.poller import poll_live_sessions

    (fake_cli.parent / "ledger.json").write_text(
        json.dumps([{"id": "b55b35ad", "status": "idle", "state": "done"}]), encoding="utf-8"
    )
    poll_live_sessions(world["home"])


def someone_is_actually_there(world: Dict[str, Any]) -> None:
    """The invariant, asserted the only way that means anything: after the run ended.

    An open task reading ``agent`` is a claim that a named agent is about to act. With no
    live run on the machine, nothing is -- and the schema cannot catch it, because the
    record is internally consistent and simply untrue. Every road in this section ends
    here, whichever of the two legitimate answers it takes to get here.
    """
    from agentjobs.dispatch.ledger import live_runs

    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    if not task.is_open or task.ball is not Ball.AGENT:
        return
    if task.ball_reason in {BallReason.AVAILABLE, BallReason.HOLD}:
        return
    running = [run for run in live_runs(world["home"]) if run.task_id == world["task_id"]]
    assert running, (
        f"{world['task_id']} is open at {task.ball}/{task.ball_reason} with no live run: "
        "the ball names an agent that does not exist."
    )


class TestTheEscalationsPromiseSurvivesTheRunItDeferredTo:
    """task-390, road one. "A run is live" is evidence about now; the question is next.

    The incident is task-230 on 2026-09-07. The finish escalated correctly, saw a live
    run on the task, concluded that run was the session its handback was addressed to,
    and stopped. That run recorded its own outcome 36 seconds later, and the task sat
    open at ``agent``/``work`` with nothing on it for seventeen minutes.

    Nothing was going to deliver the handback to it, either -- ``pending_handback`` only
    ever returns a handoff written by a **human**, and an escalation's is written by
    ``finisher``, whose reserved kind is ``agent``. So the run itself was the only
    candidate, and the only moment the question could be answered was when it stopped.
    """

    def test_a_deferred_escalation_is_re_asked_when_the_run_ends(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The whole of road one, against a real dispatched run that really ends."""
        fake_cli = make_session_dispatchable(world, tmp_path)
        approve(world)
        handle = start_live_session(world, caused_by=1)
        break_the_gate(world)

        result = run(world)

        # The deferral itself is unchanged and still correct: a live run was there.
        assert result.escalation_dispatch == "live_run"
        assert result.dispatched_run_id == handle.run_id
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.ball is Ball.AGENT

        end_the_session(world, fake_cli)

        someone_is_actually_there(world)

    def test_the_run_carries_the_finish_that_is_waiting_on_it(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """Written to the run's own directory, so it survives every process involved.

        The finish that deferred is a detached process that has exited by the time the
        run ends. A promise held anywhere but on disk would not be there to keep.
        """
        from agentjobs.dispatch.finish import ESCALATION_PENDING

        make_session_dispatchable(world, tmp_path)
        approve(world)
        handle = start_live_session(world, caused_by=1)
        break_the_gate(world)

        result = run(world)

        meta = yaml.safe_load(
            (world["home"] / "runs" / handle.run_id / "meta.yaml").read_text(encoding="utf-8")
        )
        assert meta[ESCALATION_PENDING] == result.finish_id

    def test_a_run_that_ended_without_the_ball_moving_lands_on_a_human(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The fallback half: nothing can be started, so the ball leaves the agent.

        A dirty shared clone is the refusal an escalation is most likely to meet, and it
        is the one that proves the re-ask is not merely a second chance to dispatch --
        where it cannot, ``park_for_human`` still runs and the task names a person.
        """
        fake_cli = make_session_dispatchable(world, tmp_path, require_clean_tree=True)
        approve(world)
        start_live_session(world, caused_by=1)
        break_the_gate(world)
        run(world)
        (world["root"] / "shared.txt").write_text("somebody else is editing\n", encoding="utf-8")

        end_the_session(world, fake_cli)

        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open
        assert task.ball is Ball.HUMAN
        assert task.ball_reason is BallReason.DECISION
        assert "no agent was started" in (task.ball_prompt or "")
        someone_is_actually_there(world)

    def test_a_run_that_took_the_work_is_left_alone(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The re-ask must not second-guess a session that did what it was handed.

        Deferring was *right* here, and a fix that parked every deferred escalation would
        be trading one wrong ball for another -- this is the case that says it does not.
        """
        fake_cli = make_session_dispatchable(world, tmp_path)
        approve(world)
        start_live_session(world, caused_by=1)
        break_the_gate(world)
        run(world)
        world["manager"].handoff(
            world["task_id"],
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Gate fixed; look again.",
        )

        end_the_session(world, fake_cli)

        from agentjobs.dispatch.ledger import live_runs

        task = world["manager"].get_task(world["task_id"])
        assert task is not None
        assert task.ball is Ball.HUMAN and task.ball_reason is BallReason.REVIEW
        assert "no agent was started" not in (task.ball_prompt or "")
        assert live_runs(world["home"]) == [], "the re-ask started a run nobody needed"

    def test_the_escalation_still_starts_no_second_run_while_one_is_live(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """ac-7. The reasoning the ``live_run`` branch was built on is not relaxed.

        One live run per task, always: a second would put two agents on one branch with
        one task record. The re-ask changes *when* the question is answered, never the
        answer while a session is up.
        """
        from agentjobs.dispatch.ledger import live_runs

        make_session_dispatchable(world, tmp_path)
        approve(world)
        handle = start_live_session(world, caused_by=1)
        break_the_gate(world)

        run(world)

        assert [record.run_id for record in live_runs(world["home"])] == [handle.run_id]


class TestTheHandlerKeepsItsPromiseWhenTheManagerRefuses:
    """task-390, road two. The docstring said "never raises"; it named two exceptions.

    Observed on task-230 on 2026-09-07, running the retry ENGINEERING.md documents --
    ``agentjobs finish`` by hand, on a project that had moved to the database that
    afternoon. The CLI holds a service client there, the escalation handed that client to
    ``dispatch_task``, and ``record_dispatch`` refused **by design**. The traceback came
    out through ``finish_task``, so ``park_for_human`` never ran and the escalation this
    process had already written to the record was left with nobody on it.
    """

    def test_a_red_gate_on_a_served_project_still_starts_the_repair(
        self, world: Dict[str, Any], remote: Any, tmp_path: Path
    ) -> None:
        """The incident, on the arrangement it happened on: a cut-over project.

        The manager is a real ``RemoteTaskManager`` over a real cut-over store, reached
        through the real routes -- which is what makes this road two rather than a
        rehearsal of it. On the parent commit it raises ``RemoteStoreUnsupported`` out of
        ``finish_task``, with the traceback the task record carries.

        What it proves about the fix is the *behaviour*, not the plumbing: the retry
        ENGINEERING.md documents starts a repair session on this backend. Parking would
        also have stopped the crash and would have been the wrong answer.
        """
        # The session runner rather than ``make_dispatchable``'s batch one, so no
        # supervisor thread outlives the test and reaches for a database the fixture has
        # closed. What is under test is the escalation's manager, not the run's mode.
        make_session_dispatchable(world, tmp_path)
        remote.add_log_entry(
            world["task_id"],
            actor="Jeff Posey",
            type=LogEntryType.NOTE,
            body="Approved. Merge it.",
        )
        break_the_gate(world)

        result = finish_task(
            manager=remote,
            project=world["project"],
            task_id=world["task_id"],
            approver="Jeff Posey",
            home=world["home"],
            api_base="http://127.0.0.1:1",
            settings=settings(),
        )

        assert result.outcome == ESCALATED
        assert result.escalation_dispatch == "dispatched", result.render()
        assert result.dispatched_run_id
        task = remote.get_task(world["task_id"])
        assert task is not None and task.ball is Ball.AGENT

    def test_the_real_remote_manager_still_refuses(self) -> None:
        """ac-7's other half. The refusal is deliberate and this fix does not touch it.

        A run is recorded by the process that started it, and recording one over the wire
        would mean a request model carrying ``argv``. The repair was to stop handing the
        dispatch family a client, not to make the client do dispatch's job.
        """
        from agentjobs.remote_manager import RemoteStoreUnsupported, RemoteTaskManager

        with pytest.raises(RemoteStoreUnsupported, match="dispatch_manager_for"):
            RemoteTaskManager.record_dispatch(
                object(),  # type: ignore[arg-type]
                "task-001",
                actor="claude",
            )

    def test_anything_else_the_dispatch_raises_becomes_a_human_decision(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Question 2, answered as behaviour: the catch is the type, not a list of them.

        A named-exception list is only as good as the names on it, and road two is what a
        missing name costs. The exception is not swallowed -- it is what the human reads.
        """
        make_dispatchable(world, tmp_path)
        approve(world)
        break_the_gate(world)

        def explode(**kwargs: Any) -> Any:
            raise RuntimeError("the dispatch subsystem fell over")

        monkeypatch.setattr("agentjobs.dispatch.guards.dispatch_task", explode)

        result = run(world)

        assert result.outcome == ESCALATED, "the escalation itself must still be reported"
        assert result.escalation_dispatch == "dispatch_crashed"
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and task.is_open
        assert task.ball is Ball.HUMAN
        assert task.ball_reason is BallReason.DECISION
        assert "RuntimeError" in (task.ball_prompt or "")
        assert "the dispatch subsystem fell over" in (task.ball_prompt or "")
        someone_is_actually_there(world)

    def test_a_finish_whose_park_cannot_be_written_still_returns_its_result(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last thing between an escalation and a traceback out of ``finish_task``.

        ``park_for_human`` and the commit after it both go through a manager, and on a
        served project that manager is over HTTP. A refused request there would throw
        away an escalation this process has already recorded -- so it is caught, and the
        finish's own directory records that the park failed rather than the shell
        learning it and nobody else.
        """
        make_dispatchable(world, tmp_path)
        break_the_gate(world)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the service refused the handoff")

        monkeypatch.setattr("agentjobs.dispatch.finish.park_for_human", refuse)

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.escalation_dispatch == "no_human_entry"
        meta = yaml.safe_load((result.directory / "meta.yaml").read_text(encoding="utf-8"))
        assert "the service refused the handoff" in meta["escalation_park_failed"]
