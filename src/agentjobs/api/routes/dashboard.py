"""Read-only dashboard API consumed by the React client."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from agentjobs.dashboard import build_dashboard_snapshot, count_blocking_human
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Task
from agentjobs.principals import Principal
from agentjobs.projects import Project

from ..dependencies import current_identity, get_project, get_principal, get_task_manager
from ..models import AttentionResponse, DashboardResponse, ReviewIdentity, TaskRead

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_project),
    principal: Optional[Principal] = Depends(get_principal),
) -> DashboardResponse:
    """Return the dashboard projection, including its single next action.

    ``identity`` rides along for the same reason it rides along on the task detail and
    the playbook listing: the next-up panel offers a Dispatch button, a run has to be
    attributed to a real person, and a button that cannot name one must be disabled with
    the reason rather than pressable into a refusal.
    """
    snapshot = build_dashboard_snapshot(manager)
    facts = manager.dependency_facts()
    identity = current_identity(project, principal)

    def read(task: Optional[Task]) -> Optional[TaskRead]:
        return TaskRead.from_task(task, facts[task.id]) if task is not None else None

    def reads(tasks: list[Task]) -> list[TaskRead]:
        return [TaskRead.from_task(task, facts[task.id]) for task in tasks]

    return DashboardResponse(
        **{
            **snapshot,
            "active_tasks": reads(snapshot["active_tasks"]),
            "waiting_tasks": reads(snapshot["waiting_tasks"]),
            "backlog_tasks": reads(snapshot["backlog_tasks"]),
            "next_task": read(snapshot["next_task"]),
            "queue_preview": reads(snapshot["queue_preview"]),
            "identity": ReviewIdentity(
                ok=identity.ok,
                user=identity.user,
                problem=identity.problem,
                detail=identity.detail,
            ),
        }
    )


@router.get("/attention", response_model=AttentionResponse)
async def get_attention(
    manager: TaskManager = Depends(get_task_manager),
) -> AttentionResponse:
    """Return how many tasks are stopped waiting on a person, and nothing else.

    The header polls this on every surface, so it deliberately answers with one
    integer instead of the records behind it -- see :class:`AttentionResponse`. It
    is the same predicate the dashboard's own tile and the legacy header use, from
    the same function, because a badge that disagrees with the page it links to is
    worse than no badge.
    """
    return AttentionResponse(blocking=count_blocking_human(manager))
