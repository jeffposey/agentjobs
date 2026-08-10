"""Project registry endpoints and the cross-project task view."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from agentjobs.models import Task, TaskStatus

from ..dependencies import list_projects, storage_for

router = APIRouter(prefix="/api", tags=["projects"])


def _describe(project: Any, task_count: Optional[int]) -> Dict[str, Any]:
    """Render a project as an API payload."""
    return {
        "id": project.id,
        "name": project.name,
        "root": str(project.root),
        "tasks_directory": str(project.tasks_dir()),
        "task_count": task_count,
    }


@router.get("/projects", response_model=List[Dict[str, Any]])
async def get_projects() -> List[Dict[str, Any]]:
    """List every project this server can serve, with task counts.

    A project whose directory has gone missing is reported with a null task_count
    rather than failing the whole listing -- the registry is machine-local and a
    checkout can legitimately disappear.
    """
    payload: List[Dict[str, Any]] = []
    for project in list_projects():
        try:
            count: Optional[int] = len(storage_for(project).list_tasks())
        except OSError:
            count = None
        payload.append(_describe(project, count))
    return payload


@router.get("/all/tasks", response_model=List[Dict[str, Any]])
async def get_all_tasks(
    status_filter: Optional[TaskStatus] = Query(default=None, alias="status"),
    project_filter: Optional[str] = Query(default=None, alias="project"),
) -> List[Dict[str, Any]]:
    """Every task across every project, each tagged with the project it belongs to.

    Mounted at ``/api/all/tasks`` rather than as a magic id under ``/api/tasks/``,
    because ``/api/tasks/all`` would be indistinguishable from a task whose id is
    literally "all" and would shadow it.

    Read-only by design. Writes always address one project explicitly, so there stays
    exactly one code path that mutates a file.
    """
    rows: List[Dict[str, Any]] = []
    for project in list_projects():
        if project_filter and project.id != project_filter:
            continue
        try:
            tasks: List[Task] = storage_for(project).list_tasks()
        except OSError:
            continue
        for task in tasks:
            if status_filter and task.status != status_filter:
                continue
            rows.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "task": task.model_dump(mode="json", exclude_none=True),
                }
            )
    return rows
