"""Prompt endpoints for AgentJobs API."""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager
from agentjobs.models import Task

from ..dependencies import get_task_manager
from ..models import PromptAddRequest

router = APIRouter(prefix="/api/tasks", tags=["prompts"])


@router.get("/{task_id}/prompts/starter", response_model=Dict[str, str])
async def get_starter_prompt(
    task_id: str, manager: TaskManager = Depends(get_task_manager)
) -> Dict[str, str]:
    """Return the starter prompt for a task."""
    try:
        starter = manager.get_starter_prompt(task_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return {"task_id": task_id, "starter": starter}


@router.post(
    "/{task_id}/prompts",
    response_model=Task,
    status_code=status.HTTP_200_OK,
)
async def add_followup_prompt(
    task_id: str,
    payload: PromptAddRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Append a follow-up prompt entry for a task."""
    try:
        return manager.add_followup_prompt(
            task_id=task_id,
            author=payload.author,
            content=payload.content,
            context=payload.context,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
