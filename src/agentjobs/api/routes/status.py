"""Status update endpoints for AgentJobs tasks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager
from agentjobs.models import Task

from ..dependencies import get_task_manager
from ..models import ProgressUpdateRequest, StatusUpdateRequest

router = APIRouter(prefix="/api/tasks", tags=["status"])


@router.post("/{task_id}/status", response_model=Task, status_code=status.HTTP_200_OK)
async def post_status_update(
    task_id: str,
    payload: StatusUpdateRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Record a status transition for a task."""
    try:
        return manager.update_status(
            task_id=task_id,
            status=payload.status,
            author=payload.author,
            summary=payload.summary,
            details=payload.details,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/{task_id}/progress", response_model=Task, status_code=status.HTTP_200_OK)
async def post_progress_update(
    task_id: str,
    payload: ProgressUpdateRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Append a progress update entry for the task."""
    try:
        return manager.add_progress_update(
            task_id=task_id,
            author=payload.author,
            summary=payload.summary,
            details=payload.details,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
