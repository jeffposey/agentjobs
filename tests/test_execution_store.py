"""The execution journal's transactional contract (task-264).

Every test here drives the real ``ExecutionStore`` against a real SQLite file. The
assertions that matter are the negative ones: a busy, full, rolled-back or stale write
leaves *nothing* behind, and an attempt is never freed by a transition that did not
commit (durable-4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TypeVar

import pytest

from agentjobs.execution.coordinator import (
    advance_execution,
    deliver,
    flush_outbox,
    import_source_events,
    reconcile_after_restore,
    release_ended_attempts,
)
from agentjobs.execution.errors import (
    ActivityConflict,
    CapacityExhausted,
    HistoryIncompatible,
    OwnerModeConflict,
    OwnershipConflict,
    StaleOwner,
    StorageFailure,
    StoreBusy,
)
from agentjobs.execution.reducer import WORKFLOW_VERSION
from agentjobs.execution.store import (
    Attempt,
    OWNER_DURABLE,
    OWNER_LEGACY,
    SCHEMA_VERSION,
    ExecutionStore,
    OutboxItem,
    SourceEvent,
    restore_snapshot,
)

_T = TypeVar("_T")


def must(value: Optional[_T]) -> _T:
    """The value, asserted present -- a lookup the test has just made true."""
    assert value is not None
    return value


ENVELOPE = {"runner": "claude-opus-5", "posture": "auto"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ExecutionStore]:
    journal = ExecutionStore(tmp_path / "execution.db")
    yield journal
    journal.close()


def admit(store: ExecutionStore, project: str, task: str, run: str, **kwargs: Any) -> Attempt:
    kwargs.setdefault("capacity", 3)
    return store.admit(
        project_id=project,
        task_id=task,
        run_id=run,
        envelope=ENVELOPE,
        workflow_version=WORKFLOW_VERSION,
        **kwargs,
    )


def row_count(path: Path, table: str) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


class TestTheFile:
    def test_it_is_wal_with_full_sync_and_foreign_keys(self, store: ExecutionStore) -> None:
        with store.transaction() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.schema_version == SCHEMA_VERSION

    def test_a_newer_schema_is_readable_and_refuses_every_mutation(self, tmp_path: Path) -> None:
        path = tmp_path / "execution.db"
        first = ExecutionStore(path)
        admit(first, "alpha", "task-001", "run_a")
        first.close()
        raw = sqlite3.connect(str(path))
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        raw.close()

        newer = ExecutionStore(path)
        try:
            assert not newer.compatible
            assert newer.attempt("run_a") is not None, "reads continue"
            with pytest.raises(HistoryIncompatible):
                admit(newer, "alpha", "task-002", "run_b")
            with pytest.raises(HistoryIncompatible):
                newer.conclude("run_a", outcome="completed", status="finished", concluded_by="t")
        finally:
            newer.close()
        assert row_count(path, "run_attempt") == 1


class TestAdmission:
    def test_ownership_slot_and_execution_are_allocated_together(
        self, store: ExecutionStore
    ) -> None:
        attempt = admit(store, "alpha", "task-001", "run_a")
        assert attempt.state == "admitted" and attempt.takes_slot
        execution = must(store.open_execution("alpha", "task-001"))
        assert execution is not None and execution.envelope == ENVELOPE
        assert [event.kind for event in store.events(execution.execution_id)] == [
            "accepted",
            "admitted",
        ]

    def test_one_live_attempt_per_project_task(self, store: ExecutionStore) -> None:
        admit(store, "alpha", "task-001", "run_a")
        with pytest.raises(OwnershipConflict):
            admit(store, "alpha", "task-001", "run_b")

    def test_the_same_task_id_in_another_project_is_a_different_owner(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        other = admit(store, "beta", "task-001", "run_b")
        assert other.project_id == "beta"
        assert {a.run_id for a in store.live_attempts(task_id="task-001")} == {"run_a", "run_b"}
        assert [a.run_id for a in store.live_attempts(project_id="beta")] == ["run_b"]

    def test_the_last_slot_goes_to_one_and_the_refusal_names_the_holders(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a", capacity=2)
        with pytest.raises(CapacityExhausted) as refused:
            admit(store, "beta", "task-009", "run_c", capacity=2, legacy_slot_holders=["run_old"])
        assert refused.value.holders == ("run_a", "run_old")
        assert store.attempt("run_c") is None, "a refused admission writes nothing"

    def test_a_legacy_run_the_journal_already_knows_is_not_counted_twice(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a", capacity=2)
        attempt = admit(
            store, "beta", "task-002", "run_b", capacity=2, legacy_slot_holders=["run_a"]
        )
        assert attempt.run_id == "run_b"

    def test_a_live_pre_journal_run_owns_its_task(self, store: ExecutionStore) -> None:
        with pytest.raises(OwnershipConflict):
            admit(store, "alpha", "task-001", "run_a", legacy_owners=["run_old"])

    def test_an_interactive_attempt_owns_its_task_and_holds_no_slot(
        self, store: ExecutionStore
    ) -> None:
        store.admit(
            project_id="alpha", task_id="task-001", run_id="run_i", capacity=1, takes_slot=False
        )
        admitted = admit(store, "alpha", "task-002", "run_a", capacity=1)
        assert admitted.run_id == "run_a"

    def test_the_hourly_cap_counts_held_reservations_and_not_refunds(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a", hourly_limit=2)
        store.conclude("run_a", outcome="crashed", status="failed", concluded_by="t", refund=True)
        admit(store, "alpha", "task-002", "run_b", hourly_limit=2)
        admit(store, "alpha", "task-003", "run_c", hourly_limit=2)
        with pytest.raises(CapacityExhausted) as refused:
            admit(store, "alpha", "task-004", "run_d", hourly_limit=2)
        assert refused.value.limit == "hourly"


class TestTheTerminalCompareAndSet:
    def test_exactly_one_conclusion_wins_and_the_loser_reads_the_winner(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        first = store.conclude(
            "run_a", outcome="cancelled", status="cancelled", concluded_by="cancel"
        )
        second = store.conclude(
            "run_a", outcome="interrupted", status="failed", concluded_by="poll"
        )
        assert first.won and not second.won
        assert second.attempt.outcome == "cancelled"
        assert store.open_execution("alpha", "task-001") is None
        admit(store, "alpha", "task-001", "run_b")  # the task is free again

    def test_a_decision_made_before_a_stop_is_refused(self, store: ExecutionStore) -> None:
        admitted = admit(store, "alpha", "task-001", "run_a")
        read_generation = admitted.control_generation
        store.request_cancel("run_a", requester="Jeff Posey", source="gui", reason="stop")
        stale = store.conclude(
            "run_a",
            outcome="completed",
            status="finished",
            concluded_by="poll",
            expected_generation=read_generation,
        )
        assert not stale.won
        assert must(store.attempt("run_a")).is_live

    def test_a_stale_epoch_cannot_conclude_after_a_takeover(self, store: ExecutionStore) -> None:
        admitted = admit(store, "alpha", "task-001", "run_a")
        store.take_over(
            "run_a", holder="new", expected_epoch=admitted.epoch, owner_mode=OWNER_DURABLE
        )
        with pytest.raises(StaleOwner):
            store.conclude(
                "run_a",
                outcome="completed",
                status="finished",
                concluded_by="old",
                epoch=admitted.epoch,
            )
        assert must(store.attempt("run_a")).is_live, "the live writer's ownership is not freed"

    def test_the_winners_projection_is_enqueued_in_the_same_commit(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        item = OutboxItem(
            "op-1", "alpha", "task-001", "dispatch_result", {"outcome": "completed"}, run_id="run_a"
        )
        store.conclude(
            "run_a", outcome="completed", status="finished", concluded_by="t", projection=item
        )
        loser = OutboxItem(
            "op-2", "alpha", "task-001", "dispatch_result", {"outcome": "failed"}, run_id="run_a"
        )
        store.conclude(
            "run_a", outcome="failed", status="failed", concluded_by="t", projection=loser
        )
        assert [owed.operation_id for owed in store.pending_outbox()] == ["op-1"]


class TestNothingCommittedIsNeverAcknowledged:
    def test_a_fault_at_commit_leaves_no_trace(self, store: ExecutionStore) -> None:
        admit(store, "alpha", "task-001", "run_a")

        def crash(label: str) -> None:
            raise RuntimeError(f"process died before committing {label}")

        store.before_commit = crash
        with pytest.raises(RuntimeError):
            store.conclude("run_a", outcome="completed", status="finished", concluded_by="t")
        store.before_commit = None
        attempt = must(store.attempt("run_a"))
        assert attempt.is_live and attempt.outcome is None, "rolled back, not concluded"
        assert store.pending_outbox() == []

    def test_busy_is_reported_and_releases_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "execution.db"
        store = ExecutionStore(path, busy_timeout_ms=50)
        try:
            admit(store, "alpha", "task-001", "run_a")
            blocker = sqlite3.connect(str(path), isolation_level=None, timeout=0)
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(StoreBusy):
                    store.conclude(
                        "run_a", outcome="completed", status="finished", concluded_by="t"
                    )
                released = []
                with pytest.raises(StoreBusy):
                    released = release_ended_attempts(
                        store, lambda attempt: ("completed", "finished", "evidence")
                    )
                assert released == []
            finally:
                blocker.execute("ROLLBACK")
                blocker.close()
            assert must(store.attempt("run_a")).is_live
        finally:
            store.close()

    def test_a_full_disk_is_a_storage_failure_with_nothing_committed(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        with store.transaction() as connection:
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
        # SQLite's own SQLITE_FULL, not a mock: the file may not grow past this page.
        store._conn.execute(f"PRAGMA max_page_count = {pages}")
        huge = OutboxItem(
            "op-big",
            "alpha",
            "task-001",
            "dispatch_result",
            {"body": "x" * 200_000},
            run_id="run_a",
        )
        with pytest.raises(StorageFailure):
            store.conclude(
                "run_a", outcome="completed", status="finished", concluded_by="t", projection=huge
            )
        store._conn.execute("PRAGMA max_page_count = 1073741823")
        assert must(store.attempt("run_a")).is_live
        assert store.pending_outbox() == []


class TestActivities:
    def test_an_intent_is_idempotent_and_a_conflicting_input_is_refused(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        execution = must(store.open_execution("alpha", "task-001"))
        _, created = store.record_intent(
            "act-1",
            execution_id=execution.execution_id,
            kind="launch",
            input={"a": 1},
            owner_epoch=0,
        )
        _, again = store.record_intent(
            "act-1",
            execution_id=execution.execution_id,
            kind="launch",
            input={"a": 1},
            owner_epoch=0,
        )
        assert created and not again
        with pytest.raises(ActivityConflict):
            store.record_intent(
                "act-1",
                execution_id=execution.execution_id,
                kind="launch",
                input={"a": 2},
                owner_epoch=0,
            )

    def test_a_result_from_a_superseded_controller_is_refused(self, store: ExecutionStore) -> None:
        admit(store, "alpha", "task-001", "run_a")
        execution = must(store.open_execution("alpha", "task-001"))
        old = store.claim_controller(execution.execution_id, owner_mode=OWNER_DURABLE)
        store.record_intent(
            "act-1", execution_id=execution.execution_id, kind="launch", input={}, owner_epoch=old
        )
        store.claim_controller(execution.execution_id, owner_mode=OWNER_DURABLE, expected_epoch=old)
        with pytest.raises(StaleOwner):
            store.record_result("act-1", state="applied", owner_epoch=old)
        assert store.activities(execution.execution_id)[0].state == "intended"


class TestInboxAndOutbox:
    def feed(self, entries: List[SourceEvent]):
        def read(after: int, limit: int):
            return [event for event in entries if event.position > after][:limit]

        return read

    def event(
        self, position: int, entry_id: int, kind: str = "handoff", task: str = "task-001"
    ) -> SourceEvent:
        return SourceEvent(
            position,
            "alpha",
            task,
            entry_id,
            f"2026-09-13T00:00:{entry_id:02d}Z",
            kind,
            "Jeff Posey",
        )

    def test_every_entry_is_imported_once_and_the_cursor_moves_with_them(
        self, store: ExecutionStore
    ) -> None:
        entries = [self.event(1, 1), self.event(2, 2, "progress"), self.event(3, 3)]
        assert import_source_events(store, "alpha", self.feed(entries), limit=2) == 3
        assert import_source_events(store, "alpha", self.feed(entries)) == 0
        assert [item.status for item in store.inbox()] == ["pending", "observed", "pending"]
        assert store.cursor("task-log:alpha").position == 3

    def test_a_crash_before_the_import_commits_loses_nothing(self, store: ExecutionStore) -> None:
        entries = [self.event(1, 1), self.event(2, 2)]

        def crash(label: str) -> None:
            if label == "import":
                raise RuntimeError("died after reading the feed")

        store.before_commit = crash
        with pytest.raises(RuntimeError):
            import_source_events(store, "alpha", self.feed(entries))
        store.before_commit = None
        assert store.inbox() == [] and store.cursor("task-log:alpha").position == 0
        assert import_source_events(store, "alpha", self.feed(entries)) == 2

    def test_a_renumbered_feed_is_reimported_without_duplicates(
        self, store: ExecutionStore
    ) -> None:
        import_source_events(store, "alpha", self.feed([self.event(1, 1), self.event(2, 2)]))
        restored = [self.event(1, 2), self.event(2, 3)]  # entry 2 now at position 1
        assert import_source_events(store, "alpha", self.feed(restored)) == 1
        assert len(store.inbox()) == 3

    def test_a_signal_for_an_open_execution_joins_its_history(self, store: ExecutionStore) -> None:
        admit(store, "alpha", "task-001", "run_a")
        import_source_events(store, "alpha", self.feed([self.event(1, 7)]))
        execution = must(store.open_execution("alpha", "task-001"))
        assert "signal" in [event.kind for event in store.events(execution.execution_id)]

    def test_an_acknowledgement_lost_after_delivery_redelivers_the_same_operation(
        self, store: ExecutionStore
    ) -> None:
        store.enqueue(OutboxItem("op-1", "alpha", "task-001", "dispatch_result", {"n": 1}))
        applied: List[str] = []

        def apply(item: OutboxItem):
            applied.append(item.operation_id)
            return {"ok": True}

        def crash(label: str) -> None:
            if label == "acknowledge":
                raise RuntimeError("died after the task write, before the ack")

        store.before_commit = crash
        with pytest.raises(RuntimeError):
            deliver(store, store.pending_outbox()[0], apply)
        store.before_commit = None
        assert [item.operation_id for item in store.pending_outbox()] == ["op-1"]
        flush_outbox(store, apply)
        assert applied == ["op-1", "op-1"], "same operation id both times"
        assert store.pending_outbox() == []


class TestChildWaits:
    def test_grounding_is_sticky_across_later_observations(self, store: ExecutionStore) -> None:
        parent = store.accept_execution(
            "alpha", "task-100", envelope={}, workflow_version=WORKFLOW_VERSION
        )
        store.add_child_wait(parent.execution_id, project_id="alpha", task_id="task-101")
        store.add_child_wait(parent.execution_id, project_id="alpha", task_id="task-101")
        store.observe_child(
            parent.execution_id,
            project_id="alpha",
            task_id="task-101",
            revision="r1",
            grounding_cause="failed",
            attempted=True,
        )
        later = store.observe_child(
            parent.execution_id,
            project_id="alpha",
            task_id="task-101",
            revision="r2",
            terminal_state="closed",
        )
        assert later.status == "grounded" and later.grounding_cause == "failed"
        assert len(store.child_waits(parent.execution_id)) == 1
        assert later.attempts == 1


class TestOwnerModes:
    def test_a_durable_controller_never_takes_a_legacy_execution(
        self, store: ExecutionStore
    ) -> None:
        disposition, _ = store.import_legacy_run(
            run_id="run_old",
            project_id="alpha",
            task_id="task-001",
            mode="session",
            takes_slot=True,
            live=True,
            session_id="s1",
            admitted_at=None,
            status="running",
            outcome=None,
            envelope={"legacy_meta": {"driver": "claude"}},
            unknown_fields=["runner", "group"],
            workflow_version=WORKFLOW_VERSION,
        )
        assert disposition == "imported"
        execution = must(store.open_execution("alpha", "task-001"))
        assert execution.owner_mode == OWNER_LEGACY
        with pytest.raises(OwnerModeConflict):
            store.claim_controller(execution.execution_id, owner_mode=OWNER_DURABLE)
        with pytest.raises(OwnerModeConflict):
            store.transfer_owner_mode(execution.execution_id, to=OWNER_DURABLE, reason="upgrade")
        with pytest.raises(OwnershipConflict):
            admit(store, "alpha", "task-001", "run_new")  # the legacy run still owns the task
        store.conclude(
            "run_old", outcome="completed", status="finished", concluded_by="legacy poller"
        )
        admit(store, "alpha", "task-001", "run_new")
        assert must(store.open_execution("alpha", "task-001")).owner_mode == OWNER_DURABLE

    def test_importing_twice_imports_once_and_keeps_unknown_fields_unknown(
        self, store: ExecutionStore
    ) -> None:
        kwargs: Dict[str, Any] = dict(
            run_id="run_old",
            project_id="alpha",
            task_id="task-001",
            mode="batch",
            takes_slot=True,
            live=False,
            session_id=None,
            admitted_at=None,
            status="finished",
            outcome="completed",
            envelope={"legacy_meta": {"posture": "auto"}},
            unknown_fields=["runner", "group", "selection_source"],
            workflow_version=WORKFLOW_VERSION,
        )
        assert store.import_legacy_run(**kwargs)[0] == "imported"
        assert store.import_legacy_run(**kwargs)[0] == "already"
        (execution,) = store.executions()
        assert execution.provenance == "legacy_import"
        assert execution.unknown_fields == ["group", "runner", "selection_source"]
        assert "runner" not in execution.envelope["legacy_meta"]
        assert [e.kind for e in store.events(execution.execution_id)] == [
            "accepted",
            "admitted",
            "concluded",
        ]

    def test_a_live_legacy_run_beside_an_open_execution_is_reported_not_chosen(
        self, store: ExecutionStore
    ) -> None:
        admit(store, "alpha", "task-001", "run_new")
        disposition, _ = store.import_legacy_run(
            run_id="run_old",
            project_id="alpha",
            task_id="task-001",
            mode="session",
            takes_slot=True,
            live=True,
            session_id="s1",
            admitted_at=None,
            status="running",
            outcome=None,
            envelope={},
            unknown_fields=[],
            workflow_version=WORKFLOW_VERSION,
        )
        assert disposition == "conflict"
        assert store.attempt("run_old") is None


class TestBackupAndRestore:
    def test_restore_reconciles_what_happened_after_the_snapshot(
        self, store: ExecutionStore, tmp_path: Path
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        execution = must(store.open_execution("alpha", "task-001"))
        store.record_intent(
            "launch:run_a",
            execution_id=execution.execution_id,
            kind="launch",
            input={},
            owner_epoch=0,
        )
        store.record_intent(
            "merge:run_a",
            execution_id=execution.execution_id,
            kind="merge",
            input={},
            owner_epoch=0,
        )
        snapshot = store.backup(tmp_path / "snapshot.db")
        # After the snapshot, in the world: the run finished and its merge landed.
        store.close()

        restored = restore_snapshot(snapshot, tmp_path / "restored.db")
        try:
            assert must(restored.attempt("run_a")).is_live, "the snapshot believes the run is live"
            answers = {"launch": "applied", "merge": "unknown"}
            report = reconcile_after_restore(
                restored,
                reconcile_activity=lambda activity: (answers[activity.kind], {"checked": True}),
                evidence=lambda attempt: (
                    "completed",
                    "finished",
                    "its dispatch_result is on the task",
                ),
            )
            states = dict(report.activities)
            assert states == {"launch:run_a": "applied", "merge:run_a": "unknown"}
            assert report.released == ("run_a",)
            merge = [a for a in restored.activities() if a.kind == "merge"][0]
            assert merge.state == "unknown" and merge.error_class == "effect_unknown"
        finally:
            restored.close()

    def test_advance_after_restore_is_the_same_replay(
        self, store: ExecutionStore, tmp_path: Path
    ) -> None:
        admit(store, "alpha", "task-001", "run_a")
        execution = must(store.open_execution("alpha", "task-001"))
        before = advance_execution(store, execution.execution_id)
        snapshot = store.backup(tmp_path / "snap.db")
        restored = restore_snapshot(snapshot, tmp_path / "restored.db")
        try:
            after = advance_execution(restored, execution.execution_id)
            assert after.intents == before.intents
            assert after.recorded == ()
        finally:
            restored.close()
