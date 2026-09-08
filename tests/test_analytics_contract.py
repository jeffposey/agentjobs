"""The amendments ``docs/analytics-design.md`` section 6 asks of the store.

Section 6 is the coordination between task-273's schema, task-311's import and the
analytics page task-213 designed. Every item in it was found by measuring the drafted
schema against the real 367-task corpus rather than by reading it, and four of them are
*required*: without them the store is wrong in ways no query plan reveals. This module
is where they are held to.

The items, and where each is enforced:

* **A -- a backfilled creation is clamped to the earliest evidence** (§4.3).
  :class:`TestRuleA`. Git records when a change was *committed*, which postdates it;
  taking commit time literally opened the measured backlog series at **-3**, which is
  impossible and undiagnosable on a chart.
* **B -- the git backfill speaks only for axes the log does not record** (§4.4).
  :class:`TestRuleB`. Backfilling lifecycle as well double-counts every close: measured
  at ``SUM(open_delta) = 69`` against 125 open tasks, a 45% error with no symptom in any
  query plan.
* **C -- ``project.reporting_tz`` is an IANA zone name, not an offset** (§3.5).
  :class:`TestReportingTimezone`.
* **D -- the reconciliation invariant, after import, after backfill, and after a
  backup/restore round trip.** :class:`TestReconciliationInvariant`. Section 6 names it
  a dependency rather than a suggestion, because the chart and the board silently
  disagreeing is the failure the whole history contract exists to prevent.
* **E, F, G** are two indexes and a deferred foreign key, already in
  ``001_initial.sql``. ``tests/test_sqlstore_analytics.py::TestPlans`` asserts the
  indexes are still the ones answering their queries, and
  ``test_sqlstore.py::TestInvariants::test_a_parent_may_arrive_before_its_child`` the
  deferral. Nothing is repeated here.
* **H -- ``mechanical`` is populated, not merely defined.** :class:`TestRuleH`. A column
  that is always ``0`` is worse than absent, because the query over it looks correct.

**The corpus here is a real git repository**, committed at chosen instants, because
every one of these rules is about the relationship between what a commit says and when
it says it. A fixture that stubbed git out could not fail for any of the reasons these
tests exist for.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from agentjobs.sqlstore import (
    Database,
    ImportReport,
    SqlTaskStore,
    restore,
    snapshot,
    upgrade,
    verify,
)
from agentjobs.sqlstore.backfill import BACKFILL_FIELDS
from agentjobs.sqlstore.importer import CorpusImporter
from agentjobs.sqlstore.reporting_tz import ReportingTimezoneError, check_reporting_tz

#: The backlog-level query from analytics-design section 5.2, verbatim. A negative
#: level is visible on it and on nothing else.
BACKLOG = """
WITH d AS (SELECT substr(ts, 1, 10) AS day, SUM(open_delta) AS delta
             FROM task_event WHERE project_id = ? AND open_delta <> 0 GROUP BY 1)
SELECT day, SUM(delta) OVER (ORDER BY day) AS open_count FROM d ORDER BY day
"""


# ---------------------------------------------------------------------------
# Building a corpus that has a git history
# ---------------------------------------------------------------------------


def _record(
    task_id: str,
    *,
    title: str,
    created: str,
    updated: str,
    lifecycle: str = "ready",
    priority: str = "high",
    position: Optional[int] = 100,
    log: str = "",
) -> str:
    """One task document, in the shape a ``files`` project would have committed."""
    lines = [
        "schema: 2",
        f"id: {task_id}",
        f"title: {title}",
        f"created: '{created}'",
        f"updated: '{updated}'",
        f"lifecycle: {lifecycle}",
    ]
    if lifecycle == "closed":
        lines.append("outcome: completed")
    else:
        lines.extend(["ball: agent", "ball_reason: available"])
    lines.append("archived: false")
    lines.append(f"priority: {priority}")
    if position is not None and lifecycle != "closed":
        lines.append(f"queue_position: {position}")
    lines.extend(
        [
            "category: engineering",
            "spec:",
            "  summary: A summary.",
            "  description: A description.",
            "log:" if log else "log: []",
        ]
    )
    return "\n".join(lines) + "\n" + log


def _transition(entry_id: int, ts: str, body: str, data: Dict[str, str]) -> str:
    """One ``transition`` log entry, indented as PyYAML would have dumped it."""
    payload = "".join(f"    {key}: {value}\n" for key, value in data.items())
    return (
        f"- id: {entry_id}\n"
        f"  ts: '{ts}'\n"
        "  actor: claude\n"
        "  type: transition\n"
        f"  body: {body}\n"
        "  data:\n" + payload
    )


def _queue_move(entry_id: int, ts: str, *, to: int) -> str:
    """One ``queue_move`` log entry -- the per-task record of a deliberate move."""
    return (
        f"- id: {entry_id}\n"
        f"  ts: '{ts}'\n"
        "  actor: claude\n"
        "  type: queue_move\n"
        "  body: Moved.\n"
        "  data:\n"
        f"    to: {to}\n"
    )


class Corpus:
    """A tasks directory inside a real git repository, committed at chosen instants."""

    def __init__(self, root: Path) -> None:
        """Initialise an empty repository with a ``tasks/demo`` directory in it."""
        self.root = root
        self.tasks = root / "tasks" / "demo"
        self.tasks.mkdir(parents=True)
        self.commits: List[str] = []
        self._git("init", "--initial-branch=main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")

    def _git(self, *args: str, env: Optional[Dict[str, str]] = None) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            env={**os.environ, **(env or {})},
        )

    def commit(self, files: Dict[str, str], *, at: str, message: str = "chore") -> None:
        """Write ``files`` into the tasks directory and commit them at instant ``at``.

        The instant is forced onto the committer date, which is what
        :func:`agentjobs.sqlstore.backfill.observe` reads. Forced rather than allowed to
        default, because a committer date taken from the clock would make every
        assertion below depend on how long the test took to run.
        """
        for name, text in files.items():
            (self.tasks / name).write_text(text, encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "commit",
            "-m",
            message,
            env={"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at},
        )
        self.commits.append(at)


def _import(
    path: Path,
    corpus: Corpus,
    *,
    backfill_git: bool,
) -> Tuple[Database, SqlTaskStore, ImportReport]:
    """Import ``corpus`` into a fresh store at ``path``."""
    database = Database(path)
    upgrade(database, agentjobs_version="test", snapshot_before=False)
    store = SqlTaskStore(database, "demo")
    report = CorpusImporter(store, corpus.tasks).run(
        reporting_tz="America/Chicago", backfill_git=backfill_git
    )
    return database, store, report


def _rows(database: Database, sql: str, *args: Any) -> List[Dict[str, Any]]:
    """Query the store and hand back plain dicts."""
    return [dict(row) for row in database.reader().execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Rule A -- commit time is not change time
# ---------------------------------------------------------------------------


@pytest.fixture()
def closed_before_committed(tmp_path: Path) -> Iterator[Corpus]:
    """A record whose log closes it a day before git first sees the file.

    The shape section 4.3 measured on three real records twenty hours apart. It has no
    creation entry -- 79 of the live corpus's tasks have none -- so the import must
    synthesise one, and which instant it chooses is the whole of rule A.
    """
    corpus = Corpus(tmp_path / "repo")
    corpus.commit(
        {
            "task-001.yaml": _record(
                "task-001",
                title="Closed before it was committed",
                created="2026-01-01T00:00:00Z",
                updated="2026-01-02T00:00:00Z",
                lifecycle="closed",
                log=_transition(
                    1,
                    "2026-01-02T00:00:00Z",
                    "Closed by claude.",
                    {"lifecycle": "closed", "outcome": "completed"},
                ),
            )
        },
        at="2026-01-04T20:00:00+00:00",
    )
    yield corpus


class TestRuleA:
    """A backfilled creation is the earliest evidence the task existed."""

    def test_the_creation_is_the_records_own_created_not_the_commit(
        self, closed_before_committed: Corpus, tmp_path: Path
    ) -> None:
        """``min(commit time, created, first log entry)`` -- and here that is `created`.

        The commit is two days after the close. Taking it literally would put the
        creation *after* the close, which is where the measured -3 came from.
        """
        database, _, _ = _import(
            tmp_path / "a.db", closed_before_committed, backfill_git=True
        )
        try:
            created = _rows(
                database,
                "SELECT ts FROM task_event WHERE project_id='demo' AND kind='create'",
            )
            assert [row["ts"] for row in created] == ["2026-01-01T00:00:00Z"]
        finally:
            database.close()

    def test_no_event_predates_the_creation(
        self, closed_before_committed: Corpus, tmp_path: Path
    ) -> None:
        """The ordering the clamp exists to make possible, asserted directly."""
        database, _, _ = _import(
            tmp_path / "a.db", closed_before_committed, backfill_git=True
        )
        try:
            events = _rows(
                database,
                "SELECT ts, kind FROM task_event WHERE project_id='demo' ORDER BY ts",
            )
            assert events[0]["kind"] == "create"
            assert all(row["ts"] >= events[0]["ts"] for row in events)
        finally:
            database.close()

    def test_the_backlog_series_never_goes_negative(
        self, closed_before_committed: Corpus, tmp_path: Path
    ) -> None:
        """The symptom a reader would see, and could not diagnose.

        Without the clamp this corpus draws -1 on the close's day and climbs back to 0
        two days later, because the create lands in the *later* day bucket.
        """
        database, _, _ = _import(
            tmp_path / "a.db", closed_before_committed, backfill_git=True
        )
        try:
            series = database.reader().execute(BACKLOG, ("demo",)).fetchall()
            assert series
            assert min(row[1] for row in series) >= 0
        finally:
            database.close()


# ---------------------------------------------------------------------------
# Rules B and H -- what git may speak for, and which of it is not activity
# ---------------------------------------------------------------------------


@pytest.fixture()
def renumbered(tmp_path: Path) -> Iterator[Corpus]:
    """Three open tasks, a bulk renumber, a logged move, and a lone unlogged one.

    Five commits, each shaped to make one distinction bite:

    1.  The three records appear, plus a fourth that will be closed later.
    2.  One commit rewrites ``queue_position`` on all three, and **no record logs it**.
        This is the bulk renumber of section 8.7 -- item H's whole subject.
    3.  ``task-001`` moves again and **does** log a ``queue_move`` at the same instant:
        a deliberate move, which de-duplicates against the git observation and should
        leave no backfilled event at all.
    4.  ``task-002`` moves alone with no log entry. One task is not a bulk renumber.
    5.  ``task-004`` is closed, in the log. Git sees the lifecycle change too, and
        rule B says it must not be taken from there.
    """
    corpus = Corpus(tmp_path / "repo")
    opening = _transition(
        1, "2026-02-01T00:00:00Z", "Created ready by claude.", {"lifecycle": "ready"}
    )
    corpus.commit(
        {
            "task-001.yaml": _record(
                "task-001",
                title="First",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-01T00:00:00Z",
                position=100,
                log=opening,
            ),
            "task-002.yaml": _record(
                "task-002",
                title="Second",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-01T00:00:00Z",
                position=200,
                log=opening,
            ),
            "task-003.yaml": _record(
                "task-003",
                title="Third",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-01T00:00:00Z",
                position=300,
                log=opening,
            ),
            "task-004.yaml": _record(
                "task-004",
                title="Fourth",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-01T00:00:00Z",
                position=400,
                log=opening,
            ),
        },
        at="2026-02-01T01:00:00+00:00",
        message="chore: four tasks",
    )
    corpus.commit(
        {
            "task-001.yaml": _record(
                "task-001",
                title="First",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-05T00:00:00Z",
                position=10,
                log=opening,
            ),
            "task-002.yaml": _record(
                "task-002",
                title="Second",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-05T00:00:00Z",
                position=20,
                log=opening,
            ),
            "task-003.yaml": _record(
                "task-003",
                title="Third",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-05T00:00:00Z",
                position=30,
                log=opening,
            ),
        },
        at="2026-02-05T00:00:00+00:00",
        message="chore: renumber the band",
    )
    corpus.commit(
        {
            "task-001.yaml": _record(
                "task-001",
                title="First",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-10T00:00:00Z",
                position=5,
                log=opening + _queue_move(2, "2026-02-10T00:00:00Z", to=5),
            )
        },
        at="2026-02-10T00:00:00+00:00",
        message="chore: a deliberate move, logged",
    )
    corpus.commit(
        {
            "task-002.yaml": _record(
                "task-002",
                title="Second",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-15T00:00:00Z",
                position=15,
                log=opening,
            )
        },
        at="2026-02-15T00:00:00+00:00",
        message="chore: one task moves, unlogged",
    )
    corpus.commit(
        {
            "task-004.yaml": _record(
                "task-004",
                title="Fourth",
                created="2026-02-01T00:00:00Z",
                updated="2026-02-20T00:00:00Z",
                lifecycle="closed",
                position=None,
                log=opening
                + _transition(
                    2,
                    "2026-02-20T00:00:00Z",
                    "Closed by claude.",
                    {"lifecycle": "closed", "outcome": "completed"},
                ),
            )
        },
        at="2026-02-20T00:00:00+00:00",
        message="chore: close one",
    )
    yield corpus


class TestRuleB:
    """Git is authoritative only for the axes the log does not record."""

    def test_lifecycle_and_outcome_are_not_backfillable_axes(self) -> None:
        """The list itself, because it is the one place the rule can be broken quietly.

        Adding ``lifecycle`` here would look like an improvement -- more history from
        the same pass -- and would put a 45% error in the backlog level.
        """
        assert "lifecycle" not in BACKFILL_FIELDS
        assert "outcome" not in BACKFILL_FIELDS
        assert set(BACKFILL_FIELDS) == {"priority", "archived", "queue_position", "parent"}

    def test_no_backfilled_event_moves_lifecycle_or_outcome(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """Behaviour rather than configuration: the rows themselves say so."""
        database, _, _ = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            leaked = _rows(
                database,
                "SELECT COUNT(*) AS n FROM task_event WHERE project_id='demo'"
                " AND source='backfilled' AND (lifecycle_to IS NOT lifecycle_from"
                " OR outcome_to IS NOT outcome_from)",
            )
            assert leaked[0]["n"] == 0
        finally:
            database.close()

    def test_the_close_is_counted_once_not_twice(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """The measured failure, stated as the thing a reader would notice.

        ``task-004`` closes once, in its log, and git sees the same change again at
        commit time. Backfilling it would emit a second ``-1``.

        Counted on ``open_delta`` rather than on ``lifecycle_to``: every event carries
        the whole state on both sides, so a *later* event on a closed task reads
        ``lifecycle_to = 'closed'`` too without being a close. That distinction is the
        column's job, and counting the wrong one is how a 45% error stays invisible.
        """
        database, _, _ = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            closes = _rows(
                database,
                "SELECT COUNT(*) AS n, COALESCE(SUM(open_delta), 0) AS total"
                " FROM task_event WHERE project_id='demo' AND task_id='task-004'"
                " AND open_delta = -1",
            )
            assert closes[0]["n"] == 1
            net = _rows(
                database,
                "SELECT COALESCE(SUM(open_delta), 0) AS total FROM task_event"
                " WHERE project_id='demo' AND task_id='task-004'",
            )
            assert net[0]["total"] == 0
        finally:
            database.close()

    def test_the_backfill_does_not_change_the_backlog_level(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """The strongest form of the rule, and the one the 45% error would fail.

        Git may add priority, parent, archived and position history. It may not change
        how many tasks the store thinks are open -- that axis comes from the log, so
        importing the same corpus with and without the backfill has to land on the same
        level.
        """
        plain_db, plain, _ = _import(tmp_path / "plain.db", renumbered, backfill_git=False)
        filled_db, filled, _ = _import(tmp_path / "filled.db", renumbered, backfill_git=True)
        try:
            assert plain.open_delta_reconciles() == filled.open_delta_reconciles()
            assert filled_db.reader().execute(
                "SELECT COUNT(*) FROM task_event WHERE project_id='demo'"
                " AND source='backfilled'"
            ).fetchone()[0] > 0
        finally:
            plain_db.close()
            filled_db.close()


class TestRuleH:
    """``mechanical`` marks the renumbers nobody decided on."""

    @pytest.fixture()
    def imported(self, renumbered: Corpus, tmp_path: Path) -> Iterator[Database]:
        """The renumbered corpus, backfilled."""
        database, _, _ = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            yield database
        finally:
            database.close()

    def test_a_bulk_renumber_is_marked(self, imported: Database) -> None:
        """One commit renumbered three tasks and none of them logged it."""
        marked = _rows(
            imported,
            "SELECT task_id FROM task_event WHERE project_id='demo' AND mechanical=1"
            " ORDER BY task_id",
        )
        assert [row["task_id"] for row in marked] == ["task-001", "task-002", "task-003"]

    def test_a_lone_unlogged_move_is_not_a_bulk_renumber(self, imported: Database) -> None:
        """``task-002`` moves alone in a later commit; one task is not a renumber.

        The distinction matters because marking it would drop a real change out of the
        activity series, which is the opposite of what item H is for.
        """
        rows = _rows(
            imported,
            "SELECT ts, mechanical FROM task_event WHERE project_id='demo'"
            " AND task_id='task-002' AND kind='queue_move' AND source='backfilled'"
            " ORDER BY ts",
        )
        assert [(row["ts"], row["mechanical"]) for row in rows] == [
            ("2026-02-05T00:00:00Z", 1),
            ("2026-02-15T00:00:00Z", 0),
        ]

    def test_a_logged_move_leaves_no_backfilled_event_at_all(
        self, imported: Database
    ) -> None:
        """De-duplication, which is what makes the survivors mean something.

        ``task-001`` moved deliberately on 2026-02-10 and logged it. The git
        observation of the same change is dropped, so the only position event that day
        is the native one.
        """
        rows = _rows(
            imported,
            "SELECT source FROM task_event WHERE project_id='demo' AND task_id='task-001'"
            " AND kind='queue_move' AND ts LIKE '2026-02-10%'",
        )
        assert [row["source"] for row in rows] == ["reconstructed"]

    def test_nothing_but_a_position_change_is_ever_mechanical(
        self, imported: Database
    ) -> None:
        """A grooming pass over seven priorities is seven decisions, and is activity.

        Widening the mark to every axis a commit touched in bulk was considered and
        rejected: the design names the renumber, and only the renumber is a side effect
        nobody chose.
        """
        kinds = _rows(
            imported,
            "SELECT DISTINCT kind, source FROM task_event WHERE project_id='demo'"
            " AND mechanical=1",
        )
        assert kinds == [{"kind": "queue_move", "source": "backfilled"}]

    def test_a_marked_event_never_moves_the_backlog_level(
        self, imported: Database
    ) -> None:
        """Excluding mechanical rows from activity must not be able to break Q1.

        The page filters them out of the activity series while leaving the backlog
        alone. That is only safe if a renumber's ``open_delta`` is zero, which the
        generated column guarantees and this asserts.
        """
        moved = _rows(
            imported,
            "SELECT COUNT(*) AS n FROM task_event WHERE project_id='demo'"
            " AND mechanical=1 AND open_delta <> 0",
        )
        assert moved[0]["n"] == 0

    def test_an_import_without_git_marks_nothing(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """A renumber is only visible in git; the log is where it is *not* recorded."""
        database, _, _ = _import(tmp_path / "a.db", renumbered, backfill_git=False)
        try:
            assert (
                database.reader()
                .execute(
                    "SELECT COUNT(*) FROM task_event WHERE project_id='demo'"
                    " AND mechanical=1"
                )
                .fetchone()[0]
                == 0
            )
        finally:
            database.close()


# ---------------------------------------------------------------------------
# Rule C -- the reporting timezone
# ---------------------------------------------------------------------------


class TestReportingTimezone:
    """``reporting_tz`` holds a zone name, because an offset is right half the year."""

    @pytest.mark.parametrize("name", ["UTC", "America/Chicago", "Europe/London", "Etc/GMT+6"])
    def test_a_zone_name_is_accepted(self, name: str) -> None:
        """Including the ``Etc/`` names, which look like offsets and are not."""
        assert check_reporting_tz(name) == name

    @pytest.mark.parametrize("offset", ["-06:00", "+05:30", "-0600", "+00:00", "-6"])
    def test_a_fixed_offset_is_refused_and_says_why(self, offset: str) -> None:
        """The message has to carry the argument: the caller typed this deliberately.

        Task-273's first offer was ``date(ts, :tz)`` over an offset out of this column,
        so somebody following that draft will type one and needs to be told which half
        of the year it is wrong for, not merely that it is invalid.
        """
        with pytest.raises(ReportingTimezoneError) as raised:
            check_reporting_tz(offset)
        assert "America/Chicago" in str(raised.value)

    @pytest.mark.parametrize("value", ["", "   ", "America/Chicago (CST)", "Mars/Olympus"])
    def test_anything_that_is_not_a_zone_is_refused(self, value: str) -> None:
        """Empty, decorated, and plausible-but-unreal all fail the same way."""
        with pytest.raises(ReportingTimezoneError):
            check_reporting_tz(value)

    def test_the_store_refuses_an_offset_and_writes_no_row(self, tmp_path: Path) -> None:
        """``ensure_project`` is the one door this value comes through today."""
        database = Database(tmp_path / "a.db")
        upgrade(database, agentjobs_version="test", snapshot_before=False)
        try:
            store = SqlTaskStore(database, "demo")
            with pytest.raises(ReportingTimezoneError):
                store.ensure_project(reporting_tz="-06:00")
            assert (
                database.reader().execute("SELECT COUNT(*) FROM project").fetchone()[0] == 0
            )
        finally:
            database.close()

    @pytest.mark.parametrize(
        "sql, args",
        [
            (
                "INSERT INTO project(project_id, created_at, reporting_tz)"
                " VALUES ('other', '2026-01-01T00:00:00Z', ?)",
                ("-06:00",),
            ),
            ("UPDATE project SET reporting_tz = ? WHERE project_id = 'demo'", ("-06:00",)),
        ],
    )
    def test_the_database_refuses_a_writer_that_skips_the_guard(
        self, tmp_path: Path, sql: str, args: tuple
    ) -> None:
        """Migration 002: the rule as a constraint, for the writer that is not Python.

        The Python guard covers ``ensure_project``. This covers the operator at a
        sqlite3 prompt and whatever sets this column next -- the same reason every other
        invariant in this schema is a ``CHECK`` rather than a validator.
        """
        database = Database(tmp_path / "a.db")
        upgrade(database, agentjobs_version="test", snapshot_before=False)
        try:
            SqlTaskStore(database, "demo").ensure_project(reporting_tz="America/Chicago")
            with pytest.raises(sqlite3.IntegrityError, match="IANA zone name"):
                with database.write() as connection:
                    connection.execute(sql, args)
        finally:
            database.close()


# ---------------------------------------------------------------------------
# Rule D -- the invariant, at every point section 6 names
# ---------------------------------------------------------------------------


class TestReconciliationInvariant:
    """``SUM(open_delta)`` equals the number of open tasks. Everywhere, always.

    Section 6 item D names three moments, and they fail for different reasons: an
    import can replay a history that does not add up, a backfill can double-count, and
    a backup can be taken mid-write. One test each.
    """

    def test_after_an_import(self, renumbered: Corpus, tmp_path: Path) -> None:
        """Replay plus reconciliation, with no git in it."""
        database, store, report = _import(tmp_path / "a.db", renumbered, backfill_git=False)
        try:
            summed, counted = store.open_delta_reconciles()
            assert summed == counted == 3
            assert report.open_delta == report.open_rows == counted
        finally:
            database.close()

    def test_after_a_git_backfill(self, renumbered: Corpus, tmp_path: Path) -> None:
        """The moment the 45% error was measured at, and the one with no symptom."""
        database, store, report = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            summed, counted = store.open_delta_reconciles()
            assert summed == counted == 3
            assert report.open_delta == report.open_rows == counted
        finally:
            database.close()

    def test_the_series_ends_where_the_board_stands(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """The invariant is only worth asserting because this is what it buys.

        The last point of the backlog chart is the number of open tasks on the board.
        A reader who saw those two disagree would have no way to tell which was lying.
        """
        database, store, _ = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            series = database.reader().execute(BACKLOG, ("demo",)).fetchall()
            summed, counted = store.open_delta_reconciles()
            assert series[-1][1] == summed == counted
        finally:
            database.close()

    def test_after_a_backup_and_restore_round_trip(
        self, renumbered: Corpus, tmp_path: Path
    ) -> None:
        """Snapshot, verify, restore somewhere else, and count again in the copy.

        ``verify`` checks the invariant inside the snapshot, which is what makes a
        backup refusable rather than merely written. Opening the restored copy and
        counting again is the other half: a snapshot that verifies and restores to
        something different would pass the first check and fail here.
        """
        database, store, _ = _import(tmp_path / "a.db", renumbered, backfill_git=True)
        try:
            expected = store.open_delta_reconciles()
            target = tmp_path / "snap.db"
            snapshot(database, target)
            report = verify(target)
            assert report.ok, report.render()
            assert report.open_delta == report.open_rows == expected[0]

            destination = tmp_path / "restored.db"
            assert restore(target, destination).ok
        finally:
            database.close()

        restored = Database(destination)
        try:
            assert SqlTaskStore(restored, "demo").open_delta_reconciles() == expected
        finally:
            restored.close()
