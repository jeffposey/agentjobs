"""Search endpoint for AgentJobs API."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager
from agentjobs.models import Task

from ..dependencies import get_task_manager

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=List[Task])
async def search_tasks(q: str, manager: TaskManager = Depends(get_task_manager)) -> List[Task]:
    """Search tasks using a case-insensitive substring query."""
    if not q.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'q' must be provided",
        )
    return manager.search_tasks(q)
