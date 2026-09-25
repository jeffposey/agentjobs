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
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

from agentjobs.taskfiles import TaskLoadError
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
    CorpusAlreadyImported,
    CorpusImporter,
    DatabaseClosed,
    QuotationPolicyError,
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
from agentjobs.sqlstore.migrations import MIGRATIONS_DIR, available
from agentjobs.storage_protocol import TaskStore


CLOSE_UNDER_READ = """
import random
import sys
import threading
from pathlib import Path

from agentjobs.sqlstore import Database

root = Path(sys.argv[1])
for iteration in range(60):
    database = Database(root / f"race-{iteration}.db")
    with database.write() as connection:
        connection.execute("CREATE TABLE t(x)")
        connection.executemany("INSERT INTO t VALUES (?)", [(n,) for n in range(2000)])
    reading = threading.Event()

    def read() -> None:
        connection = database.reader()
        reading.set()
        try:
            while True:
                for _ in connection.execute("SELECT x FROM t"):
                    pass
        except Exception:
            return

    thread = threading.Thread(target=read)
    thread.start()
    reading.wait()
    threading.Event().wait(random.random() / 100)
    database.close()
    thread.join(timeout=30)
    assert not thread.is_alive()
print("ok")
"""
"""Close a database while another thread steps a query over it, sixty times.

Every read loops until the close stops it, so the only ways out are a Python exception
in the reading thread, which is the fixed behaviour, or the process dying natively.
"""


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
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


def loaded(store: SqlTaskStore, task_id: str) -> Task:
    """Load a task the test knows exists, narrowing away the Optional.

    ``load_task`` returns None for "no such task", which is a real answer the store
    must be able to give. In a test that has just written the task, None is a failure
    and should read as one rather than as an attribute error three lines later.
    """
    task = store.load_task(task_id)
    assert task is not None, f"{task_id} should exist at this point"
    return task


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

    def test_every_migration_is_recorded(self, database: Database) -> None:
        """A version bump without the row explaining it is a database nobody can audit.

        Compared against the migrations this build ships rather than a literal list, so
        adding one does not require editing an assertion whose point is that nothing
        was applied unrecorded.
        """
        rows = database.writer.execute(
            "SELECT version, name FROM schema_migration ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(m.version, m.name) for m in available()]
        assert rows, "a fresh database should have applied at least the initial schema"

    def test_the_log_type_constraint_admits_exactly_the_types_the_model_declares(
        self, database: Database
    ) -> None:
        """The duplication between ``LogEntryType`` and the column's CHECK, enforced.

        Keeping both is deliberate: the model refuses a bad type at every write path, and
        the constraint refuses one that reached the file some other way. The cost is that
        a new type needs a migration, and forgetting it fails at the first *write* of that
        type rather than here. task-506 paid that -- ``authorization`` passed every model
        check and the insert raised ``CHECK constraint failed`` from inside an API test.
        This is the assertion that says so first, and in one place.
        """
        store = SqlTaskStore(database, "demo")
        store.ensure_project(root="/tmp/demo", reporting_tz="UTC")
        store.save_task(make_task())
        writer = database.writer
        accepted = []
        for index, entry_type in enumerate(LogEntryType, start=1):
            try:
                writer.execute(
                    "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type) "
                    "VALUES ('demo', 'task-001', ?, '2026-01-01T00:00:00Z', 'bot', ?)",
                    (index, entry_type.value),
                )
            except sqlite3.IntegrityError:
                continue
            accepted.append(entry_type.value)

        assert accepted == [entry_type.value for entry_type in LogEntryType]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            writer.execute(
                "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type) "
                "VALUES ('demo', 'task-001', 999, '2026-01-01T00:00:00Z', 'bot', 'invented')"
            )


class TestTheAuthorizationTypeMigration:
    """What rebuilding ``log_entry`` for its widened CHECK had to leave alone (task-506).

    SQLite cannot alter a CHECK, so migration 006 rebuilds the table -- and ``attachment``
    cascades off it, while ``log_feed`` holds positions that must never be reused. Two
    attempts at that rebuild broke one or the other **silently**, with
    ``PRAGMA integrity_check`` reporting ``ok`` either way. So the migration is exercised
    against a database holding one of each, rather than against the empty one every other
    test in this file upgrades.
    """

    @pytest.fixture()
    def at_five(self, tmp_path: Path) -> Iterator[Database]:
        """A store stopped at version 5, holding a threaded entry and an attachment.

        **Every row here is raw SQL against v5's physical schema, deliberately.** The
        obvious fixture calls ``store.save_task``, and that one is a trap: the store's
        writer names the columns of the *latest* schema, so it cannot write to a database
        held at an earlier one. It worked until a migration first widened a table the
        store writes -- 007's ``check_argv`` -- and then every test in this class died on
        ``table task_acceptance has no column named check_argv``, a message about a
        column none of them has anything to do with (task-147).

        Versions 001 to 005 are frozen, so SQL written against them cannot rot. A fixture
        that reaches through today's store to build yesterday's database can, and will
        again on the next migration that adds a column.
        """
        database = Database(tmp_path / "agentjobs.db")
        writer = database.writer
        for migration in available():
            if migration.version > 5:
                break
            writer.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.sql()
                + f"\nPRAGMA user_version = {migration.version};"
            )
            writer.execute("COMMIT")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO project(project_id, root, created_at, reporting_tz) "
            "VALUES ('demo', '/tmp/demo', '2026-01-01T00:00:00Z', 'UTC')"
        )
        writer.execute(
            "INSERT INTO task(project_id, task_id, title, created_at, updated_at, "
            "lifecycle, ball, ball_reason, priority, queue_position, category, "
            "spec_summary, spec_description, last_activity_at) "
            "VALUES ('demo', 'task-001', 'Title for task-001', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', 'ready', 'agent', 'available', 'medium', 100, "
            "'engineering', 'A summary.', 'A description.', '2026-01-01T00:00:00Z')"
        )
        writer.executemany(
            "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, body, "
            "re, data_json) VALUES ('demo', 'task-001', ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "2026-01-01T00:00:00Z", "bot", "question", "Which one?", None, "{}"),
                (
                    2,
                    "2026-01-02T00:00:00Z",
                    "Ada",
                    "answer",
                    "Chose: this one",
                    1,
                    '{"selected": ["this one"]}',
                ),
            ],
        )
        writer.execute(
            "INSERT INTO blob(sha256, media_type, size_bytes, content) "
            "VALUES ('abc', 'image/png', 1, X'00')"
        )
        writer.execute(
            "INSERT INTO attachment(project_id, task_id, entry_id, ord, sha256, label) "
            "VALUES ('demo', 'task-001', 1, 0, 'abc', 'the screenshot')"
        )
        writer.execute("COMMIT")
        yield database
        database.close()

    def test_it_keeps_the_attachments(self, at_five: Database) -> None:
        """The rows the cascade silently ate, twice, on the way to this migration.

        An attachment is a person's screenshot of a defect, referenced by a log entry.
        Nothing else holds it, and a rebuild that drops ``log_entry`` with foreign keys on
        removes every one in the store without complaining.
        """
        report = upgrade(at_five, agentjobs_version="test", snapshot_before=False)

        # Asserted, because every test in this class would pass vacuously against a
        # fixture that had already been migrated past the rebuild. 007 rides along
        # because `upgrade` goes to the latest version and there is no stopping it
        # part-way; what matters is that 006 is in the list at all.
        assert report.applied[0] == "006_authorization_is_a_log_entry_type"
        assert report.applied[-1].startswith(f"{latest_version():03d}_")
        rows = at_five.writer.execute(
            "SELECT entry_id, ord, sha256, label FROM attachment"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(1, 0, "abc", "the screenshot")]

    def test_it_keeps_the_entries_and_the_feed_positions(self, at_five: Database) -> None:
        """A cursor that has read feed position N must never be shown a later entry as N.

        That is the whole reason ``log_feed`` exists rather than a rowid, so a rebuild that
        renumbered it would break the execution journal's inbox in a way nothing observes
        until a handoff goes unnoticed.
        """
        before = at_five.writer.execute(
            "SELECT feed_id, task_id, entry_id FROM log_feed ORDER BY feed_id"
        ).fetchall()
        assert len(before) == 2

        upgrade(at_five, agentjobs_version="test", snapshot_before=False)

        after = at_five.writer.execute(
            "SELECT feed_id, task_id, entry_id FROM log_feed ORDER BY feed_id"
        ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
        entries = at_five.writer.execute(
            "SELECT entry_id, actor, type, body, re FROM log_entry ORDER BY entry_id"
        ).fetchall()
        assert [tuple(row) for row in entries] == [
            (1, "bot", "question", "Which one?", None),
            (2, "Ada", "answer", "Chose: this one", 1),
        ]

    def test_the_feed_triggers_and_the_check_work_afterwards(self, at_five: Database) -> None:
        """The triggers are re-created, not merely dropped, and the new type is accepted.

        A rebuild that forgot the insert trigger would leave the feed frozen at the two
        positions above -- every entry written after the migration invisible to the
        journal, with nothing raising anything.
        """
        upgrade(at_five, agentjobs_version="test", snapshot_before=False)

        at_five.writer.execute(
            "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, body, "
            "data_json) VALUES ('demo', 'task-001', 3, '2026-01-03T00:00:00Z', 'bot', "
            "'authorization', 'Start this.', '{\"authorized_by\": \"Ada\"}')"
        )
        assert at_five.writer.execute("SELECT COUNT(*) FROM log_feed").fetchone()[0] == 3

        at_five.writer.execute("DELETE FROM log_entry WHERE entry_id = 3")

        assert at_five.writer.execute("SELECT COUNT(*) FROM log_feed").fetchone()[0] == 2
        assert at_five.writer.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_the_self_reference_survives_the_rename(self, at_five: Database) -> None:
        """``re`` threads an answer to its question, and the rename rewrites that clause.

        The migration writes the constraint as ``log_entry_006`` precisely so the
        non-legacy rename rewrites it to ``log_entry``; writing ``log_entry`` there would
        have pointed it at the table being dropped. A wrong name is not a DDL error --
        SQLite accepts it and refuses every threaded entry at write time instead.
        """
        upgrade(at_five, agentjobs_version="test", snapshot_before=False)

        sql = at_five.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'log_entry'"
        ).fetchone()[0]

        assert "log_entry_006" not in sql
        at_five.writer.execute(
            "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, re) "
            "VALUES ('demo', 'task-001', 4, '2026-01-04T00:00:00Z', 'bot', 'answer', 2)"
        )
        assert at_five.writer.execute("PRAGMA foreign_key_check").fetchall() == []


class TestThePlanReasonMigration:
    """Migration 012 widens ``task``'s reason CHECK in place rather than rebuilding it.

    Thirteen tables cascade off ``task``, so a rebuild would have had to park every one
    of them the way 006 parked ``attachment``. Instead the CHECK's text is edited under
    ``writable_schema`` -- which is only safe because it widens, and only visible to
    other connections because the schema cookie moves. Both are asserted here against a
    store holding rows, since an edit that lost the cookie bump looks fine from the
    connection that made it.
    """

    @pytest.fixture()
    def at_eleven(self, tmp_path: Path) -> Iterator[Database]:
        """A store stopped at version 11, holding a task, a log entry and an attachment.

        Raw SQL against v11's frozen schema, for the reason ``at_five`` gives.
        """
        database = Database(tmp_path / "agentjobs.db")
        writer = database.writer
        for migration in available():
            if migration.version > 11:
                break
            writer.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.sql()
                + f"\nPRAGMA user_version = {migration.version};"
            )
            writer.execute("COMMIT")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO project(project_id, root, created_at, reporting_tz) "
            "VALUES ('demo', '/tmp/demo', '2026-01-01T00:00:00Z', 'UTC')"
        )
        writer.execute(
            "INSERT INTO task(project_id, task_id, title, created_at, updated_at, "
            "lifecycle, ball, ball_reason, ball_prompt, owner, priority, queue_position, "
            "category, spec_summary, spec_description, last_activity_at, first_claimed_at) "
            "VALUES ('demo', 'task-001', 'Title for task-001', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', 'active', 'human', 'review', 'Please review.', "
            "'claude', 'medium', 100, 'engineering', 'A summary.', 'A description.', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        writer.execute(
            "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, body) "
            "VALUES ('demo', 'task-001', 1, '2026-01-01T00:00:00Z', 'bot', 'note', 'Hi')"
        )
        writer.execute(
            "INSERT INTO blob(sha256, media_type, size_bytes, content) "
            "VALUES ('abc', 'image/png', 1, X'00')"
        )
        writer.execute(
            "INSERT INTO attachment(project_id, task_id, entry_id, ord, sha256, label) "
            "VALUES ('demo', 'task-001', 1, 0, 'abc', 'the screenshot')"
        )
        writer.execute("COMMIT")
        yield database
        database.close()

    @staticmethod
    def hand_to(connection: sqlite3.Connection, ball: str, reason: str) -> None:
        connection.execute(
            "UPDATE task SET ball = ?, ball_reason = ?, ball_prompt = 'Look at this.' "
            "WHERE project_id = 'demo' AND task_id = 'task-001'",
            (ball, reason),
        )

    def test_v11_refuses_plan_and_v12_accepts_it(self, at_eleven: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            self.hand_to(at_eleven.writer, "human", "plan")

        upgrade(at_eleven, agentjobs_version="test", snapshot_before=False)

        self.hand_to(at_eleven.writer, "human", "plan")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            self.hand_to(at_eleven.writer, "agent", "plan")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            self.hand_to(at_eleven.writer, "human", "invented")

    def test_a_connection_opened_before_the_upgrade_sees_the_wider_check(
        self, at_eleven: Database
    ) -> None:
        """The cookie bump: without it this connection keeps enforcing v11's list."""
        other = sqlite3.connect(str(at_eleven.path), isolation_level=None)
        try:
            other.execute("SELECT count(*) FROM task").fetchone()  # load its schema cache
            upgrade(at_eleven, agentjobs_version="test", snapshot_before=False)
            self.hand_to(other, "human", "plan")
        finally:
            other.close()

    def test_it_moves_no_rows_and_leaves_the_file_consistent(self, at_eleven: Database) -> None:
        def counts() -> List[int]:
            return [
                at_eleven.writer.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("task", "log_entry", "attachment", "log_feed")
            ]

        before = counts()
        upgrade(at_eleven, agentjobs_version="test", snapshot_before=False)

        assert counts() == before == [1, 1, 1, 1]
        assert at_eleven.writer.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert at_eleven.writer.execute("PRAGMA foreign_key_check").fetchall() == []
        leftover = at_eleven.writer.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'migration_012_widened'"
        ).fetchone()[0]
        assert leftover == 0

    def test_the_reason_constraint_admits_exactly_the_pairs_the_model_declares(
        self, at_eleven: Database
    ) -> None:
        """The duplication between ``BALL_REASONS`` and rule 2b, enforced in one place.

        The sibling of the log-type test in ``TestSchema``: a new reason that reaches the
        model without a migration fails here rather than at its first write.
        """
        from agentjobs.models_v2 import BALL_REASONS

        upgrade(at_eleven, agentjobs_version="test", snapshot_before=False)
        for ball, reasons in BALL_REASONS.items():
            for reason in BallReason:
                admitted = True
                try:
                    self.hand_to(at_eleven.writer, ball.value, reason.value)
                except sqlite3.IntegrityError:
                    admitted = False
                assert admitted == (reason in reasons), (ball.value, reason.value)


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
        assert loaded(store, "task-002").parent == "task-001"

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

            before = loaded(store, "task-001")
            after = store.mutate_task("task-001", mutator)
            if after.assignment.owner == actor and before.assignment.owner != actor:
                winners.append(actor)

        threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1
        assert loaded(store, "task-001").assignment.owner == winners[0]

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
        task = loaded(store, "task-001")
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

    def test_closing_under_a_reading_thread_raises_rather_than_crashing(
        self, tmp_path: Path
    ) -> None:
        """A close from one thread while another steps a query must not kill the process.

        Before task-438 it did, with ``Windows fatal exception: access violation``: a
        dispatch supervisor was still reading when a test's teardown closed the store,
        and the xdist worker died with every test queued on it. Run in a subprocess for
        that reason -- a regression here must fail this test, not take the worker down.
        On the unfixed code this script crashed in five runs out of five.
        """
        script = tmp_path / "close_under_read.py"
        script.write_text(CLOSE_UNDER_READ, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-3000:]
        assert result.stdout.strip() == "ok"

    def test_a_write_after_close_names_who_closed_the_database(self, tmp_path: Path) -> None:
        """The next occurrence of task-497 is diagnosable from its own message.

        SQLite's ``Cannot operate on a closed database`` names neither the closer nor the
        moment. A supervisor thread writing after a teardown closed the store is the shape
        it came from, so the close happens on one thread and the write on another.
        """
        database = Database(tmp_path / "closed.db")

        def teardown_closes_the_store() -> None:
            database.close()

        closer = threading.Thread(target=teardown_closes_the_store, name="teardown")
        closer.start()
        closer.join()

        with pytest.raises(DatabaseClosed) as caught:
            with database.write():
                pass  # pragma: no cover - the begin is what refuses

        message = str(caught.value)
        assert "Cannot operate on a closed database" in message
        assert "thread 'teardown'" in message
        assert "in teardown_closes_the_store" in message
        assert str(tmp_path / "closed.db") in message
        # Still the exception SQLite would have raised, so no caller stops catching it.
        assert isinstance(caught.value, sqlite3.ProgrammingError)

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
        assert loaded(store, "task-001").title == "An open task"

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
        # Reading it back is a load error naming the record, not absence (task-402):
        # a record that could not be accepted must stay distinguishable from one that
        # was never there, which is the guarantee quarantine exists to keep.
        with pytest.raises(TaskLoadError, match="task-666"):
            store.load_task("task-666")

    def test_a_record_that_fails_halfway_leaves_no_partial_row(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """Quarantining a record must mean it is not in the live tables at all.

        Found by the task-311 cutover sandbox, on a copy of the corpus whose attachment
        sidecars were missing. A dangling attachment reference is refused *while the log
        is being written*, so the task row and every entry before the offending one had
        already been inserted -- and the whole import being one transaction meant nothing
        rolled them back. Six records then read as valid tasks with most of their log
        gone, which no count would have noticed. Each record now gets a savepoint.
        """
        tasks_dir = tmp_path / "tasks"
        self._write(
            tasks_dir,
            "task-001.yaml",
            _YAML_OPEN
            + """- id: 2
  ts: '2026-01-03T00:00:00Z'
  actor: claude
  type: note
  body: With a picture whose bytes are not here.
  attachments:
  - path: attachments/task-001/deadbeef.png
    media_type: image/png
    sha256: deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef
    size_bytes: 4
    label: Missing
- id: 3
  ts: '2026-01-04T00:00:00Z'
  actor: claude
  type: progress
  body: And a later entry that would otherwise be lost.
""",
        )
        self._write(tasks_dir, "task-002.yaml", _YAML_CLOSED)

        report = CorpusImporter(store, tasks_dir).run()

        # It is quarantined, named, and *not in the live tables* -- rather than present
        # with a truncated log. Reading it back names it as unreadable, which is the
        # other half: quarantined is not the same as never having existed.
        assert any("task-001" in name for name, _ in report.quarantined)
        with pytest.raises(TaskLoadError, match="task-001"):
            store.load_task("task-001")
        assert (
            store.database.reader()
            .execute("SELECT COUNT(*) AS n FROM log_entry WHERE task_id = 'task-001'")
            .fetchone()["n"]
            == 0
        )
        # And the good record beside it still arrived: one bad file is not a failed
        # import.
        assert store.load_task("task-002") is not None
        assert report.reconciles

    def test_a_second_import_is_refused_rather_than_silently_doubling_history(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """The dangerous re-run is the one that follows a *completed* import.

        The tasks would upsert harmlessly, which is why this looked repeatable; the
        reconstructed history would be written a second time and the backlog invariant
        would break with nothing raising. Refusing is what makes the failure loud.
        """
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        CorpusImporter(store, tasks_dir).run()
        with pytest.raises(CorpusAlreadyImported, match="--replace"):
            CorpusImporter(store, tasks_dir).run()

    def test_replacing_re_imports_without_duplicating_anything(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """A migration that is interrupted gets re-run, so it must be repeatable."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        first = CorpusImporter(store, tasks_dir).run()
        entries = len(loaded(store, "task-001").log)

        again = CorpusImporter(store, tasks_dir).run(replace=True)

        assert len(loaded(store, "task-001").log) == entries
        assert again.imported == first.imported
        assert again.events == first.events
        # The invariant the analytics page depends on, after a re-import as after one.
        assert again.reconciles


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


_YAML_QUOTED = """\
schema: 2
id: task-003
title: A record that quotes somebody
created: '2026-01-01T00:00:00Z'
updated: '2026-01-02T00:00:00Z'
lifecycle: ready
ball: agent
ball_reason: available
archived: false
priority: high
queue_position: 200
category: engineering
spec:
  summary: A summary.
  description: >-
    The reviewer said "yeah this whole panel is garbage, honestly", so the
    layout is being reworked.
log:
- id: 1
  ts: '2026-01-01T00:00:00Z'
  actor: claude
  type: transition
  body: Created draft by claude.
  data:
    lifecycle: draft
"""


class TestImportQuotationPolicy:
    """The last mechanical place the paraphrase rule can be enforced (task-376).

    It refuses rather than quarantining, and that is the decision worth pinning: the
    quarantine table holds `raw_text`, so quarantining a record for its content would
    put the content in the database in the same act that claimed to keep it out.
    """

    def _write(self, directory: Path, name: str, body: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(body, encoding="utf-8")

    def test_a_quoted_remark_refuses_the_import(self, store: SqlTaskStore, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        self._write(tasks_dir, "task-003.yaml", _YAML_QUOTED)

        with pytest.raises(QuotationPolicyError) as caught:
            CorpusImporter(store, tasks_dir).run()

        assert "task-003" in str(caught.value)
        assert "spec.description" in str(caught.value)

    def test_the_refusal_names_the_region_and_not_the_remark(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """An exception string reaches logs and issue trackers -- the same mistake, one layer out."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-003.yaml", _YAML_QUOTED)

        with pytest.raises(QuotationPolicyError) as caught:
            CorpusImporter(store, tasks_dir).run()

        assert "garbage" not in str(caught.value)
        assert "honestly" not in str(caught.value)

    def test_a_refusal_writes_nothing_at_all(self, store: SqlTaskStore, tmp_path: Path) -> None:
        """Not even the readable records that came before it in the directory."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)
        self._write(tasks_dir, "task-003.yaml", _YAML_QUOTED)

        with pytest.raises(QuotationPolicyError):
            CorpusImporter(store, tasks_dir).run()

        assert store.load_task("task-001") is None
        assert store.quarantined() == []

    def test_an_operator_can_import_anyway_and_the_report_says_so(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """The escape the false-positive risk is bounded by, and it is not silent."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-003.yaml", _YAML_QUOTED)

        report = CorpusImporter(store, tasks_dir).run(enforce_quotation_policy=False)

        assert report.imported == 1
        assert list(report.quoted_remarks) == ["task-003"]
        assert "quotation policy NOT enforced" in report.render()
        assert "garbage" not in report.render()

    def test_a_clean_corpus_reports_no_remarks(self, store: SqlTaskStore, tmp_path: Path) -> None:
        """Silence is the normal outcome, and the report does not mention the check."""
        tasks_dir = tmp_path / "tasks"
        self._write(tasks_dir, "task-001.yaml", _YAML_OPEN)

        report = CorpusImporter(store, tasks_dir).run()

        assert report.quoted_remarks == {}
        assert "quotation policy" not in report.render()


class TestTheNumberAnIdCarries:
    """``seq``, ``generate_task_id``, and the slugged ids both used to skip (task-378).

    ``generate_task_id`` takes the next id from ``MAX(seq)``, and the column's stated
    purpose is that an id like ``task-047-lint-debt`` counts towards that maximum
    instead of being skipped the way the file backend's glob skipped it. The parser
    read the segment after the *last* hyphen -- ``debt`` -- so it stored NULL for every
    such id and the intended fix was never in effect.

    What that cost, on 2026-09-08: four projects whose ids all carry slugs were cut over
    to the database, and the next ``agentjobs create`` in each minted ``task-001`` beside
    a ``task-001-<slug>`` weeks older than it. Nothing collided, because the two ids are
    different strings -- but the numbering had restarted, and would have restarted again
    on every create.
    """

    def test_a_slugged_id_carries_its_number(self) -> None:
        assert SqlTaskStore._sequence("task-047-lint-debt") == 47
        assert SqlTaskStore._sequence("task-001-league-import-yahoo") == 1

    def test_a_bare_id_is_unchanged(self) -> None:
        assert SqlTaskStore._sequence("task-047") == 47

    def test_an_id_carrying_no_number_has_none(self) -> None:
        assert SqlTaskStore._sequence("task-lint-debt") is None
        assert SqlTaskStore._sequence("epic") is None

    def test_the_next_id_counts_a_slugged_one(self, store: SqlTaskStore) -> None:
        """The failure as a user meets it, rather than as a parser call."""
        store.save_task(make_task("task-017-two-qb-baseline-gap"))

        assert store.generate_task_id() == "task-018"

    def test_a_slugged_and_a_bare_id_share_one_numbering(self, store: SqlTaskStore) -> None:
        store.save_task(make_task("task-003", queue_position=100))
        store.save_task(make_task("task-009-harvest-draft-intel", queue_position=200))

        assert store.generate_task_id() == "task-010"


class TestTheSeqRepair:
    """Migration 003. A parser fixed in Python fixes nothing already written."""

    def _stored(self, database: Database, task_id: str) -> object:
        row = database.writer.execute(
            "SELECT seq FROM task WHERE project_id = 'demo' AND task_id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        return row["seq"]

    def test_it_recomputes_a_row_written_by_the_old_parser(
        self, store: SqlTaskStore, database: Database
    ) -> None:
        """Written correctly, then broken back to what the old parser stored."""
        store.save_task(make_task("task-017-two-qb-baseline-gap"))
        database.writer.execute(
            "UPDATE task SET seq = NULL WHERE project_id = 'demo' AND task_id = ?",
            ("task-017-two-qb-baseline-gap",),
        )
        database.writer.commit()
        assert store.generate_task_id() == "task-001", "the state this repair is for"

        database.writer.executescript(
            (MIGRATIONS_DIR / "003_seq_is_the_number_after_the_prefix.sql").read_text(
                encoding="utf-8"
            )
        )

        assert self._stored(database, "task-017-two-qb-baseline-gap") == 17
        assert store.generate_task_id() == "task-018"

    def test_it_leaves_an_id_with_no_number_alone(
        self, store: SqlTaskStore, database: Database
    ) -> None:
        store.save_task(make_task("task-lint-debt"))

        database.writer.executescript(
            (MIGRATIONS_DIR / "003_seq_is_the_number_after_the_prefix.sql").read_text(
                encoding="utf-8"
            )
        )

        assert self._stored(database, "task-lint-debt") is None

    def test_running_it_on_a_correct_database_changes_nothing(
        self, store: SqlTaskStore, database: Database
    ) -> None:
        """It ships as a numbered migration, so it runs once -- but a repair that is
        only safe once is a repair nobody dares re-run."""
        store.save_task(make_task("task-004-a-slug", queue_position=100))
        store.save_task(make_task("task-011", queue_position=200))
        before = {
            "task-004-a-slug": self._stored(database, "task-004-a-slug"),
            "task-011": self._stored(database, "task-011"),
        }

        sql = (MIGRATIONS_DIR / "003_seq_is_the_number_after_the_prefix.sql").read_text(
            encoding="utf-8"
        )
        database.writer.executescript(sql)
        database.writer.executescript(sql)

        assert before == {"task-004-a-slug": 4, "task-011": 11}
        assert {
            "task-004-a-slug": self._stored(database, "task-004-a-slug"),
            "task-011": self._stored(database, "task-011"),
        } == before
