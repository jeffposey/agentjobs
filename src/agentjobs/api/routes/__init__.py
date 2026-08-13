"""Aggregated route exports for AgentJobs API."""

from __future__ import annotations

from .health import router as health_router
from .dashboard import router as dashboard_router
from .projects import router as projects_router
from .search import router as search_router
from .status import router as status_router
from .tasks import router as tasks_router
from .web import legacy_router as web_legacy_router
from .web import router as web_router
from .webhooks import router as webhooks_router

PROJECT_SCOPED_ROUTERS = (
    dashboard_router,
    tasks_router,
    status_router,
    search_router,
    webhooks_router,
)
"""Routers mounted twice: unscoped at /api, and again under /api/projects/{project_id}.

Their handlers resolve the project through `dependencies.request_project`, which reads
`project_id` from the request path when it is there and falls back to the default
project when it is not. Mounting one set of handlers at both prefixes is what keeps the
scoped and unscoped surfaces from drifting apart.
"""

__all__ = [
    "PROJECT_SCOPED_ROUTERS",
    "dashboard_router",
    "health_router",
    "projects_router",
    "search_router",
    "status_router",
    "tasks_router",
    "web_legacy_router",
    "web_router",
    "webhooks_router",
]
