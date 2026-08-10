"""Task CRUD endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agentjobs.manager import TaskManager
from agentjobs.models import Comment, Priority, Task, TaskStatus

from ..dependencies import get_task_manager
from ..models import TaskCreateRequest, TaskUpdateRequest

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=List[Task])
async def list_tasks(
    status_filter: Optional[TaskStatus] = None,
    priority_filter: Optional[Priority] = None,
    manager: TaskManager = Depends(get_task_manager),
) -> List[Task]:
    """List tasks with optional workflow and priority filters."""
    return manager.list_tasks(status=status_filter, priority=priority_filter)


@router.get("/next", response_model=Optional[Task])
async def get_next_task(
    priority: Optional[Priority] = None,
    manager: TaskManager = Depends(get_task_manager),
) -> Optional[Task]:
    """Return the next ready task, or ``None`` when no tasks are available."""
    return manager.get_next_task(priority=priority)


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
    task_data = payload.model_dump()
    try:
        return manager.create_task(**task_data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.put("/{task_id}", response_model=Task)
async def replace_task(
    task_id: str,
    payload: TaskCreateRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Replace task fields using the provided payload."""
    data = payload.model_dump()
    data.pop("id", None)
    try:
        return manager.replace_task(task_id, **data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch("/{task_id}", response_model=Task)
async def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Apply a partial update to a task."""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No updates provided",
        )
    try:
        return manager.update_task(task_id, **updates)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.delete("/{task_id}", response_model=Task)
async def archive_task(task_id: str, manager: TaskManager = Depends(get_task_manager)) -> Task:
    """Archive a task by setting its status to archived."""
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
    """Mark a deliverable as completed for the task."""
    try:
        return manager.mark_deliverable_complete(task_id, deliverable_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# Human action endpoints for Phase 6 workflow


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
) -> Dict[str, Any]:
    """Record human approval and hand the task back to the agent that owns it.

    This used to set ``ready``, which is the opposite of approving: ``ready`` means
    unclaimed and claimable by any eligible agent, so approving finished work threw it
    back into the pool. Approval means the human is done and the agent acts next --
    rebase, merge --no-ff, mark the branch merged, close the task (ENGINEERING.md, "The
    Merge Gate").

    Nothing here merges anything. The GUI cannot run git, and a button that implied
    otherwise would be a worse lie than the one it replaces. It records the approval in
    the task record -- which is the point, since approvals have until now lived only in
    chat -- and moves the ball.

    ``in_progress`` is the closest v1 can get. Neither v1 status means "approved,
    awaiting merge": leaving it ``waiting_for_human`` would keep a task in the human's
    inbox after the human finished with it. The schema v2 design resolves this properly
    (``ball: agent``, ``ball_reason: work``), and ``in_progress`` is exactly what that
    state maps back to in v1 -- see docs/schema-design.md section 3.
    """
    try:
        task = manager.update_status(
            task_id=task_id,
            status=TaskStatus.IN_PROGRESS,
            author=payload.user,
            summary=f"Approved by {payload.user} — cleared to merge",
            details=(
                f"{payload.user} approved this work through the web UI. The ball is "
                "back with the agent, which must now rebase onto main, merge --no-ff, "
                "mark the branch merged in branches[], and set this task completed. "
                "No merge has happened yet: the UI records approval, it does not run git."
            ),
            metadata={"action": "approve"},
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
) -> Dict[str, Any]:
    """Record requested changes and hand the task back to the agent.

    Previously this left the status untouched, so a task the human had finished with
    stayed in the human inbox and nothing indicated the agent had work to do. Like
    approval, requesting changes moves the ball: the agent acts next.
    """
    try:
        # The feedback is the payload of the handoff, so it goes in the record verbatim.
        manager.add_comment(
            task_id=task_id,
            author=payload.user,
            content=payload.feedback,
            kind="feedback",
        )
        task = manager.update_status(
            task_id=task_id,
            status=TaskStatus.IN_PROGRESS,
            author=payload.user,
            summary=f"Changes requested by {payload.user}",
            details=payload.feedback,
            metadata={"action": "request_changes"},
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
) -> Dict[str, Any]:
    """Reject and archive a task."""
    try:
        task = manager.update_status(
            task_id=task_id,
            status=TaskStatus.ARCHIVED,
            author=payload.user,
            summary=f"Rejected by {payload.user}: {payload.reason}",
            metadata={"action": "reject", "reason": payload.reason},
        )
        return {"task": task.model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/comments", response_model=Comment)
async def add_comment(
    task_id: str,
    author: str,
    content: str,
    kind: str = "comment",
    manager: TaskManager = Depends(get_task_manager),
) -> Comment:
    """Add a comment to a task."""
    try:
        return manager.add_comment(
            task_id=task_id,
            author=author,
            content=content,
            kind=kind,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
