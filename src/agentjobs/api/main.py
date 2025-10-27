"""FastAPI application setup for AgentJobs."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette import status

from agentjobs.__version__ import __version__

from .routes import (
    health_router,
    prompts_router,
    search_router,
    status_router,
    tasks_router,
    web_router,
    webhooks_router,
)

DESCRIPTION = (
    "REST API for interacting with AgentJobs tasks, including task "
    "management, status tracking, prompt coordination, and search."
)

app = FastAPI(
    title="AgentJobs API",
    description=DESCRIPTION,
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8765",
        "http://127.0.0.1:8765",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Any, exc: RequestValidationError) -> JSONResponse:
    """Return a concise 400 response for request validation failures."""
    detail = "Invalid request payload"
    errors = exc.errors()
    if errors:
        first = errors[0]
        field_path = ".".join(
            str(part)
            for part in first.get("loc", [])
            if part not in {"body", "query"}
        )
        message = first.get("msg")
        if field_path and message:
            detail = f"{field_path}: {message}"
        elif message:
            detail = message
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": detail},
    )


@app.get("/health", tags=["system"], include_in_schema=False)
async def root_health() -> dict[str, str]:
    """Root-level health endpoint for legacy consumers."""
    return {"status": "ok"}


app.include_router(health_router)
app.include_router(web_router)
app.include_router(tasks_router)
app.include_router(status_router)
app.include_router(prompts_router)
app.include_router(search_router)
app.include_router(webhooks_router)
