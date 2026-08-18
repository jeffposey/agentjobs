"""State-transition and log endpoints for AgentJobs tasks (schema v2).

Every arrow in the canonical loop -- promote, claim, handoff, release, close -- is one
endpoint here, each one manager call, each appending one log entry (design doc
section 5). There is no generic "set status" endpoint: the axes only move through
verbs that record why.

Each verb also accepts the two fields that make a retry safe: an ``operation_id`` so a
resent request replays instead of writing twice, and -- where the caller is acting on
content it has already read -- an ``expected_revision`` so a stale decision is refused.
Both are optional, and omitting them gives exactly the behaviour these endpoints
always had. That is the whole compatibility story: existing callers change nothing.

``?envelope=true`` switches the response from the bare task to a MutationResult
carrying ``replayed`` and ``warnings``. A caller retrying after a timeout cannot
otherwise tell "I did that" from "you already had".
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Union

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.dispatch.config import DispatchError
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.runner import DispatchRunError
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Task
from agentjobs.operations import OperationConflictError, RevisionConflictError
from agentjobs.projects import Project
from agentjobs.storage import TaskLockTimeout

from ..dependencies import get_task_manager, project_config, request_project, storage_for
from ..models import (
    ClaimRequest,
    CloseRequest,
    DispatchRequestBody,
    DispatchStarted,
    ErrorBody,
    ErrorDetail,
    HandoffRequest,
    LogAppendRequest,
    MutationResult,
    ProgressUpdateRequest,
    PromoteRequest,
    ReleaseRequest,
    TaskRead,
)

router = APIRouter(prefix="/tasks", tags=["status"])

#: A mutation answers with the task, or -- on request -- the envelope around it.
MutationResponse = Union[MutationResult, Task]

ENVELOPE_QUERY = Query(
    default=False,
    description=(
        "Return a MutationResult with replayed/warnings instead of the bare task. "
        "Defaults to false, so existing callers see no change."
    ),
)


class MutationError(Exception):
    """A refused mutation, carrying the structured body the caller gets back."""

    def __init__(self, status_code: int, body: ErrorBody) -> None:
        """Record the HTTP status and the structured explanation."""
        super().__init__(body.message)
        self.status_code = status_code
        self.body = body


def _as_read(task: Task) -> TaskRead:
    """Render a stored task in the read model the error and envelope bodies use."""
    return TaskRead.model_validate(task.model_dump(mode="python", exclude={"display_status"}))


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    task_id: Optional[str] = None,
    current_task: Optional[Task] = None,
    field_errors: Optional[List[ErrorDetail]] = None,
    suggested_action: Optional[str] = None,
) -> MutationError:
    """Build a refusal. ``detail`` duplicates ``message`` deliberately.

    Every existing client -- TaskClient included -- reads FastAPI's ``detail`` key.
    Carrying both makes the structured body additive rather than a breaking change to
    every error response this API returns.
    """
    return MutationError(
        status_code,
        ErrorBody(
            code=code,
            message=message,
            detail=message,
            retryable=retryable,
            task_id=task_id,
            current_task=_as_read(current_task) if current_task is not None else None,
            field_errors=field_errors or [],
            suggested_action=suggested_action,
        ),
    )


def _classify(exc: ValueError, task_id: str) -> MutationError:
    """Map a manager failure onto the stable code set.

    Most specific first. The catch-all is ``invalid_transition`` rather than
    ``internal_error`` because every remaining ValueError the manager raises comes
    from a refused precondition -- not available to claim, already closed, unmet
    dependencies, an umbrella with open children. Reporting those as internal errors
    would tell an agent to retry something that can never succeed.
    """
    if isinstance(exc, TaskNotFoundError):
        return _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            str(exc),
            task_id=task_id,
            suggested_action="List the project's tasks to see the ids it holds.",
        )
    if isinstance(exc, OperationConflictError):
        return _error(
            status.HTTP_409_CONFLICT,
            "operation_conflict",
            str(exc),
            task_id=task_id,
            suggested_action="Use a fresh operation_id, or resend the original request.",
        )
    if isinstance(exc, RevisionConflictError):
        return _error(
            status.HTTP_409_CONFLICT,
            "revision_conflict",
            str(exc),
            task_id=task_id,
            current_task=exc.current_task,
            suggested_action="Re-read the task, decide again, and resend.",
        )
    message = str(exc)
    if "unmet dependencies" in message or "umbrella task" in message:
        return _error(
            status.HTTP_409_CONFLICT,
            "dependency_blocked",
            message,
            task_id=task_id,
            suggested_action="Finish the blocking work first, or take a different task.",
        )
    return _error(status.HTTP_409_CONFLICT, "invalid_transition", message, task_id=task_id)


def acting_actor(project: Project, actor: str) -> str:
    """Return the actor id to record, refused when this project does not define it.

    The three human review routes have always validated their actor. These six did
    not, so a typo -- or an MCP client inventing an identity from a model name --
    wrote an unresolvable attribution into an append-only log. The validator itself
    still accepts anything on a project that configures no actors, so a fresh
    ``agentjobs init`` is unaffected; it only bites once a project has said who its
    actors are.

    Unlike review actions, an agent verb need not match ``default_user``. Any
    configured actor may claim or log; only the human review endpoints care which
    person is at the keyboard.
    """
    try:
        return validate_actor(project_config(project), actor)
    except UnknownActorError as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "unknown_actor",
            str(exc),
            field_errors=[ErrorDetail(path="actor", message="Not a configured actor.")],
            suggested_action="Use one of the project's configured actor ids.",
        ) from exc


def get_acting_project(request: Request) -> Project:
    """Provide the addressed project so a route can validate its actor."""
    return request_project(request)


def _log_length(task_id: str, project: Project) -> int:
    """Log length before a mutation, or -1 when the task cannot be read."""
    try:
        existing = storage_for(project).load_task(task_id)
    except Exception:  # pragma: no cover - the verb itself reports a broken file
        return -1
    return len(existing.log) if existing else -1


def _run(
    verb: Callable[[], Task],
    *,
    task_id: str,
    project: Project,
    operation_id: Optional[str],
    envelope: bool,
) -> Any:
    """Execute one manager verb and shape its result, or its refusal.

    Replay is measured rather than reported: a replayed operation is one that wrote
    nothing, so comparing the log length either side is a more direct check than
    having every verb thread a flag back up. It also cannot be wrong about a verb that
    forgot to set the flag.
    """
    before = _log_length(task_id, project) if envelope and operation_id else -1
    try:
        task = verb()
    except MutationError:
        raise
    except TaskLockTimeout as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            "lock_timeout",
            str(exc),
            retryable=True,
            task_id=task_id,
            suggested_action="Another writer holds the task. Wait briefly and retry.",
        ) from exc
    except ValueError as exc:
        raise _classify(exc, task_id) from exc

    if not envelope:
        return task
    return MutationResult(
        project_id=project.id,
        operation_id=operation_id,
        replayed=operation_id is not None and len(task.log) == before,
        task=_as_read(task),
        warnings=[],
    )


@router.post("/{task_id}/promote", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def promote_task(
    task_id: str,
    payload: PromoteRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Declare a draft's spec finished: it becomes ready and claimable."""
    actor = acting_actor(project, payload.actor)
    return _run(
        lambda: manager.promote_task(
            task_id,
            actor=actor,
            body=payload.body,
            operation_id=payload.operation_id,
            expected_revision=payload.expected_revision,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/claim", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def claim_task(
    task_id: str,
    payload: ClaimRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Claim a ready task: one winner, everyone else gets a 409."""
    agent = acting_actor(project, payload.agent)
    return _run(
        lambda: manager.claim_task(task_id, agent=agent, operation_id=payload.operation_id),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/handoff", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def handoff_task(
    task_id: str,
    payload: HandoffRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Move the ball, with its ask."""
    actor = acting_actor(project, payload.actor)
    return _run(
        lambda: manager.handoff(
            task_id,
            actor=actor,
            ball=payload.ball,
            ball_reason=payload.ball_reason,
            ball_prompt=payload.ball_prompt,
            body=payload.body,
            operation_id=payload.operation_id,
            expected_revision=payload.expected_revision,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/release", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def release_task(
    task_id: str,
    payload: ReleaseRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Return a claimed task to the pool."""
    actor = acting_actor(project, payload.actor)
    return _run(
        lambda: manager.release_task(
            task_id, actor=actor, body=payload.body, operation_id=payload.operation_id
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/close", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def close_task(
    task_id: str,
    payload: CloseRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """End the task with an outcome."""
    actor = acting_actor(project, payload.actor)
    return _run(
        lambda: manager.close_task(
            task_id,
            actor=actor,
            outcome=payload.outcome,
            body=payload.body,
            archive=payload.archive,
            operation_id=payload.operation_id,
            expected_revision=payload.expected_revision,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/log", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def append_log_entry(
    task_id: str,
    payload: LogAppendRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Append a note/progress/decision/question/answer/instruction entry."""
    actor = acting_actor(project, payload.actor)
    return _run(
        lambda: manager.add_log_entry(
            task_id,
            actor=actor,
            type=payload.type,
            body=payload.body,
            re=payload.re,
            data=payload.data,
            operation_id=payload.operation_id,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/progress", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def post_progress_update(
    task_id: str,
    payload: ProgressUpdateRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Append a progress update entry for the task."""
    author = acting_actor(project, payload.author)
    return _run(
        lambda: manager.add_progress_update(
            task_id=task_id,
            author=author,
            summary=payload.summary,
            details=payload.details,
            operation_id=payload.operation_id,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


#: HTTP status per dispatch refusal. Everything meaning "the state of the world is
#: currently wrong" is a 409 that retrying could fix after a change; a rule that no
#: amount of retrying satisfies is a 403.
_DISPATCH_STATUS: dict = {
    "not_configured": status.HTTP_409_CONFLICT,
    "disabled": status.HTTP_409_CONFLICT,
    "sentinel": status.HTTP_409_CONFLICT,
    "project_not_enabled": status.HTTP_409_CONFLICT,
    "unknown_runner": status.HTTP_409_CONFLICT,
    "invalid_config": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "not_human_clocked": status.HTTP_403_FORBIDDEN,
    "no_causing_entry": status.HTTP_409_CONFLICT,
    "task_closed": status.HTTP_409_CONFLICT,
    "live_run_exists": status.HTTP_409_CONFLICT,
    "concurrency_limit": status.HTTP_409_CONFLICT,
    "dirty_tree": status.HTTP_409_CONFLICT,
    "claim_lost": status.HTTP_409_CONFLICT,
    "owner_mismatch": status.HTTP_409_CONFLICT,
}

_DISPATCH_ACTION: dict = {
    "not_configured": "Create ~/.agentjobs/dispatch.yaml and define a runner.",
    "disabled": "Set 'enabled: true' in ~/.agentjobs/dispatch.yaml.",
    "sentinel": "Delete ~/.agentjobs/DISPATCH_DISABLED to re-enable dispatch.",
    "project_not_enabled": "Run 'agentjobs dispatch enable <project>'.",
    "unknown_runner": "Point the project at a runner this machine defines.",
    "not_human_clocked": (
        "Act on the task yourself, then dispatch. This rule is not configurable."
    ),
    "no_causing_entry": "Write the note or handoff that authorises this run first.",
    "task_closed": "Reopen the task before dispatching at it.",
    "live_run_exists": "Wait for the run to finish, or cancel it.",
    "concurrency_limit": "Wait for a run to finish, or raise max_concurrent_runs.",
    "dirty_tree": "Commit or stash the working tree, then dispatch.",
    "claim_lost": "Someone else took it. Re-read the task before deciding again.",
    "owner_mismatch": "Release the task, or dispatch the runner that owns it.",
}


@router.post(
    "/{task_id}/dispatch",
    response_model=DispatchStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def dispatch_task_endpoint(
    task_id: str,
    payload: DispatchRequestBody = DispatchRequestBody(),
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> DispatchStarted:
    """Start an agent on this task.

    Deliberately **not** merged into any approval endpoint. Approving means "I agree";
    dispatching means "spend money now", and collapsing the two would turn every
    approval into an implicit purchase (design decision D1).

    202 rather than 200: the run has started, and how it ends arrives later as
    ``dispatch_result`` entries on the task, not in this response.
    """
    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config(project),
            request=DispatchRequest(task_id=task_id, caused_by=payload.caused_by),
        )
    except DispatchRunError as exc:
        raise _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "dispatch_failed",
            str(exc),
            task_id=task_id,
            suggested_action="Check the runner's argv in ~/.agentjobs/dispatch.yaml.",
        ) from exc
    except DispatchError as exc:
        raise _dispatch_error(exc, task_id) from exc

    meta = handle.directory.read_meta()
    return DispatchStarted(
        run_id=handle.run_id,
        session_id=handle.session_id,
        mode=handle.mode.value,
        posture=str(meta.get("posture") or ""),
        task_id=task_id,
        caused_by=_as_int(meta.get("caused_by")),
    )


def _as_int(value: object) -> int:
    """Read an int out of run metadata, which is a YAML mapping of anything."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dispatch_error(exc: DispatchError, task_id: str) -> MutationError:
    """Render a dispatch refusal under its own code, never as a generic 400.

    Which gate refused is the only useful thing about one of these: "dispatch is off"
    and "that was an agent's handoff" need completely different responses from whoever
    asked, and a 400 saying "bad request" tells them neither.
    """
    reason = getattr(exc, "reason", "dispatch_refused")
    return _error(
        _DISPATCH_STATUS.get(reason, status.HTTP_409_CONFLICT),
        reason,
        str(exc),
        task_id=task_id,
        suggested_action=_DISPATCH_ACTION.get(reason),
    )


async def mutation_error_response(request: Any, exc: MutationError) -> JSONResponse:
    """Render a refused mutation as its structured body."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.body.model_dump(mode="json", exclude_none=True),
    )
