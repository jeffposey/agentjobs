"""One test per refusal path between an HTTP request and a running agent.

The load-bearing one is `TestHumanClockedRule`. Everything else here is a limit that
could reasonably be tuned; that rule is what keeps an agent-starts-agent cycle out of
every supported path, so it gets the case constructed explicitly rather than inferred.
It is not what *bounds* such a loop -- the caps in `TestBudgetCapsBindEveryTrigger` are,
and `dispatch/budget.py` says why.

Runs are started with a fake runner that exits immediately, because what is under test
is *whether* a run starts and what refuses it -- not what the run then does, which is
task-070's suite.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from datetime import timedelta
from typing import Dict, List, Optional

import pytest
from pydantic import ValidationError
import yaml

from agentjobs.dispatch.address import DEFAULT_API_BASE, ApiBaseProbe
from agentjobs.dispatch.config import (
    CONFIG_FILENAME,
    DispatchDisabledError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    Posture,
    ProjectNotEnabledError,
)
from agentjobs.dispatch.guards import (
    AuthorizerNotHumanError,
    CausingActorNotHumanError,
    ClaimLostError,
    ConflictingAuthorizationError,
    BudgetCapError,
    ConcurrencyLimitError,
    DirtyTreeError,
    DispatchRequest,
    DispatchRefused,
    LiveRun,
    LiveRunExistsError,
    NoCausingEntryError,
    OwnerMismatchError,
    RecordCannotBriefError,
    TaskBeingWorkedError,
    TaskClosedError,
    TaskOnHoldError,
    UnreachableApiBaseError,
    assert_human_clocked,
    describe_slot_holders,
    relayed_authorizer,
    dispatch_task,
    live_runs,
    record_can_brief,
    resolve_causing_entry,
)
from agentjobs.dispatch import journal as guards_journal
from agentjobs.dispatch.slots import release_slot_for_task
from agentjobs.dispatch.runner import META_FILENAME, DispatchRunner, RunHandle, runs_root
from agentjobs.execution.factory import execution_store_for
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchPosture,
    DispatchTrigger,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    utcnow,
)
from agentjobs.projects import Project
from support import task_store

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
    return TaskManager(task_store(project.root / "tasks"))


def write_dispatch_config(
    home: Path,
    fake_runner: Path,
    *,
    enabled: bool = True,
    project_enabled: bool = True,
    limits: Optional[Dict[str, object]] = None,
    api_base: Optional[str] = None,
    **project_overrides: object,
) -> Path:
    """A machine-local dispatch config that permits 'sandbox' unless told otherwise.

    ``enabled`` is the master switch and ``project_enabled`` is the per-project gate.
    They are separate parameters because they are separate gates, and a test that
    conflated them would prove nothing about either.

    ``limits`` writes the machine-wide caps. It exists so a test can raise
    ``max_concurrent_runs`` above 1 and check what still refuses at the higher ceiling
    -- the per-task lock, which is a different guarantee and must not move with it.

    ``api_base`` is a named parameter rather than one of ``project_overrides`` because
    it is machine-wide: the address is a property of where AgentJobs serves, not of any
    one project, and ``**project_overrides`` would file it under the project entry where
    nothing reads it.
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
    if api_base is not None:
        config["api_base"] = api_base
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


def run(manager, project, home, task_id, caused_by: Optional[int] = None, now=None):
    """Call the guard chain the way the endpoint and the CLI both do.

    ``now`` is the budget caps' clock and nothing else (task-334). A test that dispatches
    the same task twice in one process is inside the 60s cooldown by construction, and
    since the caps bind every trigger it has to say which moment it means rather than
    sleep through a real minute.
    """
    return dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(task_id=task_id, caused_by=caused_by),
        home=home,
        now=now,
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
    # The run really did conclude, and since task-264 the journal and a sticky terminal
    # meta both say so -- with its dispatch_result on the task, which is the evidence that
    # releases a journal attempt. So the frozen run is rebuilt as what these tests need: a
    # live run the journal has never heard of, which is exactly a run started before the
    # journal existed, and is counted by the same slot and ownership rules.
    meta = handle.directory.read_meta()
    meta.update(status="running", outcome=None, finished_at=None)
    handle.directory.write_meta(meta)
    store = execution_store_for(handle.directory.path.parent.parent)
    with store.transaction("test: hold live") as connection:
        connection.execute("DELETE FROM run_attempt WHERE run_id = ?", (handle.run_id,))


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
        # Past the cooldown, which binds this manual dispatch since task-334 and would
        # otherwise refuse the re-armed run for a reason this test is not about.
        handle = run(manager, project, home, ready_task.id, now=utcnow() + timedelta(minutes=5))
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


def _relay(*, actor: str, authorized_by: str):
    """One relayed authorisation entry, built by hand rather than through the manager.

    Built here so the rule can be asked about a payload the write path would refuse --
    an empty ``authorized_by``, or an agent named in it -- which is exactly where a
    fallback has to be shown to fail safe.
    """
    return LogEntry(
        id=1,
        ts=utcnow(),
        actor=actor,
        type=LogEntryType.AUTHORIZATION,
        body="Start this task.",
        data={"authorized_by": authorized_by},
    )


class TestARelayedAuthorizationClocksADispatch:
    """The rule judges an ``authorization`` entry on the human it names (task-506).

    Every other entry type collapses "who wrote this" and "whose act is this", and the
    rule has always answered the second by reading the first. This type separates them:
    the agent typed it, the person authorised it, and the entry says both. So the tests
    here are about what the *named* id buys and, more importantly, what it does not.
    """

    def test_it_clocks_a_dispatch_although_an_agent_wrote_it(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """ac-1's end: the newest entry is an agent's, and the dispatch proceeds.

        Constructed as the opposite of ``test_an_agent_authored_entry_cannot_cause_a_dispatch``
        above, and the difference is one field. That test's entry is an agent's handoff and
        is refused; this one is an agent's record of a person's instruction and is not.
        """
        write_dispatch_config(home, fake_runner)
        manager.record_relayed_authorization(
            ready_task.id,
            actor="claude",
            authorized_by="Jeff Posey",
            ask="File this and start it.",
            surface="an interactive chat session",
        )
        relayed = manager.get_task(ready_task.id)
        assert relayed is not None and relayed.log[-1].actor == "claude"

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_the_dispatch_is_attributed_to_the_human_not_the_relaying_agent(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Whose purchase this was, on the record that outlives the run directory.

        ``assert_human_clocked`` answers with the authoriser, and that is the actor the
        ``dispatch`` entry carries. A relay that clocked the run and then recorded the
        agent as having authorised it would put the loop back in the log's own account of
        itself, whatever the code did.
        """
        write_dispatch_config(home, fake_runner)
        relayed = manager.record_relayed_authorization(
            ready_task.id, actor="claude", authorized_by="Jeff Posey", ask="Start it."
        ).log[-1]

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        stored = manager.get_task(ready_task.id)
        assert stored is not None
        entry = next(item for item in reversed(stored.log) if item.type is LogEntryType.DISPATCH)
        assert entry.actor == "Jeff Posey"
        assert entry.data["caused_by"] == relayed.id
        assert entry.data["trigger"] == "manual"

    def test_it_counts_as_one_manual_dispatch_against_the_hourly_cap(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """ac-5. A relay buys a run; it does not buy an exemption.

        Section 7's hourly cap is what actually bounds a dispatch loop -- the human-clocked
        rule keeps the loop out of the supported paths and does not bound it. So a relay
        that arrived as anything the caps do not count would be a way round the only thing
        that does. It arrives as ``manual``, which is the trigger the caps were extended to
        cover in task-334.
        """
        write_dispatch_config(
            home,
            fake_runner,
            require_clean_tree=False,
            # Two slots, so the second dispatch is refused by the cap and not by a full
            # machine. The ceiling refuses first and would make this test pass for a
            # reason it does not claim.
            limits={"dispatches_per_hour": 1, "max_concurrent_runs": 2},
        )
        other = manager.create_task(
            title="Other",
            category="general",
            summary="s",
            description="d",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        manager.record_relayed_authorization(
            ready_task.id, actor="claude", authorized_by="Jeff Posey", ask="Start this one."
        )
        manager.record_relayed_authorization(
            other.id, actor="claude", authorized_by="Jeff Posey", ask="And this one."
        )

        first = run(manager, project, home, ready_task.id)
        hold_live(first)

        with pytest.raises(BudgetCapError) as caught:
            run(manager, project, home, other.id)

        assert caught.value.reason == "machine_per_hour"

    def test_it_is_not_consumed_by_the_dispatch_it_clocked(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The re-use semantics, stated rather than inherited by accident.

        A human's own note has never been single-use, and neither is this: nothing marks
        an entry as spent, and what bounds how many runs one authorisation yields is the
        caps. Asserted because "exactly a human entry's semantics, no new looseness" is a
        claim about this type and a claim is worth a test -- the refusal below names the
        live run, not the authorisation, which is what shows the entry is still good.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        relayed = manager.record_relayed_authorization(
            ready_task.id, actor="claude", authorized_by="Jeff Posey", ask="Start it."
        ).log[-1]
        first = run(manager, project, home, ready_task.id)
        hold_live(first)

        # Named rather than defaulted, because by now the newest entry is the dispatcher's
        # own -- and what is being asserted is the *entry*, not the ordering.
        with pytest.raises(LiveRunExistsError):
            run(manager, project, home, ready_task.id, caused_by=relayed.id)

    def test_an_agent_named_as_the_authorizer_is_refused(self) -> None:
        """Relaying an authorisation does not change who may give one.

        The entry a relay writes is agent-authored *by design*, so the only thing standing
        between this feature and an agent authorising its own successor through the front
        door is that the id it names has to be a human. Refused in the same words as an
        agent-authored entry, because it is the same rule.
        """
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(PROJECT_CONFIG, _relay(actor="claude", authorized_by="codex"))

        message = str(caught.value)
        assert "relays an authorisation by 'codex'" in message
        assert "configures as an agent" in message

    def test_an_unconfigured_authorizer_is_refused_rather_than_assumed_human(self) -> None:
        """Same rule as an unconfigured author, and for the same reason: "we do not know
        who this is" must not be able to start a process on somebody's machine."""
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(
                PROJECT_CONFIG, _relay(actor="claude", authorized_by="somebody-new")
            )

        assert "does not configure as an actor" in str(caught.value)

    def test_an_authorization_entry_naming_nobody_cannot_be_built(self) -> None:
        """An entry of this type without an authoriser is unrepresentable, not refused.

        ``AuthorizationData`` is in ``LOG_PAYLOADS``, so the check runs on the entry rather
        than at a write path -- which means it also holds for a row hand-edited into a
        file or handed over by an importer, and the model refuses to load one. That is the
        reason the payload is typed at all.
        """
        with pytest.raises(ValidationError):
            _relay(actor="claude", authorized_by="")

    def test_a_relay_that_reached_the_store_anyway_is_judged_on_its_author(self) -> None:
        """The fallback under the model, exercised past it.

        Reachable only by constructing the object without validation, which is the shape
        of a row that arrived some other way. It degrades to "an agent wrote this" and is
        refused, which is the direction a fallback in this function has to fail.
        """
        entry = LogEntry.model_construct(
            id=1,
            ts=utcnow(),
            actor="claude",
            type=LogEntryType.AUTHORIZATION,
            body="Start this task.",
            data={},
            re=None,
            attachments=None,
        )

        assert relayed_authorizer(entry) is None
        with pytest.raises(CausingActorNotHumanError) as caught:
            assert_human_clocked(PROJECT_CONFIG, entry)
        assert "'claude'" in str(caught.value)

    def test_the_marker_is_read_only_off_an_authorization_entry(self) -> None:
        """``data`` on an ordinary entry buys nothing, which is why the type carries this.

        A run holds ``task.verb`` and can put any ``data`` it likes on a ``note``. If this
        function looked at the key rather than the type, that would be the whole gate
        defeated by a field -- so the note below, which says everything a relay says, is
        still refused as the agent entry it is.
        """
        entry = LogEntry(
            id=1,
            ts=utcnow(),
            actor="claude",
            type=LogEntryType.NOTE,
            body="Jeff said to start it.",
            data={"authorized_by": "Jeff Posey"},
        )

        assert relayed_authorizer(entry) is None
        with pytest.raises(CausingActorNotHumanError):
            assert_human_clocked(PROJECT_CONFIG, entry)


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

    def test_a_held_task_is_refused_and_the_condition_is_in_the_message(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """task-231: a hold the manual path ignored would be a hold in name only.

        Auto-dispatch skipping a held task is not enough on its own -- the Dispatch
        button sits on the same page as the Hold control, so the cheapest way to defeat
        a hold would be to click the thing next to it. The release condition is put in
        the refusal because the person who hits it is the person deciding whether to
        override it, and making them go and read the record first is friction with no
        safety in it.
        """
        write_dispatch_config(home, fake_runner)
        manager.claim_task(ready_task.id, agent="claude")
        manager.handoff(
            ready_task.id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.HOLD,
            ball_prompt="Wait for the dispatch fixes to land.",
        )

        with pytest.raises(TaskOnHoldError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "task_on_hold"
        assert "Wait for the dispatch fixes to land." in str(caught.value)
        assert live_runs(home) == []

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


class TestTaskRecordsNoLongerDirtyTheDispatchedRepo:
    """task-182, and what removing its cause left behind.

    This project kept its task YAML in the repository being dispatched, which is what
    ``agentjobs init`` used to leave behind. Two of AgentJobs' own writes landed there --
    the claim, before the spawn, and the terminal ``dispatch_result``, after the run's
    last commit -- so counting either as dirt refused every dispatch on the strength of
    a file AgentJobs wrote itself. The exclusion that fixed it cost real coverage: a
    genuine change under ``tasks/`` stopped being seen.

    A record is a row now (task-402). The four cases that pinned the exclusion apart from
    genuine dirt are gone with it, because none of them can be set up: the writes below
    leave the tree clean, so there is nothing to excuse. What is asserted here instead is
    the pair that still means something -- AgentJobs' own writes do not dirty the tree,
    and a person's uncommitted work still refuses -- plus, in
    ``tests/test_dispatch_on_sqlite.py``, that a real change under ``tasks/`` refuses
    again, which is the coverage task-182 had to give up.
    """

    @staticmethod
    def commit_tasks(project: Project) -> None:
        """Commit whatever is in the tasks directory, if anything is."""
        subprocess.run(
            ["git", "-C", str(project.root), "add", "tasks"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(project.root), "commit", "-m", "tasks"], capture_output=True
        )

    def test_the_writes_around_a_dispatch_leave_the_tree_clean(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The claim before the spawn and the terminal entry after it, both invisible."""
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Left unrecorded."
        )
        assert _porcelain(project) == []

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id
        assert _porcelain(project) == []

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
        assert _porcelain(project) == [], "the finished run must have left the tree clean"

        handle = run(manager, project, home, second.id)
        settle(handle)

        assert handle.run_id

    def test_uncommitted_work_still_refuses(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The protection the exclusion used to put at risk, now with nothing excused."""
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        (project.root / "README.md").write_text("someone is mid-edit\n", encoding="utf-8")

        with pytest.raises(DirtyTreeError) as caught:
            run(manager, project, home, ready_task.id)
        assert caught.value.reason == "dirty_tree"
        assert live_runs(home) == []

    def test_the_refusal_names_the_files_that_caused_it(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Otherwise `git status` disagrees with the refusal and neither explains the other."""
        write_dispatch_config(home, fake_runner)
        self.commit_tasks(project)
        (project.root / "README.md").write_text("someone is mid-edit\n", encoding="utf-8")

        with pytest.raises(DirtyTreeError) as caught:
            run(manager, project, home, ready_task.id)
        assert "README.md" in str(caught.value)


class TestAnAgentAgentJobsDidNotStart:
    """The hole task-179 was filed for: a claim with no run behind it.

    Every agent writes the same claim on the record. Only the ones AgentJobs started
    write anything else -- a run directory, a run lock, a journal row -- so for every
    other kind of agent the claim is the entire signal, and `TestConcurrency` above,
    which reads the ledger, cannot see them at all.

    What used to stop this was an accident and these tests are built to prove it is
    gone: in each one the newest log entry is a **human's**, so `assert_human_clocked`
    is satisfied and whatever refuses is refusing for its own reason. That was the
    original 2026-08-19 observation -- a spawn-session agent working task-177 was
    protected only by its own claim being the newest entry, and task-188 made a human
    entry the newest on every dispatch by design.
    """

    def worked_by_an_unseen_agent(self, manager: TaskManager, task_id: str):
        """Exactly what a spawn-session agent leaves on a task, and nothing else.

        `claim_task` is the real verb rather than a hand-written record, because what is
        under test is whether the guard reads the state a claim actually produces. The
        human note after it is the whole point: it is what `assert_human_clocked` reads,
        so a refusal here cannot be that rule in disguise.
        """
        manager.claim_task(task_id, agent="claude")
        return manager.add_log_entry(
            task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Any news on this?"
        )

    def concluded_run(self, home: Path, project: Project, task_id: str) -> Path:
        """A run AgentJobs started at this task and watched end.

        Written by hand rather than by dispatching once and letting it finish, so the two
        tests below differ in this directory and in nothing else. A real first dispatch
        would also move the ball, write a dispatch_result, and spend a budget window, and
        any of those could be what made the second one behave differently.
        """
        directory = runs_root(home) / "run_dead"
        directory.mkdir(parents=True)
        (directory / META_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "run_id": "run_dead",
                    "task_id": task_id,
                    "project_id": project.id,
                    "mode": "session",
                    "agent": "claude",
                    "status": "finished",
                    "outcome": "completed",
                }
            ),
            encoding="utf-8",
        )
        return directory

    def test_a_claim_with_no_run_behind_it_refuses(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """sc-1. The record is the only signal there is, and it says somebody is on it."""
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        self.worked_by_an_unseen_agent(manager, ready_task.id)
        # The gate above this one sees nothing, which is the premise rather than an
        # aside: were there a live run, `LiveRunExistsError` would refuse and this test
        # would pass while proving nothing.
        assert live_runs(home) == []

        with pytest.raises(TaskBeingWorkedError) as caught:
            run(manager, project, home, ready_task.id)

        assert caught.value.reason == "task_being_worked"
        # Who holds it, so the refusal is a destination rather than a fact.
        assert "claude" in str(caught.value)
        # And nothing was started on the way to refusing.
        assert live_runs(home) == []

    def test_the_refusal_is_not_the_human_clocked_rule_in_disguise(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """sc-1's second half, stated as its own assertion rather than left implied.

        This is the accident described in the task: before task-188, a second dispatch
        onto a worked task was refused because the newest entry was the agent's own
        claim. Naming the human who wrote the newest entry here makes the difference
        visible -- the entry that causes this dispatch is a person's, and it is still
        refused.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        newest = self.worked_by_an_unseen_agent(manager, ready_task.id)
        assert newest.log[-1].actor == "Jeff Posey"

        with pytest.raises(DispatchRefused) as caught:
            run(manager, project, home, ready_task.id, caused_by=newest.log[-1].id)

        assert caught.value.reason == "task_being_worked"
        assert not isinstance(caught.value, CausingActorNotHumanError)

    def test_a_claim_left_by_a_run_that_ended_still_dispatches(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """sc-3, and the reason this guard is a ledger question rather than a record one.

        Identical record to the test above -- active, owned by `claude`, ball with an
        agent for work. The single difference is a finished run directory, which is
        AgentJobs saying *I started an agent here and I watched it end*. That is the one
        thing that can account for a claim left behind by a process which no longer
        exists, and it is what `_claim_or_verify` was written to serve.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        self.worked_by_an_unseen_agent(manager, ready_task.id)
        self.concluded_run(home, project, ready_task.id)
        # Finished, so it is not the gate above that lets this through or stops it.
        assert live_runs(home) == []

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_a_concluded_run_of_a_different_task_does_not_account_for_this_claim(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The scan is per task, so a busy machine does not quietly unlock every task."""
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        self.worked_by_an_unseen_agent(manager, ready_task.id)
        self.concluded_run(home, project, "task-999-somebody-else")

        with pytest.raises(TaskBeingWorkedError):
            run(manager, project, home, ready_task.id)

    def test_a_run_that_cannot_be_shown_to_be_this_project_does_not_account_for_it(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """`strictly_same_task`, and why the looser form would be wrong here.

        A run record naming no project matches any project when the answer *refuses* a
        dispatch -- it cannot be shown to be somebody else's, and that direction cannot
        put two agents on one task. Here a match does the opposite and permits one, so
        the same looseness would let another project's ledger row unlock this task.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        self.worked_by_an_unseen_agent(manager, ready_task.id)
        directory = self.concluded_run(home, project, ready_task.id)
        meta = yaml.safe_load((directory / META_FILENAME).read_text(encoding="utf-8"))
        meta["project_id"] = ""
        (directory / META_FILENAME).write_text(yaml.safe_dump(meta), encoding="utf-8")

        with pytest.raises(TaskBeingWorkedError):
            run(manager, project, home, ready_task.id)

    def test_a_ready_task_nobody_has_claimed_is_untouched(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The ordinary case, pinned so the new gate cannot grow into it.

        `agent`/`available` and `agent`/`work` were indistinguishable to the expression
        this task replaced. They must not become indistinguishable again in the other
        direction: almost every dispatch on this machine is of a task in exactly this
        state, and refusing one would take the feature out rather than make it safe.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_an_active_task_whose_ball_is_not_work_is_untouched(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Four fields, not one. A task sent back for revision is not one being worked.

        The ball moving off `work` is a manager verb saying the seat is free, which is
        the thing no process death can say for itself -- so this is a state the guard has
        no business refusing, and the click that follows a *Request changes* is the
        commonest dispatch there is.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        manager.claim_task(ready_task.id, agent="claude")
        manager.handoff(
            ready_task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        sent_back = manager.handoff(
            ready_task.id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Two things to change.",
        )
        assert sent_back.lifecycle is Lifecycle.ACTIVE
        assert sent_back.assignment.owner == "claude"

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")


class TestConcurrency:
    def test_a_second_run_for_the_same_task_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        authorising = ready_task.log[-1].id
        first = run(manager, project, home, ready_task.id)
        # Freeze it as live rather than letting the supervisor finish, because what is
        # under test is the guard, not the run. `hold_live`, not a bare status write: a
        # concluded run's meta status is terminal and stays terminal (task-264).
        hold_live(first)

        with pytest.raises(LiveRunExistsError) as caught:
            run(manager, project, home, ready_task.id, caused_by=authorising)
        assert caught.value.reason == "live_run_exists"

    def test_a_refusal_during_the_spawn_names_the_run_already_admitted(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """task-444: the window between admission and a launched run names its run.

        A second walk was refused inside that window on 2026-09-13 and told the lock "has
        not named its run yet", so it could not tell a sibling's healthy child from
        nothing. The spawn is where the time goes, so that is where the second dispatch
        arrives here.
        """
        from agentjobs.dispatch.ledger import read_lock_holder, run_lock_path

        write_dispatch_config(home, fake_runner, require_clean_tree=False)
        authorising = ready_task.log[-1].id
        seen: Dict[str, object] = {}
        real_start = DispatchRunner.start

        def start(self, task, **kwargs):
            holder = read_lock_holder(run_lock_path(home, task.id, project_id=project.id))
            seen["holder"] = holder
            with pytest.raises(LiveRunExistsError) as caught:
                run(manager, project, home, task.id, caused_by=authorising)
            seen["refusal"] = str(caught.value)
            return real_start(self, task, **kwargs)

        monkeypatch.setattr(DispatchRunner, "start", start)
        handle = run(manager, project, home, ready_task.id, caused_by=authorising)
        settle(handle)

        holder = seen["holder"]
        assert holder is not None and holder.run_id == handle.run_id  # type: ignore[attr-defined]
        assert handle.run_id in str(seen["refusal"])
        assert "has not named its run yet" not in str(seen["refusal"])

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
        hold_live(first)

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

    def test_a_run_whose_task_closed_no_longer_stands_in_the_way(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """task-482, at the surface a person meets: the refusal above, then no refusal.

        The first run is still live -- its session is open and somebody may be typing
        into it -- and that is asserted at the end, because a fix that ended the run
        would let the second dispatch through for the wrong reason.
        """
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
        hold_live(first)
        with pytest.raises(ConcurrencyLimitError):
            run(manager, project, home, other.id)

        # What the scripted finish does: close the task, then hand the slot back.
        manager.close_task(ready_task.id, actor="claude", outcome=Outcome.COMPLETED, body="Merged.")
        released = release_slot_for_task(home, project_id=project.id, task_id=ready_task.id)
        assert released == [first.run_id]

        second = run(manager, project, home, other.id)
        assert second.run_id != first.run_id
        still_there = next(item for item in live_runs(home) if item.run_id == first.run_id)
        assert still_there.takes_slot is False
        settle(second)

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

    def test_two_dispatches_of_different_tasks_at_ceiling_minus_one_admit_exactly_one(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P2-9, the race the directory count could not see (task-264 ac-2).

        Both dispatches are held until each has passed every check that reads run
        directories -- the slot count included -- and only then let into admission
        together. Under the old scan both would have started a run on the last slot. The
        journal's admission is one transaction, so exactly one commits.
        """
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
        holder = run(manager, project, home, ready_task.id)
        hold_live(holder)  # one of the two slots, so the ceiling is one away

        both_checked = threading.Barrier(2, timeout=30)
        original = guards_journal.admit_dispatch

        def at_the_same_instant(*args, **kwargs):
            both_checked.wait()
            return original(*args, **kwargs)

        monkeypatch.setattr(guards_journal, "admit_dispatch", at_the_same_instant)
        results: List[object] = []

        def attempt(task_id: str) -> None:
            try:
                results.append(run(manager, project, home, task_id))
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                results.append(exc)

        threads = [threading.Thread(target=attempt, args=(task_id,)) for task_id in others]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        handles = [r for r in results if not isinstance(r, Exception)]
        refusals = [r for r in results if isinstance(r, Exception)]
        assert len(handles) == 1, f"expected exactly one admission, got {results}"
        assert len(refusals) == 1 and isinstance(refusals[0], ConcurrencyLimitError), refusals
        assert holder.run_id in str(refusals[0])
        winner = handles[0]
        assert isinstance(winner, RunHandle)
        loser = [task_id for task_id in others if task_id != winner.task_id][0]
        loser_task = manager.get_task(loser)
        assert loser_task is not None
        assert not [
            entry for entry in loser_task.log if entry.type is LogEntryType.DISPATCH
        ], "the refused dispatch started nothing"
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

    def test_dispatch_now_starts_a_run_above_the_ceiling(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The overage (task-461): a full machine, and a person choosing to go past it.

        The machine is genuinely full -- the same state that raises
        ``ConcurrencyLimitError`` in the test above -- and the only difference is the
        field. Two runs live against a ceiling of one is the state this exists to
        produce, and the assertion is that both really are counted, not that the second
        started.
        """
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
        hold_live(first)

        second = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=other.id, over_ceiling=True),
            home=home,
        )
        hold_live(second)

        assert len([item for item in live_runs(home) if item.takes_slot]) == 2, (
            "the machine really is over its ceiling; a count that clamped would be the "
            "defect this feature must not introduce"
        )
        assert second.directory.read_meta().get("over_ceiling") is True

    def test_an_overage_is_named_as_one_in_the_next_refusal(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """A count above its own limit is unreadable unless the record says why.

        The next dispatch is refused as always -- an overage widens nothing for anyone
        else -- and the sentence it is refused with has to explain a machine running two
        runs against a ceiling of one, or it reads as a broken counter.
        """
        write_dispatch_config(home, fake_runner, require_clean_tree=False)
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
        second = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=others[0], over_ceiling=True),
            home=home,
        )
        hold_live(second)

        with pytest.raises(ConcurrencyLimitError) as caught:
            run(manager, project, home, others[1])

        message = str(caught.value)
        assert second.run_id in message, message
        assert "over the ceiling" in message, message
        # The overage is one run's licence, not the machine's. Nothing else gets in.
        assert len([item for item in live_runs(home) if item.takes_slot]) == 2, message

    def test_an_overage_still_answers_to_the_hourly_cap(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The bound that is kept, and the argument for not adding a second one.

        An overage is a dispatch. Section 7's hourly cap is what actually bounds a loop
        of them, so it has to bind here or the field is a way round it.
        """
        write_dispatch_config(
            home, fake_runner, require_clean_tree=False, limits={"dispatches_per_hour": 1}
        )
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
        hold_live(first)

        with pytest.raises(BudgetCapError) as caught:
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(task_id=other.id, over_ceiling=True),
                home=home,
            )

        assert caught.value.reason == "machine_per_hour"

    def test_the_task_records_that_its_run_was_an_overage(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The run's own directory is on this machine; the task record travels.

        A reader asking six months later why two agents ran against a ceiling of one has
        the task, not the run directory, so the sentence goes on the dispatch entry.
        """
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
        hold_live(first)
        second = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=other.id, over_ceiling=True),
            home=home,
        )
        settle(second)

        recorded = manager.get_task(other.id)
        assert recorded is not None
        entries = [item for item in recorded.log if item.type is LogEntryType.DISPATCH]
        assert len(entries) == 1, entries
        assert "above this machine's ceiling" in (entries[0].body or ""), entries[0].body

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
    posture: Optional[Posture] = None,
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
            posture=posture,
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
        stored = TaskManager(task_store(project.root / "tasks")).get_task(agent_filed_task.id)
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


class TestEscalationIsRecorded:
    """What choosing a wider envelope costs, at the point of use (task-307).

    The `dispatch` entry already carries `posture_source` and `posture_ceiling`
    (task-308), and joined with `caused_by` those reconstruct the same fact. These
    assert the cheaper thing: that the human's own entry says it, so nobody has to hold
    two entries side by side to answer "who decided this run could merge itself".
    """

    def test_the_authorising_entry_names_a_posture_raised_above_the_project(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False, max_posture="autonomous")

        run_as(
            manager,
            project,
            home,
            ready_task.id,
            user="Jeff Posey",
            posture=Posture.AUTONOMOUS,
        )

        stored = manager.get_task(ready_task.id)
        assert stored is not None
        body = next(
            entry.body or ""
            for entry in reversed(stored.log)
            if entry.type is LogEntryType.NOTE and "authorised a dispatch" in (entry.body or "")
        )
        assert "chose posture `autonomous` for this run, above this project's `auto`" in body

    def test_it_stays_silent_when_the_choice_does_not_widen_anything(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Narrowing is not an escalation, and neither is picking the default. A clause
        that fired on every dispatch would say nothing by saying it every time."""
        write_dispatch_config(home, fake_runner, require_clean_tree=False, max_posture="autonomous")

        run_as(
            manager,
            project,
            home,
            ready_task.id,
            user="Jeff Posey",
            posture=Posture.SUPERVISED,
        )

        stored = manager.get_task(ready_task.id)
        assert stored is not None
        body = next(
            entry.body or ""
            for entry in reversed(stored.log)
            if entry.type is LogEntryType.NOTE and "authorised a dispatch" in (entry.body or "")
        )
        assert "above this project's" not in body

    def test_a_dispatch_that_chose_nothing_reads_exactly_as_it_did_before(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, require_clean_tree=False)

        run_as(manager, project, home, ready_task.id, user="Jeff Posey")

        stored = manager.get_task(ready_task.id)
        assert stored is not None
        body = next(
            entry.body or ""
            for entry in reversed(stored.log)
            if entry.type is LogEntryType.NOTE and "authorised a dispatch" in (entry.body or "")
        )
        assert body == (
            "Jeff Posey authorised a dispatch of this task from the task page. No extra "
            "instruction was given: the task record is the brief."
        )


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


# ----- the address the run would be handed (task-193) -------------------------


def unreachable(api_base: str, **_: object) -> ApiBaseProbe:
    """A probe result for an address with nothing behind it."""
    return ApiBaseProbe(
        api_base=api_base,
        answered=False,
        is_agentjobs=False,
        detail="nothing answered ([Errno 111] Connection refused)",
    )


class TestTheAddressIsCheckedBeforeAnythingStarts:
    """Resolving an address and having a working one are different facts.

    Every dispatch from this machine's CLI resolved cleanly to ``http://localhost:8765``
    and told three real agents to use it, which nothing there serves (task-193). The
    resolver was not broken. What was missing is anyone asking whether the answer was
    true, and the reason nobody noticed is the reason it matters: an agent that cannot
    reach AgentJobs cannot report that it cannot reach AgentJobs.
    """

    def test_a_dead_address_refuses_the_dispatch(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch,
    ) -> None:
        write_dispatch_config(home, fake_runner)
        monkeypatch.setattr("agentjobs.dispatch.guards.probe_api_base", unreachable)

        with pytest.raises(UnreachableApiBaseError) as excinfo:
            run(manager, project, home, ready_task.id)

        assert excinfo.value.reason == "api_base_unreachable"
        assert live_runs(home) == []

    def test_the_refusal_names_the_address_and_where_it_came_from(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch,
    ) -> None:
        """A machine that declared nothing needs to be told to declare something --
        which is different advice from "the line you wrote is stale"."""
        write_dispatch_config(home, fake_runner)
        monkeypatch.setattr("agentjobs.dispatch.guards.probe_api_base", unreachable)

        with pytest.raises(UnreachableApiBaseError) as excinfo:
            run(manager, project, home, ready_task.id)

        message = str(excinfo.value)
        assert DEFAULT_API_BASE in message
        assert "api_base" in message
        assert str(home / CONFIG_FILENAME) in message

    def test_a_stale_declaration_is_reported_as_a_declaration(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch,
    ) -> None:
        write_dispatch_config(home, fake_runner, api_base="http://127.0.0.1:8876")
        monkeypatch.setattr("agentjobs.dispatch.guards.probe_api_base", unreachable)

        with pytest.raises(UnreachableApiBaseError) as excinfo:
            run(manager, project, home, ready_task.id)

        message = str(excinfo.value)
        assert "http://127.0.0.1:8876" in message
        assert "stale" in message

    def test_nothing_is_claimed_or_written_when_the_address_is_dead(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch,
    ) -> None:
        """The gate sits with the checks that write nothing, so a refusal leaves the
        task exactly as it was -- unclaimed, and with no authorising entry behind it."""
        write_dispatch_config(home, fake_runner)
        monkeypatch.setattr("agentjobs.dispatch.guards.probe_api_base", unreachable)
        before = manager.get_task(ready_task.id)
        assert before is not None

        with pytest.raises(UnreachableApiBaseError):
            run(manager, project, home, ready_task.id)

        after = manager.get_task(ready_task.id)
        assert after is not None
        assert after.lifecycle is before.lifecycle
        assert after.assignment.owner == before.assignment.owner
        assert len(after.log) == len(before.log)

    def test_an_observed_address_is_never_probed(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
        monkeypatch,
    ) -> None:
        """The browser reads its address off the socket answering that very request, so
        asking again is a server calling itself from inside its own request handler."""
        write_dispatch_config(home, fake_runner)
        calls: List[str] = []

        def record(api_base: str, **_: object) -> ApiBaseProbe:
            calls.append(api_base)
            return unreachable(api_base)

        monkeypatch.setattr("agentjobs.dispatch.guards.probe_api_base", record)

        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=ready_task.id),
            home=home,
            api_base="http://127.0.0.1:8876",
        )
        settle(handle)

        assert calls == []
        assert handle.api_base == "http://127.0.0.1:8876"

    def test_an_address_that_answers_lets_the_run_start(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        fake_runner: Path,
        ready_task,
    ) -> None:
        """The conftest stub answers, so this is the ordinary path with the gate in it."""
        write_dispatch_config(home, fake_runner, api_base="http://127.0.0.1:8876")

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.api_base == "http://127.0.0.1:8876"


def _record_dispatch(manager: TaskManager, task_id: str, run_id: str) -> None:
    """A dispatch entry exactly as the runner writes one, so the counts are real."""
    manager.record_dispatch(
        task_id,
        actor="Jeff Posey",
        run_id=run_id,
        agent="fake",
        runner="fake",
        mode=DispatchMode.BATCH,
        posture=DispatchPosture.SUPERVISED,
        trigger=DispatchTrigger.MANUAL,
        caused_by=1,
        argv=["python", "-c", "pass"],
        cwd=".",
        git_head="abc1234",
    )


class TestBudgetCapsBindEveryTrigger:
    """task-334: the spend caps used to bind `auto` alone, and now bind the chokepoint.

    The reason is in `dispatch/budget.py`. The short version: D3 exempted manual
    dispatch because a person clicking repeatedly is a decision rather than a
    malfunction, which assumes this machine can tell a person's click from an agent's.
    The 2026-08-21 audit showed it cannot -- so `manual` was the uncapped trigger and
    the one an agent could reach with a single unauthenticated POST.

    These cases go through `dispatch_task` rather than through `check_budget`, because
    "the function returns a refusal" and "the dispatcher acts on it, for this trigger"
    are different claims and only the second one was ever missing.
    """

    def _spend_the_lifetime_cap(self, manager: TaskManager, task_id: str) -> None:
        """Ten dispatch entries -- the default `per_task_lifetime` -- then a human's.

        Through ``record_dispatch`` rather than ``add_log_entry``: the manager refuses a
        hand-written ``dispatch`` entry, and a test that faked one would be counting
        something the dispatcher does not write.
        """
        for index in range(10):
            _record_dispatch(manager, task_id, f"run_{index}")
        manager.add_log_entry(
            task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go anyway."
        )

    def test_a_manual_dispatch_over_the_lifetime_cap_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner)
        self._spend_the_lifetime_cap(manager, ready_task.id)

        with pytest.raises(BudgetCapError) as caught:
            run(manager, project, home, ready_task.id)

        assert caught.value.reason == "per_task_lifetime"
        assert caught.value.refusal.parks_task is True
        assert live_runs(home) == []

    def test_a_manual_dispatch_inside_the_cooldown_is_refused(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The case an agent's forged click used to walk straight through."""
        write_dispatch_config(home, fake_runner)
        settle(run(manager, project, home, ready_task.id))
        manager.add_log_entry(
            ready_task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Again."
        )

        with pytest.raises(BudgetCapError) as caught:
            run(manager, project, home, ready_task.id)

        assert caught.value.reason == "cooldown"
        # Transient -- waiting fixes it -- so it is not a decision to hand to anyone.
        assert caught.value.refusal.parks_task is False
        after = manager.get_task(ready_task.id)
        assert after is not None
        assert not [
            entry
            for entry in after.log
            if entry.type is LogEntryType.TRANSITION and entry.actor == "dispatcher"
        ]

    def test_the_refusal_names_the_cap_and_the_trigger_on_the_record(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """ac-4. An HTTP refusal is read once; the record is read by the next session."""
        write_dispatch_config(home, fake_runner)
        self._spend_the_lifetime_cap(manager, ready_task.id)

        with pytest.raises(BudgetCapError):
            run(manager, project, home, ready_task.id)

        after = manager.get_task(ready_task.id)
        assert after is not None
        notes = [entry for entry in after.log if entry.type is LogEntryType.NOTE]
        refusals = [entry for entry in notes if entry.data.get("dispatch_refused")]
        assert len(refusals) == 1
        assert refusals[0].data["dispatch_refused"] == "per_task_lifetime"
        assert refusals[0].data["dispatch_trigger"] == "manual"
        assert refusals[0].actor == "dispatcher"
        assert "per_task_lifetime" in (refusals[0].body or "")

    def test_a_task_under_every_cap_still_dispatches(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The control. A cap that refuses everything would pass every test above."""
        write_dispatch_config(home, fake_runner)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")


def _seed_finished_runs(home: Path, count: int, *, minutes_ago: int) -> None:
    """``count`` run directories that have already ended, started ``minutes_ago``.

    Terminal on purpose: a live run would be refused by the concurrency ceiling or the
    per-task lock first, and this is testing the cap that counts *takeoffs* rather than
    the one that counts what is in the air.
    """
    started = (utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    root = home / "runs"
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        directory = root / f"run_seed{index:03d}"
        directory.mkdir(exist_ok=True)
        (directory / "meta.yaml").write_text(
            yaml.safe_dump(
                {
                    "run_id": directory.name,
                    "task_id": f"task-9{index:02d}",
                    "project_id": "sandbox",
                    "status": "finished",
                    "started_at": started,
                    "finished_at": started,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


class TestTheMachineWideHourlyCap:
    """The cap no per-task budget can stand in for (task-334).

    Per-task caps bound one task. N tasks each dispatching at their own limit have no
    ceiling between them, and `max_concurrent_runs` does not supply one either: it
    bounds how many runs are *alive*, so a loop that starts a run, fails it, and starts
    another never holds a slot long enough to be refused by it. This counts takeoffs.
    """

    def test_it_refuses_once_the_hour_is_full(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_dispatch_config(home, fake_runner, limits={"dispatches_per_hour": 5})
        _seed_finished_runs(home, 5, minutes_ago=10)

        with pytest.raises(BudgetCapError) as caught:
            run(manager, project, home, ready_task.id)

        assert caught.value.reason == "machine_per_hour"
        assert "5 runs in the last hour" in str(caught.value)
        # Transient: the hour rolls forward on its own, so nobody's ball moves for it.
        assert caught.value.refusal.parks_task is False
        after = manager.get_task(ready_task.id)
        assert after is not None
        assert not [
            entry
            for entry in after.log
            if entry.type is LogEntryType.TRANSITION and entry.actor == "dispatcher"
        ]

    def test_it_counts_a_rolling_hour_not_a_clock_hour(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Runs older than the window do not hold the machine down forever."""
        write_dispatch_config(home, fake_runner, limits={"dispatches_per_hour": 5})
        _seed_finished_runs(home, 5, minutes_ago=61)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_it_records_the_refusal_on_the_task(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """ac-4 again, for the cap whose cause is the machine rather than the task."""
        write_dispatch_config(home, fake_runner, limits={"dispatches_per_hour": 2})
        _seed_finished_runs(home, 2, minutes_ago=5)

        with pytest.raises(BudgetCapError):
            run(manager, project, home, ready_task.id)

        after = manager.get_task(ready_task.id)
        assert after is not None
        refusals = [
            entry for entry in after.log if entry.data.get("dispatch_refused") == "machine_per_hour"
        ]
        assert len(refusals) == 1
        assert refusals[0].data["dispatch_trigger"] == "manual"
        assert "dispatches_per_hour" in (refusals[0].body or "")

    def test_the_default_leaves_room_for_the_epic_walks_this_machine_runs(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """ac-3, with the number the assumption rests on written into the test.

        The busiest rolling hour in this machine's whole run ledger was **twelve**
        dispatches (137 runs, measured 2026-09-04), on three concurrent slots. The
        default is 30. So a walk half again as busy as anything that has ever run here
        still takes off, which is what a cap has to do to survive: one that fires on
        real work is one somebody raises to infinity.
        """
        write_dispatch_config(home, fake_runner)  # no limits block -- the shipped default
        _seed_finished_runs(home, 18, minutes_ago=30)

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")

    def test_an_unreadable_run_is_not_counted_against_the_cap(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """A run with no readable start time cannot be shown to be recent.

        Counting it would let one corrupt file on disk refuse dispatches, which is a
        worse failure than the one the cap prevents.
        """
        write_dispatch_config(home, fake_runner, limits={"dispatches_per_hour": 1})
        directory = home / "runs" / "run_broken"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text("status: finished\n", encoding="utf-8")

        handle = run(manager, project, home, ready_task.id)
        settle(handle)

        assert handle.run_id.startswith("run_")
