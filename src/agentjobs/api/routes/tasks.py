"""Task CRUD endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority, Task

from ..dependencies import get_project, get_task_manager, project_config
from ..models import TaskCreateRequest, TaskUpdateRequest

router = APIRouter(prefix="/tasks", tags=["tasks"])


def acting_user(project: Any, user: str) -> str:
    """The actor id to record, refused if this project does not define it.

    D2: an unrecognised id is a silent no-op that survives forever, and the log is the
    one structure in this system that is never rewritten. Better to reject the action
    than to write an attribution nobody can resolve later.
    """
    try:
        return validate_actor(project_config(project), user)
    except UnknownActorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=List[Task])
async def list_tasks(
    lifecycle: Optional[Lifecycle] = None,
    ball: Optional[Ball] = None,
    priority_filter: Optional[Priority] = Query(default=None, alias="priority"),
    parent: Optional[str] = Query(
        default=None, description="Return only the children of this umbrella task."
    ),
    manager: TaskManager = Depends(get_task_manager),
) -> List[Task]:
    """List tasks filtered along the state axes.

    ``?ball=human`` is the human inbox: everything waiting on a person, each row
    carrying its ``ball_prompt``. ``?ball=external`` is the blocked list.
    ``?parent=task-063-schema-v2`` is one umbrella's children.
    """
    return manager.list_tasks(
        lifecycle=lifecycle, ball=ball, priority=priority_filter, parent=parent
    )


@router.get("/broken", response_model=List[Dict[str, Any]])
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
) -> Task:
    """Create a new task record."""
    try:
        return manager.create_task(**payload.manager_kwargs())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.patch("/{task_id}", response_model=Task)
async def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Apply a partial update to a task. State axes move through the verbs, not here."""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No updates provided",
        )
    try:
        return manager.update_task(task_id, **updates)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
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


@router.post("/{task_id}/approve", response_model=Dict[str, Any])
async def approve_task(
    task_id: str,
    payload: HumanActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> Dict[str, Any]:
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
        return {"task": task.model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/request-changes", response_model=Dict[str, Any])
async def request_changes(
    task_id: str,
    payload: FeedbackActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> Dict[str, Any]:
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
        return {"task": task.model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/reject", response_model=Dict[str, Any])
async def reject_task(
    task_id: str,
    payload: RejectActionRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Any = Depends(get_project),
) -> Dict[str, Any]:
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
        return {"task": task.model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
