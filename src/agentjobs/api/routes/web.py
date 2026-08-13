"""Browser-facing routes delivering the AgentJobs web UI."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agentjobs.dashboard import awaits_human_input, blocks_human, build_dashboard_snapshot
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


def _nest_tasks(tasks: List[Task]) -> List[Dict[str, Any]]:
    """Order tasks so each parent is followed by its children, depth first.

    Returns one row per task, in the order the table should draw them, carrying what
    the template cannot work out for itself: how deep the row sits, the ancestors whose
    expansion governs its visibility, and how many children it has.

    Two shapes are handled deliberately rather than left to chance, because both would
    otherwise make rows *disappear* -- the worst way for a listing to report a data
    problem:

    - a `parent` naming a task this project does not have. The manager refuses to write
      one, but a hand-edited file can still carry it, so the task is drawn as a root.
    - a cycle. Nothing in a cycle is reachable from a root, so anything the walk did not
      emit is appended flat at the end.

    Top-level rows keep the order they arrive in, so the table's sort still decides
    where an umbrella sits. Children are ordered by id instead, matching `get_subtasks`
    and therefore the detail page: an umbrella's children are usually numbered stages,
    and drawing 054 above 050 because it was touched more recently reads as a shuffle.
    """
    by_id = {task.id: task for task in tasks}
    children: Dict[Optional[str], List[Task]] = {}
    for task in tasks:
        children.setdefault(task.parent if task.parent in by_id else None, []).append(task)
    for parent, siblings in children.items():
        if parent is not None:
            siblings.sort(key=lambda task: task.id)

    rows: List[Dict[str, Any]] = []

    def walk(task: Task, ancestors: List[str]) -> None:
        kids = children.get(task.id, [])
        rows.append(
            {
                "task": task,
                "depth": len(ancestors),
                "ancestors": ancestors,
                "child_count": len(kids),
                "open_children": sum(1 for kid in kids if kid.is_open),
            }
        )
        for kid in kids:
            if kid.id in ancestors or kid.id == task.id:
                continue
            walk(kid, [*ancestors, task.id])

    for root in children.get(None, []):
        walk(root, [])

    drawn = {row["task"].id for row in rows}
    for task in tasks:
        if task.id not in drawn:
            rows.append(
                {
                    "task": task,
                    "depth": 0,
                    "ancestors": [],
                    "child_count": 0,
                    "open_children": 0,
                }
            )
    return rows


def _child_rollup(children: List[Task]) -> Optional[Dict[str, Any]]:
    """What an umbrella's children add up to. Derived on read, never stored.

    An umbrella has no state of its own -- that is the point of it. Storing a rolled-up
    status on the parent would put two records in the position of knowing whether the
    effort is done, and they would drift the first time a child moved (design doc
    section 3, the same reasoning that makes `display_status` computed).

    "Complete" means closed *and* completed. A cancelled or superseded child is finished
    with, but counting it as complete would let an effort report itself done because
    half of it was abandoned.
    """
    if not children:
        return None
    closed = [child for child in children if child.lifecycle is Lifecycle.CLOSED]
    completed = [child for child in closed if child.outcome is Outcome.COMPLETED]
    other_closed = [child for child in closed if child.outcome is not Outcome.COMPLETED]
    open_children = [child for child in children if child.is_open]
    total = len(children)
    return {
        "total": total,
        "completed": len(completed),
        "other_closed": len(other_closed),
        "open": len(open_children),
        # Percentages of the whole, for the bar. Integers: a bar is not a measurement.
        "completed_pct": round(100 * len(completed) / total),
        "other_closed_pct": round(100 * len(other_closed) / total),
        # Who needs to do what next, named. A count tells you an umbrella is stuck; an
        # id tells you where to go.
        "waiting_on_human": [child for child in open_children if blocks_human(child)],
        "blocked": [child for child in open_children if child.ball is Ball.EXTERNAL],
        "in_flight": [child for child in open_children if child.lifecycle is Lifecycle.ACTIVE],
    }


def _get_blocking_tasks(tasks: List[Task]) -> List[Task]:
    """The alerting tier of the human inbox."""
    return sorted(
        (task for task in tasks if blocks_human(task)),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def _get_backlog_tasks(tasks: List[Task]) -> List[Task]:
    """The quiet tier: visible and counted, never styled as an alarm."""
    return sorted(
        (task for task in tasks if awaits_human_input(task)),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def get_waiting_count(manager: TaskManager) -> int:
    """The badge number: tasks where a person is actually holding work up."""
    return sum(1 for task in manager.list_tasks(ball=Ball.HUMAN) if blocks_human(task))


@router.get("", name="dashboard")
@router.get("/")
async def dashboard(
    request: Request,
    project: Project = Depends(get_project),
    manager: TaskManager = Depends(get_task_manager),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the dashboard showing task statistics and recent updates."""
    snapshot = build_dashboard_snapshot(manager)

    context = {
        "request": request,
        **snapshot,
        **_context_base(
            project=project,
            waiting_count=snapshot["stats"]["waiting_for_human"],
            broken_files=snapshot["broken_files"],
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
    # about a task, so one select can offer them side by side. "open" and "all" span
    # both axes and are offered alongside them.
    #
    # The default is "open", not "all". Closed tasks accumulate monotonically and
    # forever, so a default of "all" degrades toward archaeology as the repository
    # ages, and the question people actually ask -- "what is open?" -- was not even
    # on the menu. Closed work stays one deliberate click away.
    status_param = request.query_params.get("status", "").lower()
    valid_filters = (
        {"all", "open"} | {value.value for value in Lifecycle} | {value.value for value in Ball}
    )
    initial_status = status_param if status_param in valid_filters else "open"

    context = {
        "request": request,
        "tasks": tasks,
        "rows": _nest_tasks(tasks),
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

    children = manager.get_subtasks(task_id)
    context = {
        "request": request,
        "task": task,
        "children": children,
        "open_children": [child for child in children if child.is_open],
        "rollup": _child_rollup(children),
        # None when the id points at nothing: a dangling parent is refused on write, but
        # a file edited by hand can still carry one, and the page should show the task
        # rather than 500 over it.
        "parent_task": manager.get_task(task.parent) if task.parent else None,
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
                "waiting": sum(1 for task in tasks if blocks_human(task)),
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


@legacy_router.get("/projects/new", name="project_onboarding")
async def project_onboarding(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the deliberately small inspect-and-confirm onboarding form."""
    projects = list_projects()
    context = {
        "request": request,
        "current_year": datetime.utcnow().year,
        "waiting_count": 0,
        "project": try_resolve_default_project(),
        "base": "",
        "all_projects": projects,
    }
    return templates.TemplateResponse("project_onboarding.html", context)
