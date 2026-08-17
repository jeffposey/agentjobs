"""Health and version endpoints for AgentJobs API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentjobs.__version__ import __version__
from agentjobs.models_v2 import SCHEMA_VERSION

router = APIRouter(prefix="/api", tags=["system"])


class VersionResponse(BaseModel):
    """What a client needs to decide whether it can talk to this service.

    Two independent numbers. ``version`` is the installed AgentJobs distribution and
    governs the shape of the REST surface; ``schema_version`` is the task-record
    schema and governs the shape of the documents that surface returns. A client can
    match one and not the other, so neither is derivable from the other.
    """

    version: str = Field(description="Installed AgentJobs package version.")
    schema_version: int = Field(description="Task record schema version served.")


@router.get("/health")
async def api_health_check() -> dict[str, str]:
    """Simple health check endpoint for API consumers."""
    return {"status": "ok"}


@router.get("/version", response_model=VersionResponse)
async def api_version() -> VersionResponse:
    """Report the versions a client must match before it starts issuing calls.

    Added for the MCP server's startup probe, which refuses to serve tools against a
    service it cannot understand. The version is already in ``/openapi.json``, but
    reading it there makes every client parse a large document to learn two fields.
    """
    return VersionResponse(version=__version__, schema_version=SCHEMA_VERSION)
