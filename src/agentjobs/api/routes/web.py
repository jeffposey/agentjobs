"""Browser-facing routes delivering the AgentJobs web UI."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, Lifecycle, Outcome, Task
from agentjobs.projects import Project

from ..dependencies import (
    current_identity,
    get_project,
    get_task_manager,
    get_templates,
    list_projects,
    storage_for as _storage_for_picker,
    try_resolve_default_project,
)

router = APIRouter(default_response_class=HTMLResponse, include_in_schema=False)
"""Project-scoped pages. Mounted at /p/{project_id}; every page lives under a project."""

legacy_router = APIRouter(default_response_class=HTMLResponse, include_in_schema=False)
"""Unscoped URLs kept working. They redirect into the scoped form rather than rendering,
so there is one canonical URL per page and a bookmarked link never quietly shows a
different project than it did last time."""


def _context_base(
    *,
    project: Project,
    waiting_count: int = 0,
    broken_files: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Base context shared across templates.

    ``base`` is the URL prefix every in-page link must be built from. Templates use it
    instead of hardcoding "/tasks/...", because a task id is unique only within a
    project -- an unscoped link would silently open a different project's task.
    """
    return {
        "current_year": datetime.utcnow().year,
        "waiting_count": waiting_count,
        "project": project,
        "base": f"/p/{project.id}",
        "all_projects": list_projects(),
        "broken_files": broken_files or [],
        # Who the review buttons act as, and why not when they cannot. The template
        # surfaces the reason rather than silently falling back to an anonymous id.
        "identity": current_identity(project),
        "current_user": current_identity(project).user,
    }


def _sort_tasks_for_dashboard(tasks: List[Task]) -> List[Task]:
    """Sort in-flight tasks to prioritise critical and recently updated work."""
    return sorted(
        (task for task in tasks if task.lifecycle in (Lifecycle.READY, Lifecycle.ACTIVE)),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def _collect_recent_updates(tasks: List[Task]) -> List[Dict[str, Any]]:
    """Flatten log entries into a sorted list for the dashboard."""
    updates: List[Dict[str, Any]] = []
    for task in tasks:
        for entry in task.log:
            body = (entry.body or "").strip()
            updates.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "timestamp": entry.ts,
                    "summary": body.splitlines()[0] if body else entry.type.value,
                    "author": entry.actor,
                }
            )
    updates.sort(key=lambda record: record["timestamp"], reverse=True)
    return updates[:10]


def _get_waiting_tasks(tasks: List[Task]) -> List[Task]:
    """The human inbox: every open task whose ball a person holds."""
    waiting = [task for task in tasks if task.ball is Ball.HUMAN]
    return sorted(
        waiting,
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def get_waiting_count(manager: TaskManager) -> int:
    """Count tasks waiting for human attention."""
    return len(manager.list_tasks(ball=Ball.HUMAN))


@router.get("", name="dashboard")
@router.get("/")
async def dashboard(
    request: Request,
    project: Project = Depends(get_project),
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the dashboard showing task statistics and recent updates."""
    tasks = manager.list_tasks()
    waiting_tasks = _get_waiting_tasks(tasks)
    stats = {
        "total": len(tasks),
        "in_progress": sum(
            1 for task in tasks if task.lifecycle is Lifecycle.ACTIVE and task.ball is Ball.AGENT
        ),
        "blocked": sum(1 for task in tasks if task.ball is Ball.EXTERNAL),
        "waiting_for_human": len(waiting_tasks),
        "completed": sum(1 for task in tasks if task.outcome is Outcome.COMPLETED),
    }
    waiting_count = len(waiting_tasks)

    context = {
        "request": request,
        "stats": stats,
        "active_tasks": _sort_tasks_for_dashboard(tasks),
        "recent_updates": _collect_recent_updates(tasks),
        "waiting_tasks": waiting_tasks,
        **_context_base(
            project=project,
            waiting_count=waiting_count,
            broken_files=[e.as_dict() for e in manager.load_errors()],
        ),
    }
    return templates.TemplateResponse("dashboard.html", context)


@router.get("/tasks", name="task_list")
async def task_list(
    request: Request,
    project: Project = Depends(get_project),
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the searchable/filterable task list."""
    tasks = manager.list_tasks()
    tasks.sort(key=lambda task: (-task.updated.timestamp(), task.priority_rank()))
    waiting_count = get_waiting_count(manager)

    # The filter accepts a lifecycle or a ball holder; both are single-valued facts
    # about a task, so one select can offer them side by side.
    status_param = request.query_params.get("status", "").lower()
    valid_filters = {value.value for value in Lifecycle} | {value.value for value in Ball}
    initial_status = status_param if status_param in valid_filters else "all"

    context = {
        "request": request,
        "tasks": tasks,
        "initial_status": initial_status,
        **_context_base(
            project=project,
            waiting_count=waiting_count,
            broken_files=[e.as_dict() for e in manager.load_errors()],
        ),
    }
    return templates.TemplateResponse("task_list.html", context)


@router.get("/tasks/{task_id}", name="task_detail")
async def task_detail(
    request: Request,
    task_id: str,
    project: Project = Depends(get_project),
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the detailed view for a single task."""
    task = manager.get_task(task_id)
    if task is None:
        context = {
            "request": request,
            "task_id": task_id,
            **_context_base(project=project, waiting_count=get_waiting_count(manager)),
        }
        return templates.TemplateResponse(
            "404.html", context, status_code=status.HTTP_404_NOT_FOUND
        )

    context = {
        "request": request,
        "task": task,
        **_context_base(project=project, waiting_count=get_waiting_count(manager)),
    }
    return templates.TemplateResponse("task_detail.html", context)


# ----- unscoped URLs -----------------------------------------------------------
#
# These existed before projects did. They redirect into the scoped form rather than
# rendering, so a bookmark always resolves to a named project and there is exactly one
# canonical URL per page.


def _redirect_to_default(path: str) -> RedirectResponse:
    """Redirect an unscoped path into the default project, or to the picker.

    307 rather than 302: the redirect target is resolved per request from the working
    directory and the registry, so it is not a permanent property of the URL and must
    not be cached by the browser.
    """
    project = try_resolve_default_project()
    if project is None:
        return RedirectResponse(url="/projects", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return RedirectResponse(
        url=f"/p/{project.id}{path}", status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )


@legacy_router.get("/", name="legacy_dashboard")
async def legacy_dashboard() -> RedirectResponse:
    """Redirect the bare root into the default project."""
    return _redirect_to_default("")


@legacy_router.get("/tasks", name="legacy_task_list")
async def legacy_task_list(request: Request) -> RedirectResponse:
    """Redirect the unscoped task list, preserving any query string."""
    suffix = f"?{request.url.query}" if request.url.query else ""
    return _redirect_to_default(f"/tasks{suffix}")


@legacy_router.get("/tasks/{task_id}", name="legacy_task_detail")
async def legacy_task_detail(task_id: str) -> RedirectResponse:
    """Redirect an unscoped task link into the default project."""
    return _redirect_to_default(f"/tasks/{task_id}")


@legacy_router.get("/projects", name="project_picker")
async def project_picker(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Pick a project.

    Shown when no default can be resolved -- several projects are registered and the
    working directory is inside none of them. Guessing would mean silently showing
    someone another project's work, so this asks instead.
    """
    projects = list_projects()
    summaries = []
    for project in projects:
        try:
            tasks = TaskManager(_storage_for_picker(project)).list_tasks()
            counts = {
                "total": len(tasks),
                "waiting": sum(1 for task in tasks if task.ball is Ball.HUMAN),
            }
        except OSError:
            counts = {"total": 0, "waiting": 0}
        summaries.append({"project": project, "counts": counts})

    context = {
        "request": request,
        "summaries": summaries,
        "current_year": datetime.utcnow().year,
        "waiting_count": 0,
        "project": None,
        "base": "",
        "all_projects": projects,
    }
    return templates.TemplateResponse("project_picker.html", context)
