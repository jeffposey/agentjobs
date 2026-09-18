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
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RangeKey = str
"""One of ``30d``, ``90d``, ``12m``, ``all`` -- section 7.1's whole parameter surface."""

RANGE_KEYS: Tuple[str, ...] = ("30d", "90d", "12m", "all")
DEFAULT_RANGE: RangeKey = "90d"

#: How the backlog and holder spines are bucketed, per section 8.3: "day for 30d and
#: 90d, week for 12m, week for all".
SPINE_BUCKET: Dict[str, str] = {"30d": "day", "90d": "day", "12m": "week", "all": "week"}

#: How throughput and cycle time are bucketed. One grain coarser than the spine, and
#: deliberately so -- see :func:`bucket_for` for the argument and the alternative.
THROUGHPUT_BUCKET: Dict[str, str] = {
    "30d": "week",
    "90d": "week",
    "12m": "month",
    "all": "month",
}

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

    **Two grains, not one, and it is a decision rather than an oversight.** Section 8.3
    fixes the spine at "day for 30d and 90d, week for 12m, week for all", because the
    backlog readout is a day's level and a day's flows. Section 8.4 draws throughput as
    bars with a cycle-time percentile line over them, and ``ThroughputPoint.bucket`` is
    specified as "first day of the week/month" -- a daily grain there would put nine
    tenths of the bars at zero and leave every percentile below section 8.4's own
    ``sample < 3`` suppression threshold.

    Rejected: one grain for the whole response. It forces a choice between a throughput
    chart of 90 near-empty bars and a backlog level coarsened to weeks, which would
    remove the daily readout line section 8.3 requires. The cost of two grains is one
    extra field on ``AnalyticsRange`` so a client never has to infer which it was given.
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
SELECT ts, task_id, outcome_to
  FROM task_event
 WHERE project_id = ? AND lifecycle_to = 'closed'
   AND (lifecycle_from IS NULL OR lifecycle_from <> 'closed')
   AND ts >= ? AND ts < ?
 ORDER BY ts
"""

#: Cycle time is ``created_at`` -> ``closed_at`` on the task row (section 3.1), so it
#: is answered from ``ix_task_closed_at`` without touching the history at all.
SQL_CYCLE_TIMES = """
SELECT closed_at, created_at
  FROM task
 WHERE project_id = ? AND outcome = 'completed'
   AND closed_at IS NOT NULL AND closed_at >= ? AND closed_at < ?
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

#: Every query above, by the name the design gives it. ``tests/test_analytics_api.py``
#: walks this to record a plan for each one (acceptance ac-6).
QUERIES: Dict[str, str] = {
    "backlog opening balance": SQL_BACKLOG_OPENING,
    "backlog series": SQL_BACKLOG_EVENTS,
    "close events": SQL_CLOSE_EVENTS,
    "cycle times": SQL_CYCLE_TIMES,
    "holder opening balance": SQL_HOLDER_OPENING,
    "holder series": SQL_HOLDER_EVENTS,
    "the five current counts": SQL_TOTALS,
    "open tasks": SQL_OPEN_TASKS,
    "ball held since": SQL_BALL_SINCE,
    "inexact events": SQL_INEXACT_EVENTS,
    "event sources": SQL_EVENT_SOURCES,
    "coverage edges": SQL_COVERAGE_EDGES,
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
    ) -> None:
        self.connection = connection
        self.project_id = project_id
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

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
        """Completions, cancellations and cycle time, one point per throughput bucket.

        ``tasks_completed`` and ``completion_events`` are kept apart because a reopened
        task closes twice (section 3.3): the chart plots the first, and the second is
        there so a reader whose sums do not match has an answer rather than a suspicion.
        ``cancelled`` is closed-with-any-other-outcome, which is not throughput and is
        not hidden either -- a month of cancellations must not read as a quiet month.
        """
        grain = window.throughput_bucket
        completed: Dict[date, set] = {}
        events: Dict[date, int] = {}
        cancelled: Dict[date, set] = {}
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

        durations: Dict[date, List[float]] = {}
        for row in self._rows(
            SQL_CYCLE_TIMES, (self.project_id, _iso(window.start), _iso(window.end))
        ):
            closed_at = parse_instant(row["closed_at"])
            created_at = parse_instant(row["created_at"])
            if closed_at is None or created_at is None:
                continue
            day = bucket_start(local_day(closed_at, window.zone), grain)
            days_taken = (closed_at - created_at).total_seconds() / 86400.0
            durations.setdefault(day, []).append(max(days_taken, 0.0))

        points: List[Dict[str, Any]] = []
        for day in spine(window.first_day, window.last_day, grain):
            sample = durations.get(day, [])
            points.append(
                {
                    "bucket": day,
                    "tasks_completed": len(completed.get(day, ())),
                    "completion_events": events.get(day, 0),
                    "cancelled": len(cancelled.get(day, ())),
                    "cycle_p50_days": percentile(sample, 0.5),
                    "cycle_p90_days": percentile(sample, 0.9),
                    "sample": len(sample),
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
        }


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
    store: Any, range_key: str = DEFAULT_RANGE, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The analytics payload for one project's store.

    Takes the store rather than a connection so every caller reaches the database the
    one sanctioned way (``ENGINEERING.md``'s safety rail), and reads through the store's
    own connection choice so a read inside a write transaction still sees the
    transaction.
    """
    projection = AnalyticsProjection(store.read_connection(), store.project_id, now=now)
    return projection.build(range_key)


__all__ = [
    "AGE_BANDS",
    "AnalyticsProjection",
    "Coverage",
    "DEFAULT_RANGE",
    "QUERIES",
    "RANGE_KEYS",
    "SPINE_BUCKET",
    "THROUGHPUT_BUCKET",
    "Window",
    "build_analytics",
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
