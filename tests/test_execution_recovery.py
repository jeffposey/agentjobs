"""Bounded recovery in the journal and the reducer (task-416).

The store half is driven against real SQLite files; the reducer half is pure. What the
controller does with these facts -- performing the proposals through the production
dispatch path -- is ``tests/test_execution_controller.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, TypeVar

import pytest

from agentjobs.execution import reducer
from agentjobs.execution.coordinator import inspect_execution
from agentjobs.execution.errors import OwnershipConflict, StaleOwner
from agentjobs.execution.reducer import (
    DEFAULT_RETRY_POLICY,
    WORKFLOW_VERSION,
    Event,
    next_intents,
    replay,
)
from agentjobs.execution.store import (
    _SCHEMA,
    CONTROLLED_BY_CONTROLLER,
    SCHEMA_REVISION,
    SCHEMA_VERSION,
    ExecutionStore,
)

_T = TypeVar("_T")


def must(value: Optional[_T]) -> _T:
    assert value is not None
    return value


ENVELOPE = {"runner": "claude-opus-5", "posture": "auto", "retry_policy": dict(DEFAULT_RETRY_POLICY)}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Iterator[ExecutionStore]:
    journal = ExecutionStore(tmp_path / "execution.db", clock=clock)
    yield journal
    journal.close()


def admit(store: ExecutionStore, run: str, **kwargs: Any):
    kwargs.setdefault("capacity", 3)
    kwargs.setdefault("envelope", ENVELOPE)
    kwargs.setdefault("workflow_version", WORKFLOW_VERSION)
    return store.admit(project_id="alpha", task_id="task-001", run_id=run, **kwargs)


# ----- the schema revision ---------------------------------------------------------


class TestTheAdditiveRevision:
    def build_revision_one(self, path: Path) -> None:
        """A journal exactly as the previous build created it."""
        raw = sqlite3.connect(str(path))
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                raw.execute(statement)
        raw.execute("INSERT INTO store_meta(key, value) VALUES ('created_at', 'then')")
        raw.execute(
            "INSERT INTO run_attempt(run_id, project_id, task_id, takes_slot, holder, state, "
            "admitted_at) VALUES ('run_old', 'alpha', 'task-009', 1, 'old', 'live', 'then')"
        )
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        raw.commit()
        raw.close()

    def test_an_old_file_is_upgraded_in_place_without_changing_its_version(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "execution.db"
        self.build_revision_one(path)
        upgraded = ExecutionStore(path)
        try:
            assert upgraded.compatible and upgraded.schema_version == SCHEMA_VERSION
            assert must(upgraded.attempt("run_old")).operation_id is None
        finally:
            upgraded.close()
        raw = sqlite3.connect(str(path))
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}
            assert {"timer", "supervision", "supervision_child"} <= tables
            revision = raw.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_revision'"
            ).fetchone()[0]
            assert int(revision) == SCHEMA_REVISION
        finally:
            raw.close()

    def test_the_previous_builds_writes_still_succeed_on_the_upgraded_file(
        self, tmp_path: Path
    ) -> None:
        """What keeps a walk started before an upgrade dispatching its children after it."""
        path = tmp_path / "execution.db"
        ExecutionStore(path).close()
        raw = sqlite3.connect(str(path))
        try:
            # The previous build's exact insert shapes, with none of the new columns.
            raw.execute(
                "INSERT INTO execution(execution_id, project_id, task_id, workflow_version, "
                "envelope_json, owner_mode, state, terminal, created_at, updated_at) "
                "VALUES ('exe_old', 'alpha', 'task-002', 1, '{}', 'durable', 'accepted', 0, 'n', 'n')"
            )
            raw.execute(
                "INSERT INTO run_attempt(run_id, execution_id, project_id, task_id, mode, "
                "takes_slot, holder, holder_pid, state, reservation_json, admitted_at) "
                "VALUES ('run_x', 'exe_old', 'alpha', 'task-002', 'session', 1, 'h', 1, "
                "'admitted', '{}', 'n')"
            )
            raw.commit()
        finally:
            raw.close()
        reopened = ExecutionStore(path)
        try:
            execution = must(reopened.execution("exe_old"))
            assert execution.controlled_by is None and not execution.controller_driven
        finally:
            reopened.close()

    def test_opening_an_up_to_date_file_takes_no_write_lock(self, tmp_path: Path) -> None:
        path = tmp_path / "execution.db"
        ExecutionStore(path).close()
        holder = sqlite3.connect(str(path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            ExecutionStore(path, busy_timeout_ms=100).close()
        finally:
            holder.execute("ROLLBACK")
            holder.close()


# ----- an execution between attempts -------------------------------------------------


class TestRetryOwed:
    def test_a_controller_driven_execution_survives_a_retryable_ending(
        self, store: ExecutionStore
    ) -> None:
        first = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        store.mark_launched("run_a", session_id="s-1")
        conclusion = store.conclude(
            "run_a",
            outcome="interrupted",
            status="failed",
            concluded_by="poller",
            retry_owed=True,
            failure_class=reducer.WORKER_GONE,
        )
        assert conclusion.won and not conclusion.attempt.is_live
        execution = must(store.execution(must(first.execution_id)))
        assert not execution.terminal and execution.state == "retry_wait"
        state, _ = inspect_execution(store, execution.execution_id)
        assert state.state == reducer.S_RETRY_WAIT and state.failure_class == "worker_gone"
        assert store.live_attempts() == [], "the ownership and the slot are released"

    def test_an_execution_the_legacy_poller_follows_closes_as_before(
        self, store: ExecutionStore
    ) -> None:
        first = admit(store, "run_a")
        store.conclude(
            "run_a", outcome="interrupted", status="failed", concluded_by="p", retry_owed=True
        )
        assert must(store.execution(must(first.execution_id))).terminal

    def test_a_cancellation_is_never_owed_a_retry(self, store: ExecutionStore) -> None:
        first = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        store.request_cancel("run_a", requester="Jeff Posey", source="gui")
        store.conclude(
            "run_a", outcome="interrupted", status="failed", concluded_by="p", retry_owed=True
        )
        assert must(store.execution(must(first.execution_id))).terminal

    def test_only_an_admission_that_names_the_execution_continues_it(
        self, store: ExecutionStore
    ) -> None:
        first = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        eid = must(first.execution_id)
        store.conclude(
            "run_a", outcome="interrupted", status="failed", concluded_by="p", retry_owed=True
        )
        second = admit(store, "run_b", continues_execution_id=eid)
        assert second.execution_id == eid
        assert [a.run_id for a in store.attempts_for(eid)] == ["run_a", "run_b"]
        state, _ = inspect_execution(store, eid)
        assert state.attempt_no == 2 and state.state == reducer.S_LAUNCHING

    def test_any_other_admission_supersedes_the_waiting_execution(
        self, store: ExecutionStore
    ) -> None:
        first = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        eid = must(first.execution_id)
        store.conclude(
            "run_a", outcome="interrupted", status="failed", concluded_by="p", retry_owed=True
        )
        # A person claiming the task interactively: no envelope, no continuation.
        store.admit(
            project_id="alpha", task_id="task-001", run_id="run_person", capacity=3, takes_slot=False
        )
        old = must(store.execution(eid))
        assert old.terminal
        assert [e.kind for e in store.events(eid)][-1] == "closed"
        store.conclude("run_person", outcome="session_ended", status="finished", concluded_by="x")
        with pytest.raises(OwnershipConflict):
            admit(store, "run_c", continues_execution_id=eid)

    def test_admission_is_idempotent_on_its_operation_id(self, store: ExecutionStore) -> None:
        first = admit(store, "run_a", attempt_operation_id="walk_1:task-001:1")
        again = admit(store, "run_other", attempt_operation_id="walk_1:task-001:1")
        assert again.run_id == first.run_id == "run_a"
        assert must(store.attempt_by_operation("walk_1:task-001:1")).run_id == "run_a"


# ----- timers ------------------------------------------------------------------------


class TestTimers:
    def test_a_due_timer_fires_exactly_once_across_processes_opening_the_file(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "execution.db"
        one = ExecutionStore(path, clock=clock)
        two = ExecutionStore(path, clock=clock)
        try:
            attempt = admit(one, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
            eid = must(attempt.execution_id)
            one.set_timer(
                "t1", owner=f"execution:{eid}", kind="retry", execution_id=eid,
                due_at=clock.now + timedelta(seconds=60),
            )
            assert one.due_timers() == [] and two.due_timers() == []
            assert must(one.execution(eid)).next_due_at is not None
            clock.advance(61)
            assert [t.timer_id for t in two.due_timers()] == ["t1"]
            assert [one.fire_timer("t1"), two.fire_timer("t1"), one.fire_timer("t1")] == [
                True, False, False
            ]
            fired = [e for e in one.events(eid) if e.kind == "timer_fired"]
            assert len(fired) == 1
            assert must(one.execution(eid)).next_due_at is None
        finally:
            one.close()
            two.close()

    def test_waking_from_a_long_sleep_fires_each_overdue_timer_once(
        self, store: ExecutionStore, clock: Clock
    ) -> None:
        attempt = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        eid = must(attempt.execution_id)
        store.set_timer("t1", owner="x", kind="retry", execution_id=eid, due_at=clock.now)
        clock.advance(6 * 3600)  # the machine slept through it
        due = store.due_timers()
        assert len(due) == 1
        assert sum(store.fire_timer(t.timer_id) for t in due) == 1
        assert store.due_timers() == []

    def test_a_repeated_set_is_the_same_timer_and_a_clock_regression_fires_nothing(
        self, store: ExecutionStore, clock: Clock
    ) -> None:
        store.set_timer("t1", owner="walk:w", kind="poll", due_at=clock.now + timedelta(seconds=30))
        again = store.set_timer(
            "t1", owner="walk:w", kind="poll", due_at=clock.now + timedelta(seconds=999)
        )
        assert again.due_at == must(store.timer("t1")).due_at
        clock.advance(-3600)
        assert store.due_timers() == []

    def test_concluding_an_execution_cancels_its_timers(
        self, store: ExecutionStore, clock: Clock
    ) -> None:
        attempt = admit(store, "run_a", controlled_by=CONTROLLED_BY_CONTROLLER)
        eid = must(attempt.execution_id)
        store.set_timer("t1", owner="x", kind="retry", execution_id=eid, due_at=clock.now)
        store.conclude("run_a", outcome="completed", status="finished", concluded_by="p")
        assert must(store.timer("t1")).cancelled_at is not None
        assert store.due_timers() == []


# ----- the reducer's recovery path ---------------------------------------------------


def history(*extra: Event) -> List[Event]:
    base = [
        Event(1, "accepted", {"envelope": ENVELOPE, "workflow_version": WORKFLOW_VERSION}),
        Event(2, "admitted", {"run_id": "run_a", "attempt_no": 1}),
        Event(3, "launched", {"run_id": "run_a", "session_id": "s-1"}),
        Event(
            4,
            "concluded",
            {"run_id": "run_a", "outcome": "interrupted", "retry_owed": True,
             "failure_class": "worker_gone"},
        ),
    ]
    return base + [Event(5 + index, e.kind, e.payload) for index, e in enumerate(extra)]


def kinds(events: List[Event]) -> List[str]:
    return [intent.kind for intent in next_intents(replay("exe", events))]


class TestTheRecoveryReducer:
    def test_schedule_then_observe_then_relaunch_each_only_after_its_fact(self) -> None:
        assert kinds(history()) == ["schedule_retry"]
        scheduled = next_intents(replay("exe", history()))[0]
        timer = scheduled.input["timer_id"]
        assert kinds(history(Event(0, "timer_set", {"timer_id": timer}))) == []
        fired = history(
            Event(0, "timer_set", {"timer_id": timer}),
            Event(0, "timer_fired", {"timer_id": timer}),
        )
        assert kinds(fired) == ["observe_policy"]
        permitted = fired + [Event(7, "policy_observed", {"round": 1, "permitted": True})]
        relaunch = next_intents(replay("exe", permitted))
        assert [i.kind for i in relaunch] == ["relaunch"]
        assert relaunch[0].input["continues_execution_id"] == "exe"
        assert next_intents(replay("exe", permitted)) == relaunch, "stable across replays"

    def test_a_policy_that_says_not_now_waits_another_round_and_never_says_never(self) -> None:
        timer = "exe:retry:2:1"
        blocked = history(
            Event(0, "timer_set", {"timer_id": timer}),
            Event(0, "timer_fired", {"timer_id": timer}),
            Event(0, "policy_observed", {"round": 1, "permitted": False, "class": "policy_wait"}),
        )
        [intent] = next_intents(replay("exe", blocked))
        assert intent.kind == "schedule_retry" and intent.input["round"] == 2

    def test_a_revoked_grant_escalates_once_and_then_proposes_nothing(self) -> None:
        timer = "exe:retry:2:1"
        revoked = history(
            Event(0, "timer_set", {"timer_id": timer}),
            Event(0, "timer_fired", {"timer_id": timer}),
            Event(0, "policy_observed", {"round": 1, "permitted": False, "class": "budget_exhausted"}),
        )
        [intent] = next_intents(replay("exe", revoked))
        assert intent.kind == "escalate" and intent.input["failure_class"] == "budget_exhausted"
        done = revoked + [
            Event(8, "activity_result", {"kind": "escalate", "state": "applied",
                                         "activity_id": intent.activity_id})
        ]
        assert next_intents(replay("exe", done)) == ()

    @pytest.mark.parametrize("cause", ["worker_failed", "timed_out", "spec_gap", "auth_unavailable"])
    def test_a_code_or_work_failure_is_never_retried(self, cause: str) -> None:
        events = history()
        events[3] = Event(4, "concluded", {"run_id": "run_a", "outcome": "failed",
                                           "retry_owed": True, "failure_class": cause})
        assert kinds(events) == ["escalate"]

    def test_the_attempt_bound_is_read_off_the_envelope(self) -> None:
        events = history()
        events[1] = Event(2, "admitted", {"run_id": "run_a", "attempt_no": 3})
        [intent] = next_intents(replay("exe", events))
        assert intent.input["failure_class"] == "attempts_exhausted"

    def test_an_envelope_that_granted_no_policy_gets_no_retry(self) -> None:
        events = history()
        events[0] = Event(1, "accepted", {"envelope": {"runner": "r"}, "workflow_version": 2})
        assert kinds(events) == ["escalate"]

    def test_an_unknown_launch_parks_and_escalates_without_releasing_ownership(self) -> None:
        events = [
            Event(1, "accepted", {"envelope": ENVELOPE, "workflow_version": 2}),
            Event(2, "admitted", {"run_id": "run_a", "attempt_no": 1}),
        ]
        assert kinds(events) == ["launch_reconcile"]
        parked = events + [
            Event(3, "activity_result", {"kind": "launch_reconcile", "state": "unknown",
                                         "error_class": "effect_unknown"})
        ]
        state = replay("exe", parked)
        assert state.state == reducer.S_PARKED and state.attempt_live
        [intent] = next_intents(state)
        assert intent.kind == "escalate" and intent.input["close"] is False

    def test_delays_are_deterministic_bounded_and_grow_with_attempts(self) -> None:
        one = replay("exe", history())
        assert reducer.retry_delay_seconds(one) == reducer.retry_delay_seconds(replay("exe", history()))
        assert 60 <= reducer.retry_delay_seconds(one) <= 75
        later = history()
        later[1] = Event(2, "admitted", {"run_id": "run_a", "attempt_no": 2})
        assert 120 <= reducer.retry_delay_seconds(replay("exe", later)) <= 135

    def test_a_version_one_history_still_replays_as_it_did(self) -> None:
        events = [
            Event(1, "accepted", {"envelope": {"runner": "r"}, "workflow_version": 1}),
            Event(2, "admitted", {"run_id": "run_a", "attempt_no": 1}),
        ]
        assert kinds(events) == ["launch"]


# ----- durable supervision -----------------------------------------------------------


class TestSupervisionRecords:
    def open(self, store: ExecutionStore, **kwargs: Any):
        kwargs.setdefault("holder", "me")
        kwargs.setdefault("holder_pid", 1)
        return store.open_walk(
            project_id="alpha", parent_task_id="task-100", authority_entry=25,
            authority_actor="Jeff Posey", settings={"max_concurrent": 2}, **kwargs,
        )

    def test_the_same_authority_resumes_the_same_walk_with_a_new_epoch(
        self, store: ExecutionStore
    ) -> None:
        walk, resumed = self.open(store)
        assert not resumed
        again, resumed = self.open(store, holder="restarted", holder_alive=lambda _pid: False)
        assert resumed and again.walk_id == walk.walk_id and again.epoch == walk.epoch + 1
        with pytest.raises(StaleOwner):
            store.update_walk(walk.walk_id, epoch=walk.epoch, detail="the dead one speaks")

    def test_a_live_supervisor_is_never_taken_over(self, store: ExecutionStore) -> None:
        self.open(store)
        with pytest.raises(OwnershipConflict):
            self.open(store, holder="second", holder_alive=lambda _pid: True)

    def test_attempt_reservations_bound_a_child_and_a_refund_returns_one(
        self, store: ExecutionStore
    ) -> None:
        walk, _ = self.open(store)
        child = must(store.reserve_child_attempt(
            walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
            operation_id="op1", limit=2, used_on_record=0,
        ))
        assert child.status == "admitting" and child.attempts_reserved == 1
        with pytest.raises(OwnershipConflict):
            store.reserve_child_attempt(
                walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
                operation_id="op2", limit=2, used_on_record=0,
            )
        store.record_child(walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
                           status="retry_owed", refund=True)
        assert must(store.supervised_child(walk.walk_id, "task-101")).attempts_reserved == 0
        # The log says one attempt happened even though the reservation was refunded.
        second = must(store.reserve_child_attempt(
            walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
            operation_id="op2", limit=2, used_on_record=1,
        ))
        assert second.attempts_reserved == 2
        store.record_child(walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
                           status="retry_owed", landed={"verdict": "died"})
        assert store.reserve_child_attempt(
            walk.walk_id, epoch=walk.epoch, child_task_id="task-101",
            operation_id="op3", limit=2, used_on_record=0,
        ) is None

    def test_grounding_is_sticky_and_refuses_every_further_takeoff(
        self, store: ExecutionStore
    ) -> None:
        walk, _ = self.open(store)
        store.update_walk(walk.walk_id, epoch=walk.epoch, grounding={"stop": "first"})
        store.update_walk(walk.walk_id, epoch=walk.epoch, grounding={"stop": "second"})
        assert must(store.walk(walk.walk_id)).grounding == {"stop": "first"}
        with pytest.raises(OwnershipConflict):
            store.reserve_child_attempt(
                walk.walk_id, epoch=walk.epoch, child_task_id="task-102",
                operation_id="op", limit=2, used_on_record=0,
            )
