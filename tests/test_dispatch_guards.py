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
from typing import Dict, List, Optional

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchDisabledError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    ProjectNotEnabledError,
)
from agentjobs.dispatch.guards import (
    AuthorizerNotHumanError,
    CausingActorNotHumanError,
    ClaimLostError,
    ConflictingAuthorizationError,
    ConcurrencyLimitError,
    DirtyTreeError,
    DispatchRequest,
    DispatchRefused,
    LiveRun,
    LiveRunExistsError,
    NoCausingEntryError,
    OwnerMismatchError,
    RecordCannotBriefError,
    TaskClosedError,
    assert_human_clocked,
    describe_slot_holders,
    dispatch_task,
    live_runs,
    record_can_brief,
    resolve_causing_entry,
)
from agentjobs.dispatch.runner import DispatchRunner
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

PROJECT_CONFIG: dict[str, object] = {
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
    # Only .agentjobs/ is ignored, exactly as `agentjobs init` leaves a project: task
    # YAML is meant to be committed, and this repository commits its own. That makes the
    # tasks directory part of the tree the clean-tree gate inspects, which is the shape
    # task-182 was about -- dispatch dirties that directory itself, at both ends of a run.
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
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
    limits: Optional[Dict[str, object]] = None,
    **project_overrides: object,
) -> Path:
    """A machine-local dispatch config that permits 'sandbox' unless told otherwise.

    ``enabled`` is the master switch and ``project_enabled`` is the per-project gate.
    They are separate parameters because they are separate gates, and a test that
    conflated them would prove nothing about either.

    ``limits`` writes the machine-wide caps. It exists so a test can raise
    ``max_concurrent_runs`` above 1 and check what still refuses at the higher ceiling
    -- the per-task lock, which is a different guarantee and must not move with it.
    """
    entry = {"enabled": project_enabled, "runner": "fake", "require_clean_tree": True}
    entry.update(project_overrides)
    config: Dict[str, object] = {
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
    if limits is not None:
        config["limits"] = limits
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


def _porcelain(project: Project) -> list[str]:
    """What `git status --porcelain` says about a project, as bare paths."""
    result = subprocess.run(
        ["git", "-C", str(project.root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def settle(handle) -> None:
    """Let a batch supervisor finish so it does not race the next assertion."""
    if handle.supervisor is not None:
        handle.supervisor.join(timeout=30)


def hold_live(handle) -> None:
    """Make a finished run read as live, and keep reading that way.

    Writing ``status="running"`` on its own is a race, not a fact: the fake runner exits
    at once and its supervisor is still on its way to writing a terminal status, so the
    freeze survives only if the next assertion arrives first. That is fine for a test
    that refuses immediately and wrong for one that starts a second run in between --
    which is how the raised-ceiling test failed the first time it ran.

    Joining the supervisor before freezing removes the writer instead of outrunning it.
    """
    settle(handle)
    handle.directory.update_meta(status="running")


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

    def test_the_dispatchers_own_entry_is_refused_as_an_agents(self) -> None:
        """task-153. `dispatcher` is reserved, not configured, and must not read unknown.

        It is the id AgentJobs writes its own `dispatch` and `dispatch_result` entries
        as, so this is the branch every *re*-dispatch takes. Reporting the most
        predictable actor in the system as an unrecognised stranger is wrong on its own;
        what the stranger branch then advises is why it matters.
        """
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(PROJECT_CONFIG, _entry(actor="dispatcher"))

        message = str(caught.value)
        assert "an agent" in message
        assert "Act on the task yourself" in message

    def test_the_refusal_never_advises_configuring_the_dispatcher_as_human(self) -> None:
        """sc-3. The stranger branch's remedy is a working recipe for disabling the rule.

        Adding `dispatcher: {kind: human}` to a project's actors would clock every
        future dispatcher-written entry as a human act, and dispatch could then drive
        itself with no human in the loop. An unsupervised agent applies a
        machine-readable remedy mechanically, so this text is the exposure.
        """
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(PROJECT_CONFIG, _entry(actor="dispatcher"))

        message = str(caught.value)
        assert "kind: human" not in message
        assert "actors:" not in message
        assert "config.yaml" not in message

    def test_a_genuinely_unknown_actor_keeps_the_advice_that_suits_one(self) -> None:
        """sc-2. Naming an unconfigured *person* is a config problem, and says so."""
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(PROJECT_CONFIG, _entry(actor="somebody-new"))

        message = str(caught.value)
        assert "does not configure as an actor" in message
        assert "'kind: human'" in message

    def test_configuring_the_dispatcher_as_human_does_not_re_arm_the_rule(self) -> None:
        """The docstring says "not a configuration option"; this is what makes it true.

        Reserved ids are resolved before the project's vocabulary, so a project that
        writes the recipe into its own config by hand -- or on the strength of the old
        message -- still gets the reserved agent and is still refused.
        """
        config = dict(PROJECT_CONFIG)
        config["actors"] = [
            *PROJECT_CONFIG["actors"],  # type: ignore[misc]
            {"name": "dispatcher", "kind": "human"},
        ]

        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(config, _entry(actor="dispatcher"))
        assert "an agent" in str(caught.value)

    def test_a_second_dispatch_is_refused_and_a_human_entry_re_arms_it(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """sc-5, end to end: the rule is unchanged, only which branch reports it.

        Dispatch, let the run finish, dispatch again. The newest entry is by then the
        dispatcher's own `dispatch_result`, so the second dispatch is refused -- which
        is correct and stays. Any human-written entry re-arms it.
        """
        write_dispatch_config(home, fake_runner)
        settle(run(manager, project, home, ready_task.id))
        after_run = manager.get_task(ready_task.id)
        assert after_run is not None
        assert after_run.log[-1].actor == "dispatcher", "the run should have left its own entry"

        with pytest.raises(CausingActorNotHumanError) as caught:
            run(manager, project, home, ready_task.id)
        message = str(caught.value)
        assert "dispatcher" in message
        assert "kind: human" not in message
        assert live_runs(home) == []

        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go again."
        )
        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

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


class TestTaskRecordsInsideTheDispatchedRepo:
    """task-182: dispatch dirties the tree it inspects, at both ends of a run.

    This project keeps its task YAML in the repository being dispatched, which is the
    default `agentjobs init` leaves behind. Two of AgentJobs' own writes land there:
    the claim, before the spawn, and the terminal `dispatch_result` entry, after the
    run's last commit. Counting either as dirt refused every dispatch on the strength of
    a file AgentJobs wrote itself.

    What must survive: a human's genuinely uncommitted work still refuses. The tests
    below pin the two apart.
    """

    @staticmethod
    def commit_tasks(project: Project) -> None:
        """Commit the tasks directory, so its files are tracked as in a real project."""
        subprocess.run(
            ["git", "-C", str(project.root), "add", "tasks"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(project.root), "commit", "-m", "tasks"],
            capture_output=True,
            check=True,
        )

    def test_a_task_file_dispatch_already_modified_does_not_refuse(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The resting state after any previous run: one modified, tracked task file.

        The dispatcher appends `dispatch_result` once the run is over, which is after
        the agent's last commit however well-behaved it was. Nobody commits that but a
        human, so the next dispatch meets it.
        """
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Left uncommitted."
        )
        assert _porcelain(project) == [f"tasks/{ready_task.id}.yaml"]

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id

    def test_a_task_file_the_claim_creates_does_not_refuse(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The front half: an untracked task record, written before this very dispatch."""
        write_dispatch_config(home, fake_runner)
        assert _porcelain(project) == ["tasks/"]

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id

    def test_a_completed_run_does_not_block_the_next_dispatch(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """End to end, which is how this was found: dispatch, let it finish, dispatch again."""
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        settle(run(manager, project, home, ready_task.id))

        second = manager.create_task(
            title="Next in line",
            category="general",
            summary="Another task to dispatch.",
            description="Do the next thing.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        manager.add_log_entry(second.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
        assert _porcelain(project), "the finished run should have left the tasks directory dirty"

        handle = run(manager, project, home, second.id)
        settle(handle)

        assert handle.run_id

    def test_uncommitted_work_outside_the_tasks_directory_still_refuses(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The protection the exclusion must not give away, alongside dirt that is ignored."""
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Left uncommitted."
        )
        (project.root / "README.md").write_text("someone is mid-edit\n", encoding="utf-8")

        with pytest.raises(DirtyTreeError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "dirty_tree"
        assert live_runs(home) == []

    def test_the_refusal_names_the_files_that_caused_it(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Otherwise `git status` disagrees with the refusal and neither explains the other.

        The tasks directory is dirty here too, and must not appear: a reader told to look
        for uncommitted work needs the paths that are actually blocking them.
        """
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Left uncommitted."
        )
        (project.root / "README.md").write_text("someone is mid-edit\n", encoding="utf-8")

        with pytest.raises(DirtyTreeError) as caught:
            run(manager, project, home, ready_task.id)
        message = str(caught.value)
        assert "README.md" in message
        assert ready_task.id not in message


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

        # A count is unactionable: the task page's run list shows only that task's runs,
        # so the run holding the slot is by definition one this page cannot show. The
        # refusal has to name it, and name the task it is working.
        message = str(caught.value)
        assert first.run_id in message, message
        assert ready_task.id in message, message
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

    def test_the_per_task_lock_still_refuses_at_a_raised_ceiling(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The machine ceiling and the per-task lock are different guarantees.

        Raising ``max_concurrent_runs`` is a throughput decision about how much this
        machine may spend at once. "One live run per task" is a correctness decision
        about not putting two agents on one branch with one task record. This test
        exists because the two are easy to conflate, and conflating them is how the
        second becomes collateral damage of the first: with the ceiling at four, three
        slots are free, and nothing but the per-task rule stands between a second
        dispatch and a second agent on the same task.
        """
        write_dispatch_config(
            home, fake_runner, require_clean_tree=False, limits={"max_concurrent_runs": 4}
        )
        # Named explicitly because holding the first run live settles its supervisor,
        # which appends an agent-authored dispatch_result. Defaulting to the newest entry
        # would then be refused as not-human-clocked -- a true refusal, and not the one
        # under test.
        authorising = ready_task.log[-1].id

        first = run(manager, project, home, ready_task.id, caused_by=authorising)
        hold_live(first)

        assert len(live_runs(home)) == 1, "three of the four slots are free"

        with pytest.raises(LiveRunExistsError) as caught:
            run(manager, project, home, ready_task.id, caused_by=authorising)

        assert caught.value.reason == "live_run_exists"
        assert first.run_id in str(caught.value)
        assert len(live_runs(home)) == 1, "the refusal must not have started anything"

    def test_a_raised_ceiling_admits_a_second_task_and_then_refuses(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Two runs fit under a ceiling of two; the third is refused, naming both."""
        write_dispatch_config(
            home, fake_runner, require_clean_tree=False, limits={"max_concurrent_runs": 2}
        )
        others = []
        for title in ("Second", "Third"):
            task = manager.create_task(
                title=title,
                category="general",
                summary="s",
                description="d",
                lifecycle=Lifecycle.READY,
                actor="Jeff Posey",
            )
            manager.add_log_entry(task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
            others.append(task.id)

        first = run(manager, project, home, ready_task.id)
        hold_live(first)
        second = run(manager, project, home, others[0])
        hold_live(second)

        assert len(live_runs(home)) == 2, "both slots are legitimately in use"

        with pytest.raises(ConcurrencyLimitError) as caught:
            run(manager, project, home, others[1])

        message = str(caught.value)
        assert "allows 2 concurrent" in message, message
        for run_id, task_id in ((first.run_id, ready_task.id), (second.run_id, others[0])):
            assert run_id in message, message
            assert task_id in message, message

    def test_a_refusal_summarises_rather_than_listing_every_holder(self) -> None:
        """A ceiling raised high enough to hold twenty runs must not print twenty."""
        holders = [
            LiveRun(
                run_id=f"run_{index:04d}",
                task_id=f"task-{index:03d}",
                project_id="sandbox",
                status="running",
                path=Path("."),
            )
            for index in range(9)
        ]
        described = describe_slot_holders(holders)

        assert "run_0000 on sandbox/task-000 (running)" in described
        assert "run_0005" in described, "the sixth is the last one named"
        assert "run_0006" not in described, "the seventh is summarised, not named"
        assert "and 3 more" in described

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
        claimed = manager.get_task(ready_task.id)
        assert claimed is not None
        before = len(claimed.log)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        task = manager.get_task(ready_task.id)
        assert task is not None
        # One dispatch entry and one dispatch_result; no second claim transition.
        added = [e.type for e in task.log[before:]]
        assert LogEntryType.TRANSITION not in added


# ----- one click: the button writes the authorising entry (task-188) -----------


def run_as(
    manager,
    project,
    home,
    task_id,
    *,
    user: Optional[str],
    note: Optional[str] = None,
    surface: Optional[str] = "the task page",
):
    """Call the guard chain the way the React app's Dispatch button does."""
    return dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(
            task_id=task_id,
            authorized_by=user,
            authorization_note=note,
            surface=surface,
        ),
        home=home,
    )


@pytest.fixture
def agent_filed_task(manager: TaskManager):
    """The shape 68 of this project's 74 open tasks were in on 2026-08-20.

    A complete spec, filed by an agent, whose newest entry is therefore an agent's
    `transition`. Before task-188 this was refused, which made the refusal the default
    state of the backlog rather than the exception.
    """
    return manager.create_task(
        title="Filed by an agent",
        category="general",
        summary="A task an agent filed.",
        description="Do the thing, in detail.",
        lifecycle=Lifecycle.READY,
        actor="claude",
    )


class TestAuthorizingEntryIsWritten:
    def test_a_complete_agent_filed_task_dispatches_with_no_note_written_by_hand(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        """ac-1. The case that used to be 97% of the backlog and refused every time."""
        write_dispatch_config(home, fake_runner)
        before = manager.get_task(agent_filed_task.id)
        assert before is not None
        assert before.log[-1].actor == "claude"

        handle = run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_the_causing_entry_is_a_real_stored_entry_by_the_named_human(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        """ac-2. Not a synthesised justification: a row on disk, resolvable by id."""
        write_dispatch_config(home, fake_runner)

        handle = run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")
        settle(handle)

        # Re-read through a fresh manager rather than trusting the handle, because "it
        # is on disk" is precisely the property under test.
        stored = TaskManager(TaskStorage(project.root / "tasks")).get_task(agent_filed_task.id)
        assert stored is not None
        caused_by = handle.directory.read_meta()["caused_by"]
        entry = resolve_causing_entry(stored, caused_by)
        assert entry.actor == "Jeff Posey"
        assert entry.type is LogEntryType.NOTE
        assert entry.body is not None
        assert "Jeff Posey" in entry.body
        assert "the task page" in entry.body

    def test_the_dispatch_reads_its_evidence_from_storage_not_from_the_request(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ac-2, the half that matters.

        The forgeability requirement is that the causing entry is resolved *from the
        stored task*. Make storage stop handing the written entry back and the dispatch
        must fail -- there is nowhere else for it to get one. An implementation that
        quietly started trusting the request body would sail past this.
        """
        write_dispatch_config(home, fake_runner)
        original = TaskManager.get_task
        reads: List[str] = []

        def forgetful(self, task_id, *args, **kwargs):
            task = original(self, task_id, *args, **kwargs)
            reads.append(task_id)
            if task is not None and len(reads) > 1:
                # The post-write read the authorising path depends on comes back empty.
                task.log = []
            return task

        monkeypatch.setattr(TaskManager, "get_task", forgetful)

        with pytest.raises((DispatchRefused, IndexError)):
            run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")

        assert live_runs(home) == []

    def test_an_agent_cannot_authorize_a_dispatch(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        """ac-3. The rule survives the change: a name is not enough, the kind decides."""
        write_dispatch_config(home, fake_runner)

        with pytest.raises(AuthorizerNotHumanError) as caught:
            run_as(manager, project, home, agent_filed_task.id, user="claude")

        assert caught.value.reason == "authorizer_not_human"
        assert live_runs(home) == []
        # And nothing was written. A refused authorisation must not leave a row behind
        # in a log that is never rewritten.
        stored = manager.get_task(agent_filed_task.id)
        assert stored is not None
        assert not [e for e in stored.log if e.type is LogEntryType.NOTE]

    def test_an_unconfigured_authorizer_is_refused_rather_than_assumed_human(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        write_dispatch_config(home, fake_runner)

        with pytest.raises(AuthorizerNotHumanError):
            run_as(manager, project, home, agent_filed_task.id, user="somebody-new")

        assert live_runs(home) == []

    def test_naming_an_entry_and_writing_one_are_mutually_exclusive(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
    ) -> None:
        write_dispatch_config(home, fake_runner)

        with pytest.raises(ConflictingAuthorizationError):
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=ready_task.id, caused_by=1, authorized_by="Jeff Posey"
                ),
                home=home,
            )

    def test_no_authorizing_user_falls_back_to_the_stored_rule(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        """ac-6. A dispatch nobody signed for is refused, not signed for by the server.

        The CLI's behaviour, and what the browser would get if no human were configured.
        The alternative -- quietly attributing it to the project's `default_user` --
        would put a person's name on a run they did not ask for.
        """
        write_dispatch_config(home, fake_runner)

        with pytest.raises(CausingActorNotHumanError) as caught:
            run_as(manager, project, home, agent_filed_task.id, user=None)

        assert caught.value.reason == "not_human_clocked"
        assert live_runs(home) == []

    def test_a_refusal_after_the_early_gates_leaves_no_authorization_behind(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        """A dirty tree refuses, and the record is untouched.

        The entry is written inside the run lock, after every refusal that can be judged
        without writing. A task must not accumulate authorisations for runs that never
        started.
        """
        write_dispatch_config(home, fake_runner)
        (project.root / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        before = manager.get_task(agent_filed_task.id)
        assert before is not None

        with pytest.raises(DirtyTreeError):
            run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")

        after = manager.get_task(agent_filed_task.id)
        assert after is not None
        assert len(after.log) == len(before.log)

    def test_a_spawn_that_fails_leaves_an_authorisation_not_a_claimed_dispatch(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The composed sentence has to be true when the spawn does not happen.

        The entry is written inside the run lock and before the claim, on purpose --
        that ordering is what makes it evidence resolved from storage rather than a
        justification carried on the request. The consequence is that `runner.start`
        can still fail after it lands, and an append-only log cannot take it back. So
        the sentence describes the human's act and not the machine's outcome: it says
        they *authorised* a dispatch, which stays true, rather than that one *happened*,
        which would be the only untrue thing this feature could write into a record.
        """
        write_dispatch_config(home, fake_runner)

        def refuse_to_spawn(self, *args, **kwargs):
            raise RuntimeError("the runner could not be spawned")

        monkeypatch.setattr(DispatchRunner, "start", refuse_to_spawn)

        with pytest.raises(RuntimeError):
            run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")

        stored = manager.get_task(agent_filed_task.id)
        assert stored is not None
        notes = [e for e in stored.log if e.type is LogEntryType.NOTE]
        assert len(notes) == 1
        body = notes[0].body or ""
        assert body.startswith("Jeff Posey authorised a dispatch of this task")
        # And it does not claim a run happened, because none did.
        assert "Dispatched by" not in body
        assert [e for e in stored.log if e.type is LogEntryType.DISPATCH] == []
        assert live_runs(home) == []


class TestSufficiency:
    def test_a_description_is_enough_and_never_asks_for_text(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        agent_filed_task,
    ) -> None:
        write_dispatch_config(home, fake_runner)
        stored = manager.get_task(agent_filed_task.id)
        assert stored is not None
        assert record_can_brief(stored) is True

        handle = run_as(manager, project, home, agent_filed_task.id, user="Jeff Posey")
        settle(handle)
        assert handle.run_id.startswith("run_")

    def test_an_empty_description_stops_to_ask(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path
    ) -> None:
        """ac-4, the trigger. Keyed on the spec, not on ball_prompt."""
        write_dispatch_config(home, fake_runner)
        task = manager.create_task(
            title="Nothing to go on",
            category="general",
            summary="A task with no working spec.",
            description="   ",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )

        with pytest.raises(RecordCannotBriefError) as caught:
            run_as(manager, project, home, task.id, user="Jeff Posey")

        assert caught.value.reason == "insufficient_record"
        assert live_runs(home) == []

    def test_the_typed_text_becomes_the_authorizing_entry(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path
    ) -> None:
        """ac-4, the rest. One action serves both purposes."""
        write_dispatch_config(home, fake_runner)
        task = manager.create_task(
            title="Nothing to go on",
            category="general",
            summary="A task with no working spec.",
            description="",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )

        handle = run_as(
            manager,
            project,
            home,
            task.id,
            user="Jeff Posey",
            note="Port the widget to v2.",
        )
        settle(handle)

        stored = manager.get_task(task.id)
        assert stored is not None
        entry = resolve_causing_entry(stored, handle.directory.read_meta()["caused_by"])
        assert entry.actor == "Jeff Posey"
        assert entry.body == "Port the widget to v2."

    def test_an_empty_ball_prompt_does_not_trigger_the_ask(self, manager: TaskManager) -> None:
        """The rejected alternative, pinned.

        Every `ready` task has an empty `ball_prompt` and that is correct -- it is in the
        pool, not handed to anyone. A check keyed on it would fire on all of them.
        """
        task = manager.create_task(
            title="In the pool",
            category="general",
            summary="Ready and unassigned.",
            description="A full working specification.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        stored = manager.get_task(task.id)
        assert stored is not None
        assert stored.ball_prompt is None
        assert record_can_brief(stored) is True

    def test_missing_acceptance_criteria_do_not_trigger_the_ask(self, manager: TaskManager) -> None:
        """The other rejected trigger, pinned. A grooming gap, not an authorisation one."""
        task = manager.create_task(
            title="No acceptance criteria",
            category="general",
            summary="Exploratory.",
            description="Find out whether the cache is the problem.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        stored = manager.get_task(task.id)
        assert stored is not None
        assert stored.acceptance == []
        assert record_can_brief(stored) is True
