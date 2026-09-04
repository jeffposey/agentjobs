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
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchError,
    Posture,
    SelectionSource,
    assert_dispatch_permitted,
    dispatch_config_path,
    load_dispatch_config,
    sentinel_active,
    sentinel_path,
    set_project_enabled,
)
from agentjobs.dispatch.finish_status import (
    FinishStatus,
    finish_output,
    read_finish_status,
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
    drop_repainted_lines,
    readable_tail,
    strip_ansi,
)
from agentjobs.dispatch.transcript import (
    DEFAULT_ENTRY_LIMIT,
    MAX_ENTRY_LIMIT,
    StructuredTranscript,
    find_session_transcript,
    read_structured_transcript,
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
    group: Optional[str] = Field(
        default=None, description="Runner group this project is pointed at, if any."
    )
    posture: Optional[str] = Field(
        default=None, description="What a run here gets when nothing else names a posture."
    )
    max_posture: Optional[str] = Field(
        default=None,
        description=(
            "The widest posture any run here may get, whatever asks for it (task-308). "
            "Equal to `posture` on a project that has not set a ceiling of its own, "
            "because that is the only default that cannot silently widen an existing "
            "machine."
        ),
    )
    offerable_postures: List[str] = Field(
        default_factory=list,
        description=(
            "Every posture at or below `max_posture`, narrowest first. Sent rather "
            "than derived, for the same reason `resolved_from` is: a browser that "
            "re-implements the ceiling is the one place in the system that could offer "
            "a choice the dispatch API will refuse."
        ),
    )
    posture_merge_policies: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "What each posture does to the *branch*, keyed by posture value (task-021: "
            "`read_only` -> none, `auto`/`supervised` -> review, `autonomous` -> "
            "automatic). Sent rather than hardcoded in the client for the same reason "
            "`offerable_postures` is, and the stake is higher: this is the difference "
            "between 'stops for your review' and 'merges without you', so a browser "
            "that carried its own copy could tell an operator the opposite of what the "
            "posture they picked will actually do."
        ),
    )
    finish_enabled: bool = Field(
        default=False,
        description=(
            "Whether the scripted finish (task-241) is on for this project, which is "
            "what an autonomous merge runs through. task-021 accepted the consequence "
            "that without it there is no sanctioned mechanism for one -- so a chooser "
            "offers `autonomous` disabled here rather than granting an envelope whose "
            "merge cannot be performed."
        ),
    )
    push: bool = Field(
        default=False,
        description=(
            "Whether this project permits pushing. Per project and never a posture "
            "property (task-021), and false everywhere today. Surfaced because 'this "
            "project will merge my work without asking me, and publish it' is the one "
            "thing worth knowing beside a Dispatch button."
        ),
    )
    auto_dispatch: bool = Field(default=False, description="Auto-dispatch on approval (task-074).")
    available_runners: List[str] = Field(
        default_factory=list,
        description=(
            "Runner names this machine defines. Read-only: the browser may point a "
            "project at one of these and can never create one."
        ),
    )
    available_groups: List[str] = Field(
        default_factory=list,
        description=(
            "Runner group names this machine defines. Read-only on the same terms as "
            "available_runners: pointing a project at an existing group is selecting "
            "among machine-local definitions, and authoring one is not reachable here."
        ),
    )
    default_group: Optional[str] = Field(
        default=None,
        description="Machine-wide group, used by any project that names none of its own.",
    )
    resolved_runner: Optional[str] = Field(
        default=None,
        description=(
            "The runner that would actually start right now. Null when a gate refuses, "
            "because there is then no answer rather than a stale one."
        ),
    )
    resolved_group: Optional[str] = Field(
        default=None,
        description=(
            "The group that chose `resolved_runner`, when one participated. Null for a "
            "flat config, so a machine with no groups reads exactly as it did before "
            "they existed."
        ),
    )
    resolved_from: Optional[str] = Field(
        default=None,
        description=(
            "Which rung of the precedence ladder decided: 'project' for the project's "
            "own group, 'machine' for default_group, 'project_runner' for a plain "
            "runner. Sent rather than derived, because a browser that re-implements the "
            "ladder is the one place in the system that would disagree with the "
            "dispatcher about what runs."
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
        description=(
            "Seconds since start for a live run; the total it ran for once it ended, "
            "measured to its finish time so it stops moving. Null when a concluded run "
            "has no recorded finish time -- render that as unknown, never as a number."
        ),
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


class TranscriptCallView(BaseModel):
    """One tool call inside a run of them, for the disclosure that holds the detail."""

    name: str = Field(..., description="The tool, as the runner names it.")
    title: str = Field(default="", description="The one line that stands for this call.")
    detail: str = Field(default="", description="What was actually run or written, bounded.")
    output: str = Field(default="", description="The end of what came back, bounded.")
    failed: bool = Field(default=False, description="The call answered with an error.")
    added: int = 0
    removed: int = 0


class TranscriptEntryView(BaseModel):
    """One thing the agent did.

    ``kind`` is ``'narration'`` (the agent's own prose), ``'tools'`` (a run of
    consecutive calls, summarized, with ``calls`` holding the detail), or ``'prompt'``
    (what a human -- or the dispatcher -- asked for).
    """

    kind: str
    text: str = Field(default="", description="Prompt or narration text; empty for tools.")
    summary: str = Field(default="", description="A run of calls in one line.")
    added: int = 0
    removed: int = 0
    failed: int = Field(default=0, description="How many calls in this run answered with an error.")
    calls: List[TranscriptCallView] = Field(default_factory=list)


class DispatchRunTranscriptView(BaseModel):
    """A run's transcript as structured entries, for a panel that renders rather than dumps.

    Distinct from ``DispatchRunTailView`` and not a replacement for it. That one serves
    the terminal capture, which stays the only evidence when a session dies in a way no
    renderer models; this one serves what the session recorded about itself.
    """

    run_id: str
    live: bool = Field(..., description="Nothing has declared this run over.")
    source: str = Field(
        ...,
        description=(
            "'session-jsonl' when the session's own structured transcript was read, "
            "'none' when there was nothing to read -- in which case 'note' says why "
            "and the caller should fall back to the tail."
        ),
    )
    note: str = Field(default="", description="Why there is nothing structured, in a sentence.")
    entries: List[TranscriptEntryView] = Field(default_factory=list)
    total_entries: int = Field(default=0, description="How many entries the transcript holds.")
    truncated: bool = Field(
        default=False, description="Earlier entries exist and were not returned."
    )
    updated_at: Optional[str] = Field(
        default=None, description="When the file behind these entries last changed."
    )


class FinishStepView(BaseModel):
    """One step of a scripted finish, as the task page renders it."""

    name: str
    state: str = Field(
        ...,
        description=(
            "'done', 'skipped', 'stopped' (this is where the finish gave up), or "
            "'running' (inferred from the fixed order, and true of a finish that is "
            "between steps as well as one in the middle of this one)."
        ),
    )
    detail: str = ""
    seconds: float = 0.0
    meaning: str = Field(
        default="",
        description="What this step is, for a reader who has not read ENGINEERING.md.",
    )


class FinishGateView(BaseModel):
    """How far into the gate a finish is, when the gate is what it is doing."""

    stage: str = Field(default="", description="The stage running now; empty once it ended.")
    stages_run: int = 0
    stages_total: int = 0
    running: bool = False
    passed: Optional[bool] = Field(
        default=None, description="Null while the gate is still running."
    )
    seconds: float = 0.0
    failed_stage: str = ""


class TaskFinishView(BaseModel):
    """What is happening to this task's branch right now, or last happened to it.

    The answer to a question the API could not previously be asked. Approving a task on
    a project with the scripted finish switched on starts a process that takes minutes
    and, until task-321, said nothing to the page that started it: the ball moved to
    ``agent``/``work`` and the reader was left to guess whether anything had picked the
    approval up.
    """

    task_id: str
    project_id: str
    state: str = Field(
        ...,
        description=(
            "'starting' (spawned, nothing written yet), 'running', 'finished', "
            "'escalated' (it stopped and handed back), 'declined' (never a candidate), "
            "or 'interrupted' (it wrote no ending and its process is gone)."
        ),
    )
    live: bool = Field(..., description="Something is working on this task's branch now.")
    finish_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: Optional[float] = Field(
        default=None,
        description=(
            "Seconds since it started while live; the total it took once it ended. "
            "Computed on the server, because started_at is this machine's clock and "
            "the phone reading the page is not on it."
        ),
    )
    branch: str = ""
    worktree: str = ""
    current_step: str = ""
    steps: List[FinishStepView] = Field(default_factory=list)
    gate: Optional[FinishGateView] = None
    reason: str = ""
    stopped_at: str = Field(default="", description="Which step an escalation stopped at.")
    merge_commit: str = ""
    output_source: str = Field(
        default="none",
        description=(
            "Where the text came from: 'finish-log' (the spawned process's own output, "
            "which is the whole step table and exists only once it has ended), "
            "'gate-log' (the gate's output, for a finish run inside a session), or "
            "'none' -- which is the normal answer while one is still running."
        ),
    )
    output_tail: str = Field(default="", description="The end of that text, bounded.")
    output_url: str = Field(..., description="Where the whole of it is readable.")


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
    group: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Runner group to point this project at, instead of a single runner. Names "
            "an existing group; it never creates one. Mutually exclusive with `runner`."
        ),
    )


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
            # Rendered here rather than at capture: the stored copy stays what the
            # terminal actually showed, and this is the rendering of it -- escape
            # sequences off, and the repainted copies of each screen collapsed.
            return (
                "session-transcript",
                drop_repainted_lines(strip_ansi(text)),
                transcript.stat().st_mtime,
            )

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


def _finish_view(status: FinishStatus, project: Project) -> TaskFinishView:
    """Render one finish for the browser, output tail included.

    The tail rides in this response rather than behind a second endpoint, which is the
    one place this deliberately differs from runs. A run's transcript grows for as long
    as the session does and has to be paged; a finish's output is a step table of a
    dozen lines that does not exist at all until the process ends. Splitting it would
    cost a second poll for a field that is empty for the whole of the time anybody is
    watching.
    """
    source, text, _ = finish_output(_home(), status)
    return TaskFinishView(
        task_id=status.task_id,
        project_id=status.project_id or project.id,
        state=status.state,
        live=status.live,
        finish_id=status.finish_id,
        started_at=status.started_at,
        finished_at=status.finished_at,
        elapsed_seconds=status.elapsed_seconds,
        branch=status.branch,
        worktree=status.worktree,
        current_step=status.current_step,
        steps=[
            FinishStepView(
                name=step.name,
                state=step.state,
                detail=step.detail,
                seconds=step.seconds,
                meaning=step.meaning,
            )
            for step in status.steps
        ],
        gate=(
            FinishGateView(
                stage=status.gate.stage,
                stages_run=status.gate.stages_run,
                stages_total=status.gate.stages_total,
                running=status.gate.running,
                passed=status.gate.passed,
                seconds=status.gate.seconds,
                failed_stage=status.gate.failed_stage,
            )
            if status.gate
            else None
        ),
        reason=status.reason,
        stopped_at=status.stopped_at,
        merge_commit=status.merge_commit,
        output_source=source,
        output_tail=readable_tail(text, OUTPUT_TAIL_LINES),
        output_url=(f"/api/projects/{project.id}/dispatch/finishes/{status.task_id}/output"),
    )


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
    resolved_runner: Optional[str] = None
    resolved_group: Optional[str] = None
    resolved_from: Optional[str] = None
    try:
        # The resolution is kept rather than discarded: a project pointed at a group
        # names no runner of its own, so without this the page can only report the
        # absence -- which is how a fully configured project came to render "no runner
        # chosen" (task-184). This is the same call the CLI prints its winner from, so
        # the two surfaces cannot disagree about what would run.
        resolution = assert_dispatch_permitted(project.id, home)
        can_dispatch = True
        resolved_runner = resolution.runner.name
        selection = resolution.selection
        resolved_group = selection.group if selection else None
        resolved_from = (
            selection.source.value if selection else SelectionSource.PROJECT_RUNNER.value
        )
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
        group=settings.group if settings else None,
        posture=settings.posture.value if settings else None,
        max_posture=settings.ceiling.value if settings else None,
        offerable_postures=(
            [posture.value for posture in settings.offerable_postures()] if settings else []
        ),
        posture_merge_policies={posture.value: posture.merge_policy.value for posture in Posture},
        finish_enabled=bool(settings and settings.finish.enabled),
        push=bool(settings and settings.push),
        auto_dispatch=bool(settings and settings.auto_dispatch),
        available_runners=sorted(config.runners) if config else [],
        available_groups=sorted(config.runner_groups) if config else [],
        default_group=config.default_group if config else None,
        resolved_runner=resolved_runner,
        resolved_group=resolved_group,
        resolved_from=resolved_from,
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
    "unknown_group": "Pick a runner group this machine already defines, or write one by hand.",
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
        set_project_enabled(
            project.id, True, runner=payload.runner, group=payload.group, home=_home()
        )
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


def _session_home() -> Path:
    """The home directory whose runner transcript stores this server reads.

    A function rather than ``Path.home()`` inline so a test can point it somewhere it
    controls. It is *not* :func:`_home`: that one is AgentJobs' own home, and these files
    belong to the runner rather than to us.
    """
    return Path.home()


def _structured_transcript(
    record: RunRecord, project: Project, entries: int
) -> StructuredTranscript:
    """This run's structured transcript, or an empty one carrying the reason.

    A batch run has no session and therefore no such file; saying so is more useful than
    an empty panel, and the caller shows the captured output instead.
    """
    if not record.is_session:
        return StructuredTranscript(
            note=(
                "This run was a batch command rather than a session, so there is no "
                "structured transcript. What it wrote is below."
            )
        )
    if not record.session_id:
        return StructuredTranscript(
            note=(
                "This run has not reported a session id yet, so its structured "
                "transcript cannot be located. It appears within a poll or two of the "
                "session starting."
            )
        )
    path = find_session_transcript(record.session_id, project.root, home=_session_home())
    return read_structured_transcript(path, entries)


@router.get("/runs/{run_id}/transcript", response_model=DispatchRunTranscriptView)
async def read_dispatch_run_transcript(
    run_id: str,
    entries: int = Query(default=DEFAULT_ENTRY_LIMIT, ge=1, le=MAX_ENTRY_LIMIT),
    project: Project = Depends(request_project),
) -> DispatchRunTranscriptView:
    """What the session recorded about itself, as entries rather than as a screen.

    Polled on the same clock as ``/tail`` and read the same way -- a file, no subprocess
    -- but from a different file. ``/tail`` serves the pty capture, which is a *repaint*
    of a terminal and loses every space and line break the moment its escape sequences
    are removed. This serves the JSONL the runner writes beside it, where the agent's
    prose, each tool call and whether it failed are all still separate things.

    ``source: 'none'`` is an ordinary answer, not an error: a batch run, a session that
    has not reported its id yet, and a driver that keeps no such file all reach it. The
    ``note`` says which, and the caller falls back to the tail.
    """
    record = _owned_run(run_id, project)
    result = _structured_transcript(record, project, entries)
    return DispatchRunTranscriptView(
        run_id=record.run_id,
        live=record.is_live,
        source=result.source,
        note=result.note,
        entries=[
            TranscriptEntryView(
                kind=entry.kind,
                text=entry.text,
                summary=entry.summary,
                added=entry.added,
                removed=entry.removed,
                failed=entry.failed,
                calls=[
                    TranscriptCallView(
                        name=call.name,
                        title=call.title,
                        detail=call.detail,
                        output=call.output,
                        failed=call.failed,
                        added=call.added,
                        removed=call.removed,
                    )
                    for call in entry.calls
                ],
            )
            for entry in result.entries
        ],
        total_entries=result.total_entries,
        truncated=result.truncated,
        updated_at=_moment(result.updated_at),
    )


@router.get("/finishes/{task_id}", response_model=Optional[TaskFinishView])
async def read_task_finish(
    task_id: str, project: Project = Depends(request_project)
) -> Optional[TaskFinishView]:
    """What is happening to this task's branch, or last happened to it.

    Keyed on the task rather than on a finish id, because the question a page asks is
    "what is happening to *this*", and the reader pressing Approve has no finish id to
    ask with -- the finish that answers it does not exist yet at the moment they press.

    ``null`` is the ordinary answer for almost every task, and means no finish has run
    for it on this machine. The page renders nothing at all for that, which is why it is
    a null body rather than a 404: an absent finish is not a missing resource, and a
    task page that logged a 404 every two seconds would teach its reader to ignore them.
    """
    status = read_finish_status(_home(), task_id, project.id)
    if status is None:
        return None
    return _finish_view(status, project)


@router.get(
    "/finishes/{task_id}/output",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
async def read_task_finish_output(
    task_id: str, project: Project = Depends(request_project)
) -> PlainTextResponse:
    """A finish's output in full, as text a browser tab can show.

    Text rather than JSON, for the same reason a run's is: this is read by a person, and
    a step table wrapped in a JSON string escape is unreadable.
    """
    status = read_finish_status(_home(), task_id, project.id)
    if status is None:
        return PlainTextResponse(f"No finish has run for {task_id} on this machine.")
    source, text, _ = finish_output(_home(), status)
    if not text.strip():
        body = (
            f"The finish for {task_id} is {status.state} and has written no output yet. "
            "A spawned finish writes its step table when the process ends; while it is "
            "running, the steps on the task page are what is live."
        )
    else:
        body = text
    if len(body) > OUTPUT_BYTE_LIMIT:
        body = "(earlier output omitted)\n" + body[-OUTPUT_BYTE_LIMIT:]
    return PlainTextResponse(body)
