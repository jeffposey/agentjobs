"""What has just finished: the last few tasks to reach a terminal state.

The Dashboard says what is running and what needs a person. Neither answers "what
happened since I last looked", and once agents run more of the time that is the question
the page is opened with: work lands unattended, and the only trace of it today is a task
that has quietly left every list on the screen (task-460).

Three decisions are worth reading before the code.

**Finished means the task closed.** A run that ended without closing its task is not
finished work -- it is a run that stopped, which the live-run surface already says in the
words a person can act on. Reading a stopped run as a landing is the specific mistake
this region must not make.

**The timestamp is ``closed_at``, not ``updated``.** They differ exactly when somebody
edits a record after closing it, which is common: a correction, a redaction, a late
decision entry. ``updated`` moves for all of those and would re-date a month-old closure
to this morning, so a region whose whole claim is *recently* would print a lie with a
straight face. ``closed_at`` is stamped once by :meth:`SqlTaskStore._persist` at the
close and preserved across every later write, and it is cleared when a task is reopened,
so a row here is a task that is closed now and closed then.

**It is one query per project, over an index, parsing no task document.** The partial
index ``ix_task_closed_at(project_id, closed_at) WHERE closed_at IS NOT NULL`` covers
the whole of it, which is what makes a machine-wide fan-out across every registered
project affordable enough to sit on a page the phone polls. The alternative --
``list_tasks()`` per project, which is what ``dashboard.py`` does -- assembles every task
document in every project to read five titles off the end.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

RECENT_LIMIT = 5
"""How many closures the Dashboard's region shows.

Five one-line rows, against the three the two sampled sections beside it show. The
sections differ in height per row, not just in length: an active-task card is two lines
and a recent-update entry is two, so five single lines here is about the same wall of
pixels as three of either.

Five rather than three because of what actually produces a burst of closures: an epic
walk merges its children back to back, and a four-child epic finishing overnight would
overflow a three-row sample before the reader ever saw it. Five holds the common burst.
It is emphatically not a feed -- the Tasks list filtered to closed is the whole history,
and the region links to it.
"""

RECENT_WINDOW_DAYS = 7
"""How far back a closure may be and still be news.

A bound rather than "the last five, whenever they were". Without one, a quiet fortnight
fills the region with month-old rows under a heading that says *recently finished*, and
the reader has no way to tell that from a busy morning -- the failure being that stale
work reads as news. With it, a quiet week says so in a sentence, which is the honest
answer and the one the empty state exists for.

Seven days because the gap this answers is "since I last looked", which for a phone read
over Tailscale is hours to a day or two. A week is generously past that and still short
enough that nothing in the region is something the reader has already acted on twice.
"""


@dataclass(frozen=True)
class Closure:
    """One task that reached a terminal state, said in the fields a row needs."""

    task_id: str
    title: str
    project_id: str
    outcome: str
    closed_at: datetime


_RECENT_SQL = """
SELECT task_id, title, outcome, closed_at
  FROM task
 WHERE project_id = ?
   AND closed_at IS NOT NULL
   AND closed_at >= ?
 ORDER BY closed_at DESC
 LIMIT ?
"""
"""The whole query. ``closed_at IS NOT NULL`` is both the filter and the index's own
predicate, so the planner reads the partial index rather than the table.

There is no ``archived = 0`` clause, and that is a decision rather than an omission. A
task closed with ``--archive`` is closed in the same gesture, and it landed exactly as
much as any other; filing it away is about where it is *listed*, which is a statement
about the Tasks surface rather than about whether the work finished. Excluding them here
would make the region quietly incomplete on precisely the closures somebody chose to tidy
away in one move.
"""


def _parse(value: Optional[str]) -> Optional[datetime]:
    """A stored ISO-8601 stamp as an aware UTC datetime, or ``None``.

    The same normalisation ``analytics.parse_instant`` does, kept here rather than
    imported so this module does not pull in the analytics projection -- and spelled out
    because a naive value read as local time would shift a closure by the machine's
    offset and reorder the list.
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


def window_start(*, days: int = RECENT_WINDOW_DAYS, now: Optional[datetime] = None) -> datetime:
    """The oldest instant a closure may carry and still be shown."""
    moment = now or datetime.now(tz=timezone.utc)
    return moment - timedelta(days=days)


def _sql_instant(instant: datetime) -> str:
    """A UTC instant in the spelling ``closed_at`` itself is written in.

    The window's filter is a string comparison against that column, so the parameter has
    to be written the way the column was: ``Z``-suffixed, not ``+00:00``. The suffix only
    decides the comparison when everything before it is equal, but ``+`` sorts below every
    digit and below ``.``, so the wrong spelling makes the boundary second behave as if it
    were inside the window -- which is the same trap ``analytics._iso`` documents.
    """
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def recent_closures(
    store: Any,
    *,
    limit: int = RECENT_LIMIT,
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> List[Closure]:
    """The newest closures in one project's store, newest first.

    Takes the store rather than a connection, for the reason
    :func:`analytics.build_analytics` does: every reader reaches the database the one
    sanctioned way, and through the store's own connection choice, so a read inside a
    write transaction still sees the transaction.

    A store that cannot answer -- a backend with no SQL connection behind it, a database
    a migration has not reached -- yields no closures rather than raising. This is a
    region on a status page, and a machine-wide fan-out that took the Dashboard down
    because one of six projects was mid-migration would be a worse failure than a short
    list.
    """
    if limit <= 0:
        return []
    cutoff = since if since is not None else window_start(now=now)
    try:
        connection = store.read_connection()
    except Exception:  # pragma: no cover - a status region never fails over a backend
        return []
    try:
        rows = connection.execute(
            _RECENT_SQL, (store.project_id, _sql_instant(cutoff), limit)
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - nor over a query
        return []
    closures: List[Closure] = []
    for row in rows:
        closed_at = _parse(row["closed_at"])
        if closed_at is None:
            continue
        closures.append(
            Closure(
                task_id=row["task_id"],
                title=row["title"] or "",
                project_id=store.project_id,
                outcome=row["outcome"] or "",
                closed_at=closed_at,
            )
        )
    return closures


__all__ = [
    "Closure",
    "RECENT_LIMIT",
    "RECENT_WINDOW_DAYS",
    "recent_closures",
    "window_start",
]
