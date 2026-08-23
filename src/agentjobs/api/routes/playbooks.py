"""A project's playbooks: two read routes, and the one route that runs one.

``docs/playbooks-design.md`` §7.1. Three of the four routes here are GETs; the fourth
starts an agent, and everything about it is shaped by keeping that difference visible.

**Running is a POST, human-gated, and reuses the dispatch endpoint's contract
verbatim** -- ``user``, optional ``task``, optional ``group`` -- because it *is* the
dispatch endpoint with a brief attached. It opens no gate: the master switch, the
sentinel, per-project enablement, the concurrency cap, the clean-tree rule and the
human-clocked rule all apply exactly as they do to ``POST /tasks/{id}/dispatch``, and
naming a playbook cannot change any of them (design §6.3).

**MCP still has no run tool, and that is not an oversight** (decision P10). An MCP
mutation is callable by an agent, and an agent starting a playbook run is an agent
causing a dispatch. This route is reachable by a browser with a signed-in human, which
is a different thing.

Mounted like every other project-facing router -- unscoped at ``/api`` and again under
``/api/projects/{project_id}`` -- so the two forms cannot drift apart.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agentjobs.dispatch.config import DispatchError
from agentjobs.dispatch.runner import DispatchRunError
from agentjobs.manager import TaskManager
from agentjobs.playbooks import (
    PlaybookContract,
    PlaybookError,
    UnknownPlaybookError,
    list_playbooks,
    read_playbook,
    validate_playbook_name,
)
from agentjobs.playbooks.model import PlaybookTarget
from agentjobs.playbooks.run import PlaybookDispatchRefused, PlaybookRunError, run_playbook
from agentjobs.projects import Project

from ..dependencies import get_task_manager, project_config
from ..models import DispatchStarted, ErrorBody
from .status import (
    MutationError,
    dispatch_refusal_error,
    get_acting_project,
    serving_api_base,
)

router = APIRouter(tags=["playbooks"])

PLAYBOOK_RUN_STATUS = {
    "invalid_playbook_name": status.HTTP_400_BAD_REQUEST,
    "unknown_playbook": status.HTTP_404_NOT_FOUND,
    "invalid_playbook": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_run_task_defaults": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "target_mismatch": status.HTTP_409_CONFLICT,
    "no_authorizing_human": status.HTTP_403_FORBIDDEN,
}
"""Status per refusal decided before the dispatch layer is reached.

Its own map rather than an addition to the dispatch one, because these are refusals
about a *file*, and folding them in would put playbook vocabulary on every dispatch
error the API can return.
"""

PLAYBOOK_RUN_ACTION = {
    "unknown_playbook": "Run 'agentjobs playbook list', or 'playbook init' to copy the references in.",
    "invalid_playbook": "Fix the frontmatter named above; the file is there and repairable.",
    "no_run_task_defaults": "Give the playbook a run_task: block with at least a title.",
    "target_mismatch": "Check the playbook's target: a project one creates its own run task, a task one needs the task named.",
    "no_authorizing_human": "Sign in as a human this project configures under 'actors:'.",
}


class PlaybookRead(PlaybookContract):
    """A playbook's contract, plus where it was read from and (optionally) its brief.

    Subclasses the contract rather than restating it, so a field added to the
    frontmatter model reaches the API without a second edit that can be forgotten.
    """

    filename: str = Field(..., description="The file this was read from, e.g. groom.md.")
    body: Optional[str] = Field(
        default=None,
        description=(
            "The brief: opaque markdown prose an agent reads. Present on the "
            "single-playbook route and omitted from the collection, which is a "
            "discovery listing rather than a way to fetch every brief at once."
        ),
    )


class PlaybookProblemRead(BaseModel):
    """One reason a file in the playbooks directory is not a valid playbook."""

    filename: str
    field: Optional[str] = None
    message: str


class PlaybookCollection(BaseModel):
    """What a project's playbooks directory holds.

    Invalid files are reported beside valid ones for the reason ``/tasks/broken``
    exists: a file that fails validation and then vanishes from the listing reads as a
    playbook nobody ever wrote.
    """

    directory: str
    exists: bool = Field(
        ...,
        description=(
            "False when the directory has not been created. Not an error: it is the "
            "state every project starts in, and `agentjobs playbook init` leaves it."
        ),
    )
    playbooks: List[PlaybookRead] = Field(default_factory=list)
    problems: List[PlaybookProblemRead] = Field(default_factory=list)


@router.get("/playbooks", response_model=PlaybookCollection, response_model_exclude_none=True)
async def get_playbooks(
    project: Project = Depends(get_acting_project),
) -> PlaybookCollection:
    """Every playbook this project holds, with the files that would not load."""
    listing = list_playbooks(project.playbooks_dir())
    return PlaybookCollection(
        directory=str(listing.directory),
        exists=listing.exists,
        playbooks=[
            PlaybookRead(**playbook.contract.model_dump(), filename=playbook.path.name)
            for playbook in listing.playbooks
        ],
        problems=[
            PlaybookProblemRead(
                filename=problem.filename, field=problem.field, message=problem.message
            )
            for problem in listing.problems
        ],
    )


@router.get("/playbooks/{name}", response_model=PlaybookRead)
async def get_playbook(
    name: str,
    project: Project = Depends(get_acting_project),
) -> PlaybookRead:
    """One playbook by name, including its brief.

    422 rather than 404 for a file that exists and does not validate, matching how a
    stored task that will not parse is reported: the thing is there and repairable,
    and "not found" would send its author looking for something already on disk.
    """
    try:
        validate_playbook_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        playbook = read_playbook(project.playbooks_dir(), name)
    except UnknownPlaybookError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PlaybookError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return PlaybookRead(
        **playbook.contract.model_dump(),
        filename=playbook.path.name,
        body=playbook.body,
    )


class PlaybookRunRequestBody(BaseModel):
    """Ask AgentJobs to run a playbook.

    The same three fields the dispatch endpoint takes, because this **is** that
    endpoint with a brief attached (design section 7.1). There is no field for the
    brief, no field naming a runner, and no field naming argv: a playbook declares
    difficulty and the machine binds it, which is decision P6 and is not negotiable
    from a request body.
    """

    user: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "The signed-in human asking for this run. Must be an actor this project "
            "configures with 'kind: human'. Required for a project-target playbook, "
            "which creates a run task attributed to them -- that creation entry is the "
            "authorisation. For a task-target playbook it is task-188's authorising "
            "entry, written onto the target task exactly as POST /dispatch writes it."
        ),
    )
    task: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "The task to run a task-target playbook against. Refused for a "
            "project-target playbook, which creates its own run task."
        ),
    )
    group: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Runner group to choose from, overriding the project's. Names a group this "
            "machine already defines; it cannot open a gate that is closed."
        ),
    )
    note: Optional[str] = Field(
        default=None,
        description=(
            "What the human typed, when a task-target run's record could not brief an "
            "agent on its own. Becomes the body of the authorising entry."
        ),
    )


class PlaybookRunStarted(DispatchStarted):
    """A started playbook run: the dispatch, plus which brief it was given."""

    playbook: str = Field(..., description="The playbook whose brief this run has.")
    playbook_path: str = Field(..., description="Where it was read from, project-relative.")
    playbook_hash: str = Field(
        ...,
        description=(
            "Algorithm-prefixed content hash of the file at instantiation, pinned on "
            "the task's dispatch entry so the record says which brief ran."
        ),
    )
    created_run_task: bool = Field(
        ...,
        description=(
            "True when this run created the task it dispatched, which is every "
            "project-target run. False for a task-target run, which dispatches the "
            "task you named."
        ),
    )


def _run_refusal(exc: PlaybookRunError) -> MutationError:
    """Render a refusal decided before the dispatch layer, in the API's error shape."""
    return MutationError(
        PLAYBOOK_RUN_STATUS.get(exc.reason, status.HTTP_409_CONFLICT),
        ErrorBody(
            code=exc.reason,
            message=str(exc),
            detail=str(exc),
            retryable=False,
            suggested_action=PLAYBOOK_RUN_ACTION.get(exc.reason),
        ),
    )


@router.post(
    "/playbooks/{name}/run",
    response_model=PlaybookRunStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_playbook_endpoint(
    name: str,
    request: Request,
    payload: PlaybookRunRequestBody = PlaybookRunRequestBody(),
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> PlaybookRunStarted:
    """Start an agent on a playbook: create its run task if it has one, then dispatch.

    202 rather than 200, and for the same reason ``POST /tasks/{id}/dispatch`` is: the
    run has started, and how it ends arrives later as ``dispatch_result`` entries on the
    task rather than in this response.

    **Every dispatch gate binds, unchanged.** This endpoint opens nothing: it resolves a
    file, checks the invocation's shape against the playbook's ``target``, and then
    calls the same ``dispatch_task`` the Dispatch button calls, with a pointer attached
    that no guard reads. A machine with dispatch off refuses here as it refuses there,
    and refuses *before* a run task is created.

    ``user`` reaches the record by one of two routes, and which one depends on the
    playbook rather than on this endpoint. A project-target run creates the run task
    attributed to that human, and their creation entry is the newest stored entry the
    human-clocked rule then reads. A task-target run creates nothing, so ``user`` is
    passed to the dispatcher as task-188's ``authorized_by`` and its authorising entry
    is written onto the task named. Neither path substitutes the project's
    ``default_user`` for a human nobody named.
    """
    try:
        playbook = read_playbook(project.playbooks_dir(), name)
        targets_task = playbook.contract.target is PlaybookTarget.TASK
    except (ValueError, UnknownPlaybookError, PlaybookError):
        # Left to run_playbook below, which raises the coded refusal this route renders.
        # Read here only to decide which authorisation route `user` takes, and a name
        # that will not load has no target to decide it with.
        targets_task = False

    try:
        result = await run_in_threadpool(
            run_playbook,
            manager=manager,
            project=project,
            project_config=project_config(project),
            name=name,
            task_id=payload.task,
            group=payload.group,
            # One field, two destinations, decided by the playbook's shape. See the
            # docstring; the run module refuses to use both at once.
            created_by=None if targets_task else payload.user,
            authorized_by=payload.user if targets_task else None,
            note=payload.note,
            surface="the playbooks page" if payload.user else None,
            api_base=serving_api_base(request),
        )
    except PlaybookRunError as exc:
        raise _run_refusal(exc) from exc
    except PlaybookDispatchRefused as exc:
        # The gate's own rendering, with the run task this refusal left behind named in
        # the message the run module composed.
        refusal = dispatch_refusal_error(exc.cause, exc.run_task_id)
        refusal.body.message = str(exc)
        refusal.body.detail = str(exc)
        raise refusal from exc
    except DispatchRunError as exc:
        raise MutationError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorBody(
                code="dispatch_failed",
                message=str(exc),
                detail=str(exc),
                retryable=False,
                suggested_action="Check the runner's argv in ~/.agentjobs/dispatch.yaml.",
            ),
        ) from exc
    except DispatchError as exc:
        raise dispatch_refusal_error(exc, payload.task or name) from exc

    handle = result.handle
    meta = handle.directory.read_meta()
    posture = meta.get("posture")
    caused_by = meta.get("caused_by")
    return PlaybookRunStarted(
        run_id=handle.run_id,
        session_id=handle.session_id,
        mode=handle.mode.value,
        posture=str(posture or ""),
        task_id=result.task_id,
        caused_by=caused_by
        if isinstance(caused_by, int) and not isinstance(caused_by, bool)
        else 0,
        runner=handle.runner,
        group=handle.group,
        playbook=result.pointer.name,
        playbook_path=result.pointer.path,
        playbook_hash=result.pointer.digest,
        created_run_task=result.created_run_task,
    )
