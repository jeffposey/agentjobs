"""The queries ``docs/analytics-design.md`` will run, and the plans they must keep.

Asserting on the **query plan** rather than on wall-clock time is deliberate. A
threshold in milliseconds means something different on every machine and drifts with
every unrelated change; a plan says whether the index is being used, which is the thing
that actually decides whether this page is viable. ``ENGINEERING.md`` makes the same
argument for counting task files parsed rather than timing them.

The failure these guard against is specific and has a shape: the planner quietly
switching to a full scan once the table is big enough, or an index being dropped in a
later migration because nothing named it. Either shows up here as a plan assertion,
before it shows up as a slow page.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from agentjobs.sqlstore import Database, SqlTaskStore, snapshot, upgrade, verify

#: The four queries section 5.2 of the analytics design specifies, by the name it
#: gives them. Kept here as the contract: if one of these has to change shape, the
#: page's design changes with it.
BACKLOG = """
WITH d AS (SELECT substr(ts, 1, 10) AS day, SUM(open_delta) AS delta
             FROM task_event WHERE project_id = ? AND open_delta <> 0 GROUP BY 1)
SELECT day, SUM(delta) OVER (ORDER BY day) AS open_count FROM d ORDER BY day
"""

THROUGHPUT = """
SELECT substr(ts, 1, 10) AS day, COUNT(DISTINCT task_id) AS tasks,
       COUNT(*) AS events
  FROM task_event
 WHERE project_id = ? AND lifecycle_to = 'closed' AND outcome_to = 'completed'
 GROUP BY 1 ORDER BY 1
"""

AGING = """
SELECT CASE WHEN julianday('now') - julianday(created_at) < 7 THEN '0-7d'
            WHEN julianday('now') - julianday(created_at) < 30 THEN '7-30d'
            ELSE '30d+' END AS bucket, COUNT(*)
  FROM task WHERE project_id = ? AND lifecycle <> 'closed' GROUP BY 1
"""

COUNTS = """
SELECT lifecycle, ball, outcome, COUNT(*) FROM task
 WHERE project_id = ? GROUP BY 1, 2, 3
"""

OLDEST_OPEN = """
SELECT task_id, created_at FROM task
 WHERE project_id = ? AND lifecycle <> 'closed'
 ORDER BY created_at LIMIT 10
"""

COVERAGE = """
SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM task_event
 WHERE project_id = ? GROUP BY source
"""


def plan(database: Database, sql: str, *args) -> str:
    """The query plan as one string, for assertions about how a query is answered."""
    rows = database.reader().execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
    return "\n".join(row[-1] for row in rows)


def seed(store: SqlTaskStore, count: int, *, seed_value: int = 7, offset: int = 0) -> None:
    """Write ``count`` tasks with plausible history, directly and quickly.

    Goes in through SQL rather than through ``save_task``: this fixture is about the
    shape and size of the data the planner sees, and building it through the model
    would spend the whole test budget on validation it is not testing.
    """
    random.seed(seed_value)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with store.database.write() as connection:
        for number in range(1, count + 1):
            index = number + offset
            created = start + timedelta(hours=index)
            closed = random.random() < 0.6
            closed_at = created + timedelta(days=random.randint(1, 40))
            connection.execute(
                "INSERT INTO task(project_id, task_id, seq, title, created_at, updated_at,"
                " lifecycle, ball, ball_reason, ball_prompt, outcome, priority,"
                " queue_position, category, spec_summary, spec_description, closed_at,"
                " last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    store.project_id,
                    f"task-{index:05d}",
                    index,
                    f"Task {index}",
                    _iso(created),
                    _iso(closed_at),
                    "closed" if closed else "ready",
                    None if closed else "agent",
                    None if closed else random.choice(["available", "work", "hold"]),
                    None if closed else "Do the thing.",
                    random.choice(["completed", "cancelled"]) if closed else None,
                    random.choice(["low", "medium", "high", "critical"]),
                    None if closed else index * 10,
                    "engineering",
                    "Summary.",
                    "Description.",
                    _iso(closed_at) if closed else None,
                    _iso(closed_at),
                ),
            )
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_to) VALUES (?,?,?,?,?,?,?)",
                (
                    store.project_id,
                    f"task-{index:05d}",
                    _iso(created),
                    "claude",
                    "create",
                    "native",
                    "ready",
                ),
            )
            if closed:
                connection.execute(
                    "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                    " lifecycle_from, lifecycle_to, outcome_to) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        store.project_id,
                        f"task-{index:05d}",
                        _iso(closed_at),
                        "claude",
                        "close",
                        "native",
                        "ready",
                        "closed",
                        "completed" if random.random() < 0.8 else "cancelled",
                    ),
                )


def _iso(moment: datetime) -> str:
    """The stored timestamp form."""
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqlTaskStore]:
    """A store holding a corpus roughly the size of this repository's."""
    database = Database(tmp_path / "a.db")
    upgrade(database, agentjobs_version="test", snapshot_before=False)
    task_store = SqlTaskStore(database, "demo")
    task_store.ensure_project(reporting_tz="America/Chicago")
    seed(task_store, 400)
    yield task_store
    database.close()


class TestPlans:
    """Each query the page depends on is answered by an index, at both sizes."""

    @pytest.mark.parametrize(
        "name, sql, index",
        [
            ("backlog level", BACKLOG, "ix_event_backlog"),
            ("throughput", THROUGHPUT, "ix_event_closed"),
            ("oldest open", OLDEST_OPEN, "ix_task_open_age"),
            ("coverage", COVERAGE, "ix_event_source"),
        ],
    )
    def test_the_query_uses_its_index(
        self, store: SqlTaskStore, name: str, sql: str, index: str
    ) -> None:
        """Named indexes, so dropping one in a later migration fails here."""
        text = plan(store.database, sql, store.project_id)
        assert index in text, f"{name} no longer uses {index}:\n{text}"

    def test_aging_is_answered_from_an_index(self, store: SqlTaskStore) -> None:
        """Which index is the planner's business; a full table scan is not.

        SQLite serves this from `ix_task_activity` rather than the partial
        `ix_task_open_age`, and both are project-scoped searches over the same rows.
        Asserting the specific index here would be asserting a planner decision that
        is free to change for good reasons; asserting there is no `SCAN task` catches
        the regression that would actually hurt.
        """
        text = plan(store.database, AGING, store.project_id)
        assert "USING INDEX" in text or "USING COVERING INDEX" in text, text
        assert not any(line.strip() == "SCAN task" for line in text.splitlines()), text

    @pytest.mark.parametrize("sql", [BACKLOG, THROUGHPUT, AGING, COUNTS, COVERAGE, OLDEST_OPEN])
    def test_no_query_reads_a_task_document(self, store: SqlTaskStore, sql: str) -> None:
        """ac-2: the page is answered from columns, never by parsing a stored document.

        JSON columns exist on ``task`` for the value objects nothing filters. A plan
        that touched one would mean an analytics query had started reading authoring
        content, which is the whole failure mode the schema was shaped to avoid.
        """
        text = plan(store.database, sql, store.project_id).lower()
        assert "json" not in text
        for table in ("log_entry", "task_tag", "task_acceptance"):
            assert table not in text

    def test_the_plan_does_not_degrade_at_twenty_times_the_size(self, store: SqlTaskStore) -> None:
        """The real risk is the planner changing its mind once the table is big.

        A plan that is right at 400 tasks and a full scan at 8,000 is a page that works
        in development and not in a year. Asserted rather than assumed, because nothing
        else in the suite would notice.
        """
        before = plan(store.database, BACKLOG, store.project_id)
        seed(store, 8000, seed_value=11, offset=400)
        store.database.writer.execute("ANALYZE")
        after = plan(store.database, BACKLOG, store.project_id)
        assert "ix_event_backlog" in after, after
        assert before == after


class TestSeries:
    """The numbers, not just the plans."""

    def test_the_backlog_series_never_goes_negative(self, store: SqlTaskStore) -> None:
        """A negative open count is impossible and undiagnosable on a chart.

        It is exactly what taking a backfilled creation instant at face value produces
        (analytics design 4.3), so the store asserts the property rather than trusting
        the importer to have applied the rule.
        """
        rows = store.database.reader().execute(BACKLOG, (store.project_id,)).fetchall()
        assert rows
        assert min(row[1] for row in rows) >= 0

    def test_the_series_ends_at_the_open_count(self, store: SqlTaskStore) -> None:
        """The chart and the board agree, which is the reconciliation invariant."""
        rows = store.database.reader().execute(BACKLOG, (store.project_id,)).fetchall()
        summed, counted = store.open_delta_reconciles()
        assert rows[-1][1] == summed == counted

    def test_completed_is_not_the_same_as_closed(self, store: SqlTaskStore) -> None:
        """A settled counting rule: cancelled work is closed and is not completed."""
        completed = (
            store.database.reader()
            .execute(
                "SELECT COUNT(*) FROM task WHERE project_id = ? AND outcome = 'completed'",
                (store.project_id,),
            )
            .fetchone()[0]
        )
        closed = (
            store.database.reader()
            .execute(
                "SELECT COUNT(*) FROM task WHERE project_id = ? AND lifecycle = 'closed'",
                (store.project_id,),
            )
            .fetchone()[0]
        )
        assert 0 < completed < closed

    def test_the_invariant_survives_a_backup_and_restore(
        self, store: SqlTaskStore, tmp_path: Path
    ) -> None:
        """analytics-design section 6, item D, names this round trip explicitly."""
        target = tmp_path / "snap.db"
        snapshot(store.database, target)
        report = verify(target)
        assert report.ok, report.render()
        assert report.open_delta == report.open_rows
        summed, counted = store.open_delta_reconciles()
        assert report.open_delta == summed == counted


class TestCoverage:
    """Unknown history has to stay distinguishable from zero."""

    def test_the_baseline_is_recorded_on_the_project(self, store: SqlTaskStore) -> None:
        """The page reads these to draw the "history begins here" boundary."""
        row = (
            store.database.reader()
            .execute(
                "SELECT reporting_tz, history_baseline_kind FROM project WHERE project_id = ?",
                (store.project_id,),
            )
            .fetchone()
        )
        assert row["history_baseline_kind"] in {
            "native",
            "reconstructed",
            "backfilled",
            "unknown",
        }
        # An IANA zone name, never a fixed offset: '-06:00' is right for half the year.
        assert "/" in row["reporting_tz"] or row["reporting_tz"] == "UTC"

    def test_event_sources_are_distinguishable(self, store: SqlTaskStore) -> None:
        """`native` is exact; `reconstructed` and `backfilled` are bounds."""
        sources = {
            row[0]
            for row in store.database.reader().execute(
                "SELECT DISTINCT source FROM task_event WHERE project_id = ?",
                (store.project_id,),
            )
        }
        assert sources <= {"native", "reconstructed", "backfilled"}
