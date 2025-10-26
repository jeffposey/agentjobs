"""Browser-facing routes delivering the AgentJobs web UI."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agentjobs.manager import TaskManager
from agentjobs.models import Task, TaskStatus

from ..dependencies import get_task_manager, get_templates

router = APIRouter(default_response_class=HTMLResponse, include_in_schema=False)


def _context_base() -> Dict[str, Any]:
    """Base context shared across templates."""
    return {
        "current_year": datetime.utcnow().year,
    }


def _sort_tasks_for_dashboard(tasks: List[Task]) -> List[Task]:
    """Sort active tasks to prioritise critical and recently updated work."""
    return sorted(
        (task for task in tasks if task.is_active()),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def _collect_recent_updates(tasks: List[Task]) -> List[Dict[str, Any]]:
    """Flatten task updates into a sorted list for the dashboard."""
    updates: List[Dict[str, Any]] = []
    for task in tasks:
        for update in task.status_updates:
            updates.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "timestamp": update.timestamp,
                    "summary": update.summary,
                    "author": update.author,
                }
            )
    updates.sort(key=lambda record: record["timestamp"], reverse=True)
    return updates[:10]


@router.get("/", name="dashboard")
async def dashboard(
    request: Request,
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the dashboard showing task statistics and recent updates."""
    tasks = manager.list_tasks()
    stats = {
        "total": len(tasks),
        "in_progress": sum(1 for task in tasks if task.status == TaskStatus.IN_PROGRESS),
        "blocked": sum(1 for task in tasks if task.status == TaskStatus.BLOCKED),
        "completed": sum(1 for task in tasks if task.status == TaskStatus.COMPLETED),
    }

    context = {
        "request": request,
        "stats": stats,
        "active_tasks": _sort_tasks_for_dashboard(tasks),
        "recent_updates": _collect_recent_updates(tasks),
        **_context_base(),
    }
    return templates.TemplateResponse("dashboard.html", context)


@router.get("/tasks", name="task_list")
async def task_list(
    request: Request,
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the searchable/filterable task list."""
    tasks = manager.list_tasks()
    tasks.sort(key=lambda task: (-task.updated.timestamp(), task.priority_rank()))

    context = {
        "request": request,
        "tasks": tasks,
        **_context_base(),
    }
    return templates.TemplateResponse("task_list.html", context)


@router.get("/tasks/{task_id}", name="task_detail")
async def task_detail(
    request: Request,
    task_id: str,
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the detailed view for a single task."""
    task = manager.get_task(task_id)
    if task is None:
        context = {
            "request": request,
            "task_id": task_id,
            **_context_base(),
        }
        return templates.TemplateResponse("404.html", context, status_code=status.HTTP_404_NOT_FOUND)

    context = {
        "request": request,
        "task": task,
        **_context_base(),
    }
    return templates.TemplateResponse("task_detail.html", context)
