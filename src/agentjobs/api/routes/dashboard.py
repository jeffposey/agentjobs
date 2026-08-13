"""Read-only dashboard API consumed by the React client."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from agentjobs.dashboard import build_dashboard_snapshot
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Task

from ..dependencies import get_task_manager
from ..models import DashboardResponse, TaskRead

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    manager: TaskManager = Depends(get_task_manager),
) -> DashboardResponse:
    """Return the dashboard projection, including its single next action."""
    snapshot = build_dashboard_snapshot(manager)
    facts = manager.dependency_facts()

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
        }
    )
