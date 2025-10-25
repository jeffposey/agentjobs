"""FastAPI application placeholder for AgentJobs."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="AgentJobs", version="0.1.0")


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Simple health check endpoint for development."""
    return {"status": "ok"}
