"""One test per refusal path between an HTTP request and a running agent.

The load-bearing one is `TestHumanClockedRule`. Everything else here is a limit that
could reasonably be tuned; that rule is the reason an agent-starts-agent loop is not
representable, so it gets the case constructed explicitly rather than inferred.

Runs are started with a fake runner that exits immediately, because what is under test
is *whether* a run starts and what refuses it -- not what the run then does, which is
task-070's suite.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchDisabledError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    ProjectNotEnabledError,
)
from agentjobs.dispatch.guards import (
    CausingActorNotHumanError,
    ClaimLostError,
    ConcurrencyLimitError,
    DirtyTreeError,
    DispatchRequest,
    DispatchRefused,
    LiveRunExistsError,
    NoCausingEntryError,
    OwnerMismatchError,
    TaskClosedError,
    assert_human_clocked,
    dispatch_task,
    live_runs,
    resolve_causing_entry,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

PROJECT_CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
        {"name": "codex", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway AgentJobs home, so nothing here touches a real one."""
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


@pytest.fixture
def fake_runner(tmp_path: Path) -> Path:
    """A runner that exits immediately. What it does is not what these tests measure."""
    script = tmp_path / "runner.py"
    script.write_text("print('started')\n", encoding="utf-8")
    return script


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A registered project with a clean git tree and a configured actor vocabulary."""
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    # AgentJobs writes into the project root -- task files and its own write receipts --
    # so without this every test that creates a task dirties the very tree the
    # clean-tree gate inspects. A real project ignores the same paths; this mirrors the
    # repository's own .gitignore rather than inventing a convenience.
    (root / ".gitignore").write_text("tasks/\n.agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(TaskStorage(project.root / "tasks"))


def write_dispatch_config(
    home: Path,
    fake_runner: Path,
    *,
    enabled: bool = True,
    project_enabled: bool = True,
    **project_overrides: object,
) -> Path:
    """A machine-local dispatch config that permits 'sandbox' unless told otherwise.

    ``enabled`` is the master switch and ``project_enabled`` is the per-project gate.
    They are separate parameters because they are separate gates, and a test that
    conflated them would prove nothing about either.
    """
    entry = {"enabled": project_enabled, "runner": "fake", "require_clean_tree": True}
    entry.update(project_overrides)
    config = {
        "version": 1,
        "enabled": enabled,
        "runners": {
            "fake": {
                "argv": [sys.executable, str(fake_runner), "{prompt}"],
                # The runner is named for the invocation; `actor` is the identity it
                # writes as, and it must be one this project configures.
                "actor": "claude",
            }
        },
        "projects": {"sandbox": entry},
    }
    path = home / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def ready_task(manager: TaskManager):
    """A ready task whose newest log entry was written by a human."""
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    return manager.add_log_entry(
        task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go ahead."
    )


def run(manager, project, home, task_id, caused_by: Optional[int] = None):
    """Call the guard chain the way the endpoint and the CLI both do."""
    return dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(task_id=task_id, caused_by=caused_by),
        home=home,
    )


def settle(handle) -> None:
    """Let a batch supervisor finish so it does not race the next assertion."""
    if handle.supervisor is not None:
        handle.supervisor.join(timeout=30)


# ----- the rule ---------------------------------------------------------------


class TestHumanClockedRule:
    def test_an_agent_authored_entry_cannot_cause_a_dispatch(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The case the whole design turns on, constructed exactly."""
        write_dispatch_config(home, fake_runner)
        manager.handoff(
            ready_task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )

        with pytest.raises(CausingActorNotHumanError) as caught:
            run(manager, project, home, ready_task.id)

        assert caught.value.reason == "not_human_clocked"
        assert "claude" in str(caught.value)
        assert live_runs(home) == []

    def test_a_human_authored_entry_may_cause_one(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_an_unconfigured_actor_is_refused_rather_than_assumed_human(self) -> None:
        """ "We do not know who this is" must not be able to start a process."""
        entry = _entry(actor="somebody-new")

        with pytest.raises(CausingActorNotHumanError):
            assert_human_clocked(PROJECT_CONFIG, entry)

    def test_the_rule_reads_the_named_entry_not_just_the_newest(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """caused_by is validated, not trusted: naming an agent's entry still refuses."""
        write_dispatch_config(home, fake_runner)
        agent_entry = manager.add_log_entry(
            ready_task.id, actor="claude", type=LogEntryType.PROGRESS, body="Worked on it."
        ).log[-1]
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Fine by me."
        )

        with pytest.raises(CausingActorNotHumanError):
            run(manager, project, home, ready_task.id, caused_by=agent_entry.id)

    def test_a_task_with_no_log_cannot_be_dispatched(self, manager: TaskManager) -> None:
        task = manager.create_task(title="Bare", category="general", summary="s", description="d")
        task.log.clear()

        with pytest.raises(NoCausingEntryError):
            resolve_causing_entry(task)

    def test_naming_an_entry_that_does_not_exist_is_refused(self, ready_task) -> None:
        with pytest.raises(NoCausingEntryError) as caught:
            resolve_causing_entry(ready_task, caused_by=9999)
        assert "9999" in str(caught.value)


def _entry(*, actor: str):
    """One log entry, for the rule tests that do not need a whole task."""
    from agentjobs.models_v2 import LogEntry, utcnow

    return LogEntry(id=1, ts=utcnow(), actor=actor, type=LogEntryType.NOTE, body="x")


# ----- every other gate, each with its own code -------------------------------


class TestConfigGates:
    def test_no_config_at_all_is_refused_by_name(
        self, manager: TaskManager, project: Project, home: Path, ready_task
    ) -> None:
        with pytest.raises(DispatchNotConfiguredError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "not_configured"

    def test_master_switch_off(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, enabled=False)

        with pytest.raises(DispatchDisabledError):
            run(manager, project, home, ready_task.id)

    def test_project_not_enabled(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, project_enabled=False)

        with pytest.raises(ProjectNotEnabledError):
            run(manager, project, home, ready_task.id)

    def test_the_sentinel_refuses_after_everything_else_passed(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")

        with pytest.raises(DispatchSentinelError):
            run(manager, project, home, ready_task.id)
        assert live_runs(home) == []


class TestTaskStateGates:
    def test_a_closed_task_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)
        manager.close_task(ready_task.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        with pytest.raises(TaskClosedError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "task_closed"

    def test_a_task_owned_by_another_agent_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)
        manager.claim_task(ready_task.id, agent="codex")
        manager.add_log_entry(ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")

        with pytest.raises(OwnerMismatchError):
            run(manager, project, home, ready_task.id)

    def test_a_missing_task_is_refused_without_starting_anything(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path
    ) -> None:
        write_dispatch_config(home, fake_runner)

        with pytest.raises(DispatchRefused):
            run(manager, project, home, "task-does-not-exist")


class TestWorkingTree:
    def test_a_dirty_tree_refuses_by_default(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)
        (project.root / "in-flight.txt").write_text("mid-edit", encoding="utf-8")

        with pytest.raises(DirtyTreeError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "dirty_tree"
        assert live_runs(home) == []

    def test_a_project_may_opt_out(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        (project.root / "in-flight.txt").write_text("mid-edit", encoding="utf-8")

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id

    def test_git_head_is_recorded_on_the_dispatch_entry(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """So the diff attributable to a run is recoverable afterwards."""
        write_dispatch_config(home, fake_runner)
        expected = subprocess.run(
            ["git", "-C", str(project.root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        task = manager.get_task(ready_task.id)
        assert task is not None
        dispatched = [e for e in task.log if e.type is LogEntryType.DISPATCH][0]
        assert dispatched.data["git_head"] == expected
        assert dispatched.actor == "Jeff Posey"


class TestConcurrency:
    def test_a_second_run_for_the_same_task_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        first = run(manager, project, home, ready_task.id)
        # Freeze it as live rather than letting the supervisor finish, because what is
        # under test is the guard, not the run.
        first.directory.update_meta(status="running")

        with pytest.raises(LiveRunExistsError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "live_run_exists"
        settle(first)

    def test_the_machine_limit_refuses_and_does_not_enqueue(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """A queue would turn a click into a promise to spend money unattended."""
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        other = manager.create_task(
            title="Other",
            category="general",
            summary="s",
            description="d",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        manager.add_log_entry(other.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")

        first = run(manager, project, home, ready_task.id)
        first.directory.update_meta(status="running")

        with pytest.raises(ConcurrencyLimitError) as caught:
            run(manager, project, home, other.id)

        assert caught.value.reason == "concurrency_limit"
        assert len(live_runs(home)) == 1, "a refused dispatch must not leave a run behind"
        settle(first)

    def test_a_concurrent_double_dispatch_starts_exactly_one_process(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Claim-before-spawn: the loser pays for a rejected request, not a model call."""
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        results: List[object] = []
        barrier = threading.Barrier(2)

        def attempt() -> None:
            barrier.wait()
            try:
                results.append(run(manager, project, home, ready_task.id))
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                results.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        handles = [r for r in results if not isinstance(r, Exception)]
        refusals = [r for r in results if isinstance(r, Exception)]
        assert len(handles) == 1, f"expected one run, got {results}"
        assert len(refusals) == 1
        assert isinstance(refusals[0], (ClaimLostError, LiveRunExistsError, ConcurrencyLimitError))
        for handle in handles:
            settle(handle)

    def test_a_terminal_run_does_not_count_as_live(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        first = run(manager, project, home, ready_task.id)
        settle(first)
        first.directory.update_meta(status="finished")

        assert live_runs(home) == []

    def test_an_unreadable_run_counts_as_live(self, home: Path) -> None:
        """It cannot be shown to have ended, and assuming it did lets a second start."""
        directory = home / "runs" / "run_broken"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text("{{{ not yaml", encoding="utf-8")

        assert [run.run_id for run in live_runs(home)] == ["run_broken"]


class TestClaimBeforeSpawn:
    def test_a_ready_task_is_claimed_before_anything_starts(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        task = manager.get_task(ready_task.id)
        assert task is not None
        assert task.lifecycle is Lifecycle.ACTIVE
        # Claimed as the runner's *actor*, not its name. The runner is called "fake";
        # the identity it acts as is "claude", which this project configures.
        assert task.assignment.owner == "claude"
        types = [e.type for e in task.log]
        assert types.index(LogEntryType.TRANSITION) < types.index(LogEntryType.DISPATCH)

    def test_an_already_active_task_owned_by_the_runner_is_not_reclaimed(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        manager.claim_task(ready_task.id, agent="claude")
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Again please."
        )
        before = len(manager.get_task(ready_task.id).log)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        task = manager.get_task(ready_task.id)
        assert task is not None
        # One dispatch entry and one dispatch_result; no second claim transition.
        added = [e.type for e in task.log[before:]]
        assert LogEntryType.TRANSITION not in added
