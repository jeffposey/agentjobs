"""State-transition and log endpoints for AgentJobs tasks (schema v2).

Every arrow in the canonical loop -- claim, handoff, release, close -- is one
endpoint here, each one manager call, each appending one log entry (design doc
section 5). There is no generic "set status" endpoint: the axes only move through
verbs that record why.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agentjobs.actors import UnknownActorError, validate_actor
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Task
from agentjobs.projects import Project

from ..dependencies import get_task_manager, project_config, request_project
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


def acting_actor(project: Project, actor: str) -> str:
    """Return the actor id to record, refused when this project does not define it.

    The three human review routes have always validated their actor. These six did
    not, so a typo -- or an MCP client inventing an identity from a model name --
    wrote an unresolvable attribution into an append-only log. The validator itself
    still accepts anything on a project that configures no actors, so a fresh
    ``agentjobs init`` is unaffected; it only bites once a project has said who its
    actors are.

    Unlike review actions, an agent verb is not required to match ``default_user``.
    Any configured actor may claim or log; only the human review endpoints care which
    person is at the keyboard.
    """
    try:
        return validate_actor(project_config(project), actor)
    except UnknownActorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def get_acting_project(request: Request) -> Project:
    """Provide the addressed project so a route can validate its actor."""
    return request_project(request)


@router.post("/{task_id}/claim", response_model=Task, status_code=status.HTTP_200_OK)
async def claim_task(
    task_id: str,
    payload: ClaimRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Task:
    """Claim a ready task: one winner, everyone else gets a 409."""
    agent = acting_actor(project, payload.agent)
    try:
        return manager.claim_task(task_id, agent=agent)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/handoff", response_model=Task, status_code=status.HTTP_200_OK)
async def handoff_task(
    task_id: str,
    payload: HandoffRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Task:
    """Move the ball, with its ask."""
    actor = acting_actor(project, payload.actor)
    try:
        return manager.handoff(
            task_id,
            actor=actor,
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
    project: Project = Depends(get_acting_project),
) -> Task:
    """Return a claimed task to the pool."""
    actor = acting_actor(project, payload.actor)
    try:
        return manager.release_task(task_id, actor=actor, body=payload.body)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/{task_id}/close", response_model=Task, status_code=status.HTTP_200_OK)
async def close_task(
    task_id: str,
    payload: CloseRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> Task:
    """End the task with an outcome."""
    actor = acting_actor(project, payload.actor)
    try:
        return manager.close_task(
            task_id,
            actor=actor,
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
    project: Project = Depends(get_acting_project),
) -> Task:
    """Append a note/progress/decision/question/answer/instruction entry."""
    actor = acting_actor(project, payload.actor)
    try:
        return manager.add_log_entry(
            task_id,
            actor=actor,
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
    project: Project = Depends(get_acting_project),
) -> Task:
    """Append a progress update entry for the task."""
    author = acting_actor(project, payload.author)
    try:
        return manager.add_progress_update(
            task_id=task_id,
            author=author,
            summary=payload.summary,
            details=payload.details,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc
