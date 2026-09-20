"""The analytics projection: one read of the store, the whole page's answer.

``docs/analytics-design.md`` section 7 is the specification and section 3 the semantics.
This module is where both become code. It reads three tables -- ``task``, ``task_event``
and ``project`` -- through indexed range scans, and **it parses no task document and
touches no JSON column**; that is the entire reason the page waited for task-273's
storage rather than being built on the corpus (section 5.1: an analytics endpoint over
YAML would have cost more than the 790 ms ``/tasks`` did).

Three properties are worth knowing before reading anything below, because each is a
measured decision rather than a preference:

*   **SQL filters instants; it never names a day** (section 3.5). SQLite has no timezone
    database and its ``date()`` modifier takes a fixed offset, which is wrong for half of
    every year in any zone that observes DST. Bucketing happens here, in Python, through
    ``zoneinfo`` and the project's IANA ``reporting_tz``. Measured: 0.91 ms, against
    58.8 ms for the correct SQL form and 0.24 ms for the wrong one.
*   **The calendar spine is filled here** (section 3.5). Only 8% of days in this
    project's history carry a backlog event, so a series returned as grouped rows would
    say nothing about the other 92% -- and a chart drawn from it would imply the level
    was undefined between the points.
*   **Unknown history is never zero** (section 3.6). Every series starts at the coverage
    baseline, a range reaching back past it is clipped to it, and a bucket containing a
    reconstructed or backfilled event is marked ``estimated`` so the page can hatch the
    right days instead of guessing a prefix.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RangeKey = str
"""One of ``30d``, ``90d``, ``12m``, ``all`` -- section 7.1's whole parameter surface."""

RANGE_KEYS: Tuple[str, ...] = ("30d", "90d", "12m", "all")
DEFAULT_RANGE: RangeKey = "30d"

#: How the backlog and holder spines are bucketed, per section 8.3: "day for 30d and
#: 90d, week for 12m, week for all".
SPINE_BUCKET: Dict[str, str] = {"30d": "day", "90d": "day", "12m": "week", "all": "week"}

#: How throughput is bucketed: the spine grain, per section 19.2. The coarser grain
#: this used to carry existed only so the cycle-time percentile drawn over the bars had
#: a sample; section 19.2 moved cycle time off this chart, and what is left is a count,
#: which is honest at any grain. Kept as its own table rather than aliased to
#: :data:`SPINE_BUCKET`, so a future divergence is an edit here rather than a rewrite.
THROUGHPUT_BUCKET: Dict[str, str] = dict(SPINE_BUCKET)

#: Section 8.5's four age bands, as ``(label, exclusive upper bound in days)``. The last
#: bound is ``None``: "90d+" is everything left.
AGE_BANDS: Tuple[Tuple[str, Optional[int]], ...] = (
    ("0-6d", 7),
    ("7-29d", 30),
    ("30-89d", 90),
    ("90d+", None),
)

#: Sources whose timestamps are a bound rather than an observation (section 3.6). A
#: bucket containing one of these is ``estimated``.
INEXACT_SOURCES: Tuple[str, ...] = ("reconstructed", "backfilled")


# ---------------------------------------------------------------------------
# Time, in the project's own zone
# ---------------------------------------------------------------------------


def resolve_zone(name: Optional[str]) -> ZoneInfo:
    """The project's reporting zone, falling back to UTC rather than failing.

    ``reporting_tz`` is validated on write (``sqlstore.reporting_tz``), so a name that
    does not resolve here means this machine has no timezone database for it -- a
    deployment fact, not a data fault. A page that renders in UTC and says so is worth
    more than a 500, and the zone it used is on every response.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_instant(value: Optional[str]) -> Optional[datetime]:
    """A stored ISO-8601 timestamp as an aware UTC datetime, or ``None``.

    Every ``ts`` column is written by ``_iso`` in the store, which emits ``Z``-suffixed
    UTC. ``fromisoformat`` before Python 3.11 refuses ``Z``, and a naive value is read as
    UTC rather than as local time -- reading it as local would silently shift the whole
    history by the reporting offset.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_day(instant: datetime, zone: ZoneInfo) -> date:
    """The calendar day ``instant`` fell on, in ``zone``."""
    return instant.astimezone(zone).date()


def day_start_utc(day: date, zone: ZoneInfo) -> datetime:
    """The UTC instant at which ``day`` begins in ``zone``."""
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)


def bucket_start(day: date, bucket: str) -> date:
    """The first day of the ``bucket`` containing ``day``.

    Weeks start on Monday, which is what ``date.weekday`` already means and what the
    week-commencing labels on the chart read as.
    """
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def next_bucket(day: date, bucket: str) -> date:
    """The first day of the bucket after the one beginning at ``day``."""
    if bucket == "week":
        return day + timedelta(days=7)
    if bucket == "month":
        if day.month == 12:
            return date(day.year + 1, 1, 1)
        return date(day.year, day.month + 1, 1)
    return day + timedelta(days=1)


def spine(first: date, last: date, bucket: str) -> List[date]:
    """Every bucket from the one containing ``first`` to the one containing ``last``.

    This is section 3.5's filled calendar, and it is built regardless of what the store
    returned: 8% of days in this project carry a backlog event, and the page must be able
    to say what the level was on the other 92% rather than leave a gap a reader will
    interpolate across.
    """
    cursor = bucket_start(first, bucket)
    stop = bucket_start(last, bucket)
    days: List[date] = []
    while cursor <= stop:
        days.append(cursor)
        cursor = next_bucket(cursor, bucket)
    return days


def bucket_for(range_key: str, *, throughput: bool = False) -> str:
    """The bucket grain for one range, for the spine or for throughput.

    **The two tables now agree, and what changed is the argument for keeping both.**
    Until section 19.2 throughput was one grain coarser on purpose: the chart carried a
    cycle-time percentile line over its bars, and a daily grain would have left every
    bucket below section 8.4's ``sample < 3`` suppression threshold. Cycle time moved to
    its own panel (S1, section 18.1), so what is left here is a count of completions,
    and the owner's question -- *completed per day* -- is answered at the spine grain.

    ``AnalyticsRange.throughput_bucket`` stays, now returning the same answer as
    ``bucket``. Removing it would make every client infer one series' grain from
    another's, which is the drift section 7.2 argues a response shape should prevent;
    keeping it costs one field and leaves a future divergence to a table rather than to
    a rewrite.
    """
    table = THROUGHPUT_BUCKET if throughput else SPINE_BUCKET
    return table.get(range_key, table[DEFAULT_RANGE])


def window_first_day(range_key: str, today: date) -> date:
    """The first local day a range covers, before it is clipped to the coverage baseline.

    ``all`` has no answer of its own -- it means "as far back as the store will speak
    for" -- so it returns ``today`` and the caller replaces it with the baseline.
    """
    if range_key == "30d":
        return today - timedelta(days=29)
    if range_key == "90d":
        return today - timedelta(days=89)
    if range_key == "12m":
        year = today.year - 1
        try:
            return today.replace(year=year)
        except ValueError:  # 29 February in a year the previous one did not have
            return date(year, 3, 1)
    return today


# ---------------------------------------------------------------------------
# The rows the store is asked for
# ---------------------------------------------------------------------------

#: Section 5.2's Q1. ``open_delta`` is a STORED generated column and ``ix_event_backlog``
#: is partial on it, so this is a sum over one indexed integer.
SQL_BACKLOG_OPENING = """
SELECT COALESCE(SUM(open_delta), 0) AS balance
  FROM task_event
 WHERE project_id = ? AND open_delta <> 0 AND ts < ?
"""

SQL_BACKLOG_EVENTS = """
SELECT ts, open_delta
  FROM task_event
 WHERE project_id = ? AND open_delta <> 0 AND ts >= ? AND ts < ?
 ORDER BY ts
"""

#: Section 5.2's Q2, plus the transition guard section 3.2 requires: an event whose
#: ``lifecycle_from`` is already ``closed`` is a rewrite of a closed record, not a close.
SQL_CLOSE_EVENTS = """
SELECT ts, task_id, outcome_to, source
  FROM task_event
 WHERE project_id = ? AND lifecycle_to = 'closed'
   AND (lifecycle_from IS NULL OR lifecycle_from <> 'closed')
   AND ts >= ? AND ts < ?
 ORDER BY ts
"""

#: Ball transitions. ``ix_event_ball`` is partial on exactly this predicate.
#:
#: Spelled with ``CASE`` rather than the shorter ``SUM(ball_to = 'x') - SUM(...)``
#: because ``ball_to`` is NULL on every close: ``NULL = 'agent'`` is NULL, ``SUM``
#: skips NULLs, and the arithmetic form would drop precisely the events that empty a
#: band. The band would then never come down.
SQL_HOLDER_OPENING = """
SELECT
  COALESCE(SUM(CASE WHEN ball_to   = 'agent'    THEN 1 ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN ball_from = 'agent'    THEN 1 ELSE 0 END), 0) AS agent,
  COALESCE(SUM(CASE WHEN ball_to   = 'human'    THEN 1 ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN ball_from = 'human'    THEN 1 ELSE 0 END), 0) AS human,
  COALESCE(SUM(CASE WHEN ball_to   = 'external' THEN 1 ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN ball_from = 'external' THEN 1 ELSE 0 END), 0) AS external
  FROM task_event
 WHERE project_id = ? AND ball_to IS NOT ball_from AND ts < ?
"""

SQL_HOLDER_EVENTS = """
SELECT ts, ball_from, ball_to
  FROM task_event
 WHERE project_id = ? AND ball_to IS NOT ball_from AND ts >= ? AND ts < ?
 ORDER BY ts
"""

#: Section 5.2's five current counts, answered from ``ix_task_counts`` -- the index
#: section 6 item E asked for, measured at 14.6 ms -> 1.68 ms at twenty times this
#: corpus. The predicates are ``dashboard.py``'s, restated in SQL; the reconciliation
#: test is what keeps the two from drifting.
SQL_TOTALS = """
SELECT
  COUNT(*) AS total,
  COALESCE(SUM(CASE WHEN lifecycle = 'active' AND ball = 'agent'  THEN 1 ELSE 0 END), 0)
    AS in_progress,
  COALESCE(SUM(CASE WHEN ball = 'external'                        THEN 1 ELSE 0 END), 0)
    AS blocked,
  COALESCE(SUM(CASE WHEN ball = 'human' AND lifecycle <> 'draft'  THEN 1 ELSE 0 END), 0)
    AS waiting_for_human,
  COALESCE(SUM(CASE WHEN ball = 'human' AND lifecycle =  'draft'  THEN 1 ELSE 0 END), 0)
    AS awaiting_input,
  COALESCE(SUM(CASE WHEN outcome = 'completed'                    THEN 1 ELSE 0 END), 0)
    AS completed,
  COALESCE(SUM(CASE WHEN lifecycle <> 'closed'                    THEN 1 ELSE 0 END), 0)
    AS open
  FROM task
 WHERE project_id = ?
"""

#: Open and **not archived** (section 3.4): the aging and oldest panels are calls to
#: attention and an archived task is explicitly not one. ``ix_task_open_age`` is partial
#: on ``lifecycle <> 'closed'`` and ordered by ``created_at``, so the ten oldest are the
#: first ten rows.
SQL_OPEN_TASKS = """
SELECT task_id, title, priority, ball, ball_reason, created_at
  FROM task
 WHERE project_id = ? AND lifecycle <> 'closed' AND archived = 0
 ORDER BY created_at
"""

#: How long each open task's current holder has held it. A ``ball_reason`` change is a
#: new holding -- agent/work and agent/revise are different asks -- so the predicate is
#: wider than ``ix_event_ball``'s, and the join drives from the open tasks so the
#: history side stays an indexed lookup per task rather than a scan.
SQL_BALL_SINCE = """
SELECT e.task_id AS task_id, MAX(e.ts) AS held_since
  FROM task AS t
  JOIN task_event AS e
    ON e.project_id = t.project_id AND e.task_id = t.task_id
 WHERE t.project_id = ? AND t.lifecycle <> 'closed' AND t.archived = 0
   AND (e.ball_to IS NOT e.ball_from OR e.ball_reason_to IS NOT e.ball_reason_from)
 GROUP BY e.task_id
"""

#: Which instants the store does not vouch for exactly (section 3.6). ``IN`` rather than
#: ``<> 'native'`` so ``ix_event_source`` -- section 6 item F, 21.9 ms -> 6.58 ms at 20x
#: -- can answer it as two equality lookups instead of a scan.
SQL_INEXACT_EVENTS = """
SELECT ts
  FROM task_event
 WHERE project_id = ? AND source IN ('reconstructed', 'backfilled')
   AND ts >= ? AND ts < ?
"""

SQL_EVENT_SOURCES = """
SELECT source, COUNT(*) AS n
  FROM task_event
 WHERE project_id = ?
 GROUP BY source
"""

SQL_COVERAGE_EDGES = """
SELECT
  MIN(ts) AS first_event,
  MIN(CASE WHEN source = 'native' THEN ts END) AS native_from,
  MAX(CASE WHEN source IN ('reconstructed', 'backfilled') THEN ts END) AS inexact_until,
  MIN(CASE WHEN source IN ('reconstructed', 'backfilled') THEN ts END) AS inexact_from
  FROM task_event
 WHERE project_id = ?
"""

SQL_PROJECT_ROW = """
SELECT reporting_tz, history_baseline_at, history_baseline_kind
  FROM project
 WHERE project_id = ?
"""

# ---------------------------------------------------------------------------
# The second set (section 21): the process, not only the tasks
# ---------------------------------------------------------------------------

#: Section 17.6. Every boundary row of every task completed in the window, driven from
#: the task table so the history side is an indexed lookup per task. The fold into
#: segments is Python (:func:`fold_segments`): section 17.4's rules are a state machine
#: over a task's rows, and window functions were measured slower with a full scan.
SQL_SEGMENT_EVENTS = """
SELECT e.task_id, e.ts, e.kind, e.source,
       e.lifecycle_from, e.lifecycle_to,
       e.ball_from, e.ball_reason_from, e.ball_to, e.ball_reason_to
  FROM task AS t
  JOIN task_event AS e ON e.project_id = t.project_id AND e.task_id = t.task_id
 WHERE t.project_id = ? AND t.outcome = 'completed'
   AND t.closed_at IS NOT NULL AND t.closed_at >= ? AND t.closed_at < ?
   AND (e.kind IN ('create', 'claim', 'close', 'reopen', 'import')
        OR e.ball_to IS NOT e.ball_from OR e.ball_reason_to IS NOT e.ball_reason_from)
 ORDER BY e.task_id, e.ts
"""

#: Reopenings in the window, for the throughput marker (T1).
SQL_REOPEN_EVENTS = """
SELECT ts, task_id, source
  FROM task_event
 WHERE project_id = ? AND kind = 'reopen' AND ts >= ? AND ts < ?
"""

#: Section 18.6, R2 and R3: every entry into and exit from human/review before the end
#: of the window, paired in Python per task. The lower bound is deliberately absent --
#: an exit in the window may have its entry before it.
SQL_REVIEW_TRANSITIONS = """
SELECT task_id, ts, kind, source, lifecycle_to,
       ball_from, ball_reason_from, ball_to, ball_reason_to
  FROM task_event
 WHERE project_id = ? AND ts < ?
   AND ((ball_to = 'human' AND ball_reason_to = 'review')
        OR (ball_from = 'human' AND ball_reason_from = 'review'))
 ORDER BY task_id, ts
"""

#: R1: open, unarchived tasks whose ball is on review, with the instant it landed there
#: -- the same rule as ``SQL_BALL_SINCE``, narrowed to one holder.
SQL_IN_REVIEW = """
SELECT t.task_id AS task_id, t.title AS title, MAX(e.ts) AS held_since
  FROM task AS t
  JOIN task_event AS e
    ON e.project_id = t.project_id AND e.task_id = t.task_id
 WHERE t.project_id = ? AND t.lifecycle <> 'closed' AND t.archived = 0
   AND t.ball = 'human' AND t.ball_reason = 'review'
   AND (e.ball_to IS NOT e.ball_from OR e.ball_reason_to IS NOT e.ball_reason_from)
 GROUP BY t.task_id
"""

#: Q-2: questions asked in the window and the first threaded answer to each. An
#: answer is an entry of type ``answer`` whose ``re`` names the question; the handoff
#: the UI writes beside it also carries ``re`` and would count every answer twice.
SQL_QUESTIONS = """
SELECT q.task_id, q.entry_id, q.ts,
       (SELECT MIN(a.ts) FROM log_entry AS a
         WHERE a.project_id = q.project_id AND a.task_id = q.task_id
           AND a.re = q.entry_id AND a.type = 'answer') AS answered_at
  FROM log_entry AS q
 WHERE q.project_id = ? AND q.type = 'question' AND q.ts >= ? AND q.ts < ?
"""

#: Q-1: questions on open tasks with no threaded answer.
SQL_OPEN_QUESTIONS = """
SELECT q.task_id, q.entry_id, q.ts
  FROM log_entry AS q
  JOIN task AS t ON t.project_id = q.project_id AND t.task_id = q.task_id
 WHERE q.project_id = ? AND q.type = 'question' AND t.lifecycle <> 'closed'
   AND NOT EXISTS (SELECT 1 FROM log_entry AS a
                    WHERE a.project_id = q.project_id AND a.task_id = q.task_id
                      AND a.re = q.entry_id AND a.type = 'answer')
 ORDER BY q.ts
"""

SQL_QUESTION_COVERAGE = """
SELECT MIN(ts) AS recorded_from
  FROM log_entry
 WHERE project_id = ? AND type = 'question'
"""

#: R-1 to R-4. ``ix_run_started`` (section 20.5 B) is what makes this a range read.
SQL_RUNS = """
SELECT started_at, ended_at, outcome, trigger, duration_seconds
  FROM task_run
 WHERE project_id = ? AND started_at >= ? AND started_at < ?
"""

SQL_RUN_COVERAGE = """
SELECT MIN(started_at) AS recorded_from
  FROM task_run
 WHERE project_id = ?
"""

#: S4: runs per task completed in the window, zero included.
SQL_RUNS_PER_COMPLETED_TASK = """
SELECT t.task_id, t.closed_at, COUNT(r.run_id) AS runs
  FROM task AS t
  LEFT JOIN task_run AS r ON r.project_id = t.project_id AND r.task_id = t.task_id
 WHERE t.project_id = ? AND t.outcome = 'completed'
   AND t.closed_at IS NOT NULL AND t.closed_at >= ? AND t.closed_at < ?
 GROUP BY t.task_id
"""

#: F1, F2: finishes by start.
SQL_FINISHES = """
SELECT finish_id, started_at, outcome, reason, seconds, source
  FROM finish
 WHERE project_id = ? AND started_at >= ? AND started_at < ?
"""

#: F3, F4: every step of every finish in the window, folded to medians in Python.
SQL_FINISH_STEPS = """
SELECT f.started_at, s.step, s.seconds, s.skipped
  FROM finish AS f
  JOIN finish_step AS s ON s.project_id = f.project_id AND s.finish_id = f.finish_id
 WHERE f.project_id = ? AND f.started_at >= ? AND f.started_at < ?
"""

#: F5 and section 17.3: the finishes of each task completed in the window. The merged
#: row that closed an unreviewed task is where its finish segment comes from.
SQL_FINISHES_PER_COMPLETED_TASK = """
SELECT t.task_id, t.closed_at,
       f.finish_id, f.finished_at, f.seconds, f.merged, f.outcome, f.source
  FROM task AS t
  LEFT JOIN finish AS f ON f.project_id = t.project_id AND f.task_id = t.task_id
 WHERE t.project_id = ? AND t.outcome = 'completed'
   AND t.closed_at IS NOT NULL AND t.closed_at >= ? AND t.closed_at < ?
"""

SQL_FINISH_COVERAGE = """
SELECT MIN(started_at) AS recorded_from,
       MIN(CASE WHEN source = 'native' THEN started_at END) AS native_from
  FROM finish
 WHERE project_id = ?
"""

#: G1, G3: gate runs by start. Every scope is read; the fold keeps the full ones.
SQL_GATES = """
SELECT gate_id, started_at, seconds, scope, passed, failed_stage, origin, source
  FROM gate_run
 WHERE project_id = ? AND started_at >= ? AND started_at < ?
"""

#: G2: the stages of the window's full green gates.
SQL_GATE_STAGES = """
SELECT g.started_at, s.stage, s.seconds
  FROM gate_run AS g
  JOIN gate_stage AS s ON s.project_id = g.project_id AND s.gate_id = g.gate_id
 WHERE g.project_id = ? AND g.started_at >= ? AND g.started_at < ?
   AND g.scope = 'full' AND g.passed = 1
"""

#: G4: full gates, green or red, whatever their origin, per task completed in the
#: window. ``ix_gate_task`` is partial on ``task_id IS NOT NULL``, which the join implies.
SQL_GATES_PER_COMPLETED_TASK = """
SELECT t.task_id, t.closed_at, g.seconds, g.source
  FROM task AS t
  LEFT JOIN gate_run AS g
    ON g.project_id = t.project_id AND g.task_id = t.task_id AND g.scope = 'full'
 WHERE t.project_id = ? AND t.outcome = 'completed'
   AND t.closed_at IS NOT NULL AND t.closed_at >= ? AND t.closed_at < ?
"""

SQL_GATE_COVERAGE = """
SELECT MIN(started_at) AS recorded_from,
       MIN(CASE WHEN source = 'native' THEN started_at END) AS native_from
  FROM gate_run
 WHERE project_id = ?
"""

#: Every query above, by the name the design gives it. ``tests/test_analytics_api.py``
#: walks this to record a plan for each one (acceptance ac-6 of task-372, ac-5 of
#: task-473).
QUERIES: Dict[str, str] = {
    "backlog opening balance": SQL_BACKLOG_OPENING,
    "backlog series": SQL_BACKLOG_EVENTS,
    "close events": SQL_CLOSE_EVENTS,
    "holder opening balance": SQL_HOLDER_OPENING,
    "holder series": SQL_HOLDER_EVENTS,
    "the five current counts": SQL_TOTALS,
    "open tasks": SQL_OPEN_TASKS,
    "ball held since": SQL_BALL_SINCE,
    "inexact events": SQL_INEXACT_EVENTS,
    "event sources": SQL_EVENT_SOURCES,
    "coverage edges": SQL_COVERAGE_EDGES,
    # The second set.
    "segment events": SQL_SEGMENT_EVENTS,
    "reopen events": SQL_REOPEN_EVENTS,
    "review transitions": SQL_REVIEW_TRANSITIONS,
    "in review now": SQL_IN_REVIEW,
    "questions in range": SQL_QUESTIONS,
    "open questions": SQL_OPEN_QUESTIONS,
    "question coverage": SQL_QUESTION_COVERAGE,
    "runs in range": SQL_RUNS,
    "run coverage": SQL_RUN_COVERAGE,
    "runs per completed task": SQL_RUNS_PER_COMPLETED_TASK,
    "finishes in range": SQL_FINISHES,
    "finish steps in range": SQL_FINISH_STEPS,
    "finishes per completed task": SQL_FINISHES_PER_COMPLETED_TASK,
    "finish coverage": SQL_FINISH_COVERAGE,
    "gate runs in range": SQL_GATES,
    "gate stages in range": SQL_GATE_STAGES,
    "gate seconds per completed task": SQL_GATES_PER_COMPLETED_TASK,
    "gate coverage": SQL_GATE_COVERAGE,
}

# -- the execution journal (section 18.5, R-5 and R-6) ------------------------
#
# These run against ``execution.db``, a machine-level file with its own schema, through
# ``execution_store_for(home).read(...)`` -- never a second connection composed from a
# path. Every one is filtered by ``project_id``; the series are per project like every
# other.

#: R-5: how long an admitted dispatch waited before its session was up.
SQL_EXEC_ATTEMPTS = """
SELECT admitted_at, launched_at
  FROM run_attempt
 WHERE project_id = ? AND admitted_at >= ? AND admitted_at < ?
"""

#: R-5: the time a queued dispatch waited for a slot -- the number that will matter
#: once the queue has history (section 18.5).
SQL_EXEC_QUEUE_WAITS = """
SELECT queued_at, claimed_at
  FROM dispatch_queue
 WHERE project_id = ? AND claimed_at IS NOT NULL AND queued_at >= ? AND queued_at < ?
"""

#: R-6: run-hours lost to usage-limit pauses, per recovered waiter.
SQL_EXEC_PAUSES = """
SELECT w.stall_at, w.updated_at
  FROM auth_waiter AS w
  JOIN auth_incident AS i ON i.incident_id = w.incident_id
 WHERE w.project_id = ? AND i.kind = 'usage_limit' AND w.status = 'recovered'
   AND w.stall_at >= ? AND w.stall_at < ?
"""

SQL_EXEC_COVERAGE = """
SELECT MIN(admitted_at) AS recorded_from
  FROM run_attempt
 WHERE project_id = ?
"""

#: The execution-journal queries, planned against that file rather than the task store.
EXECUTION_QUERIES: Dict[str, str] = {
    "schedule-to-start": SQL_EXEC_ATTEMPTS,
    "dispatch queue waits": SQL_EXEC_QUEUE_WAITS,
    "usage-limit pauses": SQL_EXEC_PAUSES,
    "execution coverage": SQL_EXEC_COVERAGE,
}

#: Queries whose plan is a table scan, each with the reason that is acceptable. Section
#: 21.3: "machine-level tables with fewer rows than the plan line has characters"; an
#: index is not requested until either has hundreds. The plan test exempts these by name
#: rather than being weakened.
UNINDEXED_QUERIES: Dict[str, str] = {
    "dispatch queue waits": (
        "dispatch_queue holds one row per queued dispatch since 2026-09-18 and is "
        "filtered by project; single-digit rows, so a scan is the cheapest plan."
    ),
    "usage-limit pauses": (
        "auth_waiter holds one row per run that stalled on a quota; single-digit rows, "
        "joined to auth_incident through its primary key."
    ),
}


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The resolved range: what was asked for, and what the store can answer."""

    key: str
    start: datetime
    end: datetime
    bucket: str
    throughput_bucket: str
    zone: ZoneInfo
    first_day: date
    last_day: date


@dataclass
class Coverage:
    """Section 7.3's ``AnalyticsCoverage``, as plain data."""

    baseline_at: Optional[datetime] = None
    baseline_kind: str = "unknown"
    native_from: Optional[datetime] = None
    reconstructed_before: Optional[datetime] = None
    events: Dict[str, int] = field(default_factory=dict)
    complete: bool = False
    note: Optional[str] = None


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """The ``fraction`` percentile of ``values`` by linear interpolation.

    ``None`` for an empty sample rather than zero: section 7.3 makes the percentiles
    optional precisely so a bucket with nothing in it is not reported as instantaneous.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * weight, 2)


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _mode(values: Sequence[int]) -> Optional[int]:
    """The most common value, smallest on a tie; ``None`` for an empty sample."""
    if not values:
        return None
    counts: Dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return min(counts, key=lambda value: (-counts[value], value))


def _seconds_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


# ---------------------------------------------------------------------------
# The lifecycle segments (section 17)
# ---------------------------------------------------------------------------

SEGMENTS: Tuple[str, ...] = ("queue", "work", "waiting", "review", "finish")
"""Section 17.1's five names. They partition a task's open life exactly."""

#: The grain every per-task and per-process series is aggregated at, on every range
#: (section 18.1): a day rarely closes three tasks, and the percentiles need a sample.
WEEK = "week"


def segment_of(ball: Optional[str], reason: Optional[str], lifecycle: Optional[str]) -> str:
    """Section 17.1's rule: the segment a holder's dwell time belongs to.

    A creation with no ball (section 17.4, pre-cutover reconstructions) is taken as
    *queue* unless the task was born a draft, which is what the creations that do carry
    a ball say.
    """
    if ball is None:
        return "waiting" if lifecycle == "draft" else "queue"
    if ball == "agent":
        return "queue" if reason == "available" else "work"
    if ball == "human":
        return "review" if reason == "review" else "waiting"
    return "waiting"


@dataclass
class TaskSegments:
    """One completed task's open life, folded into section 17's segments."""

    task_id: str
    seconds: Dict[str, float] = field(default_factory=lambda: {name: 0.0 for name in SEGMENTS})
    closed_at: Optional[datetime] = None
    estimated: bool = False
    #: The last close was an ``import`` row, so its instant is unknown (section 17.4).
    excluded: bool = False
    review_rounds: int = 0
    approvals: int = 0
    first_claim: Optional[datetime] = None
    first_review: Optional[datetime] = None
    #: Work accrued since the last approval -- the finish segment's raw material.
    work_since_approval: float = 0.0

    @property
    def total(self) -> float:
        return sum(self.seconds.values())

    @property
    def unreviewed(self) -> bool:
        return self.review_rounds == 0


def fold_segments(rows: Sequence[sqlite3.Row]) -> Dict[str, TaskSegments]:
    """Section 17.4's state machine over each task's boundary rows, oldest first.

    Every instant between a task's creation and its close is on exactly one holder, so
    the fold is: at each row, credit the time since the last row to the holder that had
    it, then work out who has it now. A ``close`` stops the clock; a ``reopen`` starts
    it again, and the closed interval between them is on no holder and in no segment.

    The finish segment is settled afterwards by :func:`settle_finish`, because for an
    unreviewed task it comes from the ``finish`` table rather than from these rows.
    """
    tasks: Dict[str, TaskSegments] = {}
    holder: Optional[str] = None
    since: Optional[datetime] = None
    current: Optional[TaskSegments] = None

    for row in rows:
        task_id = str(row["task_id"])
        if current is None or current.task_id != task_id:
            current = tasks.setdefault(task_id, TaskSegments(task_id=task_id))
            holder, since = None, None
        instant = parse_instant(row["ts"])
        if instant is None:
            continue
        if row["source"] != "native":
            current.estimated = True

        kind = row["kind"]
        lifecycle_from, lifecycle_to = row["lifecycle_from"], row["lifecycle_to"]
        ball_to, reason_to = row["ball_to"], row["ball_reason_to"]
        closing = lifecycle_to == "closed" and lifecycle_from != "closed"

        # Credit the interval just ended to whoever held it.
        if holder is not None and since is not None:
            elapsed = max((instant - since).total_seconds(), 0.0)
            current.seconds[holder] += elapsed
            if holder == "work":
                current.work_since_approval += elapsed

        if kind == "claim" and current.first_claim is None:
            current.first_claim = instant

        if closing:
            if holder == "review":
                # Approve-and-close in one act (section 17.2).
                current.approvals += 1
                current.work_since_approval = 0.0
            current.closed_at = instant
            current.excluded = kind == "import"
            holder, since = None, None
            continue

        if holder is None:
            # A creation, an import reconciliation of an open task, or a reopen: the
            # clock starts with the ball the row leaves the task holding.
            if lifecycle_to == "closed":
                continue  # a rewrite of a closed record; still closed
            if kind == "reopen":
                current.excluded = False
            if ball_to is None and kind == "create":
                current.estimated = True
            holder = segment_of(ball_to, reason_to, lifecycle_to)
            since = instant
            if holder == "review":
                current.review_rounds += 1
                current.first_review = current.first_review or instant
            continue

        if ball_to is None:
            # An edit that moved nothing the holder rule reads; the clock runs on.
            since = instant
            continue

        entering = segment_of(ball_to, reason_to, lifecycle_to)
        if entering == "review" and holder != "review":
            current.review_rounds += 1
            current.first_review = current.first_review or instant
        if holder == "review" and ball_to == "agent" and reason_to == "work":
            current.approvals += 1
            current.work_since_approval = 0.0
        holder, since = entering, instant

    return tasks


def settle_finish(
    task: TaskSegments, finish_rows: Sequence[Tuple[Optional[datetime], Optional[float], bool]]
) -> None:
    """Carve the finish segment out of *work* (section 17.3).

    With an approval, finish is the work accrued since the last one, which is the span
    from that approval to the close whenever the holder stayed ``agent``/``work`` -- and
    stays inside *work* when it did not, so the five segments still partition the total.
    Without one, it is the span of the merged finish row that closed the task, capped at
    the work there is to carve it from; with no such row, finish is zero and the finish
    time is inside *work*, which is what the store records.
    """
    finish = 0.0
    if task.approvals:
        finish = min(task.work_since_approval, task.seconds["work"])
    elif task.closed_at is not None:
        for finished_at, seconds, merged in finish_rows:
            if not merged or finished_at is None or not seconds:
                continue
            if abs((finished_at - task.closed_at).total_seconds()) <= 60.0:
                finish = min(float(seconds), task.seconds["work"])
                break
    task.seconds["work"] -= finish
    task.seconds["finish"] = finish


@dataclass
class SeriesCoverage:
    """Section 21.1's per-series coverage, as plain data."""

    recorded_from: Optional[datetime] = None
    native_from: Optional[datetime] = None
    complete: bool = False
    bucket: str = WEEK
    note: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "recorded_from": self.recorded_from,
            "native_from": self.native_from,
            "complete": self.complete,
            "bucket": self.bucket,
            "note": self.note,
        }


class AnalyticsProjection:
    """Everything the analytics page needs, from one store, in one pass.

    Constructed per request. It holds a connection and a clock and nothing else, so two
    panels cannot end up computed against different instants -- which is the failure the
    one-endpoint decision (section 7.1) exists to prevent, restated at the level of a
    single response.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        now: Optional[datetime] = None,
        execution: Optional[Any] = None,
    ) -> None:
        self.connection = connection
        self.project_id = project_id
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        #: The execution journal's reader (``ExecutionStore.read``), or ``None`` when the
        #: caller has no machine home to name -- in which case the machine series is
        #: absent and says so, never zero.
        self.execution = execution
        self._finish_rows: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}

    # -- reading ----------------------------------------------------------

    def _rows(self, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
        return list(self.connection.execute(sql, tuple(params)).fetchall())

    def _row(self, sql: str, params: Sequence[Any]) -> Optional[sqlite3.Row]:
        row: Optional[sqlite3.Row] = self.connection.execute(sql, tuple(params)).fetchone()
        return row

    def project_settings(self) -> Tuple[str, Optional[str], str]:
        """``(reporting_tz, history_baseline_at, history_baseline_kind)``."""
        row = self._row(SQL_PROJECT_ROW, (self.project_id,))
        if row is None:
            return "UTC", None, "unknown"
        return (
            row["reporting_tz"] or "UTC",
            row["history_baseline_at"],
            row["history_baseline_kind"] or "unknown",
        )

    # -- assembling -------------------------------------------------------

    def coverage(self, zone: ZoneInfo) -> Coverage:
        """What the store is prepared to claim about its own history (section 3.6).

        ``baseline_at`` falls back to the first event when the project carries no
        recorded baseline, which is the ordinary state of a project that was never
        imported: its history begins where its events begin, and that is a claim rather
        than an absence. ``None`` survives for exactly one case -- a project with no
        events at all -- which is what section 7.4 asks the page to distinguish.
        """
        _tz, baseline_text, baseline_kind = self.project_settings()
        edges = self._row(SQL_COVERAGE_EDGES, (self.project_id,))
        counts = {
            str(row["source"]): int(row["n"])
            for row in self._rows(SQL_EVENT_SOURCES, (self.project_id,))
        }
        first_event = parse_instant(edges["first_event"]) if edges is not None else None
        native_from = parse_instant(edges["native_from"]) if edges is not None else None
        inexact_until = parse_instant(edges["inexact_until"]) if edges is not None else None

        baseline_at = parse_instant(baseline_text) or first_event
        if baseline_at is None:
            kind = "unknown"
        elif baseline_text:
            kind = baseline_kind
        elif inexact_until is not None and first_event is not None and first_event <= inexact_until:
            kind = "reconstructed"
        else:
            kind = "native"

        return Coverage(
            baseline_at=baseline_at,
            baseline_kind=kind,
            native_from=native_from,
            reconstructed_before=inexact_until,
            events=counts,
            complete=False,
            note=None,
        )

    def window(self, range_key: str, coverage: Coverage, zone: ZoneInfo) -> Window:
        """Resolve the requested range against what the store can answer.

        Two clippings, both from section 3.6 rule 1. The window never starts before the
        coverage baseline, because a series drawn back past it would read as a project
        that had no work rather than as history nobody recorded. And the window is
        snapped back to its own bucket boundary so the SQL filter and the spine agree
        about where the first bucket begins -- otherwise the opening balance would count
        events the first bucket also counted.
        """
        key = range_key if range_key in RANGE_KEYS else DEFAULT_RANGE
        bucket = bucket_for(key)
        today = local_day(self.now, zone)
        first = window_first_day(key, today)
        if coverage.baseline_at is not None:
            baseline_day = local_day(coverage.baseline_at, zone)
            if key == "all" or baseline_day > first:
                first = baseline_day
        first = bucket_start(min(first, today), bucket)
        return Window(
            key=key,
            start=day_start_utc(first, zone),
            end=self.now,
            bucket=bucket,
            throughput_bucket=bucket_for(key, throughput=True),
            zone=zone,
            first_day=first,
            last_day=today,
        )

    def totals(self) -> Dict[str, int]:
        """The five dashboard counts plus ``open``, computed in SQL.

        Deliberately *not* delegated to ``build_dashboard_snapshot``: the acceptance
        criterion is that the two agree, and a test comparing a function with itself
        would pass for ever without checking anything. These predicates and that
        function's Python are two independent statements of the same rules, and
        ``tests/test_analytics_api.py`` is where they are held against each other.
        """
        row = self._row(SQL_TOTALS, (self.project_id,))
        keys = (
            "total",
            "in_progress",
            "blocked",
            "waiting_for_human",
            "awaiting_input",
            "completed",
            "open",
        )
        if row is None:
            return {key: 0 for key in keys}
        return {key: int(row[key]) for key in keys}

    def estimated_buckets(self, window: Window) -> Set[date]:
        """The buckets in ``window`` containing an event the store cannot place exactly.

        Per bucket rather than per response (section 7.3): the hatch has to land on the
        days that were reconstructed, not on a prefix somebody guessed the length of.
        """
        rows = self._rows(
            SQL_INEXACT_EVENTS,
            (self.project_id, _iso(window.start), _iso(window.end)),
        )
        return {
            bucket_start(local_day(instant, window.zone), window.bucket)
            for instant in (parse_instant(row["ts"]) for row in rows)
            if instant is not None
        }

    def backlog(self, window: Window, estimated: Set[date]) -> List[Dict[str, Any]]:
        """The level and both flows, one point per bucket, no gaps.

        The level is the opening balance plus a running sum of ``open_delta`` -- which
        makes a reopening correct by construction (section 3.2) without this code
        knowing the word. ``opened`` and ``closed`` are the same deltas split by sign,
        so the bars beneath the area cannot disagree with it.
        """
        opening_row = self._row(SQL_BACKLOG_OPENING, (self.project_id, _iso(window.start)))
        level = int(opening_row["balance"]) if opening_row is not None else 0
        rows = self._rows(
            SQL_BACKLOG_EVENTS,
            (self.project_id, _iso(window.start), _iso(window.end)),
        )

        opened: Dict[date, int] = {}
        closed: Dict[date, int] = {}
        delta: Dict[date, int] = {}
        for row in rows:
            instant = parse_instant(row["ts"])
            if instant is None:
                continue
            day = bucket_start(local_day(instant, window.zone), window.bucket)
            step = int(row["open_delta"])
            delta[day] = delta.get(day, 0) + step
            if step > 0:
                opened[day] = opened.get(day, 0) + step
            else:
                closed[day] = closed.get(day, 0) - step

        points: List[Dict[str, Any]] = []
        for day in spine(window.first_day, window.last_day, window.bucket):
            level += delta.get(day, 0)
            points.append(
                {
                    "day": day,
                    "open_count": level,
                    "opened": opened.get(day, 0),
                    "closed": closed.get(day, 0),
                    "estimated": day in estimated,
                }
            )
        return points

    def holders(self, window: Window) -> List[Dict[str, Any]]:
        """Who held the open work, per bucket, on the same spine as the backlog.

        Each event row carries the ball it moved *from* as well as *to*, so a running
        count needs no knowledge of what a claim or a handoff means: every transition is
        one out of one band and one into another, and a create or a close is a
        transition with ``NULL`` on the missing side. The series therefore has to end on
        the current counts, which is what the reconciliation test asserts.
        """
        opening = self._row(SQL_HOLDER_OPENING, (self.project_id, _iso(window.start)))
        level = {
            "agent": int(opening["agent"]) if opening is not None else 0,
            "human": int(opening["human"]) if opening is not None else 0,
            "external": int(opening["external"]) if opening is not None else 0,
        }
        rows = self._rows(
            SQL_HOLDER_EVENTS,
            (self.project_id, _iso(window.start), _iso(window.end)),
        )

        steps: Dict[date, Dict[str, int]] = {}
        for row in rows:
            instant = parse_instant(row["ts"])
            if instant is None:
                continue
            day = bucket_start(local_day(instant, window.zone), window.bucket)
            bucket_steps = steps.setdefault(day, {"agent": 0, "human": 0, "external": 0})
            if row["ball_from"] in bucket_steps:
                bucket_steps[row["ball_from"]] -= 1
            if row["ball_to"] in bucket_steps:
                bucket_steps[row["ball_to"]] += 1

        points: List[Dict[str, Any]] = []
        for day in spine(window.first_day, window.last_day, window.bucket):
            for band, step in steps.get(day, {}).items():
                level[band] += step
            points.append({"day": day, **level})
        return points

    def throughput(self, window: Window) -> List[Dict[str, Any]]:
        """Completions, cancellations and reopenings, one point per throughput bucket.

        ``tasks_completed`` and ``completion_events`` are kept apart because a reopened
        task closes twice (section 3.3): the chart plots the first, and the second is
        there so a reader whose sums do not match has an answer rather than a suspicion.
        ``cancelled`` is closed-with-any-other-outcome, which is not throughput and is
        not hidden either -- a month of cancellations must not read as a quiet month.

        **The cycle-time percentiles this used to carry are gone** (section 19.2, and
        task-473's first decision, which left their removal to the task that removed
        their consumer). ``created_at`` to ``closed_at`` over a task that spent six
        weeks in the queue is a true number attributed to the wrong thing; section
        18.1's S1 and S2 answer the question it stood in for, split into the part that
        is the queue and the part that is the work. A field meaning the wrong thing is
        worse than one that is gone, so it went rather than being deprecated in place.
        """
        grain = window.throughput_bucket
        completed: Dict[date, set] = {}
        events: Dict[date, int] = {}
        cancelled: Dict[date, set] = {}
        estimated: Set[date] = set()
        for row in self._rows(
            SQL_CLOSE_EVENTS, (self.project_id, _iso(window.start), _iso(window.end))
        ):
            instant = parse_instant(row["ts"])
            if instant is None:
                continue
            day = bucket_start(local_day(instant, window.zone), grain)
            if row["outcome_to"] == "completed":
                completed.setdefault(day, set()).add(row["task_id"])
                events[day] = events.get(day, 0) + 1
            else:
                cancelled.setdefault(day, set()).add(row["task_id"])
            if row["source"] in INEXACT_SOURCES:
                estimated.add(day)

        # T1's marker: a reopening is neither a completion nor a cancellation, but a
        # bucket with three of them is telling the reader something about the ones
        # that were counted.
        reopened: Dict[date, set] = {}
        for row in self._rows(
            SQL_REOPEN_EVENTS, (self.project_id, _iso(window.start), _iso(window.end))
        ):
            instant = parse_instant(row["ts"])
            if instant is None:
                continue
            day = bucket_start(local_day(instant, window.zone), grain)
            reopened.setdefault(day, set()).add(row["task_id"])
            if row["source"] in INEXACT_SOURCES:
                estimated.add(day)

        points: List[Dict[str, Any]] = []
        for day in spine(window.first_day, window.last_day, grain):
            points.append(
                {
                    "bucket": day,
                    "tasks_completed": len(completed.get(day, ())),
                    "completion_events": events.get(day, 0),
                    "cancelled": len(cancelled.get(day, ())),
                    "reopened": len(reopened.get(day, ())),
                    "estimated": day in estimated,
                }
            )
        return points

    def open_task_rows(self) -> List[sqlite3.Row]:
        """Open, not archived, oldest first -- the input to both aging panels."""
        return self._rows(SQL_OPEN_TASKS, (self.project_id,))

    def aging(self, rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
        """Section 8.5's four bands, with the mean age inside each.

        Every band is emitted whether or not it holds anything. A missing bar reads as a
        rendering fault; a bar labelled zero reads as a fact.
        """
        ages: List[float] = [
            age for age in (self._age_days(row["created_at"]) for row in rows) if age is not None
        ]
        buckets: List[Dict[str, Any]] = []
        lower = 0.0
        for label, upper in AGE_BANDS:
            limit = float(upper) if upper is not None else None
            inside = [age for age in ages if age >= lower and (limit is None or age < limit)]
            buckets.append(
                {
                    "label": label,
                    "tasks": len(inside),
                    "mean_age_days": round(sum(inside) / len(inside), 2) if inside else 0.0,
                }
            )
            if limit is not None:
                lower = limit
        return buckets

    def oldest(self, rows: Sequence[sqlite3.Row], limit: int = 10) -> List[Dict[str, Any]]:
        """The ten oldest open, unarchived tasks -- section 8.5's answer to Q3."""
        oldest: List[Dict[str, Any]] = []
        for row in rows[:limit]:
            age = self._age_days(row["created_at"])
            oldest.append(
                {
                    "task_id": row["task_id"],
                    "title": row["title"],
                    "priority": row["priority"],
                    "ball": row["ball"],
                    "ball_reason": row["ball_reason"],
                    "age_days": round(age, 2) if age is not None else 0.0,
                }
            )
        return oldest

    def stuck(self, rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
        """Where open work is sitting, grouped by who holds it and why.

        "Days held" is measured from the last event that moved the ball **or its
        reason**, not from the last activity of any kind. A comment on a task parked for
        five weeks does not restart the clock on the parking, and a panel that said it
        did would under-report the thing it exists to surface.
        """
        held_since = {
            str(row["task_id"]): parse_instant(row["held_since"])
            for row in self._rows(SQL_BALL_SINCE, (self.project_id,))
        }
        groups: Dict[Tuple[str, str], List[Tuple[float, str]]] = {}
        for row in rows:
            ball, reason = row["ball"], row["ball_reason"]
            if ball is None or reason is None:
                continue
            since = held_since.get(str(row["task_id"]))
            days = (
                (self.now - since).total_seconds() / 86400.0
                if since is not None
                else self._age_days(row["created_at"]) or 0.0
            )
            groups.setdefault((str(ball), str(reason)), []).append(
                (max(days, 0.0), str(row["task_id"]))
            )

        summaries: List[Dict[str, Any]] = []
        for (ball, reason), held in groups.items():
            worst = max(held)
            summaries.append(
                {
                    "ball": ball,
                    "ball_reason": reason,
                    "tasks": len(held),
                    "mean_days_held": round(sum(days for days, _ in held) / len(held), 2),
                    "max_days_held": round(worst[0], 2),
                    "oldest_task_id": worst[1],
                }
            )
        summaries.sort(key=lambda group: (-group["tasks"], group["ball"], group["ball_reason"]))
        return summaries

    def _age_days(self, created_at: Optional[str]) -> Optional[float]:
        instant = parse_instant(created_at)
        if instant is None:
            return None
        return max((self.now - instant).total_seconds() / 86400.0, 0.0)

    # -- the second set (section 21) --------------------------------------

    def _window_params(self, window: Window) -> Tuple[str, str, str]:
        return (self.project_id, _iso(window.start), _iso(window.end))

    def _finishes_per_task(self, window: Window) -> List[sqlite3.Row]:
        """The window's completed tasks joined to their finishes, read once.

        Two series fold it -- the finish segment of an unreviewed task (section 17.3)
        and finishes per completed task (F5) -- and one read is the whole point of a
        projection that is constructed per request.
        """
        params = self._window_params(window)
        if params not in self._finish_rows:
            self._finish_rows[params] = self._rows(SQL_FINISHES_PER_COMPLETED_TASK, params)
        return self._finish_rows[params]

    def _bucket(self, instant: datetime, window: Window, grain: str) -> date:
        return bucket_start(local_day(instant, window.zone), grain)

    def _series_spine(
        self, window: Window, grain: str, recorded_from: Optional[datetime]
    ) -> List[date]:
        """The buckets a series may draw: from where its source starts, never before.

        Section 21.1: a series whose source is younger than the range starts where the
        source starts. An empty list is the answer for a source with no rows at all.
        """
        if recorded_from is None:
            return []
        first = max(window.first_day, local_day(recorded_from, window.zone))
        if first > window.last_day:
            return []
        return spine(first, window.last_day, grain)

    def _series_coverage(
        self,
        window: Window,
        *,
        label: str,
        recorded_from: Optional[datetime],
        native_from: Optional[datetime],
        grain: str = WEEK,
        native_label: Optional[str] = None,
    ) -> SeriesCoverage:
        """Section 21.1's object for one series, with its caption written here.

        The note is a statement about the data, so it is composed where the data is
        read rather than derived on the page from three nullable fields.
        """
        coverage = SeriesCoverage(
            recorded_from=recorded_from,
            native_from=native_from,
            complete=native_from is not None and window.start >= native_from,
            bucket=grain,
        )
        if recorded_from is None:
            coverage.note = f"No {label} recorded yet."
            return coverage
        parts: List[str] = []
        if recorded_from > window.start:
            parts.append(
                f"{label[:1].upper()}{label[1:]} are recorded from "
                f"{_local_date_text(recorded_from, window.zone)}"
            )
        if (
            native_label
            and native_from is not None
            and native_from > recorded_from
            and native_from > window.start
        ):
            parts.append(f"{native_label} only from {_local_date_text(native_from, window.zone)}")
        coverage.note = "; ".join(parts) + "." if parts else None
        return coverage

    def segment_tasks(self, window: Window) -> Dict[str, TaskSegments]:
        """Every completed task in the window, folded, with its finish settled.

        Public so a test can hold section 17.5's invariant against each task rather
        than against a percentile of them.
        """
        tasks = fold_segments(self._rows(SQL_SEGMENT_EVENTS, self._window_params(window)))
        finishes: Dict[str, List[Tuple[Optional[datetime], Optional[float], bool]]] = {}
        for row in self._finishes_per_task(window):
            if row["finish_id"] is None:
                continue
            finishes.setdefault(str(row["task_id"]), []).append(
                (
                    parse_instant(row["finished_at"]),
                    float(row["seconds"]) if row["seconds"] is not None else None,
                    bool(row["merged"]),
                )
            )
        for task_id, task in tasks.items():
            settle_finish(task, finishes.get(task_id, ()))
        return tasks

    def segments(
        self, window: Window, page_coverage: Coverage
    ) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """S1, S2 and S3: per week of close, where the completed tasks' time went."""
        tasks = self.segment_tasks(window)
        coverage = self._series_coverage(
            window,
            label="task histories",
            recorded_from=page_coverage.baseline_at,
            native_from=page_coverage.native_from,
        )

        by_bucket: Dict[date, List[TaskSegments]] = {}
        excluded: Dict[date, int] = {}
        first_review: Dict[date, List[float]] = {}
        for task in tasks.values():
            if task.closed_at is None:
                continue
            bucket = self._bucket(task.closed_at, window, WEEK)
            if task.excluded:
                excluded[bucket] = excluded.get(bucket, 0) + 1
                continue
            by_bucket.setdefault(bucket, []).append(task)
            if task.first_claim is not None and task.first_review is not None:
                review_bucket = self._bucket(task.first_review, window, WEEK)
                first_review.setdefault(review_bucket, []).append(
                    max((task.first_review - task.first_claim).total_seconds(), 0.0) / 3600.0
                )

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            sample = by_bucket.get(bucket, [])
            point: Dict[str, Any] = {
                "bucket": bucket,
                "sample": len(sample),
                "excluded": excluded.get(bucket, 0),
                "unreviewed": sum(1 for task in sample if task.unreviewed),
                "estimated": any(task.estimated for task in sample),
            }
            among: Dict[str, Dict[str, Any]] = {}
            for name in SEGMENTS:
                hours = [task.seconds[name] / 3600.0 for task in sample]
                point[f"{name}_p50_hours"] = percentile(hours, 0.5)
                point[f"{name}_p90_hours"] = percentile(hours, 0.9)
                nonzero = [value for value in hours if value > 0]
                among[name] = {"tasks": len(nonzero), "p50_hours": percentile(nonzero, 0.5)}
            totals = [task.total / 3600.0 for task in sample]
            point["total_p50_hours"] = percentile(totals, 0.5)
            point["total_p90_hours"] = percentile(totals, 0.9)
            reviews = first_review.get(bucket, [])
            point["first_review_p50_hours"] = percentile(reviews, 0.5)
            point["first_review_p90_hours"] = percentile(reviews, 0.9)
            point["first_review_sample"] = len(reviews)
            point["among"] = among
            points.append(point)
        return points, coverage

    def cost_per_task(
        self, window: Window, finish_coverage: SeriesCoverage, gate_coverage: SeriesCoverage
    ) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """S4, F5 and G4: what each completed task cost the machine, per week of close.

        Coverage is the run ledger's: it is the oldest of the three sources and the one
        that says whether a task with zero runs was undispatched or unrecorded. The gate
        and finish baselines are on their own series; the note here names the older one
        of them so a reader knows why early buckets under-report.
        """
        run_row = self._row(SQL_RUN_COVERAGE, (self.project_id,))
        runs_from = parse_instant(run_row["recorded_from"]) if run_row is not None else None
        starts = [
            instant
            for instant in (runs_from, finish_coverage.recorded_from, gate_coverage.recorded_from)
            if instant is not None
        ]
        coverage = self._series_coverage(
            window,
            label="runs, finishes and gates",
            recorded_from=min(starts) if starts else None,
            native_from=runs_from,
        )

        runs: Dict[date, List[int]] = {}
        for row in self._rows(SQL_RUNS_PER_COMPLETED_TASK, self._window_params(window)):
            closed_at = parse_instant(row["closed_at"])
            if closed_at is None:
                continue
            runs.setdefault(self._bucket(closed_at, window, WEEK), []).append(int(row["runs"]))

        finishes: Dict[date, Dict[str, int]] = {}
        estimated: Set[date] = set()
        for row in self._finishes_per_task(window):
            closed_at = parse_instant(row["closed_at"])
            if closed_at is None:
                continue
            bucket = self._bucket(closed_at, window, WEEK)
            per_task = finishes.setdefault(bucket, {})
            per_task[str(row["task_id"])] = per_task.get(str(row["task_id"]), 0) + (
                1 if row["finish_id"] is not None else 0
            )
            if row["source"] == "imported":
                estimated.add(bucket)

        gate_seconds: Dict[date, Dict[str, Optional[float]]] = {}
        for row in self._rows(SQL_GATES_PER_COMPLETED_TASK, self._window_params(window)):
            closed_at = parse_instant(row["closed_at"])
            if closed_at is None:
                continue
            bucket = self._bucket(closed_at, window, WEEK)
            task_gates = gate_seconds.setdefault(bucket, {})
            task_id = str(row["task_id"])
            if row["seconds"] is None:
                task_gates.setdefault(task_id, None)
            else:
                task_gates[task_id] = (task_gates.get(task_id) or 0.0) + float(row["seconds"])
            if row["source"] == "imported":
                estimated.add(bucket)

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            run_counts = runs.get(bucket, [])
            finish_counts = list(finishes.get(bucket, {}).values())
            gates = gate_seconds.get(bucket, {})
            minutes = [seconds / 60.0 for seconds in gates.values() if seconds is not None]
            points.append(
                {
                    "bucket": bucket,
                    "sample": len(run_counts),
                    "runs_mean": _mean([float(count) for count in run_counts]),
                    "runs_mode": _mode(run_counts),
                    "finishes_mean": _mean([float(count) for count in finish_counts]),
                    "gate_minutes_p50": percentile(minutes, 0.5),
                    "gate_minutes_p90": percentile(minutes, 0.9),
                    "without_gate": sum(1 for seconds in gates.values() if seconds is None),
                    "estimated": bucket in estimated,
                }
            )
        return points, coverage

    def finishes(self, window: Window) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """F1 to F4: the scripted finish, per week of start."""
        edges = self._row(SQL_FINISH_COVERAGE, (self.project_id,))
        coverage = self._series_coverage(
            window,
            label="finishes",
            recorded_from=parse_instant(edges["recorded_from"]) if edges is not None else None,
            native_from=parse_instant(edges["native_from"]) if edges is not None else None,
        )

        outcomes: Dict[date, Dict[str, int]] = {}
        reasons: Dict[date, Dict[str, int]] = {}
        durations: Dict[date, List[float]] = {}
        estimated: Set[date] = set()
        for row in self._rows(SQL_FINISHES, self._window_params(window)):
            started = parse_instant(row["started_at"])
            if started is None:
                continue
            bucket = self._bucket(started, window, WEEK)
            outcome = str(row["outcome"])
            counts = outcomes.setdefault(bucket, {})
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "escalated" and row["reason"]:
                bucket_reasons = reasons.setdefault(bucket, {})
                bucket_reasons[str(row["reason"])] = bucket_reasons.get(str(row["reason"]), 0) + 1
            if outcome == "finished" and row["seconds"] is not None:
                durations.setdefault(bucket, []).append(float(row["seconds"]) / 60.0)
            if row["source"] == "imported":
                estimated.add(bucket)

        steps: Dict[date, Dict[str, List[float]]] = {}
        runway: Dict[date, List[float]] = {}
        for row in self._rows(SQL_FINISH_STEPS, self._window_params(window)):
            started = parse_instant(row["started_at"])
            if started is None or row["skipped"]:
                continue
            bucket = self._bucket(started, window, WEEK)
            seconds = float(row["seconds"] or 0.0)
            steps.setdefault(bucket, {}).setdefault(str(row["step"]), []).append(seconds)
            if row["step"] == "runway" and seconds > 0:
                runway.setdefault(bucket, []).append(seconds)

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            counts = outcomes.get(bucket, {})
            sample = durations.get(bucket, [])
            medians = {
                step: median
                for step, values in steps.get(bucket, {}).items()
                if (median := percentile(values, 0.5)) is not None and median > 0
            }
            waited = runway.get(bucket, [])
            points.append(
                {
                    "bucket": bucket,
                    "finished": counts.get("finished", 0),
                    "escalated": counts.get("escalated", 0),
                    "declined": counts.get("declined", 0),
                    "interrupted": counts.get("interrupted", 0),
                    "reasons": reasons.get(bucket, {}),
                    "duration_p50_min": percentile(sample, 0.5),
                    "duration_p90_min": percentile(sample, 0.9),
                    "sample": len(sample),
                    "steps_p50_s": medians,
                    "runway_waited": len(waited),
                    "runway_p90_s": percentile(waited, 0.9),
                    "estimated": bucket in estimated,
                }
            )
        return points, coverage

    def gates(self, window: Window) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """G1 to G3: full gates per week of start."""
        edges = self._row(SQL_GATE_COVERAGE, (self.project_id,))
        coverage = self._series_coverage(
            window,
            label="gates",
            recorded_from=parse_instant(edges["recorded_from"]) if edges is not None else None,
            native_from=parse_instant(edges["native_from"]) if edges is not None else None,
            native_label="agent-side and manual gates",
        )

        full: Dict[date, int] = {}
        passed: Dict[date, int] = {}
        failed: Dict[date, Dict[str, int]] = {}
        origins: Dict[date, Dict[str, int]] = {}
        durations: Dict[date, List[float]] = {}
        estimated: Set[date] = set()
        for row in self._rows(SQL_GATES, self._window_params(window)):
            started = parse_instant(row["started_at"])
            if started is None or row["scope"] != "full":
                continue
            bucket = self._bucket(started, window, WEEK)
            full[bucket] = full.get(bucket, 0) + 1
            origin = origins.setdefault(bucket, {})
            origin[str(row["origin"])] = origin.get(str(row["origin"]), 0) + 1
            if row["passed"]:
                passed[bucket] = passed.get(bucket, 0) + 1
                if row["seconds"] is not None:
                    durations.setdefault(bucket, []).append(float(row["seconds"]) / 60.0)
            elif row["passed"] is not None and row["failed_stage"]:
                reds = failed.setdefault(bucket, {})
                reds[str(row["failed_stage"])] = reds.get(str(row["failed_stage"]), 0) + 1
            if row["source"] == "imported":
                estimated.add(bucket)

        stages: Dict[date, Dict[str, List[float]]] = {}
        for row in self._rows(SQL_GATE_STAGES, self._window_params(window)):
            started = parse_instant(row["started_at"])
            if started is None or row["seconds"] is None:
                continue
            bucket = self._bucket(started, window, WEEK)
            stages.setdefault(bucket, {}).setdefault(str(row["stage"]), []).append(
                float(row["seconds"])
            )

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            sample = durations.get(bucket, [])
            medians = {
                stage: median
                for stage, values in stages.get(bucket, {}).items()
                if (median := percentile(values, 0.5)) is not None
            }
            points.append(
                {
                    "bucket": bucket,
                    "full": full.get(bucket, 0),
                    "passed": passed.get(bucket, 0),
                    "failed_stages": failed.get(bucket, {}),
                    "duration_p50_min": percentile(sample, 0.5),
                    "duration_p90_min": percentile(sample, 0.9),
                    "sample": len(sample),
                    "stages_p50_s": medians,
                    "origins": origins.get(bucket, {}),
                    "estimated": bucket in estimated,
                }
            )
        return points, coverage

    def runs(self, window: Window) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """R-1 to R-4: dispatched runs per spine bucket of start.

        ``task_run`` carries no source column: since migration 005 re-stamped the
        cutover import from the dispatch entries' own instants (section 20.7), every
        row's ``started_at`` is the launch, so nothing here is estimated.
        """
        edge = self._row(SQL_RUN_COVERAGE, (self.project_id,))
        recorded_from = parse_instant(edge["recorded_from"]) if edge is not None else None
        coverage = self._series_coverage(
            window,
            label="runs",
            recorded_from=recorded_from,
            native_from=recorded_from,
            grain=window.bucket,
        )

        counts: Dict[date, int] = {}
        triggers: Dict[date, Dict[str, int]] = {}
        outcomes: Dict[date, Dict[str, int]] = {}
        hours: Dict[date, float] = {}
        in_flight: Dict[date, int] = {}
        durations: Dict[date, List[float]] = {}
        for row in self._rows(SQL_RUNS, self._window_params(window)):
            started = parse_instant(row["started_at"])
            if started is None:
                continue
            bucket = self._bucket(started, window, window.bucket)
            counts[bucket] = counts.get(bucket, 0) + 1
            trigger = triggers.setdefault(bucket, {})
            trigger[str(row["trigger"])] = trigger.get(str(row["trigger"]), 0) + 1
            seconds = float(row["duration_seconds"] or 0.0)
            hours[bucket] = hours.get(bucket, 0.0) + seconds / 3600.0
            if row["ended_at"] is None:
                in_flight[bucket] = in_flight.get(bucket, 0) + 1
                continue
            outcome = str(row["outcome"] or "unknown")
            bucket_outcomes = outcomes.setdefault(bucket, {})
            bucket_outcomes[outcome] = bucket_outcomes.get(outcome, 0) + 1
            if row["duration_seconds"] is not None:
                durations.setdefault(bucket, []).append(seconds / 60.0)

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, window.bucket, coverage.recorded_from):
            sample = durations.get(bucket, [])
            points.append(
                {
                    "bucket": bucket,
                    "runs": counts.get(bucket, 0),
                    "triggers": triggers.get(bucket, {}),
                    "agent_hours": round(hours.get(bucket, 0.0), 2),
                    "outcomes": outcomes.get(bucket, {}),
                    "in_flight": in_flight.get(bucket, 0),
                    "duration_p50_min": percentile(sample, 0.5),
                    "duration_p90_min": percentile(sample, 0.9),
                    "sample": len(sample),
                    "estimated": False,
                }
            )
        return points, coverage

    def machine(self, window: Window) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """R-5 and R-6, from the execution journal, per week.

        Read through the journal's own reader (section 18.5, decision 23). With no
        journal to read the series is absent and the coverage says so; a journal that
        refuses -- another process holding it, a schema this build does not know -- is
        reported the same way rather than as a 500 for the whole page.
        """
        if self.execution is None:
            return [], SeriesCoverage(note="The execution journal was not read.")
        try:
            edge = self.execution.read(SQL_EXEC_COVERAGE, (self.project_id,))
            attempts = self.execution.read(SQL_EXEC_ATTEMPTS, self._window_params(window))
            waits = self.execution.read(SQL_EXEC_QUEUE_WAITS, self._window_params(window))
            pauses = self.execution.read(SQL_EXEC_PAUSES, self._window_params(window))
        except Exception as exc:  # noqa: BLE001 - one series, not the page
            return [], SeriesCoverage(note=f"The execution journal could not be read: {exc}")

        recorded_from = parse_instant(edge[0]["recorded_from"]) if edge else None
        coverage = self._series_coverage(
            window,
            label="dispatch admissions",
            recorded_from=recorded_from,
            native_from=recorded_from,
        )

        admitted: Dict[date, int] = {}
        latency: Dict[date, List[float]] = {}
        for row in attempts:
            admitted_at = parse_instant(row["admitted_at"])
            if admitted_at is None:
                continue
            bucket = self._bucket(admitted_at, window, WEEK)
            admitted[bucket] = admitted.get(bucket, 0) + 1
            seconds = _seconds_between(admitted_at, parse_instant(row["launched_at"]))
            if seconds is not None:
                latency.setdefault(bucket, []).append(seconds)

        queued: Dict[date, List[float]] = {}
        for row in waits:
            queued_at = parse_instant(row["queued_at"])
            seconds = _seconds_between(queued_at, parse_instant(row["claimed_at"]))
            if queued_at is None or seconds is None:
                continue
            queued.setdefault(self._bucket(queued_at, window, WEEK), []).append(seconds)

        paused_hours: Dict[date, float] = {}
        paused_waiters: Dict[date, int] = {}
        for row in pauses:
            stall_at = parse_instant(row["stall_at"])
            seconds = _seconds_between(stall_at, parse_instant(row["updated_at"]))
            if stall_at is None or seconds is None:
                continue
            bucket = self._bucket(stall_at, window, WEEK)
            paused_hours[bucket] = paused_hours.get(bucket, 0.0) + seconds / 3600.0
            paused_waiters[bucket] = paused_waiters.get(bucket, 0) + 1

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            starts = latency.get(bucket, [])
            waited = queued.get(bucket, [])
            points.append(
                {
                    "bucket": bucket,
                    "admitted": admitted.get(bucket, 0),
                    "start_latency_p50_s": percentile(starts, 0.5),
                    "start_latency_p90_s": percentile(starts, 0.9),
                    "queued": len(waited),
                    "queue_wait_p50_s": percentile(waited, 0.5),
                    "queue_wait_p90_s": percentile(waited, 0.9),
                    "paused_run_hours": round(paused_hours.get(bucket, 0.0), 2),
                    "paused_waiters": paused_waiters.get(bucket, 0),
                }
            )
        return points, coverage

    def review(
        self, window: Window, page_coverage: Coverage
    ) -> Tuple[List[Dict[str, Any]], SeriesCoverage]:
        """R2, R3 and Q-2: how long a person took, per week.

        Review visits are paired per task in Python: an entry into ``human``/``review``
        and the next row that leaves it. The pairing is exact because the query returns
        both sides in order, and it is where a window function was measured slower.
        """
        coverage = self._series_coverage(
            window,
            label="review handoffs",
            recorded_from=page_coverage.baseline_at,
            native_from=page_coverage.native_from,
        )

        exits: Dict[date, List[float]] = {}
        approvals: Dict[date, int] = {}
        first_time: Dict[date, int] = {}
        estimated: Set[date] = set()
        entered: Optional[datetime] = None
        entered_estimated = False
        rounds = 0
        current_task: Optional[str] = None
        for row in self._rows(SQL_REVIEW_TRANSITIONS, (self.project_id, _iso(window.end))):
            if row["task_id"] != current_task:
                current_task, entered, rounds = str(row["task_id"]), None, 0
                entered_estimated = False
            instant = parse_instant(row["ts"])
            if instant is None:
                continue
            inexact = row["source"] != "native"
            entering = row["ball_to"] == "human" and row["ball_reason_to"] == "review"
            leaving = row["ball_from"] == "human" and row["ball_reason_from"] == "review"
            if leaving and entered is not None:
                if instant >= window.start:
                    bucket = self._bucket(instant, window, WEEK)
                    exits.setdefault(bucket, []).append(
                        max((instant - entered).total_seconds(), 0.0) / 3600.0
                    )
                    approved = row["lifecycle_to"] == "closed" or (
                        row["ball_to"] == "agent" and row["ball_reason_to"] == "work"
                    )
                    if approved:
                        approvals[bucket] = approvals.get(bucket, 0) + 1
                        if rounds == 1:
                            first_time[bucket] = first_time.get(bucket, 0) + 1
                    if inexact or entered_estimated:
                        estimated.add(bucket)
                entered = None
            if entering:
                entered, entered_estimated = instant, inexact
                rounds += 1

        questions: Dict[date, int] = {}
        answers: Dict[date, List[float]] = {}
        for row in self._rows(SQL_QUESTIONS, self._window_params(window)):
            asked = parse_instant(row["ts"])
            if asked is None:
                continue
            bucket = self._bucket(asked, window, WEEK)
            questions[bucket] = questions.get(bucket, 0) + 1
            answered = parse_instant(row["answered_at"])
            if answered is not None:
                answers.setdefault(bucket, []).append(
                    max((answered - asked).total_seconds(), 0.0) / 3600.0
                )

        points: List[Dict[str, Any]] = []
        for bucket in self._series_spine(window, WEEK, coverage.recorded_from):
            waits = exits.get(bucket, [])
            answered_in = answers.get(bucket, [])
            points.append(
                {
                    "bucket": bucket,
                    "exits": len(waits),
                    "approvals": approvals.get(bucket, 0),
                    "wait_p50_hours": percentile(waits, 0.5),
                    "wait_p90_hours": percentile(waits, 0.9),
                    "first_time_approvals": first_time.get(bucket, 0),
                    "questions": questions.get(bucket, 0),
                    "answered": len(answered_in),
                    "answer_p50_hours": percentile(answered_in, 0.5),
                    "answer_p90_hours": percentile(answered_in, 0.9),
                    "estimated": bucket in estimated,
                }
            )
        return points, coverage

    def in_review(self) -> List[Dict[str, Any]]:
        """R1: the open tasks waiting on a review, longest wait first."""
        waiting: List[Dict[str, Any]] = []
        for row in self._rows(SQL_IN_REVIEW, (self.project_id,)):
            since = parse_instant(row["held_since"])
            hours = (self.now - since).total_seconds() / 3600.0 if since is not None else 0.0
            waiting.append(
                {
                    "task_id": str(row["task_id"]),
                    "title": str(row["title"]),
                    "hours_waiting": round(max(hours, 0.0), 2),
                }
            )
        waiting.sort(key=lambda item: (-item["hours_waiting"], item["task_id"]))
        return waiting

    def open_questions(self) -> List[Dict[str, Any]]:
        """Q-1: questions on open tasks that nobody has answered, oldest first."""
        questions: List[Dict[str, Any]] = []
        for row in self._rows(SQL_OPEN_QUESTIONS, (self.project_id,)):
            asked = parse_instant(row["ts"])
            hours = (self.now - asked).total_seconds() / 3600.0 if asked is not None else 0.0
            questions.append(
                {
                    "task_id": str(row["task_id"]),
                    "entry_id": int(row["entry_id"]),
                    "hours_open": round(max(hours, 0.0), 2),
                }
            )
        return questions

    # -- the whole answer -------------------------------------------------

    def build(self, range_key: str = DEFAULT_RANGE) -> Dict[str, Any]:
        """The section 7.3 payload, as plain data the API model validates."""
        zone_name, _baseline, _kind = self.project_settings()
        zone = resolve_zone(zone_name)
        coverage = self.coverage(zone)
        window = self.window(range_key, coverage, zone)

        coverage.complete = (
            coverage.native_from is not None and window.start >= coverage.native_from
        )
        coverage.note = _coverage_note(coverage, zone)

        has_history = coverage.baseline_at is not None
        estimated = self.estimated_buckets(window) if has_history else set()
        open_rows = self.open_task_rows()

        # The second set. Each series is read whether or not the task history has a
        # baseline: a finish or a gate is recorded by its own writer and its own
        # coverage says what it can claim.
        segments, segments_coverage = self.segments(window, coverage)
        finishes, finishes_coverage = self.finishes(window)
        gates, gates_coverage = self.gates(window)
        cost, cost_coverage = self.cost_per_task(window, finishes_coverage, gates_coverage)
        runs, runs_coverage = self.runs(window)
        machine, machine_coverage = self.machine(window)
        review, review_coverage = self.review(window, coverage)

        return {
            "range": {
                "key": window.key,
                "start": window.start,
                "end": window.end,
                "bucket": window.bucket,
                "throughput_bucket": window.throughput_bucket,
                "timezone": zone_name,
            },
            "coverage": {
                "baseline_at": coverage.baseline_at,
                "baseline_kind": coverage.baseline_kind,
                "native_from": coverage.native_from,
                "reconstructed_before": coverage.reconstructed_before,
                "events": coverage.events,
                "complete": coverage.complete,
                "note": coverage.note,
            },
            "totals": self.totals(),
            "backlog": self.backlog(window, estimated) if has_history else [],
            "holders": self.holders(window) if has_history else [],
            "throughput": self.throughput(window) if has_history else [],
            "aging": self.aging(open_rows),
            "oldest": self.oldest(open_rows),
            "stuck": self.stuck(open_rows),
            "segments": segments,
            "segments_coverage": segments_coverage.as_dict(),
            "cost_per_task": cost,
            "cost_coverage": cost_coverage.as_dict(),
            "finishes": finishes,
            "finishes_coverage": finishes_coverage.as_dict(),
            "gates": gates,
            "gates_coverage": gates_coverage.as_dict(),
            "runs": runs,
            "runs_coverage": runs_coverage.as_dict(),
            "machine": machine,
            "machine_coverage": machine_coverage.as_dict(),
            "review": review,
            "review_coverage": review_coverage.as_dict(),
            "in_review": self.in_review(),
            "open_questions": self.open_questions(),
        }


def _local_date_text(instant: datetime, zone: ZoneInfo) -> str:
    """``23 Aug 2026`` -- the spelling every coverage caption uses."""
    return local_day(instant, zone).strftime("%d %b %Y").lstrip("0")


def _iso(instant: datetime) -> str:
    """A UTC instant in the spelling the store's own ``ts`` columns use.

    The comparison in every windowed query is a string comparison against these columns,
    so the parameter has to be written the way they were: ``Z``-suffixed, microseconds
    included. ``+00:00`` sorts before every digit and would silently widen each window
    to the whole history.
    """
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coverage_note(coverage: Coverage, zone: ZoneInfo) -> Optional[str]:
    """The one sentence section 9.3 puts in the footer, or ``None`` when there is none.

    Written here rather than on the page because it is a statement about the data, and
    the page must not have to derive it from four nullable fields and get it right.
    """
    if coverage.baseline_at is None:
        return None
    started = local_day(coverage.baseline_at, zone).strftime("%d %b %Y").lstrip("0")
    if coverage.reconstructed_before is None:
        return f"History from {started}."
    boundary = local_day(coverage.reconstructed_before, zone).strftime("%d %b %Y").lstrip("0")
    return (
        f"History from {started}. Events before {boundary} were reconstructed from the "
        "task records' own history, and timestamps in that span are bounds rather than "
        "observations."
    )


def build_analytics(
    store: Any,
    range_key: str = DEFAULT_RANGE,
    *,
    now: Optional[datetime] = None,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """The analytics payload for one project's store.

    Takes the store rather than a connection so every caller reaches the database the
    one sanctioned way (``ENGINEERING.md``'s safety rail), and reads through the store's
    own connection choice so a read inside a write transaction still sees the
    transaction.

    The execution journal is reached the same way -- ``execution_store_for`` on the
    machine home, never a path composed here (section 18.5, decision 23). ``home``
    defaults to the machine's own, which is what the server serves.
    """
    from agentjobs.execution.factory import execution_store_for
    from agentjobs.projects import default_home

    try:
        execution: Optional[Any] = execution_store_for(home or default_home())
    except Exception:  # noqa: BLE001 - the machine series is absent, not the page
        execution = None
    projection = AnalyticsProjection(
        store.read_connection(), store.project_id, now=now, execution=execution
    )
    return projection.build(range_key)


__all__ = [
    "AGE_BANDS",
    "AnalyticsProjection",
    "Coverage",
    "DEFAULT_RANGE",
    "EXECUTION_QUERIES",
    "QUERIES",
    "RANGE_KEYS",
    "SEGMENTS",
    "SPINE_BUCKET",
    "SeriesCoverage",
    "THROUGHPUT_BUCKET",
    "TaskSegments",
    "UNINDEXED_QUERIES",
    "WEEK",
    "Window",
    "build_analytics",
    "fold_segments",
    "segment_of",
    "settle_finish",
    "bucket_for",
    "bucket_start",
    "day_start_utc",
    "local_day",
    "next_bucket",
    "parse_instant",
    "percentile",
    "resolve_zone",
    "spine",
    "window_first_day",
]
