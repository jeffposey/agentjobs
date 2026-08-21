"""The dispatcher committing what it wrote (task-203).

These drive **real git repositories** rather than mocking ``subprocess``. The whole
value of the change is in git's own semantics -- specifically that ``git commit --only``
ignores the index, so a colleague's staged work survives -- and a mocked git would assert
that we passed the flag while proving nothing about what the flag does.

Reported symptom, for whoever reads this next: every dispatched run appended a
``dispatch_result`` after the session had made its final commit and exited, and nobody
committed it. The shared clone was left dirty after each run, and the person who noticed
was always the human.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchLimits,
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    ProjectDispatchSettings,
    RunnerMode,
)
from agentjobs.dispatch.record_commit import commit_task_record
from agentjobs.dispatch.runner import DispatchRunner, DispatchRunError
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, DispatchOutcome, Lifecycle, LogEntryType
from agentjobs.storage import TaskStorage


def git(root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def porcelain(root: Path) -> list[str]:
    """Every uncommitted path in the repository, however it is uncommitted.

    ``-uall`` because the default collapses an untracked directory to its name, and the
    whole point here is which individual file did or did not get committed.
    """
    out = git(root, "status", "--porcelain", "-uall").stdout
    return [line for line in out.splitlines() if line.strip()]


def subjects(root: Path) -> list[str]:
    return git(root, "log", "--format=%s").stdout.splitlines()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repository whose tasks directory is tracked inside it.

    This is the shape that matters: AgentJobs writes task records into the very tree it
    is dispatching against, which is exactly why its own writes end up dirtying it.
    """
    root = tmp_path / "project"
    (root / "tasks").mkdir(parents=True)
    (root / "src").mkdir()
    git(root.parent, "init", str(root))
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "--", "src/app.py")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def manager(repo: Path) -> TaskManager:
    return TaskManager(TaskStorage(repo / "tasks"))


@pytest.fixture
def task_id(manager: TaskManager) -> str:
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return created.id


def commit_everything(repo: Path) -> None:
    """Get to the state a run starts from: the task file already tracked and clean."""
    git(repo, "add", "--", "tasks")
    git(repo, "commit", "-m", "task files")


class TestTheTreeEndsClean:
    """ac-1: after a dispatched write, nothing of ours is left uncommitted."""

    def test_a_dispatch_result_is_committed(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        commit_everything(repo)
        manager.record_dispatch_result(
            task_id,
            actor="dispatcher",
            run_id="run_abc",
            outcome=DispatchOutcome.COMPLETED,
            duration_seconds=498.5,
        )
        assert porcelain(repo), "the manager write should have dirtied the tree first"

        outcome = commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert outcome.committed, outcome.detail
        assert porcelain(repo) == []
        assert subjects(repo)[0] == f"chore({task_id}): record run run_abc as completed"

    def test_the_commit_body_says_who_wrote_it_and_why(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """A dispatcher commit must be attributable in `git log` without a re-run."""
        commit_everything(repo)
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )

        commit_task_record(manager, task_id, subject="record run run_abc as completed")

        body = git(repo, "log", "-1", "--format=%b").stdout
        assert "dispatcher" in body
        assert "task-203" in body

    def test_an_untracked_task_file_is_committed_too(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """A task created and dispatched before anyone committed it is still untracked.

        ``--only`` resolves its pathspec against what git knows, so this path needs the
        intent-to-add first or the commit fails with "pathspec did not match".
        """
        assert porcelain(repo) == [f"?? tasks/{task_id}.yaml"]

        outcome = commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert outcome.committed, outcome.detail
        assert porcelain(repo) == []

    def test_a_clean_file_is_left_alone(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """Nothing to commit is not a failure, and must not produce an empty commit."""
        commit_everything(repo)
        before = subjects(repo)

        outcome = commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert not outcome.committed
        assert "nothing uncommitted" in outcome.detail
        assert subjects(repo) == before


class TestOnlyOurOwnFile:
    """ac-2 and ac-3: one path, from the working tree, ignoring everybody else."""

    def test_another_task_file_is_not_swept_in(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """The bug this replaces was a dirty file. Fixing it with `-A` would be worse."""
        other = manager.create_task(
            title="Somebody else's",
            category="infrastructure",
            summary="Another task entirely.",
            description="Not ours.",
            lifecycle=Lifecycle.READY,
        )
        commit_everything(repo)
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )
        manager.add_log_entry(
            other.id,
            actor="claude",
            type=LogEntryType.NOTE,
            body="mid-flight work by a peer",
        )

        commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert porcelain(repo) == [f" M tasks/{other.id}.yaml"]
        assert git(repo, "show", "--name-only", "--format=", "HEAD").stdout.strip() == (
            f"tasks/{task_id}.yaml"
        )

    def test_a_peers_staged_work_stays_staged_and_uncommitted(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """The failure mode to avoid: committing a half-finished edit somebody staged.

        A plain `git commit` after `git add <our path>` would take the whole index and
        publish this with our name on it. `--only` reads the named path from the working
        tree and leaves the index untouched.
        """
        commit_everything(repo)
        (repo / "src" / "app.py").write_text("x = 2  # half-finished\n", encoding="utf-8")
        git(repo, "add", "--", "src/app.py")
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )

        outcome = commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert outcome.committed, outcome.detail
        assert porcelain(repo) == ["M  src/app.py"], "the peer's staged edit must survive"
        assert git(repo, "show", "--name-only", "--format=", "HEAD").stdout.strip() == (
            f"tasks/{task_id}.yaml"
        )

    def test_an_unstaged_edit_elsewhere_is_untouched(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        commit_everything(repo)
        (repo / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )

        commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert porcelain(repo) == [" M src/app.py"]


class TestItNeverPushes:
    """ac-4, asserted rather than only written down: a decision nothing enforces drifts."""

    def test_no_remote_is_contacted(self, repo: Path, manager: TaskManager, task_id: str) -> None:
        """A bare repo as `origin`, and nothing may reach it.

        If a push were ever added, this fails: the commit would land on origin's branch.
        """
        remote = repo.parent / "origin.git"
        git(repo.parent, "init", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        commit_everything(repo)
        git(repo, "push", "-q", "origin", "HEAD")
        published = git(remote, "rev-parse", "HEAD").stdout.strip()

        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )
        assert commit_task_record(
            manager, task_id, subject="record run run_abc as completed"
        ).committed

        assert git(remote, "rev-parse", "HEAD").stdout.strip() == published
        assert git(repo, "rev-parse", "HEAD").stdout.strip() != published


class TestItNeverRaises:
    """This runs on the terminal path of a finished run. A git problem is not a crash."""

    def test_a_project_outside_any_repository(self, tmp_path: Path) -> None:
        tasks = tmp_path / "loose" / "tasks"
        tasks.mkdir(parents=True)
        manager = TaskManager(TaskStorage(tasks))
        created = manager.create_task(
            title="Ungitted",
            category="infrastructure",
            summary="No repository here.",
            description="None.",
            lifecycle=Lifecycle.READY,
        )

        outcome = commit_task_record(manager, created.id, subject="record run run_abc")

        assert not outcome.committed
        assert "git" in outcome.detail

    def test_a_task_with_no_file_on_disk(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        outcome = commit_task_record(manager, "task-999", subject="record run run_abc")

        assert not outcome.committed
        assert "task-999" in outcome.detail

    def test_a_hook_that_refuses_the_commit_is_reported_not_raised(
        self, repo: Path, manager: TaskManager, task_id: str
    ) -> None:
        """A project's own pre-commit gate must not be bypassed, and must not crash us."""
        commit_everything(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho refused by the gate >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_abc", outcome=DispatchOutcome.COMPLETED
        )

        outcome = commit_task_record(manager, task_id, subject="record run run_abc as completed")

        assert not outcome.committed
        assert "refused" in outcome.detail
        assert porcelain(repo) == [f" M tasks/{task_id}.yaml"], "left exactly as it was"


class TestThroughTheDispatcher:
    """The helper working is not the claim. The claim is that a real run ends clean.

    A batch run is the mode that can be driven end to end in a test without a session
    manager, and it exercises the same ``_commit_record`` seam the session path uses.
    """

    @staticmethod
    def dispatcher(repo: Path, manager: TaskManager, home: Path, argv: list[str]) -> DispatchRunner:
        runner = RunnerConfig(name="fake", argv=argv, env={}, mode=RunnerMode.BATCH)
        settings = ProjectDispatchSettings(
            project_id="sandbox", enabled=True, runner="fake", require_clean_tree=False
        )
        limits = DispatchLimits(run_timeout_seconds=60, session_stale_seconds=60)
        resolution = DispatchResolution(
            project_id="sandbox",
            runner=runner,
            settings=settings,
            limits=limits,
            config=DispatchConfig(enabled=True, limits=limits),
        )
        return DispatchRunner(
            manager=manager,
            resolution=resolution,
            project_root=repo,
            home=home,
            api_base="http://localhost:8899",
            grace_seconds=2.0,
        )

    def test_a_finished_run_leaves_the_tree_clean(
        self, repo: Path, manager: TaskManager, task_id: str, tmp_path: Path
    ) -> None:
        """ac-1, through the code that had the bug rather than through the fix alone."""
        claimed = manager.claim_task(task_id, agent="claude")
        commit_everything(repo)
        script = tmp_path / "agent.py"
        script.write_text("print('done')\n", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        dispatcher = self.dispatcher(repo, manager, home, [sys.executable, str(script), "{prompt}"])

        handle = dispatcher.start(claimed, actor="Jeff Posey", caused_by=1)
        # Stand in for the agent's own handoff, which the fake one cannot make.
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        assert handle.supervisor is not None
        handle.supervisor.join(timeout=60)
        assert not handle.supervisor.is_alive()

        assert porcelain(repo) == [], "the dispatcher left its own write uncommitted"
        assert subjects(repo)[0].startswith(f"chore({task_id}): record run ")
        meta = handle.directory.read_meta()
        assert "committed" in str(meta.get("record_commit"))

    def test_a_run_that_could_not_start_still_leaves_the_tree_clean(
        self, repo: Path, manager: TaskManager, task_id: str, tmp_path: Path
    ) -> None:
        """No session ever existed, so this commit carries the dispatch entry too."""
        claimed = manager.claim_task(task_id, agent="claude")
        commit_everything(repo)
        home = tmp_path / "home"
        home.mkdir()
        dispatcher = self.dispatcher(
            repo, manager, home, [str(tmp_path / "not-a-program"), "{prompt}"]
        )

        with pytest.raises(DispatchRunError):
            dispatcher.start(claimed, actor="Jeff Posey", caused_by=1)

        assert porcelain(repo) == []
        assert "crashed before it started" in subjects(repo)[0]
