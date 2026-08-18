"""Task CRUD endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.operations import OperationConflictError, RevisionConflictError
from agentjobs.projects import Project
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DependencyType,
    Lifecycle,
    Outcome,
    Priority,
    Task,
)

from .status import acting_actor, get_acting_project
from ..dependencies import current_identity, get_project, get_task_manager, project_config
from ..models import (
    BrokenTaskFile,
    DependencyRelation,
    HumanActionResponse,
    ReviewIdentity,
    ScopedDependencyEdge,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskRead,
    TaskUpdateRequest,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _read_tasks(manager: TaskManager, tasks: List[Task]) -> List[TaskRead]:
    facts = manager.dependency_facts()
    return [TaskRead.from_task(task, facts[task.id]) for task in tasks]


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
    return _read_tasks(manager, tasks)


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


@router.get("/next", response_model=Optional[Task])
async def get_next_task(
    priority: Optional[Priority] = None,
    agent: Optional[str] = None,
    manager: TaskManager = Depends(get_task_manager),
) -> Optional[Task]:
    """The next claimable task: ready, eligible for the agent, no unmet needs."""
    return manager.get_next_task(priority=priority, agent=agent)


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
            reason=(
                f"Needs {dependency.task}; it is not a task in this project."
                if dependency.task not in by_id
                else (
                    f"Needs {dependency.task}; it is still open."
                    if by_id[dependency.task].is_open
                    else f"Needs {dependency.task}; it is done."
                )
            ),
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
            **kwargs,
        )
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


class RejectActionRequest(HumanActionRequest):
    """Reject task with reason."""

    reason: str = Field(
        ...,
        min_length=1,
        description="Rejection reason",
        examples=["Out of scope"],
    )


@router.post("/{task_id}/approve", response_model=HumanActionResponse)
async def approve_task(
    task_id: str,
    payload: HumanActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Record human approval and hand the ball back to the agent (agent/work).

    Nothing here merges anything. The GUI cannot run git, and a button that implied
    otherwise would lie. It records the approval in the task record -- which is the
    point, since approvals used to live only in chat -- and moves the ball: the agent
    must now rebase, merge --no-ff, mark the branch merged, and close the task
    (ENGINEERING.md, "The Merge Gate").
    """
    user = acting_user(project, payload.user)
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt=(
                "Approved -- cleared to merge. Rebase onto main, merge --no-ff, mark "
                "the branch merged in branches[], and close this task completed. "
                "No merge has happened yet: the UI records approval, it does not run git."
            ),
            body=f"Approved by {user} through the web UI.",
        )
        return HumanActionResponse(task=task)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/request-changes", response_model=HumanActionResponse)
async def request_changes(
    task_id: str,
    payload: FeedbackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> HumanActionResponse:
    """Record requested changes and hand the ball back to the agent (agent/revise).

    The feedback is the payload of the handoff, so it rides in the ball_prompt and the
    log entry verbatim.
    """
    user = acting_user(project, payload.user)
    try:
        task = manager.handoff(
            task_id,
            actor=user,
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt=payload.feedback,
            body=f"Changes requested by {user}:\n\n{payload.feedback}",
        )
        return HumanActionResponse(task=task)
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
