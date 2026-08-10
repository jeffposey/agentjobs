"""State-transition and log endpoints for AgentJobs tasks (schema v2).

Every arrow in the canonical loop -- claim, handoff, release, close -- is one
endpoint here, each one manager call, each appending one log entry (design doc
section 5). There is no generic "set status" endpoint: the axes only move through
verbs that record why.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Task

from ..dependencies import get_task_manager
from ..models import (
    ClaimRequest,
    CloseRequest,
    HandoffRequest,
    LogAppendRequest,
    ProgressUpdateRequest,
    ReleaseRequest,
)

router = APIRouter(prefix="/tasks", tags=["status"])


def _map_error(exc: ValueError) -> HTTPException:
    """404 for a missing task, 409 for a refused transition."""
    if isinstance(exc, TaskNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{task_id}/claim", response_model=Task, status_code=status.HTTP_200_OK)
async def claim_task(
    task_id: str,
    payload: ClaimRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Claim a ready task: one winner, everyone else gets a 409."""
    try:
        return manager.claim_task(task_id, agent=payload.agent)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/handoff", response_model=Task, status_code=status.HTTP_200_OK)
async def handoff_task(
    task_id: str,
    payload: HandoffRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Move the ball, with its ask."""
    try:
        return manager.handoff(
            task_id,
            actor=payload.actor,
            ball=payload.ball,
            ball_reason=payload.ball_reason,
            ball_prompt=payload.ball_prompt,
            body=payload.body,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/release", response_model=Task, status_code=status.HTTP_200_OK)
async def release_task(
    task_id: str,
    payload: ReleaseRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Return a claimed task to the pool."""
    try:
        return manager.release_task(task_id, actor=payload.actor, body=payload.body)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/close", response_model=Task, status_code=status.HTTP_200_OK)
async def close_task(
    task_id: str,
    payload: CloseRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """End the task with an outcome."""
    try:
        return manager.close_task(
            task_id,
            actor=payload.actor,
            outcome=payload.outcome,
            body=payload.body,
            archive=payload.archive,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/log", response_model=Task, status_code=status.HTTP_200_OK)
async def append_log_entry(
    task_id: str,
    payload: LogAppendRequest,
    manager: TaskManager = Depends(get_task_manager),
) -> Task:
    """Append a note/progress/decision/question/answer/instruction entry."""
    try:
        return manager.add_log_entry(
            task_id,
            actor=payload.actor,
            type=payload.type,
            body=payload.body,
            re=payload.re,
            data=payload.data,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc


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
        raise _map_error(exc) from exc
