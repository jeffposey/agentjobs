"""Aggregated route exports for AgentJobs API."""

from __future__ import annotations

from .health import router as health_router
from .prompts import router as prompts_router
from .search import router as search_router
from .status import router as status_router
from .tasks import router as tasks_router
from .web import router as web_router

__all__ = [
    "health_router",
    "prompts_router",
    "search_router",
    "status_router",
    "tasks_router",
    "web_router",
]
