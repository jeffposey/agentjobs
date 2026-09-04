"""What is running on this machine right now, across every project.

Every other run view in AgentJobs is scoped to one task or one project, and each of them
is right to be. This one is deliberately not, because **the resource being spent is the
machine**: ``limits.max_concurrent_runs`` is machine-level, the ledger under
``~/.agentjobs/runs/`` is machine-level, and the run occupying the last slot is usually
on somebody else's task in somebody else's project. A page that can only see its own
project cannot answer "why can I not dispatch this", which is the question (task-328).

That is also why this router is mounted **once**, at ``/api/runs``, rather than joining
``PROJECT_SCOPED_ROUTERS``. Those are mounted twice -- ``/api`` and
``/api/projects/{project_id}`` -- and resolve their project from the path. A handler
here would ignore that parameter, so the same answer would be served under every
spelling of it: a URL asserting a scope the body does not have.

**Read-only, and that is a boundary rather than an oversight** (task-328's
``out_of_scope``). Cancelling from a machine-wide list is the obvious next feature and
is deliberately not here; a cancel offered beside a row that a poll refreshed a second
ago is a click aimed at whatever has since taken that row's place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentjobs.dispatch.config import DispatchError, load_dispatch_config
from agentjobs.dispatch.finish_status import read_finish_status
from agentjobs.dispatch.ledger import (
    KIND_FINISH,
    KIND_RUNWAY,
    LockHolder,
    RunRecord,
    live_lock_holders,
    live_runs,
    run_health,
    runway_lock_name,
)
from agentjobs.projects import Project, default_home

from ..dependencies import list_projects, storage_for

router = APIRouter(prefix="/api/runs", tags=["dispatch"])


class LiveRunView(BaseModel):
    """One run that is happening now, said in words a person can act on."""

    run_id: str
    task_id: str
    task_title: str = Field(
        default="",
        description=(
            "The task's title, resolved server-side from its project's storage. Empty "
            "when the record cannot be read -- a run whose task file has been renamed "
            "or removed is exactly the kind of thing this surface should still show."
        ),
    )
    project_id: str
    project_name: str = Field(
        default="",
        description=(
            "The project's display name. Falls back to the id for a run whose project "
            "is no longer registered, rather than rendering a blank cell."
        ),
    )
    mode: str
    session: bool = Field(..., description="A session run, as opposed to a batch one.")
    posture: str
    status: str = Field(..., description="The ledger's own word. Prefer `health` for display.")
    health: str = Field(
        ...,
        description=(
            "What the run is actually doing: working, starting, parked, silent, "
            "orphaned or unknown. `live` means only that nothing has declared the run "
            "over, so this is the field a surface renders."
        ),
    )
    started_at: Optional[str] = None
    elapsed_seconds: Optional[float] = Field(
        default=None,
        description=(
            "Seconds since this run started, computed on the server. The phone reading "
            "this page is not on the clock that wrote `started_at`."
        ),
    )
    task_url: str = Field(..., description="Where this run's task is, in this app.")
    output_url: str = Field(..., description="Where this run's captured output is readable.")


class MachineHolderView(BaseModel):
    """Something holding a lock on this machine that is not a dispatched run.

    A scripted finish (task-241) and the merge runway (task-223) are both real work
    competing for this machine, both leave a lock behind, and neither has a run record
    -- so neither is reachable through ``live_runs``. Leaving them out would make the
    surface answer "nothing is happening" during the three minutes a merge takes.
    """

    kind: str = Field(..., description="`finish` or `runway`.")
    lock_name: str = Field(..., description="The lock file's stem. A task id, or a runway key.")
    task_id: str = Field(default="", description="Empty for a runway, which holds no task.")
    project_id: str = ""
    project_name: str = ""
    finish_id: str = ""
    pid: Optional[int] = None
    started_at: str = ""
    elapsed_seconds: Optional[float] = None
    detail: str = Field(
        default="",
        description="What it is doing right now, e.g. the finish step it is on.",
    )
    task_url: str = Field(default="", description="Empty when there is no task to link to.")


class LiveRunsView(BaseModel):
    """Everything both machine-wide surfaces need, in one response.

    The capacity numbers ride along with the list rather than being fetched separately
    or recomputed in TypeScript: a browser that divided a count by a ceiling it read from
    somewhere else would be the one place in the system able to disagree with
    ``dispatch/guards.py`` about whether the machine is full.
    """

    occupied: int = Field(
        ...,
        description=(
            "Run slots in use. Counted exactly as the concurrency guard counts them -- "
            "`len(live_runs(home))` -- so this surface and a refused dispatch can never "
            "disagree. Finishes and the runway are not in it: they hold locks, not run "
            "slots."
        ),
    )
    max_concurrent_runs: int = Field(
        ..., description="`limits.max_concurrent_runs` from ~/.agentjobs/dispatch.yaml."
    )
    dispatch_configured: bool = Field(
        ...,
        description=(
            "False on a machine with no dispatch.yaml at all, where the ceiling below is "
            "a default rather than a setting anyone chose."
        ),
    )
    runs: List[LiveRunView]
    holders: List[MachineHolderView]
    generated_at: str = Field(..., description="When this answer was assembled, in UTC.")


def _home() -> Path:
    """The AgentJobs home whose dispatch config and runs this server acts on."""
    return default_home()


def _ceiling() -> tuple[int, bool]:
    """``(max_concurrent_runs, configured)``, never raising at a reader.

    A machine with no dispatch config has no runs either, so the honest answer is the
    default ceiling and a flag saying nobody chose it -- not a 500 on a status page.
    """
    try:
        config = load_dispatch_config(_home())
    except DispatchError:
        return 1, False
    if config is None:
        return 1, False
    return config.limits.max_concurrent_runs, True


def _projects_by_id() -> Dict[str, Project]:
    """Every project this server can serve, keyed by id."""
    return {project.id: project for project in list_projects()}


def _task_title(project: Optional[Project], task_id: str) -> str:
    """One task's title, or ``""`` when it cannot be read.

    Deliberately swallows every failure. This is a status page about *runs*: a run whose
    project has been unregistered, or whose task file has been renamed out from under
    it, is precisely the situation worth showing, and a traceback would hide it.
    """
    if project is None or not task_id:
        return ""
    try:
        task = storage_for(project).load_task(task_id)
    except Exception:  # pragma: no cover - a status page never fails over a title
        return ""
    return getattr(task, "title", "") or ""


def _task_url(project_id: str, task_id: str) -> str:
    """The React app's route for one task, in whichever project owns it.

    Built here rather than in the browser because the whole point of this surface is
    that its rows belong to *other* projects, and a client that assembled the link from
    the project it happens to be displaying would send every row to the wrong place.
    """
    if not project_id or not task_id:
        return ""
    return f"/p/{project_id}/tasks/{task_id}"


def _elapsed_since(started_at: str) -> Optional[float]:
    """Seconds since an ISO-8601 stamp, or ``None`` when it cannot be read."""
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def _run_view(record: RunRecord, projects: Dict[str, Project]) -> LiveRunView:
    """Render one live run for the browser."""
    project = projects.get(record.project_id)
    return LiveRunView(
        run_id=record.run_id,
        task_id=record.task_id,
        task_title=_task_title(project, record.task_id),
        project_id=record.project_id,
        project_name=project.name if project else record.project_id,
        mode=record.mode,
        session=record.is_session,
        posture=record.posture,
        status=record.status,
        health=run_health(record),
        started_at=record.started_at.isoformat() if record.started_at else None,
        elapsed_seconds=record.elapsed_seconds(),
        task_url=_task_url(record.project_id, record.task_id),
        output_url=(
            f"/api/projects/{record.project_id}/dispatch/runs/{record.run_id}/output"
            if record.project_id
            else ""
        ),
    )


def _runway_owners(projects: Dict[str, Project]) -> Dict[str, Project]:
    """Runway lock name -> the project whose checkout it protects.

    A runway lock is keyed on a hash of the resolved repository path, so the name cannot
    be read back into a path. It can be recognised, though, by hashing the roots we know
    about -- which is enough to say *whose* merge is in progress. Two projects sharing a
    clone share a runway by design, so the first match is as good as any.
    """
    owners: Dict[str, Project] = {}
    for project in projects.values():
        try:
            owners.setdefault(runway_lock_name(project.root), project)
        except OSError:  # pragma: no cover - an unresolvable root is not fatal here
            continue
    return owners


def _holder_view(
    lock_name: str,
    holder: LockHolder,
    projects: Dict[str, Project],
    runways: Dict[str, Project],
) -> MachineHolderView:
    """Render one non-run lock holder."""
    if holder.is_runway:
        project = runways.get(lock_name)
        return MachineHolderView(
            kind=KIND_RUNWAY,
            lock_name=lock_name,
            project_id=project.id if project else "",
            project_name=project.name if project else "an unregistered checkout",
            finish_id=holder.finish_id,
            pid=holder.pid,
            started_at=holder.started_at,
            elapsed_seconds=_elapsed_since(holder.started_at),
            detail="merging -- every other finish in this repository is queued behind it",
        )

    task_id = lock_name
    try:
        # No project id: the finish's own meta records which project it belongs to, and
        # this lock does not say. Asking each registered project in turn would be the
        # same scan repeated once per project for one answer.
        status = read_finish_status(_home(), task_id)
    except Exception:  # pragma: no cover - a status page never fails over a detail
        status = None
    project_id = status.project_id if status else ""
    project = projects.get(project_id)
    return MachineHolderView(
        kind=KIND_FINISH,
        lock_name=lock_name,
        task_id=task_id,
        project_id=project_id,
        project_name=project.name if project else project_id,
        finish_id=holder.finish_id or (status.finish_id if status else ""),
        pid=holder.pid,
        started_at=holder.started_at or (status.started_at if status else ""),
        elapsed_seconds=_elapsed_since(holder.started_at)
        or (status.elapsed_seconds if status else None),
        detail=(status.current_step if status and status.current_step else "merging"),
        task_url=_task_url(project_id, task_id),
    )


@router.get("/live", response_model=LiveRunsView)
async def list_live_runs() -> LiveRunsView:
    """Every run happening on this machine, with what is left of its capacity.

    One request answers both surfaces task-328 ships -- the Runs tab and the Dashboard's
    capacity row -- which is why the ceiling and the occupied count are in the body
    rather than left to a second call.
    """
    home = _home()
    projects = _projects_by_id()
    records = live_runs(home)
    ceiling, configured = _ceiling()

    runways = _runway_owners(projects)
    holders = [
        _holder_view(lock_name, holder, projects, runways)
        for lock_name, holder in live_lock_holders(home)
        # Finishes and runways only. A dispatch's lock is not a *third* thing happening:
        # it is one of the runs above, and listing it again would imply the machine is
        # twice as busy as it is. The one dispatch lock that names no live run is the
        # sub-second window between taking the lock and the run existing to be named,
        # and a row that appears for that long is noise rather than information.
        if holder.is_runway or holder.is_finish
    ]

    return LiveRunsView(
        occupied=len(records),
        max_concurrent_runs=ceiling,
        dispatch_configured=configured,
        runs=[_run_view(record, projects) for record in records],
        holders=holders,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
