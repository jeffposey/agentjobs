"""Health and version endpoints for AgentJobs API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agentjobs.__version__ import __version__
from agentjobs.environment import describe_source
from agentjobs.models_v2 import SCHEMA_VERSION
from agentjobs.storage import yaml_loader_name

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
    yaml_loader: str = Field(
        description=(
            "Which YAML parser reads task files. The pure-Python fallback is about "
            "thirteen times slower than libyaml and is the usual explanation for a "
            "sluggish install, so it is reported rather than left to be guessed at."
        )
    )
    source_root: str = Field(
        description=(
            "Directory this process imported its own code from. Startup refuses when "
            "that is the wrong checkout, but the answer is reported here too: on a "
            "machine with several worktrees it is the difference between a stale "
            "server and a wrongly-installed one, and guessing costs a forensic session."
        )
    )


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
    return VersionResponse(
        version=__version__,
        schema_version=SCHEMA_VERSION,
        yaml_loader=yaml_loader_name(),
        source_root=describe_source(),
    )
