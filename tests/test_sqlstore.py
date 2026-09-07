"""The SQLite store: round trip, invariants, concurrency, history and backup.

The tests that matter here are the ones that would have caught something. Three
classes of defect are covered deliberately, each because it has actually happened or
was measured to be possible on task-273:

* **Invariants the file backend could only check.** A duplicate queue position was a
  race (audit F4); here it is a constraint, and the test asserts the database refuses
  it rather than asserting that some Python noticed.
* **History that disagrees with the board.** ``SUM(open_delta)`` against the count of
  open tasks. The analytics design depends on it (section 6, item D), and an import
  that breaks it produces charts that are wrong with no symptom in any query plan.
* **Concurrency.** Two writers claiming one task, and eight appending to one log. The
  first was a real double-claim race under files; the second was a read-modify-write
  on one document.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.sqlstore import (
    CorpusImporter,
    Database,
    SqlStoreError,
    SqlTaskStore,
    current_version,
    latest_version,
    restore,
    snapshot,
    upgrade,
    verify,
)
from agentjobs.storage_protocol import TaskStore


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    """A migrated, empty store."""
    db = Database(tmp_path / "agentjobs.db")
    upgrade(db, agentjobs_version="test", snapshot_before=False)
    yield db
    db.close()


@pytest.fixture()
def store(database: Database) -> SqlTaskStore:
    """A store bound to one project, with that project's row created."""
    task_store = SqlTaskStore(database, "demo")
    task_store.ensure_project(root="/tmp/demo", reporting_tz="America/Chicago")
    return task_store


def make_task(task_id: str = "task-001", **overrides) -> Task:
    """A minimal valid task, so each test states only what it is about."""
    now = datetime.now(tz=timezone.utc)
    fields = {
        "id": task_id,
        "title": f"Title for {task_id}",
        "created": now,
        "updated": now,
        "lifecycle": Lifecycle.READY,
        "ball": Ball.AGENT,
        "ball_reason": BallReason.AVAILABLE,
        "priority": Priority.MEDIUM,
        "queue_position": 100,
        "category": "engineering",
        "spec": Spec(summary="A summary.", description="A description."),
    }
    fields.update(overrides)
    return Task(**fields)


class TestSchema:
    """Migration mechanics: the upgrade path, and what it refuses."""

    def test_upgrade_is_idempotent(self, database: Database) -> None:
        """Running the migrator twice applies nothing the second time."""
        assert current_version(database.writer) == latest_version()
        assert upgrade(database, agentjobs_version="test").applied == []

    def test_a_newer_database_is_refused_rather_than_opened(self, database: Database) -> None:
        """An old binary meeting a new database declines instead of corrupting it."""
        database.writer.execute(f"PRAGMA user_version = {latest_version() + 5}")
        with pytest.raises(SqlStoreError, match="newer AgentJobs"):
            upgrade(database, agentjobs_version="test")

    def test_the_migration_is_recorded(self, database: Database) -> None:
        """A version bump without the row explaining it is a database nobody can audit."""
        rows = database.writer.execute("SELECT version, name FROM schema_migration").fetchall()
        assert [tuple(row) for row in rows] == [(1, "initial")]


class TestRoundTrip:
    """A task goes in and comes back out as the same document."""

    def test_save_then_load_preserves_the_document(self, store: SqlTaskStore) -> None:
        """Every field survives, including the nested value objects held as JSON."""
        task = make_task(
            tags=["storage", "sqlite"],
            spec=Spec(
                summary="Sum.",
                description="Desc.",
                intent="Why.",
                constraints="Hard rules.",
                out_of_scope="Not this.",
                context=[{"path": "src/x.py", "why": "the surface"}],
            ),
            acceptance=[{"id": "ac-1", "text": "It works.", "status": "pending"}],
            deliverables=[{"path": "docs/x.md", "note": "the doc"}],
            dependencies=[{"task": "task-002", "type": "needs", "note": "first"}],
            branches=[{"name": "feat/x", "status": "active"}],
            links=[{"url": "https://example.com/pr/1", "rel": "pr", "title": "PR"}],
        )
        store.save_task(task)
        loaded = store.load_task("task-001")
        assert loaded is not None
        expected = task.model_dump(mode="json", by_alias=True, exclude={"display_status"})
        actual = loaded.model_dump(mode="json", by_alias=True, exclude={"display_status"})
        expected.pop("updated")
        actual.pop("updated")
        assert actual == expected

    def test_the_id_suffix_callers_still_pass_is_tolerated(self, store: SqlTaskStore) -> None:
        """Ids reach storage with and without the historical ``.yaml`` suffix."""
        store.save_task(make_task())
        assert store.load_task("task-001.yaml") is not None

    def test_a_missing_task_is_none_not_an_error(self, store: SqlTaskStore) -> None:
        """ "Is there a task with this id" has "no" as a legitimate answer."""
        assert store.load_task("task-999") is None

    def test_the_store_satisfies_the_boundary(self, store: SqlTaskStore) -> None:
        """Every method the Protocol names is present."""
        assert isinstance(store, TaskStore)


class TestInvariants:
    """Rules the database refuses to break, rather than rules Python checks."""

    def test_a_duplicate_queue_slot_is_refused(self, store: SqlTaskStore) -> None:
        """Audit finding F4 stops being a race and becomes a constraint."""
        store.save_task(make_task("task-001", queue_position=100))
        with pytest.raises(sqlite3.IntegrityError):
            store.save_task(make_task("task-002", queue_position=100))

    def test_the_same_slot_in_another_band_is_fine(self, store: SqlTaskStore) -> None:
        """Uniqueness is per band, because the queue is ordered per band."""
        store.save_task(make_task("task-001", queue_position=100))
        store.save_task(make_task("task-002", queue_position=100, priority=Priority.HIGH))
        assert len(store.list_tasks()) == 2

    def test_a_parent_that_does_not_exist_is_refused(self, store: SqlTaskStore) -> None:
        """Cross-task integrity that `validation.py` had to scan the corpus for."""
        with pytest.raises(sqlite3.IntegrityError):
            store.save_task(make_task("task-001", parent="task-404"))

    def test_a_parent_may_arrive_before_its_child(self, store: SqlTaskStore) -> None:
        """The deferred foreign key, which an importer depends on (analytics 6, item G)."""
        with store.transaction():
            store.save_task(make_task("task-002", queue_position=200, parent="task-001"))
            store.save_task(make_task("task-001", queue_position=100))
        assert store.load_task("task-002").parent == "task-001"

    def test_a_reason_from_the_wrong_vocabulary_is_refused(self, store: SqlTaskStore) -> None:
        """Rule 2: `agent/review` and `human/work` are not states, so they cannot exist."""
        store.save_task(make_task())
        with pytest.raises(sqlite3.IntegrityError):
            store.database.writer.execute(
                "UPDATE task SET ball_reason = 'review' WHERE task_id = 'task-001'"
            )

    def test_a_closed_task_cannot_hold_a_ball(self, store: SqlTaskStore) -> None:
        """Rule 1 in the other direction: a closed task is over, so nobody holds it."""
        store.save_task(make_task())
        store.mutate_task("task-001", _close)
        with pytest.raises(sqlite3.IntegrityError):
            store.database.writer.execute(
                "UPDATE task SET ball = 'agent' WHERE task_id = 'task-001'"
            )

    def test_an_open_task_cannot_lose_its_ball(self, store: SqlTaskStore) -> None:
        """Rule 1: an open task nobody holds is the limbo the model exists to prevent."""
        store.save_task(make_task())
        with pytest.raises(sqlite3.IntegrityError):
            store.database.writer.execute(
                "UPDATE task SET ball = NULL, ball_reason = NULL WHERE task_id = 'task-001'"
            )

    def test_a_handoff_without_an_ask_is_refused(self, store: SqlTaskStore) -> None:
        """Rule 4: a notification with no payload cannot be written down."""
        store.save_task(make_task())
        with pytest.raises(sqlite3.IntegrityError):
            store.database.writer.execute(
                "UPDATE task SET ball_reason = 'work', ball_prompt = '   ' "
                "WHERE task_id = 'task-001'"
            )


class TestHistory:
    """Every write records what moved, and the record adds up."""

    def test_creating_a_task_records_a_create_event(self, store: SqlTaskStore) -> None:
        """History is derived by the store, not remembered by the caller."""
        store.save_task(make_task())
        rows = (
            store.database.reader()
            .execute("SELECT kind, lifecycle_from, lifecycle_to, open_delta FROM task_event")
            .fetchall()
        )
        assert [tuple(row) for row in rows] == [("create", None, "ready", 1)]

    def test_closing_and_reopening_nets_to_zero(self, store: SqlTaskStore) -> None:
        """The backlog delta is what the whole level chart is a running sum of."""
        store.save_task(make_task())
        store.mutate_task("task-001", _close)
        store.mutate_task("task-001", _reopen)
        store.mutate_task("task-001", _close)
        assert store.open_delta_reconciles() == (0, 0)

    def test_the_invariant_holds_across_a_mixed_workload(self, store: SqlTaskStore) -> None:
        """analytics-design section 6, item D, asserted in the store's own suite."""
        for index in range(1, 8):
            store.save_task(make_task(f"task-{index:03d}", queue_position=index * 100))
        for index in (2, 4, 6):
            store.mutate_task(f"task-{index:03d}", _close)
        store.mutate_task("task-004", _reopen)
        summed, counted = store.open_delta_reconciles()
        assert summed == counted == 5

    def test_a_handoff_is_recorded_with_both_sides(self, store: SqlTaskStore) -> None:
        """Before *and* after, which is what a replay could never recover."""
        store.save_task(make_task())
        store.mutate_task("task-001", _handoff_to_human)
        row = (
            store.database.reader()
            .execute(
                "SELECT kind, ball_from, ball_to, ball_reason_from, ball_reason_to "
                "FROM task_event ORDER BY event_id DESC LIMIT 1"
            )
            .fetchone()
        )
        assert tuple(row) == ("handoff", "agent", "human", "available", "review")

    def test_an_unchanged_save_writes_no_event(self, store: SqlTaskStore) -> None:
        """Saving a task that did not move is not history; it is noise."""
        task = store.save_task(make_task())
        store.save_task(task)
        count = store.database.reader().execute("SELECT COUNT(*) FROM task_event").fetchone()[0]
        assert count == 1


class TestConcurrency:
    """The races the file backend had, exercised rather than reasoned about."""

    def test_only_one_of_two_claimants_wins(self, store: SqlTaskStore) -> None:
        """The double-claim race: two agents both read `ready` and both write `active`."""
        store.save_task(make_task())
        winners: List[str] = []
        barrier = threading.Barrier(2)

        def claim(actor: str) -> None:
            barrier.wait()

            def mutator(task: Task) -> Task | None:
                if task.lifecycle is not Lifecycle.READY:
                    return None
                task.lifecycle = Lifecycle.ACTIVE
                task.ball_reason = BallReason.WORK
                task.ball_prompt = "Work it."
                task.assignment = Assignment(owner=actor)
                return task

            before = store.load_task("task-001")
            after = store.mutate_task("task-001", mutator)
            if after.assignment.owner == actor and before.assignment.owner != actor:
                winners.append(actor)

        threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1
        assert store.load_task("task-001").assignment.owner == winners[0]

    def test_concurrent_log_appends_all_land(self, store: SqlTaskStore) -> None:
        """Eight writers, one task. Under files this is a read-modify-write on a document."""
        store.save_task(make_task())
        appended = 8
        barrier = threading.Barrier(appended)

        def append(index: int) -> None:
            barrier.wait()

            def mutator(task: Task) -> Task:
                task.log.append(
                    LogEntry(
                        id=task.next_log_id(),
                        ts=datetime.now(tz=timezone.utc),
                        actor="claude",
                        type=LogEntryType.PROGRESS,
                        body=f"append {index}",
                    )
                )
                return task

            store.mutate_task("task-001", mutator)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(appended)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        task = store.load_task("task-001")
        assert len(task.log) == appended
        assert len({entry.id for entry in task.log}) == appended

    def test_a_band_reorder_is_atomic(self, store: SqlTaskStore) -> None:
        """Lift the band out of the way, then place it, inside one transaction.

        The unique index is checked per statement, so a naive swap fails half way. This
        is the shape every multi-row move must take, and the test is here so a later
        refactor that drops the lift is caught.
        """
        for index in range(1, 5):
            store.save_task(make_task(f"task-{index:03d}", queue_position=index * 100))
        with store.transaction() as connection:
            connection.execute(
                "UPDATE task SET queue_position = queue_position + 1000000 "
                "WHERE project_id = 'demo' AND queue_position IS NOT NULL"
            )
            order = ["task-004", "task-001", "task-002", "task-003"]
            for place, task_id in enumerate(order, start=1):
                connection.execute(
                    "UPDATE task SET queue_position = ? WHERE project_id = 'demo' "
                    "AND task_id = ?",
                    (place * 100, task_id),
                )
        assert [task.id for task in store.list_tasks()][:4] == order

    def test_a_failed_transaction_leaves_nothing_behind(self, store: SqlTaskStore) -> None:
        """Crash recovery, at the granularity that matters: all of a write or none."""
        store.save_task(make_task())
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.save_task(make_task("task-002", queue_position=200))
                raise RuntimeError("something went wrong mid-write")
        assert store.load_task("task-002") is None
        assert store.load_task("task-001") is not None


class TestBackup:
    """A snapshot is consistent, and a restore is verified before it is trusted."""

    def test_a_snapshot_taken_under_load_verifies(self, store: SqlTaskStore) -> None:
        """Taken while a writer is committing, which is the only interesting case."""
        for index in range(1, 6):
            store.save_task(make_task(f"task-{index:03d}", queue_position=index * 100))
        stop = threading.Event()

        def hammer() -> None:
            counter = 0
            while not stop.is_set():
                counter += 1

                def mutator(task: Task) -> Task:
                    task.log.append(
                        LogEntry(
                            id=task.next_log_id(),
                            ts=datetime.now(tz=timezone.utc),
                            actor="claude",
                            type=LogEntryType.PROGRESS,
                            body=f"tick {counter}",
                        )
                    )
                    return task

                store.mutate_task("task-001", mutator)

        writer = threading.Thread(target=hammer)
        writer.start()
        try:
            target = store.database.path.with_name("snapshot.db")
            snapshot(store.database, target)
        finally:
            stop.set()
            writer.join()
        report = verify(target)
        assert report.ok, report.render()
        assert report.counts["task"] == 5

    def test_a_snapshot_will_not_silently_overwrite(self, store: SqlTaskStore) -> None:
        """Backups are evidence; clobbering one by accident is not recoverable."""
        store.save_task(make_task())
        target = store.database.path.with_name("snapshot.db")
        snapshot(store.database, target)
        with pytest.raises(SqlStoreError, match="already exists"):
            snapshot(store.database, target)

    def test_a_corrupt_snapshot_is_refused(self, store: SqlTaskStore, tmp_path: Path) -> None:
        """The verification is the point, so it has to actually reject something."""
        store.save_task(make_task())
        target = tmp_path / "snap.db"
        snapshot(store.database, target)
        # Break the invariant the way a bad import would: an event that says a task
        # closed, with no such task closing.
        broken = sqlite3.connect(target)
        broken.execute(
            "INSERT INTO task_event(project_id, task_id, ts, actor, kind, lifecycle_from,"
            " lifecycle_to) VALUES ('demo','task-001','2026-01-01T00:00:00Z','x','close',"
            "'ready','closed')"
        )
        broken.commit()
        broken.close()
        report = verify(target)
        assert not report.ok
        assert "backlog invariant" in report.render()
        with pytest.raises(SqlStoreError, match="refusing to restore"):
            restore(target, tmp_path / "live.db")

    def test_restore_puts_the_snapshot_in_place(self, store: SqlTaskStore, tmp_path: Path) -> None:
        """And keeps what it replaced, because restores get done twice."""
        store.save_task(make_task())
        target = tmp_path / "snap.db"
        snapshot(store.database, target)
        destination = tmp_path / "restored.db"
        destination.write_bytes(b"not a database")
        report = restore(target, destination)
        assert report.ok
        restored = Database(destination)
        try:
            assert restored.reader().execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
        finally:
            restored.close()
        assert list(tmp_path.glob("restored.db.replaced.*"))


class TestImport:
    """Reading a YAML corpus, including the records that cannot be read."""

    def _write(self, directory: Path, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(body, encoding="utf-8")

    def test_a_corpus_imports_and_reconciles(self, store: SqlTaskStore, tmp_path: Path) -> None:
        """The invariant holds after an import, which is when it is easiest to break."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        self._write(tasks_dir, "task-002.yaml", _YAML_CLOSED)
        report = CorpusImporter(store, tasks_dir).run()
        assert report.imported == 2
        assert report.reconciles, report.render()
        assert report.open_rows == 1
        assert store.load_task("task-001").title == "An open task"

    def test_an_unreadable_record_is_quarantined_not_skipped(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """The live tables keep every constraint; the bad record stays inspectable."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        self._write(tasks_dir, "task-666.yaml", "id: task-666\nthis: is not a task\n")
        report = CorpusImporter(store, tasks_dir).run()
        assert report.imported == 1
        assert len(report.quarantined) == 1
        held = store.quarantined()
        assert len(held) == 1
        assert held[0]["task_id_guess"] == "task-666"
        assert store.load_task("task-666") is None

    def test_importing_twice_does_not_duplicate_the_log(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """A migration that is interrupted gets re-run, so it must be repeatable."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        CorpusImporter(store, tasks_dir).run()
        first = len(store.load_task("task-001").log)
        CorpusImporter(store, tasks_dir).run()
        assert len(store.load_task("task-001").log) == first


def _close(task: Task) -> Task:
    """Close a task the way the manager does."""
    task.lifecycle = Lifecycle.CLOSED
    task.ball = None
    task.ball_reason = None
    task.ball_prompt = None
    task.outcome = Outcome.COMPLETED
    task.queue_position = None
    task.assignment = Assignment()
    return task


def _reopen(task: Task) -> Task:
    """Reopen a closed task."""
    task.lifecycle = Lifecycle.READY
    task.ball = Ball.AGENT
    task.ball_reason = BallReason.AVAILABLE
    task.outcome = None
    task.queue_position = 9999
    return task


def _handoff_to_human(task: Task) -> Task:
    """Move the ball to a human for review."""
    task.ball = Ball.HUMAN
    task.ball_reason = BallReason.REVIEW
    task.ball_prompt = "Please review."
    task.log.append(
        LogEntry(
            id=task.next_log_id(),
            ts=datetime.now(tz=timezone.utc),
            actor="claude",
            type=LogEntryType.HANDOFF,
            body="Handing off.",
            data={"ball": "human", "ball_reason": "review"},
        )
    )
    return task


_YAML_OPEN = """\
schema: 2
id: task-001
title: An open task
created: '2026-01-01T00:00:00Z'
updated: '2026-01-02T00:00:00Z'
lifecycle: ready
ball: agent
ball_reason: available
archived: false
priority: high
queue_position: 100
category: engineering
spec:
  summary: A summary.
  description: A description.
log:
- id: 1
  ts: '2026-01-01T00:00:00Z'
  actor: claude
  type: transition
  body: Created draft by claude.
  data:
    lifecycle: draft
"""

_YAML_CLOSED = """\
schema: 2
id: task-002
title: A closed task
created: '2026-01-01T00:00:00Z'
updated: '2026-01-03T00:00:00Z'
lifecycle: closed
outcome: completed
archived: false
priority: medium
category: engineering
spec:
  summary: A summary.
  description: A description.
log:
- id: 1
  ts: '2026-01-01T00:00:00Z'
  actor: claude
  type: transition
  body: Created draft by claude.
  data:
    lifecycle: draft
- id: 2
  ts: '2026-01-03T00:00:00Z'
  actor: claude
  type: transition
  body: Closed by claude.
  data:
    lifecycle: closed
    outcome: completed
"""
