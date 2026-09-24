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
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agentjobs.dispatch import pull as dispatch_pull
from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch import start_pause
from agentjobs.dispatch.config import machine_ceiling
from agentjobs.dispatch.finish_status import (
    OVERTAKEN,
    FinishStatus,
    machine_live_finishes,
    read_finish_status,
)
from agentjobs.dispatch.ledger import (
    KIND_FINISH,
    KIND_RUNWAY,
    HEALTH_FINISHING,
    HEALTH_WORK_DONE,
    LockHolder,
    RunRecord,
    live_lock_holders,
    split_lock_name,
    live_runs,
    run_health,
    runway_lock_name,
)
from agentjobs.dispatch import epic
from agentjobs.dispatch.journal import journal
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.execution.store import QueuedDispatch, Supervision
from agentjobs.exposure import Visibility, readable_by
from agentjobs.models_v2 import Outcome, StatusCategory, closed_status
from agentjobs.principals import Principal
from agentjobs.projects import Project, default_home

from ..dependencies import get_principal, manager_for, storage_for, visible_projects

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
            "handback, work_done, finishing, orphaned or unknown. `live` means only that "
            "nothing has declared the run over, so this is the field a surface renders. "
            "`finishing` comes from the same live-finish lookup as the task read's "
            "`live_finish`, so it and the task's Finishing chip cannot disagree (task-533)."
        ),
    )
    task_display_status: str = Field(
        default="",
        description=(
            "When `health` is `work_done`: the closed task's own status word -- "
            "Completed, Cancelled, Superseded or Duplicate -- so the run's chip says what "
            "the task's chip says rather than a word of its own (task-577). Empty when "
            "the task cannot be read, or the run's task is still open."
        ),
    )
    task_status_category: Optional[StatusCategory] = Field(
        default=None,
        description="The category `task_display_status` is drawn in, beside it.",
    )
    finish_id: str = Field(
        default="",
        description="The live finish for this run's task, when `health` is `finishing`.",
    )
    finish_step: str = Field(
        default="",
        description=(
            "The step that finish is on, in the finish's own vocabulary (`runway`, "
            "`gate`...). `runway` means it is queued for the repository's merge runway."
        ),
    )
    runway_behind: str = Field(
        default="",
        description=(
            "When the finish is queued for the runway: the task whose finish holds it, "
            "if that can be told. Empty otherwise."
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
    over_ceiling: bool = Field(
        default=False,
        description=(
            "This run was started above `max_concurrent_runs` because a person chose to "
            "(task-461). While it lives, `occupied` exceeds the ceiling -- and a board "
            "that could not say which run explains that would be showing a count nobody "
            "can reconcile."
        ),
    )
    holds_slot: bool = Field(
        ...,
        description=(
            "Whether this run is one of the `occupied` slots. False for an interactive "
            "session and a walk, which never took one, and for a run whose task has "
            "closed while its session stayed open (task-482) -- that one released its "
            "slot and reads `work_done` in `health`. Sent rather than re-derived from "
            "`mode`, so a board can never draw a run in a slot cell the server does not "
            "count."
        ),
    )


class MachineHolderView(BaseModel):
    """Something holding a lock on this machine that is not a dispatched run.

    A scripted finish (task-241) and the merge runway (task-223) are both real work
    competing for this machine, both leave a lock behind, and neither has a run record
    -- so neither is reachable through ``live_runs``. Leaving them out would make the
    surface answer "nothing is happening" during the three minutes a merge takes.
    """

    kind: str = Field(..., description="`finish` or `runway`.")
    lock_name: str = Field(
        ...,
        description=(
            "The lock file's stem: a task id, or a runway key. For a finish drawn from its "
            "record because it holds no finish lock yet (task-533), `project:task`."
        ),
    )
    task_id: str = Field(
        default="",
        description=(
            "The task. For a runway, the task whose live finish holds it, or empty when "
            "no live finish names that runway's finish id (task-533)."
        ),
    )
    task_title: str = ""
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
    overtaken: bool = Field(
        default=False,
        description=(
            "True when this finish holds a task that is already closed and it merged "
            "nothing, so it is not the finish that finished it (task-514)."
        ),
    )
    runway_behind: str = Field(
        default="",
        description=(
            "For a finish queued for the runway: the task whose finish holds it, if that "
            "can be told (task-533)."
        ),
    )


class QueuedDispatchView(BaseModel):
    """One authorised dispatch waiting for a slot (task-459).

    Beside the live runs rather than in a call of its own, for the reason the capacity
    numbers are: a board that read "three of three busy" from one endpoint and "two
    waiting" from another would have two answers about one machine, taken a second apart.
    """

    queue_id: str = Field(
        ...,
        description=(
            "The entry's id. It is what `POST .../dispatch/runs/{run_id}/cancel` takes "
            "while the dispatch is waiting -- a queued entry cancels through the same "
            "route as the run it has not become."
        ),
    )
    position: int = Field(..., description="1-based place in line. FIFO by when it was queued.")
    task_id: str
    task_title: str = ""
    project_id: str
    project_name: str = ""
    queued_at: str = Field(..., description="When it joined the queue, UTC.")
    waiting_seconds: Optional[float] = Field(
        default=None,
        description=(
            "How long it has been waiting, computed on the server. The phone reading "
            "this page is not on the clock that wrote `queued_at`."
        ),
    )
    queued_by: str = Field(
        default="", description="Who asked for it. Empty for a dispatch nobody signed."
    )
    source: str = Field(default="manual", description="`manual` today; `pull` is reserved.")
    status: str = Field(
        ...,
        description=(
            "`queued`, or `starting` for the seconds a tick is putting it through the "
            "dispatch gates. A `starting` entry is not yet a run and may still be refused."
        ),
    )
    detail: str = Field(
        default="",
        description=(
            "Why it is still waiting, when a start has already been tried and refused "
            "for a condition that clears on its own."
        ),
    )
    task_url: str = Field(..., description="Where this entry's task is, in this app.")
    paused_by: str = Field(
        default="",
        description=(
            "The incident id holding this entry off (task-463), or empty when nothing "
            "is. An entry with this set is not being tried at all: no attempt, no "
            "hourly cap, nothing written to its task. It keeps its position."
        ),
    )


class StartPauseView(BaseModel):
    """One open incident, as the reason the machine is starting nothing on a credential.

    **Machine-wide and about starts, not about runs.** The runs it already parked are
    task-417's business and appear on their own tasks; what this says is why the two
    clickless starters -- the dispatch queue and the pull mode -- are quiet, which is a
    question a person asks of the board and of nothing else.

    It is a list rather than one object because the pause is per credential: a machine
    with a Claude home out of quota and a working Codex runner has one pause, and the
    Codex rows carry on starting beside it.
    """

    incident_id: str = Field(..., description="task-417's incident. The same id its park names.")
    kind: str = Field(
        ...,
        description=(
            "`usage_limit`, `auth` or `spend_limit`. Only `usage_limit` ends by itself, "
            "which is why `resets_at` is null for the other two."
        ),
    )
    kind_word: str = Field(
        ..., description="How the kind reads in a sentence: `usage limit`, `login`, `spend limit`."
    )
    runner: str = Field(
        ...,
        description=(
            "The runner whose starts are held. Two runners sharing one login each report "
            "the same incident under their own name, because a board shows rows rather "
            "than credentials."
        ),
    )
    resets_at: Optional[str] = Field(
        default=None,
        description=(
            "When the subscription window reopens, UTC, or null. **Render it in the "
            "reader's own zone**: the limit is a wall-clock fact to whoever is waiting "
            "for it. Null means nothing has undertaken to clear this on its own and the "
            "board must not print a time."
        ),
    )
    opened_at: str = Field(..., description="When the incident opened, UTC.")
    resumes_by_itself: bool = Field(
        ...,
        description=(
            "True when the reset arrives with no person involved, so the board may say "
            "'paused until <time>' rather than 'until the incident clears'."
        ),
    )
    queued: int = Field(
        default=0, description="Queued entries waiting on this incident, over the whole machine."
    )
    projects: List[str] = Field(
        default_factory=list,
        description="Armed projects whose pull mode is held by it, by project id.",
    )
    detail: str = Field(
        default="", description="The same sentence the tick's own report prints, in UTC."
    )


class ArmedProjectView(BaseModel):
    """One project the pull mode is armed for, as the slot board draws it (task-462).

    Machine-wide, beside the runs and the waiting dispatches, because that is the scope
    of the thing being described: the pull mode competes for the same `max_concurrent_runs`
    every other row here is about, and a board that learned "armed" from a per-project
    call would show an arming that is not the one taking the slot in front of it.
    """

    project_id: str
    project_name: str = ""
    arming_id: str
    armed_by: str = Field(default="", description="The person who armed it.")
    armed_at: str = Field(..., description="When they armed it, UTC.")
    bound: str = Field(
        ...,
        description=(
            "What is left of the bound, as one phrase -- e.g. `1 of 3 starts used`, "
            "`until 2026-09-19T06:00:00Z`, `until disarmed`. Composed on the server so "
            "the board, the CLI and the dispatch panel say the same words."
        ),
    )
    starts_left: Optional[int] = Field(
        default=None, description="Runs it may still start, or null when the bound is a moment."
    )
    posture: Optional[str] = Field(
        default=None, description="The envelope pulled runs get, or null for the project default."
    )
    next_task_id: str = Field(
        default="",
        description=(
            "What `task_next` says it would start next. Sent so a person can see it "
            "*before* it happens and move something else to the top if they would rather. "
            "Empty when nothing in that backlog is claimable right now."
        ),
    )
    next_task_title: str = ""
    next_task_url: str = Field(default="", description="Where that task is, in this app.")
    paused_by: str = Field(
        default="",
        description=(
            "The incident id holding this arming's starts off (task-463), or empty. The "
            "arming keeps its bound and its authority while this is set and starts again "
            "on the first tick after the incident closes; `next_task_id` still says what "
            "it will start then."
        ),
    )


class EpicWalkView(BaseModel):
    """One epic walk that is still open, as the board draws it (task-523).

    A walk is the only thing on this machine that dispatches work with no human act at
    the moment of dispatch, and since task-458 it is hosted by the server rather than by
    a blocking process -- so its own ``mode: "walk"`` run is normally over before any
    board is drawn and there was nothing on the page to explain the children arriving.
    This row is that explanation.

    **It holds no slot**, exactly as its run did not. Nothing here is counted against
    ``occupied`` or takes a cell from the board; a walk is supervision, and the slots it
    fills are filled by the children's own runs, which are already in ``runs``.
    """

    walk_id: str
    project_id: str
    project_name: str = Field(
        default="", description="Falls back to the id for a project no longer registered."
    )
    parent_task_id: str = Field(..., description="The epic being walked.")
    parent_task_title: str = Field(
        default="", description="Resolved server-side, empty when the record cannot be read."
    )
    parent_task_url: str = Field(default="", description="Where that task is, in this app.")
    started_at: str = Field(
        default="",
        description=(
            "When the walk was authorised, UTC. The age of a walk is most of what a "
            "reader wants from it: an epic that has been supervising for four hours is "
            "a different fact from one that started a minute ago."
        ),
    )
    children_total: int = Field(
        default=0,
        description=(
            "Every child of the parent, counted from the task graph rather than from "
            "the walk's own rows. The walk only has a row for a child it has already "
            "touched, so counting its rows would report a five-child epic that has "
            "flown two as having three children."
        ),
    )
    children_completed: int = Field(
        default=0, description="Children closed with outcome `completed`."
    )
    children_in_flight: int = Field(
        default=0,
        description=(
            "Children this walk has in the air now -- admitted or flying, and still "
            "open. Each one is a run of its own and appears in `runs` on its own terms."
        ),
    )
    children_remaining: int = Field(
        default=0, description="Open children that are not in flight: what is still to come."
    )
    in_flight_task_ids: List[str] = Field(
        default_factory=list, description="The children in the air, so a reader can follow them."
    )
    grounded: bool = Field(
        ...,
        description=(
            "The walk has stopped taking off. **The field this section exists for**: a "
            "grounded walk and a quiet one look identical from the outside, and a "
            "reader who cannot tell them apart reads a stall as progress."
        ),
    )
    grounded_reason: str = Field(
        default="",
        description="The `WalkStop` that grounded it, or empty while it is still flying.",
    )
    grounded_word: str = Field(
        default="",
        description=(
            "How that reason reads in a sentence, composed on the server so the board "
            "and the CLI say the same words."
        ),
    )
    waiting_on_task_id: str = Field(
        default="",
        description=(
            "The open child this walk is merely *waiting* on, or empty when the "
            "grounding is a real stop. Answered against the live records by "
            "`epic.waiting_child`, which is the same function the walk itself asks, so "
            "the board cannot disagree with the tick about whether this clears."
        ),
    )
    waiting_on_task_title: str = ""
    waiting_on_task_url: str = ""
    resumes_by_itself: bool = Field(
        default=False,
        description=(
            "True when the thing it is waiting on clears without anybody deciding it "
            "has, so the board may say the walk takes off again on its own. False on a "
            "real stop, where the epic moves again only if a person does something."
        ),
    )
    detail: str = Field(default="", description="What the walk last recorded about its stop.")


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
            "`len(slot_runs(home))` -- so this surface and a refused dispatch can never "
            "disagree. An interactive run (mode `interactive`) is in `runs` and not in "
            "this count: it holds its task, not a slot (task-354). Nor is a run whose "
            "task has closed while its session stayed open, which released its slot "
            "(task-482) -- `holds_slot` is false on both. Finishes and the runway are "
            "not in it either: they hold locks, not run slots."
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
    queued: List[QueuedDispatchView] = Field(
        default_factory=list,
        description=(
            "Dispatches waiting for a slot, in the order they will start (task-459). "
            "Empty on a machine where nobody has queued one. Filtered by what this "
            "caller may see, exactly as `runs` is."
        ),
    )
    armed: List[ArmedProjectView] = Field(
        default_factory=list,
        description=(
            "Projects the pull mode is armed for (task-462), oldest arming first. Empty "
            "on a machine where nobody has armed one. Filtered by what this caller may "
            "see, exactly as `runs` is."
        ),
    )
    walks: List[EpicWalkView] = Field(
        default_factory=list,
        description=(
            "Epic walks still open on this machine (task-523), oldest first. Empty on a "
            "machine where none is, which is the ordinary case. Filtered by what this "
            "caller may see, exactly as `runs` is -- and **not** counted in `occupied`: "
            "a walk holds no run slot, and drawing one as though it did would make this "
            "surface disagree with the concurrency guard."
        ),
    )
    queue_limit: int = Field(
        default=0,
        description=(
            "`limits.dispatch_queue_limit` from ~/.agentjobs/dispatch.yaml: how many "
            "dispatches may wait at once."
        ),
    )
    paused: List[StartPauseView] = Field(
        default_factory=list,
        description=(
            "Why the machine is starting nothing on a credential (task-463). Empty on "
            "the ordinary machine, which is the point: a board that says nothing here "
            "is a board with nothing to explain. **Unfiltered by project**, on the same "
            "argument as `occupied`: the rows answer 'what may I read' and this answers "
            "'why is nothing starting', and a machine out of quota is out of quota "
            "whether or not the run that spent it is one this caller may see."
        ),
    )
    generated_at: str = Field(..., description="When this answer was assembled, in UTC.")


def _home() -> Path:
    """The AgentJobs home whose dispatch config and runs this server acts on."""
    return default_home()


def _ceiling() -> tuple[int, bool]:
    """``(max_concurrent_runs, configured)`` for this server's machine.

    The reading itself lives in :func:`machine_ceiling` rather than here, because the
    dashboard endpoint now needs the same number -- it sizes its queue preview from it
    so that a board of N cells has N *different* tasks to offer (task-092) -- and two
    copies of the fallback are two chances for the two surfaces to disagree about how
    big the board is.
    """
    return machine_ceiling(_home())


def _projects_by_id(principal: Optional[Principal]) -> Dict[str, Project]:
    """Every project this caller may see, keyed by id.

    Filtered rather than complete (task-333), and this map is then what decides which
    rows exist at all: a run belonging to a project outside it is dropped. That is the
    stronger reading of "keyed by id" and the one this surface needs -- a row here
    carries a task title, a project name and a link, so a run of a hidden project would
    disclose three things about it even without its output.
    """
    return {project.id: project for project in visible_projects(principal)}


def _load_task(project: Optional[Project], task_id: str) -> Any:
    """One task, or ``None`` when it cannot be read.

    Deliberately swallows every failure. This is a status page about *runs*: a run whose
    project has been unregistered, or whose task file has been renamed out from under
    it, is precisely the situation worth showing, and a traceback would hide it.
    """
    if project is None or not task_id:
        return None
    try:
        return storage_for(project).load_task(task_id)
    except Exception:  # pragma: no cover - a status page never fails over a title
        return None


def _task_title(project: Optional[Project], task_id: str) -> str:
    """One task's title, or ``""`` when it cannot be read."""
    return getattr(_load_task(project, task_id), "title", "") or ""


def _project_owning(task_id: str, projects: Dict[str, Project]) -> str:
    """The id of the project whose storage holds this task, or ``""``.

    Task ids are only unique within a project, so the first hit wins and two projects
    that both use ``task-001`` can be attributed to the wrong one. That is accepted
    here: this is a status row about a merge in progress, the alternative is a row that
    names no project at all, and the id is displayed beside the name either way.
    """
    for project in projects.values():
        try:
            if storage_for(project).load_task(task_id) is not None:
                return project.id
        except Exception:  # pragma: no cover - a status page never fails over a lookup
            continue
    return ""


def _may_see(project_id: str, projects: Dict[str, Project], principal: Optional[Principal]) -> bool:
    """Whether this caller may be shown a row belonging to ``project_id``.

    A row naming a project is shown when that project is in the caller's visible set.
    A row naming **no** project -- a run whose ledger entry predates the field, a runway
    lock on a checkout that is not registered here -- cannot be attributed, so there is
    nothing to check its exposure against. Those are treated as if they were local-only,
    which shows them to the machine's own callers and withholds them from a remote one:
    the one answer that cannot accidentally publish a hidden project's work.
    """
    if project_id:
        return project_id in projects
    return readable_by(Visibility.LOCAL, principal)


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


def _run_view(
    record: RunRecord,
    projects: Dict[str, Project],
    finish: Optional[FinishStatus] = None,
    runway_behind: str = "",
) -> LiveRunView:
    """Render one live run for the browser.

    ``finish`` is the live finish for this run's task, already filtered to an open task
    by the caller -- see ``run_health``'s ``finishing`` for why a closed one is not.
    """
    project = projects.get(record.project_id)
    health = run_health(record, finishing=finish is not None)
    finishing = health == HEALTH_FINISHING and finish is not None
    task = _load_task(project, record.task_id)
    # A run whose task has closed is drawn in its task's own word (task-577): "Completed"
    # beside a Completed task, not a sixth word the status vocabulary does not have.
    closed = None
    outcome = getattr(task, "outcome", None)
    if health == HEALTH_WORK_DONE and outcome is not None:
        closed = closed_status(Outcome(outcome))
    return LiveRunView(
        task_display_status=closed.label if closed else "",
        task_status_category=closed.category if closed else None,
        finish_id=finish.finish_id if finishing and finish else "",
        finish_step=finish.current_step if finishing and finish else "",
        runway_behind=runway_behind if finishing else "",
        run_id=record.run_id,
        task_id=record.task_id,
        task_title=getattr(task, "title", "") or "",
        project_id=record.project_id,
        project_name=project.name if project else record.project_id,
        mode=record.mode,
        session=record.is_session,
        posture=record.posture,
        status=record.status,
        health=health,
        started_at=record.started_at.isoformat() if record.started_at else None,
        elapsed_seconds=record.elapsed_seconds(),
        task_url=_task_url(record.project_id, record.task_id),
        output_url=(
            f"/api/projects/{record.project_id}/dispatch/runs/{record.run_id}/output"
            if record.project_id
            else ""
        ),
        over_ceiling=record.over_ceiling,
        holds_slot=record.takes_slot,
    )


def queued_dispatch_view(
    entry: QueuedDispatch,
    position: int,
    projects: Dict[str, Project],
    *,
    paused_by: str = "",
) -> QueuedDispatchView:
    """Render one waiting dispatch for the browser.

    Public, and the one renderer: the project-scoped cancel route returns the entry it
    removed, and two renderings of one row would eventually disagree about what a
    queued dispatch is called.

    ``paused_by`` defaults to empty so the cancel route -- which is answering "what did I
    just remove" and not "why is the machine quiet" -- does not have to read the incident
    book to render a row it is about to throw away.
    """
    project = projects.get(entry.project_id)
    return QueuedDispatchView(
        paused_by=paused_by,
        queue_id=entry.queue_id,
        position=position,
        task_id=entry.task_id,
        task_title=_task_title(project, entry.task_id),
        project_id=entry.project_id,
        project_name=project.name if project else entry.project_id,
        queued_at=entry.queued_at,
        waiting_seconds=_elapsed_since(entry.queued_at),
        queued_by=entry.queued_by,
        source=entry.source,
        status=entry.status,
        detail=entry.detail,
        task_url=_task_url(entry.project_id, entry.task_id),
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
            detail="every other finish in this repository is queued behind it",
        )

    lock_project, task_id = split_lock_name(lock_name)
    try:
        # No project id: the finish's own meta records which project it belongs to, and
        # this lock does not say. Asking each registered project in turn would be the
        # same scan repeated once per project for one answer.
        status = read_finish_status(
            _home(),
            task_id,
            lock_project,
            task_open=_holder_task_is_open(projects, lock_project, task_id),
        )
    except Exception:  # pragma: no cover - a status page never fails over a detail
        status = None
    project_id = (status.project_id if status else "") or lock_project
    if not project_id:
        # No finish record to read the project off. That is an ordinary state rather
        # than a fault -- ``newest_finish_directory`` scans a bounded number of
        # directories, so an older finish falls out of reach while its lock is still
        # held -- and the task id is enough to find the owner without it.
        project_id = _project_owning(task_id, projects)
    project = projects.get(project_id)
    return MachineHolderView(
        kind=KIND_FINISH,
        lock_name=lock_name,
        task_id=task_id,
        task_title=_task_title(project, task_id),
        project_id=project_id,
        project_name=project.name if project else project_id,
        finish_id=holder.finish_id or (status.finish_id if status else ""),
        pid=holder.pid,
        started_at=holder.started_at or (status.started_at if status else ""),
        elapsed_seconds=_elapsed_since(holder.started_at)
        or (status.elapsed_seconds if status else None),
        detail=(
            status.current_step
            if status and status.current_step
            else ("overtaken" if status and status.state == OVERTAKEN else "merging")
        ),
        task_url=_task_url(project_id, task_id),
        overtaken=bool(status and status.state == OVERTAKEN),
    )


def _holder_task_is_open(
    projects: Dict[str, Project], project_id: str, task_id: str
) -> Optional[bool]:
    """Whether the task this lock names is still open, or ``None`` when nothing can say.

    The board renders from the *lock*, not from the finish's own record, which is why it
    kept saying **Finishing** about a task that had been closed for twenty minutes
    (task-514). ``None`` where the project is not registered here or the store will not
    answer: only an explicit ``False`` is treated as evidence.
    """
    project = projects.get(project_id) if project_id else None
    if project is None or not task_id:
        return None
    try:
        task = storage_for(project).load_task(task_id)
    except Exception:  # pragma: no cover - a status page never fails over a detail
        return None
    return None if task is None else bool(getattr(task, "is_open", True))


FinishKey = Tuple[str, str]


def _live_finishes(home: Path, project_ids: List[str]) -> Dict[FinishKey, FinishStatus]:
    """Every live finish on the machine, or none when it cannot be read.

    A status page never fails over a detail: an unreadable finish directory costs the
    word "Finishing", not the whole board.
    """
    try:
        return machine_live_finishes(home, project_ids)
    except Exception:  # pragma: no cover - a status page never fails over a detail
        return {}


def _name_runway_holders(
    holders: List[MachineHolderView],
    finishing: Dict[FinishKey, FinishStatus],
    projects: Dict[str, Project],
) -> None:
    """Give each runway holder the task whose finish holds it (task-533).

    A runway lock is keyed on the repository, so it names no task, and the board said
    "an unregistered checkout" -- or nothing -- about the merge everybody else was
    queued behind. Its finish id does name an attempt, and a live finish with that id
    names the task.
    """
    by_finish = {status.finish_id: key for key, status in finishing.items() if status.finish_id}
    for view in holders:
        if view.kind != KIND_RUNWAY or not view.finish_id or view.task_id:
            continue
        key = by_finish.get(view.finish_id)
        if key is None:
            continue
        project_id, task_id = key
        project = projects.get(project_id)
        view.task_id = task_id
        view.task_title = _task_title(project, task_id)
        view.task_url = _task_url(project_id, task_id)
        if not view.project_id:
            view.project_id = project_id
            view.project_name = project.name if project else project_id


def _open_task_finishes(
    finishing: Dict[FinishKey, FinishStatus], projects: Dict[str, Project]
) -> Dict[FinishKey, FinishStatus]:
    """The live finishes whose task is still open.

    ``derived_display_status`` keeps a closed task's outcome while its finish spends a
    second or two cleaning up, so a surface that said "Finishing" there would disagree
    with the task page. One store read per live finish -- none on an idle machine.
    """
    return {
        key: status
        for key, status in finishing.items()
        if _holder_task_is_open(projects, key[0], key[1]) is not False
    }


def _runway_queue(
    holders: List[MachineHolderView], finishing: Dict[FinishKey, FinishStatus]
) -> Dict[FinishKey, str]:
    """For each finish waiting for its runway, the task whose finish is on it."""
    on_runway = {
        view.project_id: view.task_id
        for view in holders
        if view.kind == KIND_RUNWAY and view.task_id and view.project_id
    }
    behind: Dict[FinishKey, str] = {}
    for (project_id, task_id), status in finishing.items():
        holder = on_runway.get(project_id, "")
        if status.current_step == "runway" and holder and holder != task_id:
            behind[(project_id, task_id)] = holder
    return behind


def _unheld_finish_views(
    finishing: Dict[FinishKey, FinishStatus],
    records: List[RunRecord],
    holders: List[MachineHolderView],
    projects: Dict[str, Project],
    behind: Dict[FinishKey, str],
) -> List[MachineHolderView]:
    """A card for each live finish that nothing else on the board draws (task-533).

    A finish is drawn by its own ``kind=finish`` lock, or -- under ``--posture-release``,
    which keeps the run's lock and adopts no finish one -- by that run's tile reading
    ``finishing``. What neither covers is a finish holding no lock of its own yet: the
    second between a spawn and its process taking one, or a finish run by hand from a
    shell with no run record. Those are drawn from the finish record, so every task the
    task read calls Finishing has a cell on the board.
    """
    covered = {(record.project_id, record.task_id) for record in records}
    covered.update((view.project_id, view.task_id) for view in holders if view.kind == KIND_FINISH)
    views: List[MachineHolderView] = []
    for key, status in sorted(finishing.items()):
        if key in covered:
            continue
        project_id, task_id = key
        project = projects.get(project_id)
        views.append(
            MachineHolderView(
                kind=KIND_FINISH,
                lock_name=f"{project_id}:{task_id}",
                task_id=task_id,
                task_title=_task_title(project, task_id),
                project_id=project_id,
                project_name=project.name if project else project_id,
                finish_id=status.finish_id,
                started_at=status.started_at,
                elapsed_seconds=_elapsed_since(status.started_at) or status.elapsed_seconds,
                detail=status.current_step or "merging",
                task_url=_task_url(project_id, task_id),
                runway_behind=behind.get(key, ""),
            )
        )
    return views


def _armed_view(
    arming: Any, projects: Dict[str, Project], *, paused_by: str = ""
) -> ArmedProjectView:
    """Render one armed project, with what it would start next. Never raises.

    The next-task read goes through the project's manager and can fail in every way a
    backlog can -- an unregistered project, a queue needing repair. All of them come back
    as an empty `next_task_id`, because this is a status page and the fact worth showing
    is *that the project is armed*; a board that 500'd over the preview would hide the
    arming as well as the problem.
    """
    project = projects.get(arming.project_id)
    task = None
    if project is not None:
        try:
            task = dispatch_pull.next_task(manager_for(project))
        except Exception:  # noqa: BLE001 - see the docstring
            task = None
    return ArmedProjectView(
        paused_by=paused_by,
        project_id=arming.project_id,
        project_name=project.name if project else arming.project_id,
        arming_id=arming.arming_id,
        armed_by=arming.armed_by,
        armed_at=arming.armed_at,
        bound=dispatch_pull.bound_sentence(arming),
        starts_left=arming.starts_left,
        posture=arming.posture,
        next_task_id=task.id if task else "",
        next_task_title=task.title if task else "",
        next_task_url=_task_url(arming.project_id, task.id) if task else "",
    )


GROUNDING_WORDS: Dict[str, str] = {
    "child_needs_a_human": "a child needs a person",
    "child_closed_unresolved": "a child closed unresolved",
    "child_exhausted_attempts": "a child used both of its attempts",
    "child_timed_out": "a child never reached a terminal state",
    "could_not_start_child": "a child could not be started",
    "already_supervised": "another supervisor holds this epic",
    "no_eligible_child": "no child is claimable",
    "all_children_done": "every child is done",
}
"""How a ``WalkStop`` reads in a sentence.

Server-side, on the same argument as ``StartPauseView.kind_word``: the board, the CLI
and any future notification should say the same words about the same state, and a map
in TypeScript is a second vocabulary that drifts on the first new stop reason.
"""


def _walk_children(project: Optional[Project], parent_task_id: str) -> Optional[List[Any]]:
    """Every child of one epic, or ``None`` when the graph cannot be read.

    Never raises. This is a status page: a walk whose project has been unregistered, or
    whose backlog needs a queue repair, is precisely the thing worth showing, and a row
    with no counts on it is better than a board that 500s over one.
    """
    if project is None or not parent_task_id:
        return None
    try:
        return list(manager_for(project).get_subtasks(parent_task_id))
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _walk_view(walk: Supervision, projects: Dict[str, Project]) -> EpicWalkView:
    """Render one open walk for the browser. Never raises, for ``_walk_children``'s reason."""
    project = projects.get(walk.project_id)
    children = _walk_children(project, walk.parent_task_id)
    open_ids = {child.id for child in children if child.is_open} if children else set()

    # In flight is the walk's own rows intersected with the children still open. The
    # rows alone would keep saying "flying" for the seconds between a child closing and
    # the next tick landing it, which is the one moment a reader is most likely to be
    # watching.
    try:
        rows = journal(_home()).supervised_children(walk.walk_id)
    except ExecutionStoreError:  # pragma: no cover - a status page never fails over a count
        rows = []
    in_flight = [
        row.child_task_id
        for row in rows
        if row.status in ("admitting", "flying") and row.child_task_id in open_ids
    ]

    completed = (
        sum(
            1
            for child in children
            if not child.is_open and getattr(child.outcome, "value", child.outcome) == "completed"
        )
        if children
        else 0
    )

    grounding = walk.grounding or {}
    reason = str(grounding.get("stop") or "")
    waiting = _waiting_child(project, walk, reason, str(grounding.get("child") or ""))

    return EpicWalkView(
        walk_id=walk.walk_id,
        project_id=walk.project_id,
        project_name=project.name if project else walk.project_id,
        parent_task_id=walk.parent_task_id,
        parent_task_title=_task_title(project, walk.parent_task_id),
        parent_task_url=_task_url(walk.project_id, walk.parent_task_id),
        started_at=walk.created_at,
        children_total=len(children) if children else 0,
        children_completed=completed,
        children_in_flight=len(in_flight),
        children_remaining=max(0, len(open_ids) - len(in_flight)),
        in_flight_task_ids=in_flight,
        grounded=bool(walk.grounding),
        grounded_reason=reason,
        grounded_word=GROUNDING_WORDS.get(reason, reason.replace("_", " ")),
        waiting_on_task_id=waiting.id if waiting else "",
        waiting_on_task_title=waiting.title if waiting else "",
        waiting_on_task_url=_task_url(walk.project_id, waiting.id) if waiting else "",
        resumes_by_itself=waiting is not None,
        detail=walk.detail or "",
    )


def _waiting_child(
    project: Optional[Project], walk: Supervision, reason: str, stopped_on: str
) -> Optional[Any]:
    """The open child a grounded walk is merely waiting on, or ``None`` on a real stop.

    Asked of :func:`epic.waiting_child` rather than answered from the stop reason, for
    the reason that function exists: ``no_eligible_child`` covers both "somebody else is
    working the last child" and "the graph is deadlocked", and only the first of those
    clears on its own. A board that guessed from the enum would tell a reader a stuck
    epic was about to resume.
    """
    if project is None or not walk.grounding or not reason:
        return None
    try:
        stop = epic.WalkStop(reason)
    except ValueError:  # pragma: no cover - a stop reason this build does not know
        return None
    try:
        return epic.waiting_child(manager_for(project), walk.parent_task_id, stop, stopped_on)
    except Exception:  # noqa: BLE001 - a status page never fails over a detail
        return None


def _walk_views(projects: Dict[str, Project], principal: Optional[Principal]) -> List[EpicWalkView]:
    """Every open walk this caller may see, oldest first."""
    try:
        walks = journal(_home()).open_walks()
    except ExecutionStoreError:  # pragma: no cover - a status page never fails over a section
        return []
    return [
        _walk_view(walk, projects)
        for walk in walks
        if _may_see(walk.project_id, projects, principal)
    ]


@router.get("/live", response_model=LiveRunsView)
async def list_live_runs(
    principal: Optional[Principal] = Depends(get_principal),
) -> LiveRunsView:
    """Every run this caller may see, with what is left of the machine's capacity.

    One request answers both surfaces task-328 ships -- the Runs tab and the Dashboard's
    capacity row -- which is why the ceiling and the occupied count are in the body
    rather than left to a second call.

    **``occupied`` counts every run, including the ones not listed** (task-333). The two
    numbers answer different questions and only one of them is about exposure: the rows
    are "what may I read", while the capacity is "why can I not dispatch", and a machine
    that is full because of a hidden project's run is still full. Subtracting the hidden
    rows from the count would make this surface disagree with ``dispatch/guards.py``
    about whether there is a slot, which is the one thing its docstring says it must
    never do.

    **What the live-finish lookup adds (task-533), measured 2026-09-24** with
    ``machine_live_finishes`` against a home of 311 finish directories, median of 15:
    0.09 ms with no lock held; 33 ms with three runs and no finish (the one shared scan
    confirming none of them is merging); 65 ms with one of them finishing; 67 ms with six
    runs and one finishing. The live home the same day -- three run locks, 311
    directories, nothing finishing -- measured 68 ms. The cost follows live locks and
    live finishes, never tasks or rows, and each live finish adds one store read for
    whether its task is still open.
    """
    home = _home()
    projects = _projects_by_id(principal)
    every = live_runs(home)
    records = [record for record in every if _may_see(record.project_id, projects, principal)]
    # `occupied` is slots; `runs` is everything running. An interactive session is in
    # the second and not the first, and the two answer different questions (task-354).
    occupied = [record for record in every if record.takes_slot]
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

    # The live finishes, from the lookup the task read's `live_finish` uses (task-533).
    # One answer for the whole response, so a run tile, a finish card and the task's own
    # chip are the same fact rather than three readings of it.
    # The runs' projects and the finish holders' too: a pre-task-264 lock name carries no
    # project, so the holder's own record is the only place that says which to ask.
    finishing = _live_finishes(
        home,
        [record.project_id for record in records] + [view.project_id for view in holders],
    )
    _name_runway_holders(holders, finishing, projects)
    open_finishes = _open_task_finishes(finishing, projects)
    behind = _runway_queue(holders, open_finishes)
    for view in holders:
        if view.kind == KIND_FINISH:
            view.runway_behind = behind.get((view.project_id, view.task_id), "")
    holders.extend(_unheld_finish_views(open_finishes, records, holders, projects, behind))
    holders = [holder for holder in holders if _may_see(holder.project_id, projects, principal)]

    # FIFO, and the position is assigned over the machine's whole queue before this
    # caller's filter runs: a rail numbered 1, 2, 3 over rows that are really 1, 2 and 5
    # would be a lie about when the third one starts.
    # One reading of the incident book for the whole answer (task-463). Each row is
    # judged against it rather than re-reading it per row, so a board of twenty waiting
    # entries costs the same as a board of one.
    incidents = start_pause.open_pauses(home)
    every_entry = dispatch_queue.waiting(home)
    entry_pauses = {
        entry.queue_id: start_pause.pause_for(
            home,
            entry.project_id,
            runner=entry.request.get("runner"),
            group=entry.request.get("group"),
            incidents=incidents,
        )
        for entry in every_entry
    }
    waiting = [
        queued_dispatch_view(
            entry,
            index,
            projects,
            paused_by=(pause.incident_id if (pause := entry_pauses[entry.queue_id]) else ""),
        )
        for index, entry in enumerate(every_entry, start=1)
        if _may_see(entry.project_id, projects, principal)
    ]

    every_arming = list(dispatch_pull.armings(home))
    arming_pauses = {
        arming.arming_id: start_pause.pause_for(home, arming.project_id, incidents=incidents)
        for arming in every_arming
    }

    return LiveRunsView(
        occupied=len(occupied),
        max_concurrent_runs=ceiling,
        dispatch_configured=configured,
        runs=[
            _run_view(
                record,
                projects,
                open_finishes.get((record.project_id, record.task_id)),
                behind.get((record.project_id, record.task_id), ""),
            )
            for record in records
        ],
        holders=holders,
        queued=waiting,
        armed=[
            _armed_view(
                arming,
                projects,
                paused_by=(pause.incident_id if (pause := arming_pauses[arming.arming_id]) else ""),
            )
            for arming in every_arming
            if _may_see(arming.project_id, projects, principal)
        ],
        walks=_walk_views(projects, principal),
        queue_limit=dispatch_queue.queue_limit(home),
        paused=_pause_views(entry_pauses.values(), arming_pauses, every_arming),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _pause_views(
    entries: Any,
    arming_pauses: Dict[str, Any],
    armings: List[Any],
) -> List[StartPauseView]:
    """Every open pause, with what is waiting on it counted over the whole machine.

    **The counts are unfiltered while the rows above are filtered**, and the two are
    different questions on purpose -- the same split ``occupied`` makes. "Three entries
    are waiting on this reset" is a fact about the machine; showing a caller two because
    the third is in a project they cannot see would be a number that disagrees with the
    reason beside it.
    """
    found: Dict[str, StartPauseView] = {}
    project_of = {arming.arming_id: arming.project_id for arming in armings}

    def seen(pause: Any) -> StartPauseView:
        view = found.get(pause.incident_id)
        if view is None:
            view = StartPauseView(
                incident_id=pause.incident_id,
                kind=pause.kind,
                kind_word=pause.kind_word,
                runner=pause.runner,
                resets_at=pause.resets_at.isoformat() if pause.resets_at else None,
                opened_at=pause.opened_at.isoformat(),
                resumes_by_itself=pause.resumes_by_itself,
                detail=pause.sentence(),
            )
            found[pause.incident_id] = view
        return view

    for pause in entries:
        if pause is not None:
            seen(pause).queued += 1
    for arming_id, pause in arming_pauses.items():
        if pause is not None:
            view = seen(pause)
            project_id = project_of.get(arming_id, "")
            if project_id and project_id not in view.projects:
                view.projects.append(project_id)
    return list(found.values())
