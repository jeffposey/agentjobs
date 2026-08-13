"""Read-only dashboard API consumed by the React client."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentjobs.dashboard import build_dashboard_snapshot
from agentjobs.manager import TaskManager

from ..dependencies import get_task_manager
from ..models import DashboardResponse

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    manager: TaskManager = Depends(get_task_manager),
) -> DashboardResponse:
    """Return the dashboard projection, including its single next action."""
    return DashboardResponse.model_validate(build_dashboard_snapshot(manager))
