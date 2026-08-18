"""Tests for the dispatch and dispatch_result log entry types.

These entries are the durable half of a dispatch: the run directory under
``~/.agentjobs/runs/`` is machine-local and disposable, the log entry is what git keeps.
So the tests are about the entry being trustworthy -- only the manager can write one, its
payload is validated rather than merely documented, and the derived count cannot drift
from the entries it counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentjobs.actors import DISPATCHER, UnknownActorError, load_actors, reserved_actors
from agentjobs.actors import validate_actor
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    MANAGER_WRITTEN_LOG_TYPES,
    DispatchMode,
    DispatchOutcome,
    DispatchPosture,
    DispatchTrigger,
    LogEntry,
    LogEntryType,
    Task,
    utcnow,
)
from agentjobs.storage import TaskStorage


def dispatch_payload(**overrides: object) -> dict:
    """A complete dispatch payload, the way the dispatcher would write it."""
    payload = {
        "run_id": "run_a1b2c3d4",
        "agent": "claude",
        "runner": "claude",
        "mode": "session",
        "posture": "supervised",
        "trigger": "manual",
        "caused_by": 6,
        "argv": ["claude", "--bg", "--remote-control", "-p", "read the record"],
        "cwd": "C:/projects/agentjobs",
        "git_head": "4887b74",
    }
    payload.update(overrides)
    return payload


def round_trip(task: Task, tmp_path: Path) -> Task:
    """Save and reload through storage -- the path a real task file actually takes."""
    storage = TaskStorage(tmp_path)
    storage.save_task(task)
    reloaded = storage.load_task(task.id)
    assert reloaded is not None
    return reloaded


def entry(entry_id: int, entry_type: LogEntryType, **kwargs: object) -> LogEntry:
    """One log entry, defaulting everything the test does not care about."""
    return LogEntry(
        id=entry_id,
        ts=kwargs.pop("ts", utcnow()),  # type: ignore[arg-type]
        actor=kwargs.pop("actor", "claude"),  # type: ignore[arg-type]
        type=entry_type,
        **kwargs,  # type: ignore[arg-type]
    )


def make_task(**overrides: object) -> Task:
    """A minimal valid task."""
    fields: dict = {
        "id": "task-999-example",
        "title": "Example",
        "created": utcnow(),
        "updated": utcnow(),
        "category": "infrastructure",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "ball_prompt": "Work it.",
        "spec": {"summary": "A task.", "description": "Do the thing."},
    }
    fields.update(overrides)
    return Task.model_validate(fields)


class TestEntryTypes:
    def test_both_types_exist_and_are_manager_written(self) -> None:
        assert LogEntryType("dispatch") is LogEntryType.DISPATCH
        assert LogEntryType("dispatch_result") is LogEntryType.DISPATCH_RESULT
        assert LogEntryType.DISPATCH in MANAGER_WRITTEN_LOG_TYPES
        assert LogEntryType.DISPATCH_RESULT in MANAGER_WRITTEN_LOG_TYPES

    def test_a_dispatch_entry_round_trips_through_yaml(self, tmp_path: Path) -> None:
        task = make_task(
            log=[
                {
                    "id": 1,
                    "ts": "2026-08-18T14:02:11Z",
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "body": "Dispatched claude to work this task.",
                    "data": dispatch_payload(),
                }
            ]
        )

        reloaded = round_trip(task, tmp_path)

        assert reloaded.log[0].type is LogEntryType.DISPATCH
        assert reloaded.log[0].data["argv"][0] == "claude"
        assert reloaded.log[0].data["git_head"] == "4887b74"

    def test_a_dispatch_result_entry_round_trips_and_threads_to_its_dispatch(
        self, tmp_path: Path
    ) -> None:
        task = make_task(
            log=[
                {
                    "id": 1,
                    "ts": "2026-08-18T14:02:11Z",
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "data": dispatch_payload(),
                },
                {
                    "id": 2,
                    "ts": "2026-08-18T14:19:40Z",
                    "actor": "claude",
                    "type": "dispatch_result",
                    "re": 1,
                    "data": {
                        "run_id": "run_a1b2c3d4",
                        "outcome": "completed",
                        "exit_code": 0,
                        "duration_seconds": 1049,
                        "log_path": "~/.agentjobs/runs/run_a1b2c3d4/",
                    },
                },
            ]
        )

        reloaded = round_trip(task, tmp_path)

        assert reloaded.log[1].re == 1
        assert reloaded.log[1].data["outcome"] == "completed"

    def test_a_session_result_may_omit_the_exit_code(self) -> None:
        """A session reports no exit code, so requiring one would make it unwritable."""
        result = entry(
            1,
            LogEntryType.DISPATCH_RESULT,
            data={"run_id": "run_x", "outcome": "finished_without_handoff"},
        )

        assert result.data["outcome"] == "finished_without_handoff"

    def test_a_dispatch_entry_with_an_incomplete_payload_is_rejected(self) -> None:
        """An entry that cannot say what ran is worse than no entry: it looks like evidence."""
        with pytest.raises(ValidationError):
            entry(1, LogEntryType.DISPATCH, data={"run_id": "run_x"})

    def test_a_dispatch_entry_with_an_unknown_payload_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            entry(1, LogEntryType.DISPATCH, data=dispatch_payload(exit_code=0))

    def test_an_unknown_outcome_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            entry(1, LogEntryType.DISPATCH_RESULT, data={"run_id": "r", "outcome": "fine"})

    def test_the_operation_marker_is_not_treated_as_payload(self) -> None:
        """Every manager-written entry carries one; it is infrastructure, not payload."""
        payload = dispatch_payload()
        payload["operation"] = {"id": "abc", "kind": "dispatch", "fingerprint": "x"}

        appended = entry(1, LogEntryType.DISPATCH, data=payload)

        assert appended.data["operation"]["kind"] == "dispatch"

    def test_other_entry_types_still_accept_arbitrary_data(self) -> None:
        note = entry(1, LogEntryType.NOTE, data={"anything": ["at", "all"]})

        assert note.data["anything"] == ["at", "all"]


class TestReservedDispatcherActor:
    def test_dispatcher_is_accepted_by_a_project_that_defines_other_actors(self) -> None:
        config = {
            "actors": [
                {"name": "Jeff Posey", "kind": "human"},
                {"name": "claude", "kind": "agent"},
            ]
        }

        assert validate_actor(config, DISPATCHER) == DISPATCHER
        with pytest.raises(UnknownActorError):
            validate_actor(config, "someone-else")

    def test_dispatcher_is_not_offered_as_a_configured_actor(self) -> None:
        """A reserved id is not a choice: nobody may act as it, so no picker lists it."""
        config = {"actors": [{"name": "Jeff Posey", "kind": "human"}]}

        assert DISPATCHER not in load_actors(config)
        assert reserved_actors()[DISPATCHER].kind == "agent"

    def test_reserving_it_does_not_break_an_unconfigured_project(self) -> None:
        """Merging reserved ids into the vocabulary would have silently done exactly this."""
        assert validate_actor({}, "anybody-at-all") == "anybody-at-all"


class TestDerivedDispatchCount:
    def test_a_task_with_three_dispatch_entries_reports_three(self) -> None:
        log = []
        for index in range(1, 4):
            log.append(
                {
                    "id": index,
                    "ts": utcnow(),
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "data": dispatch_payload(run_id=f"run_{index}"),
                }
            )
        log.append({"id": 4, "ts": utcnow(), "actor": "claude", "type": "note", "body": "hi"})

        assert make_task(log=log).dispatch_count == 3

    def test_a_task_never_dispatched_reports_zero(self) -> None:
        assert make_task().dispatch_count == 0

    def test_no_stored_counter_field_exists(self) -> None:
        """Derived means derived: a file carrying a count is rejected, not round-tripped."""
        assert "dispatch_count" not in Task.model_fields
        assert "dispatch_count" not in make_task().model_dump(mode="json", by_alias=True)
        with pytest.raises(ValidationError):
            make_task(dispatch_count=3)

    def test_dispatches_since_counts_only_recent_ones(self) -> None:
        now = utcnow()
        task = make_task(
            log=[
                {
                    "id": 1,
                    "ts": now - timedelta(days=2),
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "data": dispatch_payload(run_id="old"),
                },
                {
                    "id": 2,
                    "ts": now - timedelta(hours=1),
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "data": dispatch_payload(run_id="recent"),
                },
            ]
        )

        assert task.dispatch_count == 2
        assert task.dispatches_since(now - timedelta(days=1)) == 1

    def test_dispatches_since_tolerates_a_naive_cutoff(self) -> None:
        """A cap that counts nothing because two datetimes were not comparable is worse."""
        now = utcnow()
        task = make_task(
            log=[
                {
                    "id": 1,
                    "ts": now,
                    "actor": "Jeff Posey",
                    "type": "dispatch",
                    "data": dispatch_payload(),
                }
            ]
        )

        naive_cutoff = (now - timedelta(hours=1)).replace(tzinfo=None)

        assert task.dispatches_since(naive_cutoff) == 1


class TestManagerWritesThem:
    def manager(self, tmp_path: Path) -> TaskManager:
        return TaskManager(TaskStorage(tmp_path))

    def seed(self, tmp_path: Path) -> tuple[TaskManager, str]:
        manager = self.manager(tmp_path)
        task = manager.create_task(
            title="Dispatchable",
            category="infrastructure",
            summary="A task to dispatch.",
            description="Do the thing.",
        )
        return manager, task.id

    def test_a_caller_cannot_post_a_dispatch_entry(self, tmp_path: Path) -> None:
        manager, task_id = self.seed(tmp_path)

        for forged in (LogEntryType.DISPATCH, LogEntryType.DISPATCH_RESULT):
            with pytest.raises(ValueError) as caught:
                manager.add_log_entry(task_id, actor="claude", type=forged, data=dispatch_payload())
            assert "not written directly" in str(caught.value)

    def test_record_dispatch_appends_the_entry(self, tmp_path: Path) -> None:
        manager, task_id = self.seed(tmp_path)

        task = manager.record_dispatch(
            task_id,
            actor="Jeff Posey",
            run_id="run_a1b2c3d4",
            agent="claude",
            runner="claude",
            mode=DispatchMode.SESSION,
            posture=DispatchPosture.SUPERVISED,
            trigger=DispatchTrigger.MANUAL,
            caused_by=1,
            argv=["claude", "--bg", "-p", "read the record"],
            cwd=str(tmp_path),
            git_head="4887b74",
        )

        appended = task.log[-1]
        assert appended.type is LogEntryType.DISPATCH
        assert appended.actor == "Jeff Posey"
        assert appended.data["mode"] == "session"
        assert appended.data["argv"][0] == "claude"
        assert task.dispatch_count == 1

    def test_record_dispatch_result_threads_back_and_survives_a_reload(
        self, tmp_path: Path
    ) -> None:
        manager, task_id = self.seed(tmp_path)
        dispatched = manager.record_dispatch(
            task_id,
            actor="Jeff Posey",
            run_id="run_a1b2c3d4",
            agent="claude",
            runner="claude",
            mode=DispatchMode.BATCH,
            posture=DispatchPosture.READ_ONLY,
            trigger=DispatchTrigger.MANUAL,
            caused_by=1,
            argv=["claude", "-p", "review this"],
            cwd=str(tmp_path),
            git_head="4887b74",
        )
        dispatch_id = dispatched.log[-1].id

        manager.record_dispatch_result(
            task_id,
            actor=DISPATCHER,
            run_id="run_a1b2c3d4",
            outcome=DispatchOutcome.FINISHED_WITHOUT_HANDOFF,
            re=dispatch_id,
            exit_code=0,
            duration_seconds=1049.0,
            log_path="~/.agentjobs/runs/run_a1b2c3d4/",
        )

        reloaded = TaskStorage(tmp_path).load_task(task_id)
        assert reloaded is not None
        result = reloaded.log[-1]
        assert result.type is LogEntryType.DISPATCH_RESULT
        assert result.re == dispatch_id
        assert result.actor == DISPATCHER
        assert result.data["outcome"] == "finished_without_handoff"

    def test_an_absent_optional_is_not_written_as_null(self, tmp_path: Path) -> None:
        """A session has no exit code; the key should be absent, not present and empty."""
        manager, task_id = self.seed(tmp_path)

        task = manager.record_dispatch_result(
            task_id,
            actor=DISPATCHER,
            run_id="run_x",
            outcome=DispatchOutcome.CANCELLED,
        )

        assert "exit_code" not in task.log[-1].data
        assert "session_id" not in task.log[-1].data

    def test_a_replayed_operation_does_not_dispatch_twice(self, tmp_path: Path) -> None:
        manager, task_id = self.seed(tmp_path)
        arguments = dict(
            actor="Jeff Posey",
            run_id="run_a1b2c3d4",
            agent="claude",
            runner="claude",
            mode=DispatchMode.SESSION,
            posture=DispatchPosture.SUPERVISED,
            trigger=DispatchTrigger.MANUAL,
            caused_by=1,
            argv=["claude", "-p", "go"],
            cwd=str(tmp_path),
            git_head="4887b74",
            operation_id="11111111-2222-3333-4444-555555555555",
        )

        manager.record_dispatch(task_id, **arguments)  # type: ignore[arg-type]
        task = manager.record_dispatch(task_id, **arguments)  # type: ignore[arg-type]

        assert task.dispatch_count == 1


class TestNothingElseChanged:
    def test_existing_task_files_still_load(self) -> None:
        """Additive means additive: the real corpus is the regression test."""
        tasks_dir = Path(__file__).resolve().parents[1] / "tasks" / "agentjobs"
        storage = TaskStorage(tasks_dir)

        loaded = storage.list_tasks()

        assert len(loaded) > 100

    def test_the_authored_type_list_the_mcp_offers_excludes_both(self) -> None:
        from agentjobs.mcp.mutation_tools import AUTHORED_LOG_TYPES

        assert "dispatch" not in AUTHORED_LOG_TYPES
        assert "dispatch_result" not in AUTHORED_LOG_TYPES
        assert "transition" not in AUTHORED_LOG_TYPES
        assert "note" in AUTHORED_LOG_TYPES


def test_utc_is_what_the_model_normalises_to() -> None:
    """Guards the assumption dispatches_since makes about naive timestamps."""
    assert utcnow().tzinfo is timezone.utc
    assert isinstance(utcnow(), datetime)
