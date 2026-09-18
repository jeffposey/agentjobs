"""What has just finished, across every project this caller may see (task-460).

Mounted **once**, at ``/api/recent``, for the reason ``routes/runs.py`` is: the answer is
not about one project. The Dashboard's question here is "what landed while I was not
looking", and on a machine running several projects the thing that landed overnight is
routinely in a different one. A handler under
``/api/projects/{project_id}/...`` would either ignore that segment -- a URL asserting a
scope the body does not have -- or answer a narrower question than the one asked.

**The fan-out is here rather than in the browser.** One request, one indexed query per
visible project, merged and truncated on the server. The alternative is a page that
issues a request per project and sorts the union in TypeScript, which costs a request per
project on every poll and puts the ordering rule somewhere no test of this module can see
it.

**Read-only, and exposure is the whole of its access control.** A closure carries a task
id, a title and a project name, so a project the caller may not see must contribute no
rows at all -- not a redacted row, and not a count. ``visible_projects`` is the same
filter ``/api/runs/live`` applies for the same reason (task-333); this route adds no
capability check because it adds no capability: reads are governed by exposure, not by
``docs/authorization.md``'s verb table.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from agentjobs.closures import (
    RECENT_LIMIT,
    RECENT_WINDOW_DAYS,
    Closure,
    recent_closures,
    window_start,
)
from agentjobs.principals import Principal
from agentjobs.projects import Project

from ..dependencies import get_principal, storage_for, visible_projects

router = APIRouter(prefix="/api/recent", tags=["dashboard"])

MAX_LIMIT = 20
"""The most rows one request may ask for.

The region shows :data:`RECENT_LIMIT`. This ceiling exists so the parameter cannot be
turned into a full history export by a caller that guesses at it -- the Tasks list
filtered to closed is that, with the paging and the filters a history needs.
"""

MAX_WINDOW_DAYS = 90
"""The furthest back one request may reach, for the same reason as :data:`MAX_LIMIT`."""


class ClosureView(BaseModel):
    """One finished task, in the fields a row on the Dashboard renders."""

    task_id: str
    task_title: str
    project_id: str
    project_name: str = Field(
        default="",
        description="The project's display name, falling back to its id.",
    )
    outcome: str = Field(
        ...,
        description=(
            "How it ended: `completed`, `cancelled`, `superseded` or `duplicate`. Shown "
            "as written -- a region that printed every row as *done* would be hiding the "
            "difference the reader is scanning for."
        ),
    )
    closed_at: str = Field(
        ...,
        description=(
            "When the task closed, in UTC. This is the store's `closed_at`, stamped at "
            "the close and untouched by later edits -- not `updated`, which an edit "
            "after closing would move."
        ),
    )
    age_seconds: float = Field(
        ...,
        description=(
            "Seconds since it closed, computed on the server. The phone reading this "
            "page is not on the clock that wrote the stamp, and *how long ago* is what "
            "the row is actually read for."
        ),
    )
    task_url: str = Field(..., description="Where this task is, in this app.")


class RecentClosuresView(BaseModel):
    """The region's whole answer, with the bounds it was computed under."""

    closures: List[ClosureView]
    limit: int = Field(..., description="The most rows this answer could have held.")
    window_days: int = Field(
        ...,
        description=(
            "How far back it looked. On the wire because the empty state says it -- "
            "*nothing has finished in the last 7 days* -- and a page that hard-coded the "
            "number would keep saying seven after this endpoint stopped meaning it."
        ),
    )
    generated_at: str = Field(..., description="When this answer was assembled, in UTC.")


def _task_url(project_id: str, task_id: str) -> str:
    """The React app's route for one task, in whichever project owns it.

    Built here rather than in the browser because these rows belong to *other* projects
    by design, and a client assembling the link from the project it happens to be
    displaying would send half of them to the wrong place.
    """
    return f"/p/{project_id}/tasks/{task_id}"


def _view(closure: Closure, project: Project, now: datetime) -> ClosureView:
    """Render one closure for the browser."""
    return ClosureView(
        task_id=closure.task_id,
        task_title=closure.title,
        project_id=closure.project_id,
        project_name=project.name or closure.project_id,
        outcome=closure.outcome,
        closed_at=closure.closed_at.isoformat(),
        age_seconds=max(0.0, (now - closure.closed_at).total_seconds()),
        task_url=_task_url(closure.project_id, closure.task_id),
    )


@router.get("/closures", response_model=RecentClosuresView)
async def list_recent_closures(
    limit: int = Query(default=RECENT_LIMIT, ge=1, le=MAX_LIMIT),
    days: int = Query(default=RECENT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
    principal: Optional[Principal] = Depends(get_principal),
) -> RecentClosuresView:
    """The newest closed tasks this caller may see, newest first.

    **Each project is asked for ``limit`` rows and the merge keeps ``limit``.** Asking
    for fewer per project would make the answer depend on how the closures happen to be
    distributed -- one busy project would be cut short while a quiet one contributed a
    week-old row ahead of it.

    **A project that cannot be read contributes nothing and does not fail the request.**
    A store mid-migration, or one whose database has been moved, is a deployment fact;
    taking the Dashboard down over it would be a worse answer than a list that is one
    project short.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = window_start(days=days, now=now)
    collected: List[tuple[Closure, Project]] = []
    for project in visible_projects(principal):
        try:
            storage = storage_for(project)
        except Exception:  # pragma: no cover - a status region never fails over a store
            continue
        for closure in recent_closures(storage, limit=limit, since=cutoff):
            collected.append((closure, project))
    # Newest first across every project, then the id, so two closures sharing a
    # microsecond -- an import, a scripted finish closing siblings -- have a settled
    # order rather than whichever project was read first.
    collected.sort(key=lambda pair: (-pair[0].closed_at.timestamp(), pair[0].task_id))
    return RecentClosuresView(
        closures=[_view(closure, project, now) for closure, project in collected[:limit]],
        limit=limit,
        window_days=days,
        generated_at=now.isoformat(),
    )
