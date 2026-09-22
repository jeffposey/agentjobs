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
from starlette.concurrency import run_in_threadpool

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.capabilities import Capability
from agentjobs.dispatch.address import api_base_from_server
from agentjobs.dispatch.chains import (
    ChainAuthorization,
    ChainRefused,
    authorize_chain,
    chain_history,
    revoke_chain,
)
from agentjobs.dispatch.checks import NoChecksError, evaluate_task
from agentjobs.dispatch.config import DispatchError, Posture
from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch.guards import DispatchRequest, actor_kind, assert_authorizer_is_human
from agentjobs.dispatch.queue import dispatch_or_queue
from agentjobs.execution.store import QueuedDispatch
from agentjobs.dispatch.interactive import settle_for_task, start_interactive_run
from agentjobs.dispatch.runner import DispatchRunError
from agentjobs.manager import MoveOutcome, TaskManager, TaskNotFoundError
from agentjobs.models_v2 import CheckOutcome, LogEntryType, Task
from agentjobs.operations import OperationConflictError, RevisionConflictError
from agentjobs.projects import Project, default_home
from agentjobs.session_identity import SessionIdentity
from agentjobs.sqlstore import TaskLockTimeout

from ..authorization import assert_actor_agrees, assert_holds
from ..dependencies import get_task_manager, project_config, request_project, storage_for
from ..models import (
    ChainAuthorizeRequest,
    ChainIteration,
    ChainList,
    ChainRead,
    ChainRevokeRequest,
    CheckRunResult,
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
    QueueKeepRequest,
    QueueMoveRequest,
    RedactRequest,
    RelayAuthorizationRequest,
    ReleaseRequest,
    ReprioritizeRequest,
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


def classify_refusal(exc: ValueError, task_id: str) -> MutationError:
    """Map a manager failure onto the stable code set.

    Public because it is not the workflow verbs' private business: any route that lets
    a manager refusal reach the caller owes them the same code set. ``update_task``
    answered a stale ``expected_revision`` with a bare ``detail`` and no ``code`` until
    task-230, which meant the browser could tell a revision conflict apart from a lock
    timeout only by reading the sentence -- so it did not try, and every refused edit
    read "reload the page and try again".

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


def lock_timeout_error(
    exc: TaskLockTimeout, *, task_id: Optional[str] = None, held: str = "task"
) -> MutationError:
    """A contended lock is a 409 that says to try again, not a 500 that says stop.

    Shared with the queue routes, which take the same locks against the same files and
    would otherwise each spell out that a timeout is retryable. Getting that wrong in
    one place is a caller told to give up on a wait of a few hundred milliseconds.
    """
    return _error(
        status.HTTP_409_CONFLICT,
        "lock_timeout",
        str(exc),
        retryable=True,
        task_id=task_id,
        suggested_action=f"Another writer holds the {held}. Wait briefly and retry.",
    )


def acting_actor(request: Request, project: Project, actor: str) -> str:
    """Return the actor id to record, refused when it is not this project's or not yours.

    Two checks, and they answer different questions. ``validate_actor`` asks whether the
    project has heard of this id at all: a typo -- or an MCP client inventing an identity
    from a model name -- otherwise writes an unresolvable attribution into an
    append-only log. It still accepts anything on a project that configures no actors, so
    a fresh ``agentjobs init`` is unaffected.

    :func:`~agentjobs.api.authorization.assert_actor_agrees` asks whether this caller may
    write as that id (task-332). An agent verb is not a review, so it is *not* required to
    be the acting human: a run must write as the agent it was dispatched as, while a
    person at this machine may attribute a write to the tool they are driving. What is
    refused either way is claiming to be somebody else -- a run naming a human, or one
    caller naming another person.
    """
    try:
        validated = validate_actor(project_config(project), actor)
    except UnknownActorError as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "unknown_actor",
            str(exc),
            field_errors=[ErrorDetail(path="actor", message="Not a configured actor.")],
            suggested_action="Use one of the project's configured actor ids.",
        ) from exc
    assert_actor_agrees(request, project_config(project), actor)
    return validated


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
    verb: Callable[[], Union[Task, MoveOutcome]],
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

    A queue move answers with a :class:`~agentjobs.manager.MoveOutcome` rather than a
    bare task, because the check's findings are not a property of the task -- they are
    a property of the move, and a caller reading the task back a second later has no
    way to recover them. Every other verb still returns the task and lands on the
    ``else`` branch untouched.
    """
    before = _log_length(task_id, project) if envelope and operation_id else -1
    try:
        result = verb()
    except MutationError:
        raise
    except TaskLockTimeout as exc:
        raise lock_timeout_error(exc, task_id=task_id) from exc
    except ValueError as exc:
        raise classify_refusal(exc, task_id) from exc

    if isinstance(result, MoveOutcome):
        task, advisory = result.task, result.as_dict()
    else:
        task, advisory = result, {"queue_warnings": [], "queue_undo": None}

    if not envelope:
        return task
    return MutationResult(
        project_id=project.id,
        operation_id=operation_id,
        replayed=operation_id is not None and len(task.log) == before,
        task=_as_read(task),
        warnings=[],
        **advisory,
    )


@router.post("/{task_id}/promote", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def promote_task(
    task_id: str,
    request: Request,
    payload: PromoteRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Declare a draft's spec finished: it becomes ready and claimable."""
    actor = acting_actor(request, project, payload.actor)
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
    request: Request,
    payload: ClaimRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Claim a ready task: one winner, everyone else gets a 409.

    A claim that names its session also writes an interactive run record (task-354), so
    the work shows as running on every surface that reads the ledger. Only after the
    claim landed, only for an agent actor, and never a second one for a task that has a
    live run -- which is what makes a replayed claim harmless.
    """
    agent = acting_actor(request, project, payload.agent)
    result = _run(
        lambda: manager.claim_task(task_id, agent=agent, operation_id=payload.operation_id),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )
    if payload.session_id and actor_kind(project_config(project), agent) != "human":
        task = manager.get_task(task_id)
        if task is not None and task.assignment.owner == agent:
            start_interactive_run(
                home=default_home(),
                project=project,
                task=task,
                identity=SessionIdentity(
                    session_id=payload.session_id,
                    cwd=payload.session_cwd or str(project.root),
                ),
                actor=agent,
                caused_by=task.log[-1].id if task.log else None,
            )
    return result


def _settle_interactive(manager: TaskManager, task_id: str) -> None:
    """After a verb that may have moved the ball: end the task's interactive runs if so.

    The poller would catch it a tick later; doing it here is what makes the board drop
    the card the moment the session hands off, rather than ten seconds after.
    """
    settle_for_task(
        default_home(),
        manager.get_task(task_id),
        task_id,
        project_id=str(getattr(manager.storage, "project_id", "")),
    )


@router.post("/{task_id}/handoff", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def handoff_task(
    task_id: str,
    request: Request,
    payload: HandoffRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Move the ball, with its ask."""
    actor = acting_actor(request, project, payload.actor)
    result = _run(
        lambda: manager.handoff(
            task_id,
            actor=actor,
            ball=payload.ball,
            ball_reason=payload.ball_reason,
            ball_prompt=payload.ball_prompt,
            body=payload.body,
            questions=payload.questions,
            operation_id=payload.operation_id,
            expected_revision=payload.expected_revision,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )
    _settle_interactive(manager, task_id)
    return result


@router.post("/{task_id}/release", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def release_task(
    task_id: str,
    request: Request,
    payload: ReleaseRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Return a claimed task to the pool."""
    actor = acting_actor(request, project, payload.actor)
    result = _run(
        lambda: manager.release_task(
            task_id, actor=actor, body=payload.body, operation_id=payload.operation_id
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )
    _settle_interactive(manager, task_id)
    return result


@router.post("/{task_id}/close", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def close_task(
    task_id: str,
    request: Request,
    payload: CloseRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """End the task with an outcome."""
    actor = acting_actor(request, project, payload.actor)
    result = _run(
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
    _settle_interactive(manager, task_id)
    return result


@router.post(
    "/{task_id}/queue-move", response_model=MutationResponse, status_code=status.HTTP_200_OK
)
async def queue_move_task(
    task_id: str,
    request: Request,
    payload: QueueMoveRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Change where a task stands in its band. The only way the order changes.

    A verb like every other one here, which is why it lives beside them rather than
    with the read routes: attributed, retry-safe, refused against a stale read, and
    logged. The route computes no position -- it names a placement and the manager
    decides what number that is, because the arithmetic has to happen under the queue
    lock and a route holds no locks.

    The move lands and is never blocked by what the check finds. Ask for
    ``?envelope=true`` to be told what it found: ``queue_warnings`` carries the
    findings, ``queue_undo`` the placement that puts the task back where it came from.
    Without the envelope this answers with the bare task exactly as it always did.
    """
    actor = acting_actor(request, project, payload.actor)
    return _run(
        lambda: manager.move_with_warnings(
            task_id,
            before=payload.before,
            after=payload.after,
            top=payload.top,
            bottom=payload.bottom,
            with_children=payload.with_children,
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


@router.post(
    "/{task_id}/queue-keep", response_model=MutationResponse, status_code=status.HTTP_200_OK
)
async def queue_keep_task(
    task_id: str,
    request: Request,
    payload: QueueKeepRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Keep a place that was moved over a stated warning, and record why that is binding.

    The other button on the queue-move notice. `undo` needs no route of its own -- it
    is an ordinary move back through the route above -- but keeping does, because what
    it writes is not a move: it is the strong anchor, a position a person defended
    against an objection they had read, which `reorder` may not overturn.

    Refused when the last move produced no warnings. An anchor claims "informed, and
    kept anyway", and one written where nothing was ever said would bind `reorder` to a
    decision nobody took.
    """
    actor = acting_actor(request, project, payload.actor)
    return _run(
        lambda: manager.keep_queue_move(
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


@router.post(
    "/{task_id}/reprioritize", response_model=MutationResponse, status_code=status.HTTP_200_OK
)
async def reprioritize_task(
    task_id: str,
    request: Request,
    payload: ReprioritizeRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Change a task's band and its place in that band, in one decision.

    Not a `PATCH` of `priority`, deliberately. A band change moves a task between two
    orderings, so it has to land somewhere in the new one -- and the generic patch has
    no way to say where, no queue lock, and no `queue_move` entry to record the
    decision. Sending `priority` through `PATCH /tasks/{id}` still works and still
    rejoins the band at the bottom; this is the route that lets a caller say otherwise.
    """
    actor = acting_actor(request, project, payload.actor)
    return _run(
        lambda: manager.reprioritize(
            task_id,
            payload.priority,
            before=payload.before,
            after=payload.after,
            top=payload.top,
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


@router.post("/{task_id}/log", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def append_log_entry(
    task_id: str,
    request: Request,
    payload: LogAppendRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Append a note/progress/decision/question/answer/instruction entry."""
    actor = acting_actor(request, project, payload.actor)
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


@router.post(
    "/{task_id}/authorization", response_model=MutationResponse, status_code=status.HTTP_200_OK
)
async def relay_authorization(
    task_id: str,
    request: Request,
    payload: RelayAuthorizationRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Record that a human authorised a dispatch, as the agent they told (task-506).

    The third shape of an authorising entry, and the only one whose author and subject
    are different people. A person clicking Dispatch gets an entry under their own name.
    A person writing a note by hand gets the same. An agent told in a chat session to
    start the task it has just filed had neither, and the workaround was to write the
    note as *them* -- a signature in an append-only log that its owner did not put
    there, indistinguishable by any reader from a click. This writes an entry that says
    what actually happened: ``actor`` is the agent, ``authorized_by`` is the person,
    ``body`` is what the agent was asked for in its own words.

    **It is a separate route because it is a separate capability.**
    ``dispatch.relay_authorization`` is held by the two human kinds and by no ``run``,
    and the route table is keyed by endpoint -- so putting this on ``POST /log``, which
    every run may call, would have made the gate a condition inside a handler instead of
    a row in the table ``tests/test_authorization.py`` checks for completeness.

    **It starts nothing.** It is not an approval, it releases no merge gate, it arms
    nothing, and it spends no run slot. A dispatch afterwards is an ordinary ``manual``
    dispatch, judged by every gate in ``dispatch/guards.py`` and counted against every
    cap in ``dispatch/budget.py``. Nor is the entry consumed: it clocks exactly as many
    runs as a human's own note would, which is to say as many as those caps allow.

    **What it is not is proof.** The server cannot tell whether the person said anything,
    just as it cannot verify the ``user`` field on a dispatch (design section 2's P1-2).
    What it buys is a record that is true about who typed it, and a write a dispatched
    run is refused -- see ``docs/authorization.md``.
    """
    actor = acting_actor(request, project, payload.actor)
    # Refused before anything is written, so a bad or agent id never leaves a row in an
    # append-only log. The same guard the dispatch endpoint's `user` passes through, for
    # the same reason and with the same refusal code: an agent named as the authoriser is
    # `authorizer_not_human`, and relaying one does not change who authorised it.
    try:
        assert_authorizer_is_human(project_config(project), payload.authorized_by)
    except DispatchError as exc:
        raise dispatch_refusal_error(exc, task_id) from exc
    return _run(
        lambda: manager.record_relayed_authorization(
            task_id,
            actor=actor,
            authorized_by=payload.authorized_by,
            ask=payload.ask,
            surface=payload.surface,
            operation_id=payload.operation_id,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/redact", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def redact_task_region(
    task_id: str,
    request: Request,
    payload: RedactRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Replace one prose region with a stated redaction, recording that it happened."""
    actor = acting_actor(request, project, payload.actor)
    return _run(
        lambda: manager.redact(
            task_id,
            field=payload.field,
            replacement=payload.replacement,
            reason=payload.reason,
            actor=actor,
            operation_id=payload.operation_id,
            expected_revision=payload.expected_revision,
        ),
        task_id=task_id,
        project=project,
        operation_id=payload.operation_id,
        envelope=envelope,
    )


@router.post("/{task_id}/progress", response_model=MutationResponse, status_code=status.HTTP_200_OK)
async def post_progress_update(
    task_id: str,
    request: Request,
    payload: ProgressUpdateRequest,
    envelope: bool = ENVELOPE_QUERY,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Any:
    """Append a progress update entry for the task."""
    author = acting_actor(request, project, payload.author)
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
    "unknown_group": status.HTTP_409_CONFLICT,
    "no_eligible_runner": status.HTTP_409_CONFLICT,
    # A continuation's recorded runner, or its Stop (task-375). Both are fixed by a change
    # of state -- re-enabling the runner, or a person dispatching afresh.
    "recorded_runner_unavailable": status.HTTP_409_CONFLICT,
    "grant_stopped": status.HTTP_409_CONFLICT,
    "invalid_config": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "not_human_clocked": status.HTTP_403_FORBIDDEN,
    "authorizer_not_human": status.HTTP_403_FORBIDDEN,
    "conflicting_authorization": status.HTTP_400_BAD_REQUEST,
    "insufficient_record": status.HTTP_409_CONFLICT,
    "no_causing_entry": status.HTTP_409_CONFLICT,
    "task_closed": status.HTTP_409_CONFLICT,
    "task_on_hold": status.HTTP_409_CONFLICT,
    "live_run_exists": status.HTTP_409_CONFLICT,
    # An agent AgentJobs did not start is holding the task (task-179). 409 and not 403:
    # it is a state, and one manager verb away from clearing.
    "task_being_worked": status.HTTP_409_CONFLICT,
    "concurrency_limit": status.HTTP_409_CONFLICT,
    # The dispatch queue's own two (task-459). Both clear on their own or on one click,
    # so both are 409 rather than 403 for the same reason the caps above are.
    "dispatch_queue_full": status.HTTP_409_CONFLICT,
    "already_queued": status.HTTP_409_CONFLICT,
    # The budget caps (task-334). 409 rather than 403: none of them is a rule no amount
    # of retrying satisfies, which is what a 403 means here -- three of the four expire
    # on their own, and the fourth is raisable in a file on this machine.
    "per_task_per_day": status.HTTP_409_CONFLICT,
    "per_task_lifetime": status.HTTP_409_CONFLICT,
    "cooldown": status.HTTP_409_CONFLICT,
    "machine_per_hour": status.HTTP_409_CONFLICT,
    "dirty_tree": status.HTTP_409_CONFLICT,
    "posture_above_ceiling": status.HTTP_403_FORBIDDEN,
    "claim_lost": status.HTTP_409_CONFLICT,
    "owner_mismatch": status.HTTP_409_CONFLICT,
}

_DISPATCH_ACTION: dict = {
    "not_configured": "Create ~/.agentjobs/dispatch.yaml and define a runner.",
    "disabled": "Set 'enabled: true' in ~/.agentjobs/dispatch.yaml.",
    "sentinel": "Delete ~/.agentjobs/DISPATCH_DISABLED to re-enable dispatch.",
    "project_not_enabled": "Run 'agentjobs dispatch enable <project>'.",
    "unknown_runner": "Point the project at a runner this machine defines.",
    "unknown_group": "Name a runner group defined in ~/.agentjobs/dispatch.yaml.",
    "no_eligible_runner": (
        "Enable a member of the group by hand, or install the CLI one of them needs."
    ),
    "recorded_runner_unavailable": (
        "Re-enable or reinstall the runner this work was granted, or dispatch the task "
        "again to choose a different one."
    ),
    "grant_stopped": "Dispatch the task again to start a new run after the Stop.",
    "not_human_clocked": (
        "Act on the task yourself, then dispatch. This rule is not configurable."
    ),
    "authorizer_not_human": (
        "Dispatch as a human this project configures. This rule is not configurable."
    ),
    "conflicting_authorization": ("Send either 'caused_by' or 'user', not both."),
    "insufficient_record": (
        "Say what the agent should do; it is written onto the task as the authorising " "entry."
    ),
    "no_causing_entry": "Write the note or handoff that authorises this run first.",
    "task_closed": "Reopen the task before dispatching at it.",
    "task_on_hold": "Release the hold from the review panel, then dispatch.",
    "live_run_exists": "Wait for the run to finish, or cancel it.",
    "task_being_worked": (
        "Let the agent holding it hand off, or release the task if it is gone -- which "
        "records that somebody decided it was."
    ),
    "concurrency_limit": (
        "Send the dispatch again with if_full=queue to have it start when a slot frees, "
        "cancel one of the runs named above, or raise limits.max_concurrent_runs in "
        "~/.agentjobs/dispatch.yaml."
    ),
    "dispatch_queue_full": (
        "Cancel one of the waiting dispatches from the slot board, or raise "
        "limits.dispatch_queue_limit in ~/.agentjobs/dispatch.yaml."
    ),
    "already_queued": (
        "This task is already waiting for a slot. Cancel that entry from the slot board "
        "if you meant to change what it will run."
    ),
    "dirty_tree": "Commit or stash the working tree, then dispatch.",
    "posture_above_ceiling": (
        "Choose a posture at or below the project's ceiling, or raise "
        "projects.<id>.max_posture in ~/.agentjobs/dispatch.yaml by hand. Nothing "
        "reachable over the network writes that file, which is the point of it."
    ),
    "claim_lost": "Someone else took it. Re-read the task before deciding again.",
    "owner_mismatch": "Release the task, or dispatch the runner that owns it.",
    "per_task_per_day": (
        "Read why the earlier runs did not finish before starting another. The cap is "
        "limits.auto.per_task_per_day in ~/.agentjobs/dispatch.yaml."
    ),
    "per_task_lifetime": (
        "This task has been running and not finishing. Fix the task rather than the "
        "cap; it is limits.auto.per_task_lifetime in ~/.agentjobs/dispatch.yaml."
    ),
    "cooldown": "Wait out the cooldown and dispatch again.",
    "machine_per_hour": (
        "Wait for the hour to roll forward, or raise limits.dispatches_per_hour in "
        "~/.agentjobs/dispatch.yaml. Nothing reachable over the network writes that "
        "file, which is the point of it."
    ),
}


def serving_api_base(request: Request) -> Optional[str]:
    """The address this server is actually listening on, for a dispatch to hand over.

    ``scope["server"]`` is the listening socket's own name, which is why it is used in
    preference to the ``Host`` header: the dashboard is commonly published through a
    proxy, and the header then names an address that means nothing to the agent process
    starting on this machine. ``None`` when the ASGI server did not supply one, which
    hands the question back to ``dispatch/address.py`` rather than guessing.
    """
    server = request.scope.get("server")
    if not server:
        return None
    host, port = server
    return api_base_from_server(host, port)


@router.post(
    "/{task_id}/dispatch",
    response_model=DispatchStarted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def dispatch_task_endpoint(
    task_id: str,
    request: Request,
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

    The address handed to the agent is this server's own, taken from the socket the
    request arrived on. Until 2026-08-19 nothing was passed and the runner's default
    won, so a dashboard on any other port dispatched agents at ``:8765`` -- an address
    that, on the machine this was built for, is deliberately dead (task-154).

    **``user`` makes this one click (task-188).** Supplied, the guard layer writes that
    human's authorising entry onto the task and dispatches on it, so a person no longer
    has to know to write a note by hand before every run. It is validated as a
    configured human and it is not the dispatch's ``actor``; see
    :class:`DispatchRequestBody` for why those are different things.

    **Omitted, this endpoint behaves exactly as it did before.** It does *not* quietly
    substitute the project's ``default_user``: a run has to be signed for by whoever
    asked for it, and a server-side default would produce an entry that looks like a
    person's authorisation and is really just a config value. So a caller with no
    signed-in user falls back to the pre-existing rule -- the newest stored entry must
    be a human's -- and is refused with ``not_human_clocked`` if it is not. That is the
    same answer the CLI gets, and the React app disables the button and says so rather
    than letting someone press it into a refusal.
    """
    # The identity claim this endpoint accepts is checked against the principal before
    # the guard layer writes it onto the task (task-332). It has always been validated as
    # a configured human; what was missing is that a caller could name *any* configured
    # human, from a list `GET /api/projects` publishes -- which is the whole of audit
    # finding S-1's dispatch line.
    #
    # `require_human` is deliberately off. Whether the named id is a person at all is
    # already the guard layer's refusal, under its own code (`authorizer_not_human`) with
    # its own remedy; adding it here would replace that specific answer with a vaguer one
    # for no gain. What this adds is the half nothing checked: that the caller is the
    # person they named.
    if payload.user:
        assert_actor_agrees(request, project_config(project), payload.user, field="user")
    if payload.over_ceiling:
        # A second capability on the same route, because it answers a second question
        # (task-461): `dispatch.start` is whether this caller may spend money, and
        # `dispatch.over_ceiling` is whether they may spend it on a machine that has
        # already said it is full. Only a person may, and the UI hiding the button is
        # not the check -- this is.
        assert_holds(request, Capability.DISPATCH_OVER_CEILING)
    try:
        # ``dispatch_task`` starts Codex App Server synchronously. Keep it off
        # FastAPI's event loop: the child must handshake with this same AgentJobs
        # server over MCP while startup is in progress. Running it inline deadlocks
        # that handshake, which surfaced as "connection closed: initialize response".
        handle = await run_in_threadpool(
            dispatch_or_queue,
            manager=manager,
            project=project,
            project_config=project_config(project),
            request=DispatchRequest(
                task_id=task_id,
                caused_by=payload.caused_by,
                runner=payload.runner,
                group=payload.group,
                # Passed straight through, and deliberately not defaulted to the
                # project's `default_user` when the client omits it. A dispatch nobody
                # signed for must fall back to the entry the log already holds -- which
                # is what the CLI does -- rather than have this endpoint invent a
                # signature on the record. See the endpoint docstring.
                authorized_by=payload.user,
                authorization_note=payload.note,
                surface="the task page" if payload.user else None,
                # Converted rather than passed through: the API and the dispatch layer
                # keep separate enums that mirror each other, and the mirror is checked
                # here rather than by the two happening to agree.
                posture=Posture(payload.posture.value) if payload.posture else None,
                # Never read by a gate. It decides only what happens to a dispatch the
                # ceiling refuses: told to the caller, or recorded as waiting for a slot.
                if_full=payload.if_full,
                # Read by exactly one gate, which it skips. Its right to be set was
                # decided above; the guard layer honours it without re-deriving that,
                # exactly as it does for `authorized_by`.
                over_ceiling=payload.over_ceiling,
            ),
            # Both halves of this call read it: a dispatch that starts hands it to the
            # agent, and one that queues *stores* it, so the start minutes later tells
            # its agent the address this server answers on rather than a default.
            api_base=serving_api_base(request),
            queued_by=payload.user or "",
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
        raise dispatch_refusal_error(exc, task_id) from exc

    if isinstance(handle, QueuedDispatch):
        # Accepted, not started. The 202 is the same because the meaning is the same --
        # "taken, and the outcome reaches the task record" -- and `queued` is what tells
        # the two apart. See `DispatchStarted`.
        return DispatchStarted(
            run_id=handle.queue_id,
            mode="",
            posture="",
            task_id=task_id,
            caused_by=0,
            queued=True,
            queue_position=dispatch_queue.position(default_home(), handle.queue_id),
            queued_at=handle.queued_at,
        )

    meta = handle.directory.read_meta()
    return DispatchStarted(
        run_id=handle.run_id,
        session_id=handle.session_id,
        mode=handle.mode.value,
        posture=str(meta.get("posture") or ""),
        task_id=task_id,
        caused_by=_as_int(meta.get("caused_by")),
        runner=handle.runner,
        group=handle.group,
        over_ceiling=payload.over_ceiling,
    )


@router.post("/{task_id}/check", response_model=CheckRunResult)
async def check_task_acceptance(
    task_id: str,
    request: Request,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> CheckRunResult:
    """Run this task's executable acceptance checks and answer with what each one did.

    The same vector ``agentjobs check`` prints, written to the record the same way: the
    criteria's statuses move and exactly one ``check_result`` entry records the pass.

    **A POST because it executes commands**, not because it writes -- and that is also
    why it is classified under ``dispatch.start`` rather than under the ``task.*``
    capabilities a run already holds. A run may edit its own task, so a run able to call
    this could write a check and then have this machine run it, which is starting a
    process of its own choosing on somebody else's hardware. That is the act the dispatch
    gates bound, and this goes through the same gate: ``assert_dispatch_permitted`` runs
    before any process starts, and its refusals render under their own reason codes.

    Refused with ``no_checks`` on a task whose criteria are all prose. Answering an empty
    success would be indistinguishable, to any client, from every check having passed.
    """
    task = manager.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"Task '{task_id}' not found.",
            task_id=task_id,
        )
    # Resolved before anything runs, so a project that cannot attribute the entry is
    # refused rather than left with processes having run and nothing recording it.
    configured = project_config(project)
    default_user = str(configured.get("default_user") or "")
    if not default_user:
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_actor",
            "This project configures no default_user, so there is nobody to attribute "
            "the check_result entry to. Set default_user in .agentjobs/config.yaml, or "
            "run `agentjobs check --actor <id>`.",
            task_id=task_id,
        )
    actor = acting_actor(request, project, default_user)
    try:
        # Off the event loop: a pass may run for its whole 900-second budget, and every
        # other request to this server would wait behind it.
        report = await run_in_threadpool(evaluate_task, task, project=project)
    except NoChecksError as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_checks",
            str(exc),
            task_id=task_id,
            suggested_action="Give a criterion a `check` argv, then run this again.",
        ) from exc
    except DispatchError as exc:
        raise dispatch_refusal_error(exc, task_id) from exc

    updated = manager.record_check_result(
        task_id,
        actor=actor,
        results=report.results,
        unchecked=report.unchecked,
    )
    return CheckRunResult(
        task_id=task_id,
        results=report.results,
        unchecked=report.unchecked,
        ok=report.ok,
        entry_id=updated.log[-1].id,
    )


# ----- bounded agent loops (task-150) ----------------------------------------


def _chain_read(task: Task, chain: ChainAuthorization) -> ChainRead:
    """One chain, assembled from the entries that are the only record of it.

    Derived on every read rather than stored anywhere. A second copy of a chain's history
    could disagree with the entries it was derived from, and the entries are what a
    person auditing a loop next week will actually read.
    """
    matches = chain.matches(task)
    expired = chain.expired()
    iterations = [
        ChainIteration(
            iteration=int(entry.data.get("iteration") or 0),
            entry_id=entry.id,
            ts=entry.ts,
            results=[
                CheckOutcome.model_validate(item) for item in entry.data.get("results") or []
            ],
            unchecked=[str(item) for item in entry.data.get("unchecked") or []],
        )
        for entry in task.log
        if entry.type is LogEntryType.CHECK_RESULT
        and str(entry.data.get("chain_id") or "") == chain.chain_id
    ]
    return ChainRead(
        chain_id=chain.chain_id,
        task_id=task.id,
        entry_id=chain.entry.id,
        authorized_by=chain.entry.actor,
        authorized_at=chain.authorized_at,
        max_iterations=chain.data.max_iterations,
        wall_clock_seconds=chain.data.wall_clock_seconds,
        deadline=chain.deadline,
        check_digest=chain.data.check_digest,
        criteria=list(chain.data.criteria),
        revoked=chain.revoked,
        revoked_at=chain.revoked_by_entry.ts if chain.revoked_by_entry else None,
        expired=expired,
        digest_matches=matches,
        live=not chain.revoked and not expired and matches,
        iterations=iterations,
    )


def _chain_actor(
    request: Request, project: Project, claimed: Optional[str], task_id: str
) -> str:
    """The human a chain authorisation or revocation is attributed to.

    Three checks, and they are the dispatch endpoint's three, reached the same way rather
    than reimplemented: the id must be one this project configures
    (``validate_actor``), the caller must actually be that principal
    (``assert_actor_agrees``), and the id must be a person rather than an agent
    (``assert_authorizer_is_human``). A chain is a standing authorisation to start runs,
    so all three matter, and the third is the one that would otherwise let an agent id
    sign for a loop.

    **The project's ``default_user`` is not substituted for an omitted ``user``**, unlike
    the check route, and the difference is the same one the dispatch endpoint draws: a
    pass over the checks needs somebody to attribute an entry to, while an authorisation
    needs somebody to have *made* it. A config value standing in for a person would
    produce a row that reads as an authorisation and is really a default.
    """
    named = (claimed or "").strip()
    if not named:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "no_authorizer",
            "A chain has to be authorised by a named person: this entry is the whole of "
            "the driver's authority to start runs, and a server-side default would look "
            "like somebody's decision without being one. Send `user`.",
            task_id=task_id,
            field_errors=[ErrorDetail(path="user", message="Name the person authorising this.")],
        )
    actor = acting_actor(request, project, named)
    try:
        assert_authorizer_is_human(project_config(project), actor)
    except DispatchError as exc:
        raise dispatch_refusal_error(exc, task_id) from exc
    return actor


@router.get("/{task_id}/chains", response_model=ChainList)
async def read_task_chains(
    task_id: str,
    manager: TaskManager = Depends(get_task_manager),
) -> ChainList:
    """Every chain this task has had, oldest first, each with its iteration history.

    A read, and deliberately absent from ``ROUTE_CAPABILITIES``: it executes nothing and
    answers with what the log already holds. What a caller may *do* with a chain is gated
    at the two verbs below.
    """
    task = manager.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"Task '{task_id}' not found.",
            task_id=task_id,
        )
    return ChainList(
        task_id=task_id, chains=[_chain_read(task, item) for item in chain_history(task)]
    )


@router.post("/{task_id}/chain", response_model=ChainRead, status_code=status.HTTP_201_CREATED)
async def authorize_task_chain(
    task_id: str,
    request: Request,
    payload: ChainAuthorizeRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> ChainRead:
    """Authorise a bounded chain of dispatches against this task's checks (task-150).

    **Classified under ``dispatch.start``, which no run holds**, and the alternative was
    argued rather than assumed. A capability of its own -- ``CHAIN_AUTHORIZE`` -- would
    draw exactly the same line for exactly the same principals, and this module's
    coarseness is deliberate: *a capability per route would be a role system with extra
    steps*. What a chain authorisation buys is up to twenty dispatches against one task
    with nobody clicking again, which is the act ``dispatch.start`` already names, bought
    in advance. ``task.review`` was the other candidate and is wrong for the opposite
    reason: this is not a judgement about work somebody did, it is a purchase.

    The digest, the chain id and the covered criteria are computed here from the **stored
    task**. There is no request field for any of them, so there is nothing a caller could
    supply that would move the definition of done the chain converges on.

    ``user`` is validated the way a dispatch's is: the id must be one this project
    configures with ``kind: human``. An agent may not authorise a chain any more than it
    may authorise a dispatch, and this is the same refusal reached through the same
    function.
    """
    task = manager.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"Task '{task_id}' not found.",
            task_id=task_id,
        )
    actor = _chain_actor(request, project, payload.user, task_id)
    try:
        # Off the event loop, for the reason the check route is: authorising evaluates the
        # whole check set to decide whether there is anything to converge on, and that may
        # run for its entire 900-second budget.
        outcome = await run_in_threadpool(
            _authorize,
            manager,
            project,
            task,
            actor,
            payload.max_iterations,
            payload.wall_clock_seconds,
            payload.note,
        )
    except ChainRefused as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            getattr(exc, "reason", "chain_refused"),
            str(exc),
            task_id=task_id,
        ) from exc
    except DispatchError as exc:
        raise dispatch_refusal_error(exc, task_id) from exc
    return _chain_read(outcome.task, outcome.chain)


def _authorize(
    manager: TaskManager,
    project: Project,
    task: Task,
    actor: str,
    max_iterations: int,
    wall_clock_seconds: int,
    note: Optional[str],
) -> Any:
    """``authorize_chain``'s positional form, so the threadpool call stays readable."""
    return authorize_chain(
        manager=manager,
        project=project,
        task=task,
        actor=actor,
        max_iterations=max_iterations,
        wall_clock_seconds=wall_clock_seconds,
        note=note,
    )


@router.post("/{task_id}/chain/revoke", response_model=ChainList)
async def revoke_task_chain(
    task_id: str,
    request: Request,
    payload: ChainRevokeRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> ChainList:
    """Stop a chain before its next iteration. One click, no arguments required.

    Shares ``dispatch.start`` with the route above for the reason a kill switch always
    shares a capability with its switch: everyone who may start it may stop it. The
    asymmetric alternative -- anyone may stop, only some may start -- reads well and is
    wrong here, because the principal it would newly admit is a dispatched run, and a run
    able to revoke could stop a chain a person is relying on and then report that it
    converged.

    **This does not stop a run that is already executing.** An iteration is a dispatch and
    a dispatch is somebody's session; ``POST .../dispatch/runs/{id}/cancel`` ends one of
    those. What this guarantees is that no further iteration begins.
    """
    task = manager.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"Task '{task_id}' not found.",
            task_id=task_id,
        )
    actor = _chain_actor(request, project, payload.user, task_id)
    try:
        updated = revoke_chain(
            manager=manager,
            task=task,
            actor=actor,
            chain_id=payload.chain_id,
            note=payload.note,
        )
    except ChainRefused as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            getattr(exc, "reason", "chain_refused"),
            str(exc),
            task_id=task_id,
        ) from exc
    return ChainList(
        task_id=task_id,
        chains=[_chain_read(updated, item) for item in chain_history(updated)],
    )


def _as_int(value: object) -> int:
    """Read an int out of run metadata, which is a YAML mapping of anything."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def dispatch_refusal_error(exc: DispatchError, task_id: Optional[str]) -> MutationError:
    """Render a dispatch refusal under its own code, never as a generic 400.

    Public because the playbook run route renders the same refusals: a playbook run is
    a dispatch, and two renderings of one gate's answer would eventually disagree about
    the status or the remedy.

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
