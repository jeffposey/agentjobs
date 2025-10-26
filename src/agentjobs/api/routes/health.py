"""Health check endpoint for AgentJobs API."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def api_health_check() -> dict[str, str]:
    """Simple health check endpoint for API consumers."""
    return {"status": "ok"}
