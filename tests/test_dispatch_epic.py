"""The epic walk: what authorises a child run, what bounds it, and where it stops.

Three properties carry this feature and each gets its case constructed explicitly rather
than inferred from a happy path.

**A child run is still human-clocked.** ``TestInheritedAuthorization`` asserts the entry
a child dispatch is judged on is a *stored* row naming a configured human, and that the
human it names is the one who authorised the parent. A walk that could start a run on an
agent's entry would have made the rule in ``dispatch.guards`` decorative.

**The bound is mechanical.** ``TestAttemptBudget`` asserts the third run of a child under
one authorisation is refused, and that the count comes off the child's own log rather
than out of the walk's memory -- so a walk started fresh tomorrow inherits it.

**One bad child stops everything.** ``TestWalkStops`` asserts that for each of the four
ways a child can fail to be clean, no sibling is started afterwards. Skipping a bad child
is the failure mode this whole design exists to prevent, and "it did not happen in the
one test we wrote" is not the same as "it cannot happen".

The walk's clock, sleeps and dispatcher are injected. A walk is a loop over processes
that cost money and take an hour; the alternative to injecting them is a suite that
proves the stop rule by never exercising it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from agentjobs.dispatch.config import MergeMode
from agentjobs.dispatch.epic import (
    CHILD_ATTEMPT_LIMIT,
    ChildAttempt,
    ChildAttemptsExhaustedError,
    ChildVerdict,
    EpicAuthorization,
    NotAChildError,
    ParentNotHumanClockedError,
    ParentNotSupervisedError,
    WalkResult,
    WalkSettings,
    WalkStop,
    assert_attempts_remain,
    count_attempts,
    describe_settings,
    frontier,
    inherited_merge_mode,
    next_eligible_child,
    parent_authorizing_entry,
    resolve_epic_authorization,
    walk_epic,
    walk_handoff_prompt,
    walk_report,
)
from agentjobs.dispatch.guards import (
    BudgetCapError,
    ConflictingAuthorizationError,
    DispatchRequest,
    LiveRunExistsError,
    dispatch_task,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
    Outcome,
)
from agentjobs.projects import Project
from support import task_store

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(task_store(project.root / "tasks"))


def make_parent(
    manager: TaskManager,
    *,
    dispatched: bool = True,
    merge_mode: MergeMode = MergeMode.AUTOMERGE,
    merge_mode_source: Optional[str] = None,
) -> str:
    """An active epic whose dispatch a human authorised, as a real one looks.

    ``posture_source`` defaults to ``None`` because that is what a dispatch entry written
    before task-308 looks like, and the field's own documentation says an absent value
    reads as ``project``. Every test that does not care about inheritance therefore gets
    an epic that passes nothing down, which is the behaviour those tests were written
    against.
    """
    parent = manager.create_task(
        title="An epic",
        category="general",
        summary="Umbrella.",
        description="Several children.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.add_log_entry(
        parent.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Work this epic."
    )
    stored = manager.get_task(parent.id)
    assert stored is not None
    human_entry = stored.log[-1].id
    manager.claim_task(parent.id, agent="claude")
    if dispatched:
        manager.record_dispatch(
            parent.id,
            actor="Jeff Posey",
            run_id="run_parent",
            agent="claude",
            runner="fake",
            mode=DispatchMode.SESSION,
            merge_mode=merge_mode,
            merge_mode_source=merge_mode_source,
            trigger=DispatchTrigger.MANUAL,
            caused_by=human_entry,
            argv=["fake"],
            cwd=".",
            git_head="0000000",
        )
    return parent.id


def make_child(manager: TaskManager, parent_id: str, title: str) -> str:
    child = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=f"Do {title}.",
        lifecycle=Lifecycle.READY,
        actor="claude",
        parent=parent_id,
    )
    return child.id


@dataclass
class FakeHandle:
    run_id: str


class Dispatcher:
    """A stand-in for ``dispatch_task`` that records what it was asked to start.

    It also does the one thing the real dispatcher does that the walk depends on:
    claims the child, so the second pass through the loop does not offer the same task
    again. Everything else a dispatch does happens in another process and is another
    suite's business.
    """

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager
        self.started: List[str] = []

    def __call__(self, *, manager, project, project_config, request, home=None, api_base=None):
        self.started.append(request.task_id)
        assert request.on_behalf_of_parent is True
        assert request.trigger is DispatchTrigger.CHILD
        task = self.manager.get_task(request.task_id)
        assert task is not None
        if task.lifecycle is Lifecycle.READY:
            self.manager.claim_task(task.id, agent="claude")
        return FakeHandle(run_id=f"run_{request.task_id}")


def drive(
    manager: TaskManager,
    project: Project,
    parent_id: str,
    *,
    dispatcher: Dispatcher,
    script: Dict[str, List[str]],
    run_status: Optional[Dict[str, str]] = None,
    settings: Optional[WalkSettings] = None,
    merge_mode: Optional[MergeMode] = None,
):
    """Run a walk whose children behave according to ``script``.

    ``script`` maps a child id to the moves that child makes, one per poll: ``complete``,
    ``cancel``, ``park``, ``die`` or ``wait``. Driving the children from a script rather
    than from real sessions is what makes the stop rule testable at all -- the states are
    the four the guide enumerates, and each of them is a fact written to a task record.
    """
    statuses = dict(run_status or {})
    pending = {child: list(moves) for child, moves in script.items()}
    clock = {"now": 0.0}

    def read_status(run_id: str) -> Optional[str]:
        return statuses.get(run_id)

    def tick(_seconds: float) -> None:
        clock["now"] += 1.0
        for child_id, moves in pending.items():
            task = manager.get_task(child_id)
            if task is None or not task.is_open or not moves:
                continue
            if task.ball is not Ball.AGENT:
                continue
            # Only the child the walk actually started moves. A child still `ready` has
            # not been dispatched, and letting it finish anyway would quietly prove the
            # opposite of what the stop tests are for.
            if task.lifecycle is not Lifecycle.ACTIVE:
                continue
            move = moves.pop(0)
            if move == "complete":
                manager.close_task(child_id, actor="claude", outcome=Outcome.COMPLETED)
            elif move == "cancel":
                manager.close_task(child_id, actor="claude", outcome=Outcome.CANCELLED)
            elif move == "park":
                manager.handoff(
                    child_id,
                    actor="claude",
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.REVIEW,
                    ball_prompt="Look at this.",
                )
            elif move == "die":
                statuses[f"run_{child_id}"] = "failed"

    return walk_epic(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        parent_id=parent_id,
        settings=settings or WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0),
        merge_mode=merge_mode,
        dispatch=dispatcher,
        read_run_status=read_status,
        sleep=tick,
        now=lambda: clock["now"],
    )


# ----- the authorisation a child inherits -------------------------------------


class TestInheritedAuthorization:
    def test_takes_the_entry_the_parents_own_dispatch_was_caused_by(
        self, manager: TaskManager
    ) -> None:
        parent_id = make_parent(manager)
        parent = manager.get_task(parent_id)
        assert parent is not None
        entry = parent_authorizing_entry(parent)
        assert entry is not None
        assert entry.actor == "Jeff Posey"
        assert entry.body == "Work this epic."

    def test_falls_back_to_the_newest_entry_when_nothing_dispatched_the_parent(
        self, manager: TaskManager
    ) -> None:
        parent_id = make_parent(manager, dispatched=False)
        parent = manager.get_task(parent_id)
        assert parent is not None
        entry = parent_authorizing_entry(parent)
        assert entry is not None
        # The claim wrote a `transition` newer than the human's note, and the fallback
        # steps over it: an epic must be claimed before it can be walked, so counting the
        # claim would mean the fallback could never fire at all.
        assert entry.actor == "Jeff Posey"
        assert entry.body == "Work this epic."

    def test_resolves_to_the_human_who_authorised_the_parent(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        authorization = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        assert authorization.actor.id == "Jeff Posey"
        assert authorization.parent.id == parent_id
        assert authorization.attempts_used == 0
        assert authorization.attempts_left == CHILD_ATTEMPT_LIMIT

    def test_a_task_with_no_parent_has_nothing_to_inherit(self, manager: TaskManager) -> None:
        orphan = manager.create_task(
            title="Alone",
            category="general",
            summary="No parent.",
            description="Nothing above it.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        stored = manager.get_task(orphan.id)
        assert stored is not None
        with pytest.raises(NotAChildError):
            resolve_epic_authorization(manager, PROJECT_CONFIG, stored)

    def test_an_epic_nobody_is_working_authorises_nothing(self, manager: TaskManager) -> None:
        parent = manager.create_task(
            title="An epic",
            category="general",
            summary="Umbrella.",
            description="Several children.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        child_id = make_child(manager, parent.id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        with pytest.raises(ParentNotSupervisedError):
            resolve_epic_authorization(manager, PROJECT_CONFIG, child)

    def test_an_agents_own_note_shadows_the_human_and_refuses(self, manager: TaskManager) -> None:
        """Only `transition` is stepped over. An agent that has been working is not."""
        parent_id = make_parent(manager, dispatched=False)
        manager.add_log_entry(
            parent_id, actor="claude", type=LogEntryType.PROGRESS, body="Been at it a while."
        )
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        with pytest.raises(ParentNotHumanClockedError):
            resolve_epic_authorization(manager, PROJECT_CONFIG, child)

    def test_an_epic_no_human_ever_touched_authorises_nothing(self, manager: TaskManager) -> None:
        parent = manager.create_task(
            title="Filed by an agent",
            category="general",
            summary="Nobody human has said anything about this.",
            description="An agent filed it and an agent claimed it.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.claim_task(parent.id, agent="claude")
        child_id = make_child(manager, parent.id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        with pytest.raises(ParentNotHumanClockedError):
            resolve_epic_authorization(manager, PROJECT_CONFIG, child)

    def test_a_parent_that_is_not_there_is_a_refusal_and_not_a_crash(
        self, manager: TaskManager, project: Project
    ) -> None:
        """The walk has to refuse rather than raise: it runs unattended, and a traceback
        is not a handoff.

        It used to arrive at this state by deleting the parent's *file* -- a branch
        checked out that predated the epic, or a record somebody moved. That cannot
        happen now (task-402): ``parent_id`` is a foreign key, so a child's parent is
        either a row or the child was never written. The refusal is reached the way it
        still can be, with a child naming a parent this manager cannot see, and it is
        kept because the walk must never turn an unreadable graph into a traceback.
        """
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "Orphaned")
        stored = manager.get_task(child_id)
        assert stored is not None
        orphaned = stored.model_copy(update={"parent": "task-000-not-a-task"})

        with pytest.raises(ParentNotSupervisedError):
            resolve_epic_authorization(manager, PROJECT_CONFIG, orphaned)


# ----- the bound --------------------------------------------------------------


class TestAttemptBudget:
    def test_counts_only_entries_naming_this_parent_and_this_entry(
        self, manager: TaskManager
    ) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        for marker in (
            {"epic": {"parent": parent_id, "entry": 2}},
            {"epic": {"parent": parent_id, "entry": 2}},
            {"epic": {"parent": parent_id, "entry": 99}},
            {"epic": {"parent": "task-other", "entry": 2}},
            {"authorizes_dispatch": True},
        ):
            manager.add_log_entry(
                child_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="x", data=marker
            )
        child = manager.get_task(child_id)
        assert child is not None
        assert count_attempts(child, parent_id=parent_id, entry_id=2) == 2

    def test_refuses_the_run_after_the_limit(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        authorization = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        spent = EpicAuthorization(
            parent=authorization.parent,
            entry=authorization.entry,
            actor=authorization.actor,
            attempts_used=CHILD_ATTEMPT_LIMIT,
        )
        assert spent.attempts_left == 0
        with pytest.raises(ChildAttemptsExhaustedError) as caught:
            assert_attempts_remain(spent, child)
        # The refusal has to say how a human gets past it, or it is a dead end at 3am.
        assert "authorise" in str(caught.value)

    def test_a_fresh_human_authorisation_starts_a_new_budget(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        first = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        for _ in range(CHILD_ATTEMPT_LIMIT):
            manager.add_log_entry(
                child_id,
                actor="Jeff Posey",
                type=LogEntryType.NOTE,
                body="attempt",
                data=first.data(),
            )
        child = manager.get_task(child_id)
        assert child is not None
        assert resolve_epic_authorization(manager, PROJECT_CONFIG, child).attempts_left == 0

        # The human dispatches the epic again. Same child, same walk, new budget --
        # and a record that a person chose to give it one.
        manager.add_log_entry(
            parent_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Try it again."
        )
        parent = manager.get_task(parent_id)
        assert parent is not None
        manager.record_dispatch(
            parent_id,
            actor="Jeff Posey",
            run_id="run_parent_2",
            agent="claude",
            runner="fake",
            mode=DispatchMode.SESSION,
            merge_mode=MergeMode.AUTOMERGE,
            trigger=DispatchTrigger.MANUAL,
            caused_by=parent.log[-1].id,
            argv=["fake"],
            cwd=".",
            git_head="0000000",
        )
        child = manager.get_task(child_id)
        assert child is not None
        assert resolve_epic_authorization(manager, PROJECT_CONFIG, child).attempts_left == (
            CHILD_ATTEMPT_LIMIT
        )


class TestRequestConflicts:
    """Naming an entry, creating one, and inheriting one are three different acts."""

    @pytest.mark.parametrize(
        "extra",
        [{"caused_by": 1}, {"authorized_by": "Jeff Posey"}],
        ids=["caused_by", "authorized_by"],
    )
    def test_refuses_a_request_that_asks_for_two_authorisations(
        self, manager: TaskManager, project: Project, extra: Dict[str, object]
    ) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        with pytest.raises(ConflictingAuthorizationError):
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id, on_behalf_of_parent=True, **extra  # type: ignore[arg-type]
                ),
            )


# ----- what the walk does -----------------------------------------------------


class TestWalkCompletes:
    def test_takes_every_child_in_turn_and_stops_when_none_is_open(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["complete"], second: ["complete"]},
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.started == [first, second]
        assert result.merged_children == [first, second]

    def test_never_closes_the_parent(self, manager: TaskManager, project: Project) -> None:
        """The one judgement in the loop that is not mechanical stays with the caller."""
        parent_id = make_parent(manager)
        only = make_child(manager, parent_id, "Only")
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=Dispatcher(manager),
            script={only: ["complete"]},
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert parent.lifecycle is Lifecycle.ACTIVE
        assert "still open" in walk_report(result)

    def test_max_children_stops_early_and_says_what_is_left(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["complete"], second: ["complete"]},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_children=1),
        )
        assert dispatcher.started == [first]
        assert result.stop is WalkStop.NO_ELIGIBLE_CHILD
        assert second in result.detail


class TestWalkStops:
    """Every way a child can be unclean, and the sibling that must not be started."""

    def test_a_parked_child_stops_the_walk(self, manager: TaskManager, project: Project) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["park"], second: ["complete"]},
        )
        assert result.stop is WalkStop.CHILD_NEEDS_A_HUMAN
        assert dispatcher.started == [first]
        assert result.attempts[-1].verdict is ChildVerdict.PARKED
        assert result.stopped_on == first, "the walk records which child grounded it"

    def test_the_handoff_names_the_child_that_grounded_the_walk(self) -> None:
        """Not the last one to land, which under concurrency is a child that succeeded.

        The incident (task-466): task-212's handoff told the owner the walk had stopped
        on task-464 while quoting task-465's reason and run id. task-464 had merged
        cleanly and needed nothing; task-465 was the one waiting on him. Attempts are
        ordered by when each child landed, and the walk watches down whatever is already
        in the air after it grounds, so the last lander is routinely the wrong answer.
        """
        result = WalkResult(
            parent_id="task-212",
            stop=WalkStop.CHILD_NEEDS_A_HUMAN,
            detail="ball is human/decision: somebody has to look at it.",
            stopped_on="task-465",
            attempts=[
                ChildAttempt("task-465", 1, "run_parked", ChildVerdict.PARKED, "parked"),
                ChildAttempt("task-464", 1, "run_clean", ChildVerdict.COMPLETED, "closed"),
            ],
        )

        prompt = walk_handoff_prompt(result)

        assert "stopped on task-465" in prompt
        assert "stopped on task-464" not in prompt

    def test_a_child_closed_unresolved_stops_the_walk(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["cancel"], second: ["complete"]},
        )
        assert result.stop is WalkStop.CHILD_CLOSED_UNRESOLVED
        assert dispatcher.started == [first]

    def test_a_dead_child_is_retried_exactly_once_and_then_stops(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")

        # The real dispatcher writes the authorising entry the counter reads, so a fake
        # one has to as well -- otherwise the budget never depletes and this test would
        # pass by looping forever.
        class Counting(Dispatcher):
            def __call__(self, **kwargs):
                request = kwargs["request"]
                task = self.manager.get_task(request.task_id)
                assert task is not None
                authorization = resolve_epic_authorization(self.manager, PROJECT_CONFIG, task)
                self.manager.add_log_entry(
                    request.task_id,
                    actor="Jeff Posey",
                    type=LogEntryType.NOTE,
                    body=authorization.describe(),
                    data=authorization.data(),
                )
                return super().__call__(**kwargs)

        dispatcher = Counting(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["die", "die"], second: ["complete"]},
        )
        assert dispatcher.started == [first, first]
        assert result.stop is WalkStop.CHILD_EXHAUSTED_ATTEMPTS
        assert [a.verdict for a in result.attempts] == [ChildVerdict.DIED, ChildVerdict.DIED]
        # The sibling was never touched. That is the property, not the count above.
        assert second not in dispatcher.started

    def test_a_child_held_by_an_unattributable_dispatch_is_neither_dead_nor_waited_on_forever(
        self, manager: TaskManager, project: Project, tmp_path: Path, monkeypatch
    ) -> None:
        """task-444: `live_run_exists` is not a death, and the wait on it is bounded.

        Nothing in this machine home is live, so the holder never becomes attributable --
        the case the grace exists for. Meanwhile the sibling the refusal says nothing
        about is started and lands.
        """
        from agentjobs.dispatch import epic as epic_module

        monkeypatch.setattr(epic_module, "CONTENTION_GRACE_SECONDS", 5.0)
        parent_id = make_parent(manager)
        held = make_child(manager, parent_id, "Held")
        free = make_child(manager, parent_id, "Free")

        class Refusing(Dispatcher):
            def __call__(self, **kwargs):
                if kwargs["request"].task_id == held:
                    self.started.append(held)
                    raise LiveRunExistsError(f"{held} is held by run run_elsewhere")
                return super().__call__(**kwargs)

        dispatcher = Refusing(manager)
        clock = {"now": 0.0}

        def tick(_seconds: float) -> None:
            clock["now"] += 1.0
            task = manager.get_task(free)
            if task is not None and task.lifecycle is Lifecycle.ACTIVE:
                manager.close_task(free, actor="claude", outcome=Outcome.COMPLETED)
            assert clock["now"] < 100, "the wait on a held child was not bounded"

        events: List[str] = []
        result = walk_epic(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            parent_id=parent_id,
            home=tmp_path / "machine",
            durable=False,
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0),
            dispatch=dispatcher,
            read_run_status=lambda _run: None,
            sleep=tick,
            now=lambda: clock["now"],
            on_event=events.append,
        )
        assert result.stop is WalkStop.COULD_NOT_START_CHILD
        assert "without its record saying whose authorisation" in result.detail
        assert ChildVerdict.DIED not in [a.verdict for a in result.attempts]
        assert [a.child_id for a in result.attempts] == [free]
        assert dispatcher.started.count(held) >= 2, "a holder that let go was tried again"
        assert any("already held by another dispatch" in line for line in events)

    def test_a_child_that_never_settles_times_out_rather_than_waiting_for_morning(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        only = make_child(manager, parent_id, "Only")
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=Dispatcher(manager),
            script={only: ["wait", "wait", "wait", "wait", "wait"]},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=3.0),
        )
        assert result.stop is WalkStop.CHILD_TIMED_OUT

    def test_a_deadlocked_epic_is_not_reported_as_a_finished_one(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        blocked = make_child(manager, parent_id, "Blocked")
        manager.update_task(
            blocked,
            actor="claude",
            dependencies=[{"task": "task-nonexistent", "type": "needs"}],
        )
        result = drive(manager, project, parent_id, dispatcher=Dispatcher(manager), script={})
        assert result.stop is WalkStop.NO_ELIGIBLE_CHILD
        assert blocked in result.detail


class TestChildSelection:
    def test_the_queue_decides_which_child_is_next(self, manager: TaskManager) -> None:
        """Not the walk. Moving a child in the queue has to move it in the walk."""
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        chosen = next_eligible_child(manager, parent_id)
        assert chosen is not None and chosen.id == first
        manager.move(second, actor="Jeff Posey", top=True)
        chosen = next_eligible_child(manager, parent_id)
        assert chosen is not None and chosen.id == second

    def test_a_sibling_outside_this_epic_is_never_offered(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        mine = make_child(manager, parent_id, "Mine")
        other = manager.create_task(
            title="Unrelated",
            category="general",
            summary="Different epic.",
            description="Nothing to do with this one.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.move(other.id, actor="Jeff Posey", top=True)
        chosen = next_eligible_child(manager, parent_id)
        assert chosen is not None and chosen.id == mine


# ----- the real dispatcher, not a stand-in ------------------------------------


class TestWhichPostureSourcesCrossTheBoundary:
    """Only a posture a person chose for the epic reaches its children (task-316).

    Read off the parent's newest ``dispatch`` entry, which only the manager may append --
    so unlike an ordinary note it is not a row anything reachable over the API can write.
    That is what makes it safe to take an execution envelope from at all.
    """

    def test_a_dispatch_time_posture_is_inherited(self, manager: TaskManager) -> None:
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is MergeMode.AUTOMERGE

    def test_an_already_inherited_posture_crosses_a_second_generation(
        self, manager: TaskManager
    ) -> None:
        """An epic whose child is itself an epic passes the original click on.

        Dropping it at the second level would make the walk's behaviour depend on how
        somebody happened to shape the tree, which is the opposite of the predictability
        the whole feature claims.
        """
        parent_id = make_parent(manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="epic")
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is MergeMode.AUTOMERGE

    def test_a_posture_from_the_parents_task_record_does_not_cross(
        self, manager: TaskManager
    ) -> None:
        """The load-bearing refusal.

        A task record is a git-tracked file any agent that can write the repository can
        write, including the agent working that very task. Letting it cross would mean
        one agent editing one field on its own parent widens every child in the epic at
        once -- and on a project whose ceiling is already ``autonomous``, the clamp that
        bounds that source bounds nothing.
        """
        parent_id = make_parent(manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="task")
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is None

    def test_the_project_default_does_not_cross(self, manager: TaskManager) -> None:
        """It already reaches every child on its own; relabelling it would only mislead."""
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="project"
        )
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is None

    def test_an_entry_written_before_postures_had_sources_reads_as_the_project(
        self, manager: TaskManager
    ) -> None:
        """An absent ``posture_source`` is 'project', never 'unknown' -- so, no inheritance."""
        parent_id = make_parent(manager, merge_mode=MergeMode.AUTOMERGE)
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is None

    def test_an_epic_nobody_dispatched_has_nothing_to_pass_down(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager, dispatched=False)
        parent = manager.get_task(parent_id)
        assert parent is not None
        assert inherited_merge_mode(parent) is None

    def test_the_authorisation_carries_it_and_names_it_on_the_childs_record(
        self, manager: TaskManager
    ) -> None:
        """The child's own record is where the human's name and the envelope meet."""
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        authorization = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        assert authorization.merge_mode is MergeMode.AUTOMERGE
        assert "merge mode `automerge`" in authorization.describe()

    def test_an_uninherited_epic_says_nothing_about_postures(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        authorization = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        assert authorization.merge_mode is None
        assert "merge_mode" not in authorization.describe()


class TestTheWalkSaysWhatEnvelopeItWillUse:
    """Printed before a walk starts, always -- an unusual-only line gets skimmed."""

    def test_it_names_an_inherited_posture(self, manager: TaskManager) -> None:
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        parent = manager.get_task(parent_id)
        assert parent is not None
        lines = describe_settings(WalkSettings(), inherited=inherited_merge_mode(parent))
        assert any("automerge (inherited from the epic" in line for line in lines)

    def test_a_walk_level_choice_is_named_as_such(self) -> None:
        lines = describe_settings(WalkSettings(), merge_mode=MergeMode.REVIEW)
        assert any("review (chosen for this walk)" in line for line in lines)

    def test_nothing_overriding_still_states_what_will_happen(self) -> None:
        lines = describe_settings(WalkSettings())
        assert any("merge mode children start at: the project default" in line for line in lines)


class RecordingDispatcher(Dispatcher):
    """The stand-in above, plus the posture each request carried."""

    def __init__(self, manager: TaskManager) -> None:
        super().__init__(manager)
        self.merge_modes: List[Optional[MergeMode]] = []

    def __call__(self, **kwargs):
        self.merge_modes.append(kwargs["request"].merge_mode)
        return super().__call__(**kwargs)


class TestAWalkLevelPostureReachesEveryChild:
    """``dispatch walk --posture``: the case with no parent run to inherit from."""

    def test_it_is_put_on_every_childs_dispatch_request(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager, dispatched=False)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        dispatcher = RecordingDispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["complete"], second: ["complete"]},
            merge_mode=MergeMode.REVIEW,
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.merge_modes == [MergeMode.REVIEW, MergeMode.REVIEW]

    def test_a_walk_that_names_none_leaves_each_child_to_resolve_its_own(
        self, manager: TaskManager, project: Project
    ) -> None:
        """The case every supervising run takes.

        The inheritance happens *below* the walk, inside ``dispatch_task``, read off the
        parent's stored record -- so the walk asserts nothing about the envelope and
        there is nothing here for a caller to get wrong or forge.
        """
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        dispatcher = RecordingDispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={child_id: ["complete"]},
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.merge_modes == [None]


class TestRealDispatchInheritsAuthorization:
    """The whole guard chain, run for real, so the stand-in above cannot be flattering.

    Everything before this exercises the walk with a fake dispatcher. That proves the
    loop and proves nothing about whether ``dispatch_task`` actually accepts an inherited
    authorisation -- which is the half of this feature that touches the rule the design
    calls structural. So one case goes all the way through, with a runner that exits
    immediately, and asserts on what landed in the record.
    """

    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        machine = tmp_path / "home"
        machine.mkdir()
        return machine

    @pytest.fixture
    def configured(self, home: Path, project: Project, tmp_path: Path) -> Path:
        runner = tmp_path / "runner.py"
        runner.write_text("print('started')\n", encoding="utf-8")
        (project.root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
        subprocess.run(
            ["git", "config", "user.email", "t@t.t"], cwd=project.root, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=project.root, capture_output=True)
        config = {
            "version": 1,
            "enabled": True,
            "runners": {
                "fake": {
                    "argv": [__import__("sys").executable, str(runner), "{prompt}"],
                    "actor": "claude",
                }
            },
            "projects": {
                "sandbox": {"enabled": True, "runner": "fake", "require_clean_tree": False}
            },
        }
        path = home / "dispatch.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    def test_writes_a_human_authorising_entry_on_the_child_and_starts(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")

        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(
                task_id=child_id,
                trigger=DispatchTrigger.CHILD,
                on_behalf_of_parent=True,
            ),
            home=home,
            api_base="http://127.0.0.1:8765",
        )
        if handle.supervisor is not None:
            handle.supervisor.join(timeout=30)

        child = manager.get_task(child_id)
        assert child is not None
        authorising = [
            entry for entry in child.log if isinstance(entry.data, dict) and "epic" in entry.data
        ]
        assert len(authorising) == 1
        # A stored row, naming a configured human -- which is the property the rule in
        # dispatch.guards is about, not the flag beside it.
        assert authorising[0].actor == "Jeff Posey"
        assert authorising[0].data["epic"]["parent"] == parent_id
        assert authorising[0].data["epic"]["attempt"] == 1

        dispatched = [entry for entry in child.log if entry.type is LogEntryType.DISPATCH]
        assert len(dispatched) == 1
        assert dispatched[0].data["trigger"] == "child"
        assert dispatched[0].data["caused_by"] == authorising[0].id

    def test_the_third_run_of_one_child_is_refused_by_the_dispatcher_itself(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """The bound is not the walk's politeness. It refuses below the walk."""
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        child = manager.get_task(child_id)
        assert child is not None
        spent = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        for _ in range(CHILD_ATTEMPT_LIMIT):
            manager.add_log_entry(
                child_id,
                actor="Jeff Posey",
                type=LogEntryType.NOTE,
                body="already tried",
                data=spent.data(),
            )
        with pytest.raises(ChildAttemptsExhaustedError):
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id,
                    trigger=DispatchTrigger.CHILD,
                    on_behalf_of_parent=True,
                ),
                home=home,
                api_base="http://127.0.0.1:8765",
            )

    def test_a_child_over_the_lifetime_cap_is_refused_by_the_budget(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """ac-1 for the `child` trigger (task-334).

        The attempt budget above bounds one *parent authorisation*, so a child re-filed
        under a fresh authorisation starts over -- it is a bound on the walk, not on the
        child. The per-task caps are the bound on the child, and until task-334 they were
        applied in `auto.py` and so were not applied here at all.
        """
        parent_id = make_parent(manager)
        child_id = make_child(manager, parent_id, "First")
        for index in range(10):  # the default `per_task_lifetime`
            manager.record_dispatch(
                child_id,
                actor="Jeff Posey",
                run_id=f"run_{index}",
                agent="fake",
                runner="fake",
                mode=DispatchMode.BATCH,
                merge_mode=MergeMode.REVIEW,
                trigger=DispatchTrigger.CHILD,
                caused_by=1,
                argv=["python", "-c", "pass"],
                cwd=".",
                git_head="abc1234",
            )

        with pytest.raises(BudgetCapError) as caught:
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id,
                    trigger=DispatchTrigger.CHILD,
                    on_behalf_of_parent=True,
                ),
                home=home,
                api_base="http://127.0.0.1:8765",
            )

        assert caught.value.reason == "per_task_lifetime"
        child = manager.get_task(child_id)
        assert child is not None
        refusals = [entry for entry in child.log if entry.data.get("dispatch_refused")]
        assert len(refusals) == 1
        assert refusals[0].data["dispatch_trigger"] == "child"
        # Nobody is watching a walk, so a count cap parks the child with a person.
        assert child.ball is Ball.HUMAN
        assert child.ball_reason is BallReason.DECISION


class TestARealChildRunGetsTheEpicsEnvelope:
    """ac-4, run through the whole guard chain rather than through the resolver alone.

    The project here is configured the way this repository is: default ``auto``, ceiling
    ``autonomous``. That is the exact shape of the defect -- task-269 was dispatched
    ``autonomous`` above an ``auto`` default, its supervisor's prompt said the merge gate
    was released for its children, and every child was started at ``auto`` and stopped
    the walk on the first review handoff. Asserting on the resolver would not have caught
    it: the resolver was right and nothing passed it the parent's posture.
    """

    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        machine = tmp_path / "home"
        machine.mkdir()
        return machine

    @pytest.fixture
    def configured(self, home: Path, project: Project, tmp_path: Path) -> Path:
        runner = tmp_path / "runner.py"
        runner.write_text("print('started')\n", encoding="utf-8")
        (project.root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
        subprocess.run(
            ["git", "config", "user.email", "t@t.t"], cwd=project.root, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=project.root, capture_output=True)
        config = {
            "version": 1,
            "enabled": True,
            "runners": {
                "fake": {
                    "argv": [__import__("sys").executable, str(runner), "{prompt}"],
                    "actor": "claude",
                }
            },
            "projects": {
                "sandbox": {
                    "enabled": True,
                    "runner": "fake",
                    "require_clean_tree": False,
                    "merge_mode": "review",
                    "allow_automerge": True,
                }
            },
        }
        path = home / "dispatch.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    def start(self, manager: TaskManager, project: Project, home: Path, child_id: str):
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(
                task_id=child_id,
                trigger=DispatchTrigger.CHILD,
                on_behalf_of_parent=True,
            ),
            home=home,
            api_base="http://127.0.0.1:8765",
        )
        if handle.supervisor is not None:
            handle.supervisor.join(timeout=30)
        task = manager.get_task(child_id)
        assert task is not None
        entries = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
        assert entries, "no dispatch entry was written"
        return handle, dict(entries[-1].data)

    def test_a_child_of_an_epic_dispatched_above_the_default_runs_at_the_epics_posture(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        handle, entry = self.start(manager, project, home, child_id)
        assert entry["merge_mode"] == "automerge"
        assert handle.merge_mode is not None
        assert handle.merge_mode.merge_mode is MergeMode.AUTOMERGE

    def test_the_childs_record_says_the_posture_came_from_the_epic(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """ac-3. ``project`` is what the defect wrote and it pointed at the wrong file."""
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = self.start(manager, project, home, child_id)
        assert entry["merge_mode_source"] == "epic"
        assert entry["allow_automerge"] is True

    def test_the_prompt_the_child_is_given_matches_what_the_supervisor_was_told(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """ac-1, stated as the two prompts agreeing rather than as a field comparison.

        The supervisor's own generated prompt claims *"a child you start merges its own
        work once its gate is green"*. That claim is only true if the child's prompt tells
        the child the same thing, and the child's prompt is built from its resolved
        posture -- so this is the end of the chain the defect broke.
        """
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = self.start(manager, project, home, child_id)
        prompt = entry["argv"][-1]
        assert "releases the merge gate" in prompt
        assert "Do not merge." not in prompt

    def test_an_epic_dispatched_at_the_project_default_changes_nothing(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """The other half of the claim: nothing is widened that was not chosen."""
        parent_id = make_parent(manager, merge_mode=MergeMode.REVIEW, merge_mode_source="project")
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = self.start(manager, project, home, child_id)
        assert entry["merge_mode"] == "review"
        assert entry["merge_mode_source"] == "project"
        assert "Do not merge." in entry["argv"][-1]

    def test_a_posture_written_on_the_parents_record_does_not_widen_its_children(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """The refusal that keeps one agent-written field from widening a dozen runs."""
        parent_id = make_parent(manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="task")
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = self.start(manager, project, home, child_id)
        assert entry["merge_mode"] == "review"
        assert entry["merge_mode_source"] == "project"

    def test_the_childs_own_record_still_loses_to_the_epics_choice(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """Precedence, asserted where it is actually spent rather than in the resolver.

        A child carrying the task-269 workaround -- ``posture:`` written onto its own
        record -- must not now mean something different from the epic it belongs to.
        """
        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        manager.update_task(child_id, actor="claude", merge_mode=MergeMode.REVIEW)
        _handle, entry = self.start(manager, project, home, child_id)
        assert entry["merge_mode"] == "automerge"
        assert entry["merge_mode_source"] == "epic"


class TestTheWatcherReadsLivenessFirst:
    """The race that made three clean children look dead on the first real walk.

    A child writes its last word and *then* its process exits; only after that does
    anything mark the run terminal. Read the record before the status and there is a
    window in which the record looks unfinished and the status looks terminal at the
    same time -- and the walk calls that a death, spends the retry, and re-runs work that
    is already on the base branch.

    The case below reproduces exactly that interleaving by making the run go terminal at
    the same moment the child closes, and asserts the walk reads it as what it is.
    """

    def test_a_child_that_closes_as_its_run_ends_is_completed_not_dead(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        only = make_child(manager, parent_id, "Only")
        dispatcher = Dispatcher(manager)
        statuses: Dict[str, str] = {}
        clock = {"now": 0.0}

        def read_status(run_id: str) -> Optional[str]:
            # Terminal from the very first read, while the record still says open. That
            # is the worst case and the one that fired: whichever is read first decides
            # the verdict, so the ordering has to be the safe one.
            statuses.setdefault(run_id, "finished")
            return statuses[run_id]

        def tick(_seconds: float) -> None:
            clock["now"] += 1.0

        # The child closes itself the instant the walk starts watching, exactly as a
        # finish does moments before its process exits.
        class ClosingDispatcher(Dispatcher):
            def __call__(self, **kwargs):
                handle = super().__call__(**kwargs)
                self.manager.close_task(
                    kwargs["request"].task_id, actor="claude", outcome=Outcome.COMPLETED
                )
                return handle

        closing = ClosingDispatcher(manager)
        result = walk_epic(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            parent_id=parent_id,
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=100.0),
            dispatch=closing,
            read_run_status=read_status,
            sleep=tick,
            now=lambda: clock["now"],
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert [a.verdict for a in result.attempts] == [ChildVerdict.COMPLETED]
        assert closing.started == [only]
        assert dispatcher.started == []


# `TestTheWalkReadsThroughTheSnapshot` stood here. One CLI invocation used to be one
# logical read, memoised by `storage.corpus_snapshot`, and a walk broke that premise: it
# is a single invocation running for as long as an epic takes, and every fact it turns on
# is written by a different process. Read through the snapshot, it saw each child exactly
# as it was when it started watching, forever. The test warmed the scope the way the CLI
# callback did and then required the walk to see a child close inside it.
#
# There is no snapshot (task-402): it memoised a parser that no read reaches, and it went
# with the backend. The premise it protected is now structural -- a walk's every read is
# a query against the store another process is writing -- so there is nothing left to
# hold wrong. What the test also proved, that the walk notices a child closing, is
# asserted by every case in `TestTheWalk` above.
#
# `agentjobs.corpus` reinstated a memo in task-485, and this paragraph is why its scope
# is **one HTTP request** and is opened in one place, the API middleware. The walk is a
# single invocation that runs for as long as an epic takes; a scope that lasted an
# invocation would freeze its view of every child again, exactly as described above.
# Nothing in the dispatch family opens one, and nothing should.


class TestConcurrentWalk:
    """Independent children fly together; the graph, not a barrier, decides who flies.

    The invariant every case here is an instance of: **at no point does an eligible,
    unclaimed child exist while a slot is free.** A walk that satisfied every other
    assertion in this file and violated that one would be the serial walk with extra
    machinery bolted to it.
    """

    def test_every_independent_child_starts_before_any_of_them_finishes(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        children = [make_child(manager, parent_id, f"Child {n}") for n in range(3)]
        dispatcher = Dispatcher(manager)
        # Each child waits a tick before completing, so a serial walk could not have all
        # three started at the moment the first one lands.
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={child: ["wait", "complete"] for child in children},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_concurrent=3),
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert sorted(dispatcher.started) == sorted(children)
        assert result.peak_in_flight == 3

    def test_a_slot_count_of_one_is_the_serial_walk_exactly(
        self, manager: TaskManager, project: Project
    ) -> None:
        """The old behaviour is still reachable, and is what an unset setting means."""
        parent_id = make_parent(manager)
        children = [make_child(manager, parent_id, f"Child {n}") for n in range(3)]
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={child: ["wait", "complete"] for child in children},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0),
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.started == children
        assert result.peak_in_flight == 1

    def test_a_freed_child_starts_without_waiting_for_its_siblings(
        self, manager: TaskManager, project: Project
    ) -> None:
        """No barrier: a dependent starts when *its* need closes, not when a wave does.

        The long-running sibling is the point. Under a wave scheduler the dependent would
        wait for it, because a wave ends when its slowest member does.
        """
        parent_id = make_parent(manager)
        gate = make_child(manager, parent_id, "Gate")
        slow = make_child(manager, parent_id, "Slow")
        dependent = make_child(manager, parent_id, "Dependent")
        manager.update_task(
            dependent, actor="claude", dependencies=[{"task": gate, "type": "needs"}]
        )
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={
                gate: ["complete"],
                slow: ["wait", "wait", "wait", "wait", "complete"],
                dependent: ["complete"],
            },
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_concurrent=2),
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        # Landed order, not start order: the dependent got a slot the moment the gate
        # closed, and finished long before the slow sibling it never depended on.
        landed = [attempt.child_id for attempt in result.attempts]
        assert landed.index(dependent) < landed.index(slow)

    def test_a_diamond_child_waits_for_its_second_parent(
        self, manager: TaskManager, project: Project
    ) -> None:
        """A child unblocked by one parent is not thereby eligible.

        A scheduler that pushed newly-freed dependents onto a ready queue on completion
        would start ``both`` as soon as ``left`` closed, against an unmet ``right``. This
        is the case where "X unblocked me" and "I am eligible" differ, and asking
        claimability afresh is what cannot get it wrong.
        """
        parent_id = make_parent(manager)
        left = make_child(manager, parent_id, "Left")
        right = make_child(manager, parent_id, "Right")
        both = make_child(manager, parent_id, "Both")
        manager.update_task(
            both,
            actor="claude",
            dependencies=[
                {"task": left, "type": "needs"},
                {"task": right, "type": "needs"},
            ],
        )
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={
                left: ["complete"],
                right: ["wait", "wait", "complete"],
                both: ["complete"],
            },
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_concurrent=3),
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        # `both` starts last and lands last. Started before `right` closed, it would have
        # been dispatched against a prerequisite that had not landed.
        assert dispatcher.started.index(both) > dispatcher.started.index(right)
        landed = [attempt.child_id for attempt in result.attempts]
        assert landed.index(both) > landed.index(right)

    def test_a_bad_child_grounds_takeoffs_and_lets_the_others_land(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        bad = make_child(manager, parent_id, "Bad")
        flying = make_child(manager, parent_id, "Flying")
        waiting = make_child(manager, parent_id, "Waiting")
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={
                bad: ["park"],
                flying: ["wait", "wait", "complete"],
                waiting: ["complete"],
            },
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_concurrent=2),
        )
        assert result.stop is WalkStop.CHILD_NEEDS_A_HUMAN
        # The two that were airborne when the bad one parked both landed; the third never
        # took off, because a grounded walk starts nothing further.
        assert sorted(dispatcher.started) == sorted([bad, flying])
        assert waiting not in dispatcher.started
        verdicts = {attempt.child_id: attempt.verdict for attempt in result.attempts}
        assert verdicts[bad] is ChildVerdict.PARKED
        assert verdicts[flying] is ChildVerdict.COMPLETED

    def test_max_children_still_caps_a_concurrent_walk(
        self, manager: TaskManager, project: Project
    ) -> None:
        parent_id = make_parent(manager)
        children = [make_child(manager, parent_id, f"Child {n}") for n in range(4)]
        dispatcher = Dispatcher(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={child: ["complete"] for child in children},
            settings=WalkSettings(
                poll_seconds=0.0,
                child_timeout_seconds=1000.0,
                max_concurrent=4,
                max_children=2,
            ),
        )
        assert result.stop is WalkStop.NO_ELIGIBLE_CHILD
        assert len(dispatcher.started) == 2
        assert "max-children" in result.detail


class TestBackpressure:
    """A full machine is a queue, not a verdict about this epic."""

    def test_a_concurrency_refusal_is_waited_out_rather_than_stopping_the_walk(
        self, manager: TaskManager, project: Project
    ) -> None:
        from agentjobs.dispatch.guards import ConcurrencyLimitError

        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")

        class Contended(Dispatcher):
            """Refuses the second child once, as a busy machine would."""

            def __init__(self, manager: TaskManager) -> None:
                super().__init__(manager)
                self.refusals = 0

            def __call__(self, **kwargs):
                if kwargs["request"].task_id == second and self.refusals == 0:
                    self.refusals += 1
                    raise ConcurrencyLimitError("This machine allows 1 concurrent run(s).")
                return super().__call__(**kwargs)

        dispatcher = Contended(manager)
        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=dispatcher,
            script={first: ["wait", "wait", "complete"], second: ["complete"]},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0, max_concurrent=2),
        )
        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.refusals == 1
        assert sorted(dispatcher.started) == sorted([first, second])

    def test_a_permanently_full_machine_stops_the_walk_rather_than_spinning(
        self, manager: TaskManager, project: Project
    ) -> None:
        from agentjobs.dispatch.guards import ConcurrencyLimitError

        parent_id = make_parent(manager)
        make_child(manager, parent_id, "Only")

        class AlwaysFull(Dispatcher):
            def __call__(self, **kwargs):
                raise ConcurrencyLimitError("This machine allows 1 concurrent run(s).")

        result = drive(
            manager,
            project,
            parent_id,
            dispatcher=AlwaysFull(manager),
            script={},
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=5.0, max_concurrent=2),
        )
        assert result.stop is WalkStop.COULD_NOT_START_CHILD
        assert "max_concurrent_runs" in result.detail


class TestFrontierOrder:
    """The graph decides eligibility, the queue decides order, out-degree breaks ties."""

    def test_the_queue_orders_the_frontier(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        assert [task.id for task in frontier(manager, parent_id)] == [first, second]
        manager.move(second, actor="Jeff Posey", top=True)
        assert [task.id for task in frontier(manager, parent_id)] == [second, first]

    def test_a_blocked_child_is_not_on_the_frontier(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        gate = make_child(manager, parent_id, "Gate")
        blocked = make_child(manager, parent_id, "Blocked")
        manager.update_task(blocked, actor="claude", dependencies=[{"task": gate, "type": "needs"}])
        assert [task.id for task in frontier(manager, parent_id)] == [gate]

    def test_a_child_already_in_flight_is_excluded_by_name(self, manager: TaskManager) -> None:
        parent_id = make_parent(manager)
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")
        assert [task.id for task in frontier(manager, parent_id, exclude=(first,))] == [second]

    def test_a_sibling_outside_this_epic_is_never_on_the_frontier(
        self, manager: TaskManager
    ) -> None:
        parent_id = make_parent(manager)
        mine = make_child(manager, parent_id, "Mine")
        other = manager.create_task(
            title="Unrelated",
            category="general",
            summary="Different epic.",
            description="Nothing to do with this one.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.move(other.id, actor="Jeff Posey", top=True)
        assert [task.id for task in frontier(manager, parent_id)] == [mine]


class TestAResumedChildIsNotSilentlyAuto:
    """task-375 ac-3, the task-358 shape run through the whole guard chain.

    The epic is dispatched ``autonomous``; its child already has a conversation that last
    ran at ``auto``. Before task-375 the walk resumed that conversation with no posture
    clause at all, recorded ``autonomous`` on the child, and the child -- correctly --
    obeyed the only clause it had ever been told and handed off for review.
    """

    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        machine = tmp_path / "home"
        machine.mkdir()
        return machine

    @pytest.fixture
    def cli(self, tmp_path: Path) -> Path:
        from test_dispatch_wake import FAKE_CLI

        script = tmp_path / "fakecli.py"
        script.write_text(__import__("textwrap").dedent(FAKE_CLI), encoding="utf-8")
        (tmp_path / "sessions.json").write_text("[]", encoding="utf-8")
        return script

    @pytest.fixture
    def configured(self, home: Path, project: Project, cli: Path) -> Path:
        config = {
            "version": 1,
            "enabled": True,
            "runners": {
                "fake": {
                    "argv": [
                        __import__("sys").executable,
                        str(cli),
                        "--bg",
                        "--remote-control",
                        "{prompt}",
                    ],
                    "actor": "claude",
                    "mode": "session",
                }
            },
            "projects": {
                "sandbox": {
                    "enabled": True,
                    "runner": "fake",
                    "require_clean_tree": False,
                    "merge_mode": "review",
                    "allow_automerge": True,
                }
            },
        }
        path = home / "dispatch.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    def seed_previous_session(self, home: Path, cli: Path, child_id: str, merge_mode: str) -> None:
        from agentjobs.dispatch.runner import RunDirectory
        from test_dispatch_wake import set_sessions, stopped_row

        RunDirectory.create(
            home,
            "run_8ad284df",
            {
                "run_id": "run_8ad284df",
                "task_id": child_id,
                "project_id": "sandbox",
                "mode": "session",
                "driver": "claude",
                "merge_mode": merge_mode,
                "merge_mode_source": "project",
                "status": "finished",
                "session_id": "c3b3a806",
                "started_at": "2026-09-07T01:00:00+00:00",
            },
        )
        set_sessions(cli.parent, [stopped_row("c3b3a806", "c3b3a806-full-uuid")])

    def start_child(self, manager: TaskManager, project: Project, home: Path, child_id: str):
        dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(
                task_id=child_id, trigger=DispatchTrigger.CHILD, on_behalf_of_parent=True
            ),
            home=home,
            api_base="http://127.0.0.1:8765",
        )
        task = manager.get_task(child_id)
        assert task is not None
        entry = [e for e in task.log if e.type is LogEntryType.DISPATCH][-1]
        return entry

    def test_the_child_gets_a_fresh_session_told_autonomous(
        self, manager: TaskManager, project: Project, home: Path, cli: Path, configured: Path
    ) -> None:
        from test_dispatch_wake import ran_argv, ran_stdin

        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        self.seed_previous_session(home, cli, child_id, merge_mode="review")

        entry = self.start_child(manager, project, home, child_id)

        argv = ran_argv(cli.parent)
        assert "--resume" not in argv, "resumed a session that had only been told auto"
        assert ran_stdin(cli.parent) == ""
        assert any("releases the merge gate" in element for element in argv)
        data = dict(entry.data or {})
        assert data["merge_mode"] == "automerge"
        assert data["merge_mode_source"] == "epic"
        assert data["delivery"]["merge_mode_delivered"] is True
        assert "merge mode `review`" in data["delivery"]["resume_refused"]
        assert "Resumed" not in (entry.body or "")

    def test_a_child_session_already_at_autonomous_is_resumed_and_told_again(
        self, manager: TaskManager, project: Project, home: Path, cli: Path, configured: Path
    ) -> None:
        from test_dispatch_wake import ran_argv, ran_stdin

        parent_id = make_parent(
            manager, merge_mode=MergeMode.AUTOMERGE, merge_mode_source="dispatch"
        )
        child_id = make_child(manager, parent_id, "First")
        self.seed_previous_session(home, cli, child_id, merge_mode="automerge")

        entry = self.start_child(manager, project, home, child_id)

        assert "--resume" in ran_argv(cli.parent)
        assert "releases the merge gate" in ran_stdin(cli.parent)
        data = dict(entry.data or {})
        assert data["delivery"]["channel"] == "stdin"
        assert data["delivery"]["merge_mode_delivered"] is True
