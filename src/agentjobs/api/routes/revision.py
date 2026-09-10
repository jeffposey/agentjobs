"""Cheap project revision API consumed by the React live-update controller."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentjobs.store_factory import TaskStoreBackend

from ..dependencies import get_task_storage
from ..models import ProjectRevisionResponse

router = APIRouter(tags=["live updates"])


@router.get("/revision", response_model=ProjectRevisionResponse)
async def get_project_revision(
    storage: TaskStoreBackend = Depends(get_task_storage),
) -> ProjectRevisionResponse:
    """Answer whether this project changed, without loading the task collection."""
    revision, task_count = storage.project_revision()
    return ProjectRevisionResponse(revision=revision, task_count=task_count)
