"""Task CRUD endpoints."""

from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.attachments import AttachmentError, AttachmentPayload
from agentjobs.dispatch.auto import maybe_auto_dispatch
from agentjobs.operations import OperationConflictError, RevisionConflictError
from agentjobs.projects import Project
from agentjobs.queue import QueueCorruptionError
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.storage import TaskStorage
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DependencyType,
    Lifecycle,
    Outcome,
    Priority,
    Task,
)

from .status import acting_actor, get_acting_project, serving_api_base
from ..dependencies import (
    current_identity,
    get_project,
    get_task_manager,
    get_task_storage,
    project_config,
)
from ..models import (
    AttachmentUpload,
    BrokenTaskFile,
    DependencyRelation,
    HumanActionResponse,
    NextExplanationResponse,
    ReviewIdentity,
    ScopedDependencyEdge,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskRead,
    TaskUpdateRequest,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _relation(
    task_id: str,
    *,
    by_id: Dict[str, Task],
    note: Optional[str],
    reason: str,
) -> DependencyRelation:
    target = by_id.get(task_id)
    return DependencyRelation(
        task_id=task_id,
        title=target.title if target else None,
        exists=target is not None,
        state="missing" if target is None else ("open" if target.is_open else "done"),
        note=note,
        reason=reason,
    )


def _needs_reason(task_id: str, *, by_id: Dict[str, Task]) -> str:
    """Say, in words, why a prerequisite is or is not satisfied.

    A closed prerequisite used to read "it is done" whatever its outcome, so a task
    whose blocker had been *superseded* -- abandoned, its work never carried out --
    reported the same sentence as one whose blocker was finished. The outcome is the
    fact worth carrying here, and the reader is exactly the person who has to decide
    whether the dependency still means anything.

    ``state`` is left alone deliberately: whether a non-completed outcome satisfies a
    need is a semantics question, and this only fixes what the sentence claims.
    """
    target = by_id.get(task_id)
    if target is None:
        return f"Needs {task_id}; it is not a task in this project."
    if target.is_open:
        return f"Needs {task_id}; it is still open."
    outcome = (target.outcome or Outcome.COMPLETED).value
    return f"Needs {task_id}; it is closed as {outcome}."


def acting_user(project: Any, user: str) -> str:
    """The actor id to record, refused if this project does not define it.

    D2: an unrecognised id is a silent no-op that survives forever, and the log is the
    one structure in this system that is never rewritten. Better to reject the action
    than to write an attribution nobody can resolve later.
    """
    try:
        validated = validate_actor(project_config(project), user)
    except UnknownActorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    identity = current_identity(project)
    if not identity.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=identity.detail,
        )
    if user != identity.user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Review actions must be attributed to configured user {identity.user!r}.",
        )
    return validated


def decoded_attachments(uploads: Sequence[AttachmentUpload]) -> List[AttachmentPayload]:
    """Turn base64 uploads into payloads, refusing anything that will not decode.

    Refused here rather than deeper, because the person is still looking at the form
    with their prose in it: a 400 they can read beats a write that half-succeeded.
    """
    payloads: List[AttachmentPayload] = []
    for index, upload in enumerate(uploads):
        try:
            data = base64.b64decode(upload.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Attachment {index + 1} is not valid base64 data.",
            ) from exc
        payloads.append(AttachmentPayload(data=data, label=upload.label))
    return payloads


@router.get("", response_model=List[TaskRead])
async def list_tasks(
    lifecycle: Optional[Lifecycle] = None,
    ball: Optional[Ball] = None,
    priority_filter: Optional[Priority] = Query(default=None, alias="priority"),
    parent: Optional[str] = Query(
        default=None, description="Return only the children of this umbrella task."
    ),
    manager: TaskManager = Depends(get_task_manager),
) -> List[TaskRead]:
    """List tasks filtered along the state axes.

    ``?ball=human`` is the human inbox: everything waiting on a person, each row
    carrying its ``ball_prompt``. ``?ball=external`` is the blocked list.
    ``?parent=task-063-schema-v2`` is one umbrella's children.
    """
    tasks = manager.list_tasks(
        lifecycle=lifecycle, ball=ball, priority=priority_filter, parent=parent
    )
    return TaskRead.from_tasks(manager, tasks)


@router.get("/broken", response_model=List[BrokenTaskFile])
async def list_broken_tasks(
    manager: TaskManager = Depends(get_task_manager),
) -> List[Dict[str, Any]]:
    """Files in the task directory that exist but cannot be loaded.

    Declared before /{task_id} so "broken" is not captured as a task id. These used to
    be invisible: storage returned None for them and every listing simply omitted the
    task. An unmigrated v1 file shows up here by filename.
    """
    return [error.as_dict() for error in manager.load_errors()]


def queue_broken(exc: QueueCorruptionError) -> HTTPException:
    """Render a refusal to answer as 409, with the ids and the repair command intact.

    ``QueueCorruptionError`` is a ``RuntimeError``, not a ``ValueError``, so it does
    **not** fall through the ValueError handling every other route here relies on --
    without this it would surface as a 500, which reads as "the server is broken"
    when the truth is "your corpus is, and here is the command that fixes it". 409 is
    the right status for the same reason it is on a refused verb: the request was
    well-formed and the state it addressed will not permit it.

    The message is passed through verbatim rather than summarised, because everything
    a caller needs -- every offending task id, its band, and ``agentjobs queue
    repair`` -- is already in it, and design section 8 asks for exactly that detail.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(exc),
    )


@router.get("/next", response_model=Optional[Task])
async def get_next_task(
    priority: Optional[Priority] = None,
    agent: Optional[str] = None,
    manager: TaskManager = Depends(get_task_manager),
) -> Optional[Task]:
    """The next claimable task: ready, eligible for the agent, no unmet needs.

    Answers 409 rather than guessing when the queue it would have to read is broken.
    """
    try:
        return manager.get_next_task(priority=priority, agent=agent)
    except QueueCorruptionError as exc:
        raise queue_broken(exc) from exc


@router.get("/next/explain", response_model=NextExplanationResponse)
async def explain_next_task(
    priority: Optional[Priority] = None,
    agent: Optional[str] = None,
    manager: TaskManager = Depends(get_task_manager),
) -> NextExplanationResponse:
    """Why this task is next, and every open task it stands in front of.

    Declared before ``/{task_id}`` so "next" is not captured as a task id, exactly as
    ``/next`` and ``/broken`` are. The body is design section 9's structure, produced
    by the manager rather than assembled here: the route is a transcription.
    """
    try:
        explanation = manager.explain_next(priority=priority, agent=agent)
    except QueueCorruptionError as exc:
        raise queue_broken(exc) from exc
    return NextExplanationResponse.model_validate(explanation.as_dict())


@router.get("/{task_id}/detail", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: str,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> TaskDetailResponse:
    """Return the complete resumption and review contract for one task."""
    task = manager.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    identity = current_identity(project)
    tasks = manager.list_tasks()
    by_id = {candidate.id: candidate for candidate in tasks}
    facts = manager.dependency_facts(tasks)
    children = manager.get_subtasks(task_id)
    child_ids = {child.id for child in children}
    needs = [
        _relation(
            dependency.task,
            by_id=by_id,
            note=dependency.note,
            reason=_needs_reason(dependency.task, by_id=by_id),
        )
        for dependency in task.dependencies
        if dependency.type is DependencyType.NEEDS
    ]
    blocks = [
        _relation(
            candidate.id,
            by_id=by_id,
            note=dependency.note,
            reason=f"{candidate.id} needs this task.",
        )
        for candidate in tasks
        for dependency in candidate.dependencies
        if dependency.type is DependencyType.NEEDS and dependency.task == task.id
    ]
    blocks.extend(
        _relation(
            dependency.task,
            by_id=by_id,
            note=dependency.note,
            reason=f"This task declares that it blocks {dependency.task}.",
        )
        for dependency in task.dependencies
        if dependency.type is DependencyType.BLOCKS
    )
    # `related` neither blocks nor is blocked, so it never reaches dependency_facts.
    # It is still the edge a reader follows -- a reported issue points this way at the
    # page it was noticed on -- and an edge the product stores but never shows is one
    # nobody can act on.
    related = [
        _relation(
            dependency.task,
            by_id=by_id,
            note=dependency.note,
            reason=(
                f"Related to {dependency.task}; it is not a task in this project."
                if dependency.task not in by_id
                else f"Related to {dependency.task}."
            ),
        )
        for dependency in task.dependencies
        if dependency.type is DependencyType.RELATED
    ]
    child_dependency_edges = []
    for child in children:
        for dependency in child.dependencies:
            if dependency.type is DependencyType.NEEDS:
                source, target = dependency.task, child.id
            elif dependency.type is DependencyType.BLOCKS:
                source, target = child.id, dependency.task
            else:
                continue
            child_dependency_edges.append(
                ScopedDependencyEdge(
                    source=source,
                    target=target,
                    note=dependency.note,
                    source_exists=source in by_id,
                    target_exists=target in by_id,
                    source_contained=source in child_ids,
                    target_contained=target in child_ids,
                )
            )
    return TaskDetailResponse(
        task=TaskRead.from_task(task, facts[task.id]),
        parent_task=(
            TaskRead.from_task(by_id[task.parent], facts[task.parent])
            if task.parent and task.parent in by_id
            else None
        ),
        children=[TaskRead.from_task(child, facts[child.id]) for child in children],
        needs=needs,
        blocks=blocks,
        related=related,
        child_dependency_edges=child_dependency_edges,
        identity=ReviewIdentity(
            ok=identity.ok,
            user=identity.user,
            problem=identity.problem,
            detail=identity.detail,
        ),
    )


@router.get("/{task_id}/attachments/{filename}")
async def get_attachment(
    task_id: str,
    filename: str,
    manager: TaskManager = Depends(get_task_manager),
    storage: TaskStorage = Depends(get_task_storage),
) -> Response:
    """Serve one image a log entry references.

    Resolved through the task's own record rather than straight off the filesystem, so
    the only files this can ever return are ones an entry actually points at. That also
    supplies the recorded hash, which is checked before the bytes are handed back: a
    file edited or corrupted since it was stored is refused rather than rendered.
    """
    task = manager.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found"
        )
    wanted = f"attachments/{task_id}/{filename}"
    record = next(
        (
            attachment
            for entry in task.log
            for attachment in (entry.attachments or [])
            if attachment.path == wanted
        ),
        None,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No log entry on {task_id} references {filename}.",
        )
    try:
        data = storage.attachments.read(record)
    except AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=record.media_type,
        # Content-addressed: the name is the hash, so these bytes can never change.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, manager: TaskManager = Depends(get_task_manager)) -> Task:
    """Retrieve a specific task by identifier."""
    task = manager.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    return task


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Task:
    """Create a new task record.

    With an operation_id the create runs under the project-wide creation lock and a
    retry resolves to the task the first attempt made, rather than producing a second
    one with a different generated id.
    """
    kwargs = payload.manager_kwargs()
    kwargs.pop("operation_id", None)
    actor = kwargs.pop("actor", None)
    if actor is not None:
        # Validated whenever it is supplied, not only alongside an operation_id: the
        # id is written into an append-only log either way, and an attribution nobody
        # can resolve later is worse than a refused request (D2).
        actor = acting_actor(project, str(actor))
    try:
        return manager.create_task(
            actor=actor,
            operation_id=payload.operation_id,
            attachments=decoded_attachments(payload.attachments),
            **kwargs,
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OperationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.patch("/{task_id}", response_model=Task)
async def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    actor: Optional[str] = Query(
        default=None,
        description="Actor recorded on the manager-owned note an operation_id creates.",
    ),
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Apply a partial update to a task. State axes move through the verbs, not here."""
    updates = payload.model_dump(exclude_unset=True)
    operation_id = updates.pop("operation_id", None)
    expected_revision = updates.pop("expected_revision", None)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No updates provided",
        )
    try:
        return manager.update_task(
            task_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            actor=actor,
            **updates,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (OperationConflictError, RevisionConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.delete("/{task_id}", response_model=Task)
async def archive_task(task_id: str, manager: TaskManager = Depends(get_task_manager)) -> Task:
    """Archive a task. An open task is closed as cancelled first."""
    try:
        return manager.archive_task(task_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch("/{task_id}/deliverables/{deliverable_path:path}", response_model=Task)
async def mark_deliverable(
    task_id: str,
    deliverable_path: str,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Mark a deliverable as done for the task."""
    try:
        return manager.mark_deliverable_complete(task_id, deliverable_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# Human action endpoints for the review loop

NL2 = "\n\n"
"""A blank line between a sentence AgentJobs wrote and one a human did.

Every prompt in this module separates the two the same way, so an agent reading
one can tell at a glance where the fixed part stops and the human's words start.
"""


class HumanActionRequest(BaseModel):
    """Base request for human actions."""

    user: str = Field(..., description="User performing the action", examples=["jeff"])


class FeedbackActionRequest(HumanActionRequest):
    """Request changes with feedback."""

    feedback: str = Field(
        ...,
        min_length=1,
        description="Feedback text",
        examples=["Please add error handling"],
    )
    attachments: List[AttachmentUpload] = Field(
        default_factory=list,
        description="Images evidencing the feedback, stored as sidecar files.",
    )


class SendBackActionRequest(FeedbackActionRequest):
    """Feedback whose meaning is carried by which route received it.

    The same shape as requesting changes, on purpose: every send-back is a note the
    human wrote plus the ball moving to the agent, and the only thing that differs is
    what the note *means*. That difference is the route and the ball_reason it writes,
    not a discriminator inside the payload -- one act per route is what /approve,
    /request-changes and /reject already do, and it is what makes a network log or a
    server log readable without cross-referencing a body.
    """


class NoteActionRequest(HumanActionRequest):
    """A human action carrying an optional note.

    Distinct from ``FeedbackActionRequest`` in exactly one way, and it is the one that
    matters: the text is optional. Approving, and releasing a hold, are complete acts
    on their own -- a required field here would make "yes, go" impossible to say
    without saying more, which is what pushed approvals-with-a-comment onto the
    request-changes path in the first place (task-228). ``None`` and ``""`` both mean
    no note, and the route then writes precisely what it wrote before this existed.
    """

    note: Optional[str] = Field(
        None,
        description="Optional note, recorded verbatim in the handoff prompt and the log.",
        examples=["Fold the naming nit in before you merge."],
    )


class RejectActionRequest(HumanActionRequest):
    """Reject task with reason."""

    reason: str = Field(
        ...,
        min_length=1,
        description="Rejection reason",
        examples=["Out of scope"],
    )


def after_human_handoff(
    manager: TaskManager, project: Project, task: Task, request: Request
) -> Task:
    """Start an agent if this project opted into auto-dispatch, and never fail.

    Called after a human action that has already been written, so the task's newest log
    entry is that human act -- which is what makes the human-clocked check in
    ``maybe_auto_dispatch`` mean something rather than being circular.

    The approval succeeded before this ran, so nothing here may turn it into an error.
    Auto-dispatch reports its own refusals onto the task record and returns rather than
    raising; the task is re-read afterwards so the caller answers with what is now on
    disk, including a run that just started or a cap that just parked it.

    ``request`` is taken purely for the serving address, so an agent started by Approve
    is told the same thing as one started by Dispatch. The two paths reaching different
    answers is exactly the failure task-154 fixed, and passing the request is what makes
    them the same code rather than the same constant written twice.
    """
    outcome = maybe_auto_dispatch(
        manager=manager,
        project=project,
        project_config=project_config(project),
        task=task,
        api_base=serving_api_base(request),
    )
    if not outcome.considered:
        return task
    return manager.get_task(task.id) or task


APPROVAL_CLEARANCE = (
    "Approved -- cleared to merge. Rebase onto main, merge --no-ff, mark "
    "the branch merged in branches[], and close this task completed. "
    "No merge has happened yet: the UI records approval, it does not run git."
)
"""The sentence every approval carries, note or no note.

Named rather than written twice so the with-note branch cannot drift from the
without-note one. An approval that quietly lost its merge clearance because somebody
attached a sentence to it would produce exactly the round trip this route pair exists
to remove.
"""


@router.post("/{task_id}/approve", response_model=HumanActionResponse)
async def approve_task(
    task_id: str,
    request: Request,
    payload: NoteActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Record human approval and hand the ball back to the agent (agent/work).

    Nothing here merges anything. The GUI cannot run git, and a button that implied
    otherwise would lie. It records the approval in the task record -- which is the
    point, since approvals used to live only in chat -- and moves the ball: the agent
    must now rebase, merge --no-ff, mark the branch merged, and close the task
    (ENGINEERING.md, "The Merge Gate").

    ``note`` is optional and strictly additive (task-228). With none, this writes byte
    for byte what it wrote before the field existed. With one, the note rides *after*
    the merge clearance, verbatim and unsummarised, followed by a sentence saying it is
    context to carry into the merge -- because an agent that reads an approval note as
    a fresh review round has inverted the point of attaching one. Before this, an
    approval carrying a sentence had to go through Request Changes: a round trip the
    human did not ask for, and a record that said `revise` about work that was approved.
    """
    user = acting_user(project, payload.user)
    note = (payload.note or "").strip()
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt=(
                (
                    APPROVAL_CLEARANCE
                    + NL2
                    + f"Note from {user}:"
                    + NL2
                    + note
                    + NL2
                    + "That note is context to carry into the merge, not another "
                    + "review round: you are still cleared to merge."
                )
                if note
                else APPROVAL_CLEARANCE
            ),
            body=(
                f"Approved by {user} through the web UI:" + NL2 + note
                if note
                else f"Approved by {user} through the web UI."
            ),
        )
        return HumanActionResponse(task=after_human_handoff(manager, project, task, request))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/request-changes", response_model=HumanActionResponse)
async def request_changes(
    task_id: str,
    request: Request,
    payload: FeedbackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Record requested changes and hand the ball back to the agent (agent/revise).

    The feedback is the payload of the handoff, so it rides in the ball_prompt and the
    log entry verbatim.
    """
    user = acting_user(project, payload.user)
    attachments = decoded_attachments(payload.attachments)
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt=payload.feedback,
            body=f"Changes requested by {user}:\n\n{payload.feedback}",
            attachments=attachments,
        )
        # Requesting changes is a human act that moves the ball to an agent, exactly as
        # approving is. Covering only Approve would mean the one handoff that always
        # comes with instructions attached is the one that needs a second click.
        return HumanActionResponse(task=after_human_handoff(manager, project, task, request))
    except AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


def _send_back(
    *,
    task_id: str,
    request: Request,
    payload: SendBackActionRequest,
    manager: TaskManager,
    project: Any,
    user: str,
    ball_reason: BallReason,
    prompt: str,
    body: str,
    dispatchable: bool = True,
) -> HumanActionResponse:
    """The shared body of every send-it-back-to-the-agent route.

    Four human acts differ only in the reason they record and the sentence they write
    around the note. Everything else -- decoding attachments, the 400 and 404 mappings,
    and whether an agent may start on the result -- is the same code, and writing it
    four times is how three of them drift.

    ``dispatchable`` is False only for a hold, and it is belt to `maybe_auto_dispatch`'s
    braces: that function refuses `agent/hold` on its own, and this route does not ask
    it in the first place. Starting a run off the click that said stop is the failure
    worth two independent guards.
    """
    attachments = decoded_attachments(payload.attachments)
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=ball_reason,
            ball_prompt=prompt,
            body=body,
            attachments=attachments,
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not dispatchable:
        return HumanActionResponse(task=task)
    return HumanActionResponse(task=after_human_handoff(manager, project, task, request))


@router.post("/{task_id}/answer", response_model=HumanActionResponse)
async def answer_task(
    task_id: str,
    request: Request,
    payload: SendBackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Supply what the agent was waiting for and hand the ball back (agent/answer).

    Not a revision: nothing the agent did was wrong, and a record that says otherwise
    makes the next reader reconstruct which it was. The answer rides in the ball_prompt
    and the log verbatim, exactly as requested changes do.
    """
    user = acting_user(project, payload.user)
    return _send_back(
        task_id=task_id,
        request=request,
        payload=payload,
        manager=manager,
        project=project,
        user=user,
        ball_reason=BallReason.ANSWER,
        prompt=payload.feedback,
        body=f"Answered by {user}:" + NL2 + payload.feedback,
    )


@router.post("/{task_id}/redirect", response_model=HumanActionResponse)
async def redirect_task(
    task_id: str,
    request: Request,
    payload: SendBackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Re-brief the agent and hand the ball back (agent/redirect).

    The instructions changed; the work done so far stands. Recorded apart from `revise`
    because a reader who cannot tell a re-brief from a rejection has to reconstruct
    which it was from prose -- task-222 entry 14 had to supersede entry 13 to say
    exactly that.
    """
    user = acting_user(project, payload.user)
    return _send_back(
        task_id=task_id,
        request=request,
        payload=payload,
        manager=manager,
        project=project,
        user=user,
        ball_reason=BallReason.REDIRECT,
        prompt=payload.feedback,
        body=f"New instructions from {user}:" + NL2 + payload.feedback,
    )


@router.post("/{task_id}/hold", response_model=HumanActionResponse)
async def hold_task(
    task_id: str,
    request: Request,
    payload: SendBackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Stop the task, with the release condition on the record (agent/hold).

    The condition is prefixed rather than left bare, because this is the one send-back
    whose prompt must not read as work: an agent that skims it and carries on has done
    the opposite of what it was told. `hold` is also the one agent-side reason no
    dispatch path will act on, so a held task cannot be started by clicking Dispatch
    beside the Hold button that stopped it.
    """
    user = acting_user(project, payload.user)
    return _send_back(
        task_id=task_id,
        request=request,
        payload=payload,
        manager=manager,
        project=project,
        user=user,
        ball_reason=BallReason.HOLD,
        prompt=(
            "ON HOLD -- do not resume this task until the condition below is met and a "
            "human has released it." + NL2 + payload.feedback
        ),
        body=f"Put on hold by {user}:" + NL2 + payload.feedback,
        dispatchable=False,
    )


@router.post("/{task_id}/resume", response_model=HumanActionResponse)
async def resume_task(
    task_id: str,
    request: Request,
    payload: NoteActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Release a hold and put the task back to work (agent/work).

    The counterpart to /hold, and the reason a hold is not a dead end. The note is
    optional because "carry on" is a complete instruction on its own; demanding a
    sentence to say it would push the human back onto the send-back controls, which is
    how one reason came to carry four intents in the first place.
    """
    user = acting_user(project, payload.user)
    note = (payload.note or "").strip()
    released = f"Hold released by {user} -- resume this task."
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt=(released + NL2 + note if note else released),
            body=(
                f"Hold released by {user}:" + NL2 + note if note else f"Hold released by {user}."
            ),
        )
        return HumanActionResponse(task=after_human_handoff(manager, project, task, request))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/reject", response_model=HumanActionResponse)
async def reject_task(
    task_id: str,
    payload: RejectActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Reject a task: closed as cancelled, archived, reason on the record."""
    user = acting_user(project, payload.user)
    try:
        task = manager.close_task(
            task_id,
            actor=user,
            outcome=Outcome.CANCELLED,
            body=f"Rejected by {user}: {payload.reason}",
            archive=True,
        )
        return HumanActionResponse(task=task)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
