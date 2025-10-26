"""Task CRUD endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager
from agentjobs.models import Priority, Task, TaskStatus

from ..dependencies import get_task_manager
from ..models import TaskCreateRequest, TaskUpdateRequest

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
    """Return the next planned task, or ``None`` when no tasks are available."""
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
