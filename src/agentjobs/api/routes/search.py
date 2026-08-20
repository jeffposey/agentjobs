"""Search endpoint for AgentJobs API."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager

from ..dependencies import get_task_manager
from ..models import TaskRead

router = APIRouter(prefix="", tags=["search"])


@router.get("/search", response_model=List[TaskRead])
async def search_tasks(q: str, manager: TaskManager = Depends(get_task_manager)) -> List[TaskRead]:
    """Search tasks using a case-insensitive substring query.

    Rows carry the same computed dependency facts as ``GET /tasks`` -- ``actionable``,
    ``unmet_needs``, ``open_children_count`` -- so a result can be acted on without a
    second request per row.
    """
    # Those facts are why this returns TaskRead rather than Task. It answered with the
    # bare stored record until task-180, so the computed fields were absent from every
    # search row, and the MCP summary layer turned the absence of open_children_count
    # into 0 -- a parent with six open children reported as having none.
    if not q.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'q' must be provided",
        )
    return TaskRead.from_tasks(manager, manager.search_tasks(q))
