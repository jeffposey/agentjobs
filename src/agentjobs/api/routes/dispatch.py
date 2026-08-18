"""Dispatch state, the per-project toggle, and the run ledger, over HTTP.

The browser needs three things the CLI already has: whether this project may dispatch
right now and why not, what runs exist for a task, and how to stop one. This module is
those three, and deliberately nothing more.

**What is missing here is the point.** There is no endpoint that writes a runner, edits
an argv, sets a posture, or flips the master switch. Dispatch turns an unauthenticated
localhost API into remote code execution on this machine, so the browser-reachable
surface may switch a capability that a human already wrote into
``~/.agentjobs/dispatch.yaml`` on and off, and may never widen it (design section 6,
gate 3). ``set_project_enabled`` enforces the same rule one layer down by refusing a
runner the machine does not define, so this is defence in depth rather than a single
check.

Disable is the exception to every other rule about ceremony: it takes no body, asks no
questions, and works whether or not dispatch is configured. A kill switch you cannot
reach is not one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchError,
    assert_dispatch_permitted,
    dispatch_config_path,
    load_dispatch_config,
    sentinel_active,
    sentinel_path,
    set_project_enabled,
)
from agentjobs.dispatch.ledger import (
    DispatchLedger,
    LedgerError,
    RunRecord,
    find_run,
    list_runs,
)
from agentjobs.dispatch.runner import (
    OUTPUT_TAIL_LINES,
    STDERR_FILENAME,
    STDOUT_FILENAME,
    TRANSCRIPT_FILENAME,
    readable_tail,
    strip_ansi,
)
from agentjobs.manager import TaskManager
from agentjobs.projects import Project, default_home

from ..dependencies import get_task_manager, request_project
from ..models import ErrorBody
from .status import MutationError

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

OUTPUT_BYTE_LIMIT = 512_000
"""How much of a run's output the browser is handed.

A session transcript is unbounded and this endpoint exists so a human can see what an
agent did, not so a tab can be filled with a gigabyte. The tail is kept rather than the
head: what a run was doing when it stopped is the part anyone reads.
"""


# ----- read models ------------------------------------------------------------


class DispatchRefusalView(BaseModel):
    """The gate that currently refuses this project, in the API's own vocabulary.

    Carried on the state rather than raised as an error, because "you cannot dispatch,
    here is exactly why" is the normal answer for a project nobody has enabled -- not a
    failure. The GUI renders ``reason``-specific copy from it; ``message`` is the
    library's own sentence and is always safe to show.
    """

    reason: str = Field(..., description="Stable machine-readable code, e.g. 'disabled'.")
    message: str = Field(..., description="The refusal, in words.")


class DispatchStateView(BaseModel):
    """Everything the GUI needs to decide what to offer, and what to explain."""

    project_id: str
    configured: bool = Field(..., description="A dispatch.yaml exists on this machine.")
    master_enabled: bool = Field(..., description="The machine-wide 'enabled:' switch.")
    sentinel_active: bool = Field(..., description="DISPATCH_DISABLED exists; all runs refused.")
    project_enabled: bool = Field(..., description="This project is enabled for dispatch.")
    runner: Optional[str] = Field(default=None, description="Runner this project is pointed at.")
    posture: Optional[str] = Field(default=None, description="What a run here may do.")
    auto_dispatch: bool = Field(default=False, description="Auto-dispatch on approval (task-074).")
    available_runners: List[str] = Field(
        default_factory=list,
        description=(
            "Runner names this machine defines. Read-only: the browser may point a "
            "project at one of these and can never create one."
        ),
    )
    can_dispatch: bool = Field(..., description="Every gate is open right now.")
    refusal: Optional[DispatchRefusalView] = Field(
        default=None, description="Which gate refuses, when can_dispatch is false."
    )
    config_path: str = Field(..., description="Where a human edits any of this.")
    sentinel_file: str = Field(..., description="Path of the kill-switch sentinel.")


class DispatchRunView(BaseModel):
    """One run, as the browser sees it."""

    run_id: str
    task_id: str
    project_id: str
    mode: str
    posture: str
    status: str
    outcome: Optional[str] = None
    session_id: Optional[str] = None
    started_at: Optional[str] = None
    elapsed_seconds: Optional[float] = Field(
        default=None,
        description="Seconds since start for a live run; total duration once it ended.",
    )
    live: bool = Field(..., description="Nothing has declared this run over.")
    caused_by: Optional[int] = Field(
        default=None, description="Log entry id this run was attributed to."
    )
    output_url: str = Field(..., description="Where this run's captured output is readable.")


class DispatchRunTailView(BaseModel):
    """The end of a run's output, for a page that is watching it happen.

    Bounded on purpose. A session transcript grows for as long as the session does, and a
    pane on the task page that renders all of it turns the page a reader came to for the
    task record into a terminal emulator with a task record somewhere above it.
    """

    run_id: str
    live: bool = Field(..., description="Nothing has declared this run over.")
    source: str = Field(
        ...,
        description=(
            "Where the text came from: 'session-transcript' (the session's own output), "
            "'captured-output' (what the process wrote to stdout/stderr), or 'none'."
        ),
    )
    lines: int = Field(..., description="How many lines this tail is bounded to.")
    text: str = Field(..., description="The tail itself, escape sequences already removed.")
    updated_at: Optional[str] = Field(
        default=None, description="When the file behind this text last changed."
    )


class DispatchCancelResult(BaseModel):
    """What cancelling asked for, and whether it happened."""

    run_id: str
    stopped: bool
    detail: str
    run: DispatchRunView


class DispatchEnableRequest(BaseModel):
    """Point a project at a runner this machine already defines, and turn it on.

    ``runner`` names an existing runner; it never creates one. Omitted, the project
    keeps the runner it already names, or takes the only one defined -- the same rule
    ``agentjobs dispatch enable`` follows, so the two surfaces cannot disagree.
    """

    runner: Optional[str] = Field(default=None, min_length=1)


# ----- helpers ----------------------------------------------------------------


def _home() -> Path:
    """The AgentJobs home whose dispatch config and runs this server acts on."""
    return default_home()


def _run_view(record: RunRecord, project: Project) -> DispatchRunView:
    """Render a ledger record for the browser, elapsed time computed server-side.

    Computed here rather than in the browser because a run's ``started_at`` is this
    machine's clock and the phone reading the page is not on it. A tablet five minutes
    fast would otherwise show every run as having started in the future.
    """
    return DispatchRunView(
        run_id=record.run_id,
        task_id=record.task_id,
        project_id=record.project_id,
        mode=record.mode,
        posture=record.posture,
        status=record.status,
        outcome=record.outcome,
        session_id=record.session_id,
        started_at=record.started_at.isoformat() if record.started_at else None,
        elapsed_seconds=record.elapsed_seconds(),
        live=record.is_live,
        caused_by=record.caused_by,
        output_url=(f"/api/projects/{project.id}/dispatch/runs/{record.run_id}/output"),
    )


def _owned_run(run_id: str, project: Project) -> RunRecord:
    """One run of this project, or a 404 that does not admit runs of another exist.

    A run belonging to a different project is reported as absent rather than forbidden:
    the page asking is scoped to one project, and "you may not read that one" would tell
    it about runs it has no business knowing are there.
    """
    try:
        record = find_run(_home(), run_id)
    except LedgerError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if record.project_id and record.project_id != project.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} does not belong to project {project.id!r}.",
        )
    return record


def _read(path: Path) -> str:
    """A file's text, or a sentence saying why not. Never raises at a reader."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - unreadable mid-write
        return f"(could not be read: {exc})"


def _run_output(record: RunRecord) -> Tuple[str, str, Optional[float]]:
    """This run's output as a human should read it: (source, text, when it last changed).

    The two modes genuinely differ and are not unified here. A **batch** run's
    ``stdout.log`` is its output. A **session** run's is the launcher's backgrounding
    banner, and always will be, however long the session lives -- its own output is the
    transcript the poller copies in beside it. Serving one from the other's file is how
    "View output" came to show 249 bytes of banner for a run that had been working for
    seven minutes.

    A session run with no transcript yet falls back to what was captured rather than
    reporting nothing: before the first poll the banner is all there is, and it is at
    least true.
    """
    transcript = record.path / TRANSCRIPT_FILENAME
    if record.is_session and transcript.is_file():
        text = _read(transcript)
        if text.strip():
            # Stripped here rather than at capture: the stored copy stays what the
            # terminal actually showed, and this is the rendering of it.
            return "session-transcript", strip_ansi(text), transcript.stat().st_mtime

    sections: List[str] = []
    latest: Optional[float] = None
    for name in (STDOUT_FILENAME, STDERR_FILENAME):
        candidate = record.path / name
        if not candidate.is_file():
            continue
        text = _read(candidate)
        if text.strip():
            sections.append(f"--- {name} ---\n{text}")
            latest = max(latest or 0.0, candidate.stat().st_mtime)
    if not sections:
        return "none", "", None
    return "captured-output", "\n\n".join(sections), latest


def _moment(mtime: Optional[float]) -> Optional[str]:
    """A file modification time as the browser reads timestamps everywhere else."""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _state(project: Project) -> DispatchStateView:
    """Resolve every gate for this project without starting anything."""
    home = _home()
    config: Optional[DispatchConfig]
    try:
        config = load_dispatch_config(home)
    except DispatchError:
        # An unparseable config is not an absent one, and saying "not configured" would
        # send the reader off to create a file that already exists. Report it as the
        # refusal it is, below, and show the rest as unknown.
        config = None
        unreadable = True
    else:
        unreadable = False

    settings = config.project(project.id) if config else None
    refusal: Optional[DispatchRefusalView] = None
    try:
        assert_dispatch_permitted(project.id, home)
        can_dispatch = True
    except DispatchError as exc:
        can_dispatch = False
        refusal = DispatchRefusalView(
            reason=getattr(exc, "reason", "dispatch_error"), message=str(exc)
        )

    return DispatchStateView(
        project_id=project.id,
        configured=config is not None or unreadable,
        master_enabled=bool(config and config.enabled),
        sentinel_active=sentinel_active(home),
        project_enabled=bool(settings and settings.enabled),
        runner=settings.runner if settings else None,
        posture=settings.posture.value if settings else None,
        auto_dispatch=bool(settings and settings.auto_dispatch),
        available_runners=sorted(config.runners) if config else [],
        can_dispatch=can_dispatch,
        refusal=refusal,
        config_path=str(dispatch_config_path(home)),
        sentinel_file=str(sentinel_path(home)),
    )


def _refusal_error(exc: DispatchError) -> MutationError:
    """Render a toggle refusal under the gate's own code, never as a bare 400.

    A ``MutationError`` rather than an ``HTTPException`` so the body has the same shape
    every other refusal in this API has -- ``code`` and ``message`` at the top level, not
    buried under FastAPI's ``detail``. The browser reads all refusals through one
    function, and one endpoint answering in a different shape is how that function
    silently starts returning null.
    """
    reason = getattr(exc, "reason", "dispatch_error")
    return MutationError(
        status.HTTP_409_CONFLICT,
        ErrorBody(
            code=reason,
            message=str(exc),
            detail=str(exc),
            retryable=False,
            suggested_action=_TOGGLE_ACTION.get(reason),
        ),
    )


_TOGGLE_ACTION = {
    "not_configured": "Create ~/.agentjobs/dispatch.yaml and define a runner first.",
    "unknown_runner": "Pick a runner this machine already defines, or add one by hand.",
    "invalid_config": "Fix the YAML in ~/.agentjobs/dispatch.yaml, then try again.",
}


# ----- endpoints --------------------------------------------------------------


@router.get("", response_model=DispatchStateView)
async def get_dispatch_state(project: Project = Depends(request_project)) -> DispatchStateView:
    """Whether this project may dispatch right now, and which gate says otherwise."""
    return _state(project)


@router.post("/enable", response_model=DispatchStateView)
async def enable_dispatch(
    payload: DispatchEnableRequest = DispatchEnableRequest(),
    project: Project = Depends(request_project),
) -> DispatchStateView:
    """Enable dispatch for this project against an already-defined runner."""
    try:
        set_project_enabled(project.id, True, runner=payload.runner, home=_home())
    except DispatchError as exc:
        raise _refusal_error(exc) from exc
    return _state(project)


@router.post("/disable", response_model=DispatchStateView)
async def disable_dispatch(project: Project = Depends(request_project)) -> DispatchStateView:
    """Stop dispatching for this project. Takes nothing, asks nothing.

    Refuses only when there is no config file at all, which is already a state in which
    nothing can dispatch -- so there is no reachable case where a human wants this off
    and cannot have it.
    """
    try:
        set_project_enabled(project.id, False, home=_home())
    except DispatchError as exc:
        raise _refusal_error(exc) from exc
    return _state(project)


@router.get("/runs", response_model=List[DispatchRunView])
async def list_dispatch_runs(
    task_id: Optional[str] = Query(
        default=None, description="Only runs for this task. Omitted, every run in the project."
    ),
    limit: int = Query(default=20, ge=1, le=200),
    project: Project = Depends(request_project),
) -> List[DispatchRunView]:
    """Runs belonging to this project, newest first.

    Filtered by project rather than returning the machine's whole ledger: a run
    directory records which project it belongs to, and a page about one project has no
    business showing another's.
    """
    records = [record for record in list_runs(_home()) if record.project_id == project.id]
    if task_id:
        records = [record for record in records if record.task_id == task_id]
    return [_run_view(record, project) for record in records[:limit]]


@router.post("/runs/{run_id}/cancel", response_model=DispatchCancelResult)
async def cancel_dispatch_run(
    run_id: str,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(request_project),
) -> DispatchCancelResult:
    """Stop one run and write its cancellation to the task record."""
    home = _home()
    # The manager is handed in rather than looked up. This request already resolved the
    # project, and a server serving an implicit project -- AGENTJOBS_PROJECT_ROOT, no
    # registry entry -- would otherwise stop the run and have nowhere to write what
    # happened to it.
    ledger = DispatchLedger(home, managers={project.id: manager})
    _owned_run(run_id, project)
    try:
        result = ledger.cancel(run_id)
    except LedgerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DispatchCancelResult(
        run_id=result.run_id,
        stopped=result.stopped,
        detail=result.detail,
        run=_run_view(find_run(home, run_id), project),
    )


@router.get(
    "/runs/{run_id}/output",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
async def read_dispatch_run_output(
    run_id: str, project: Project = Depends(request_project)
) -> PlainTextResponse:
    """A run's output in full, as text a browser tab can show.

    Text rather than JSON because this is the one dispatch response a human reads
    directly, and a transcript wrapped in a JSON string escape is unreadable.
    """
    record = _owned_run(run_id, project)
    _, text, _ = _run_output(record)
    body = text or f"No output captured for run {run_id}."
    if len(body) > OUTPUT_BYTE_LIMIT:
        body = "(earlier output omitted)\n" + body[-OUTPUT_BYTE_LIMIT:]
    return PlainTextResponse(body)


@router.get("/runs/{run_id}/tail", response_model=DispatchRunTailView)
async def read_dispatch_run_tail(
    run_id: str,
    lines: int = Query(default=OUTPUT_TAIL_LINES, ge=1, le=200),
    project: Project = Depends(request_project),
) -> DispatchRunTailView:
    """The end of a run's output, for a page watching it while it happens.

    A separate endpoint from ``/output`` rather than a parameter on it, because the two
    are read by different things: ``/output`` is a link a human opens, so it answers in
    text and gives them everything, while this is polled by a page and answers in the
    shape the generated client already understands.

    **No subprocess is involved.** The text comes from a file the session poller wrote,
    so a hundred people watching one run costs a hundred file reads and no more processes
    than nobody watching it -- and the tail can never be fresher than the poller's own
    interval, which is the point rather than a limitation.
    """
    record = _owned_run(run_id, project)
    source, text, mtime = _run_output(record)
    return DispatchRunTailView(
        run_id=record.run_id,
        live=record.is_live,
        source=source,
        lines=lines,
        text=readable_tail(text, lines),
        updated_at=_moment(mtime),
    )
