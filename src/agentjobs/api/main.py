"""FastAPI application setup for AgentJobs."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette import status

from agentjobs.__version__ import __version__
from agentjobs.projects import ProjectError
from agentjobs.storage import TaskLoadError

from .routes import (
    PROJECT_SCOPED_ROUTERS,
    health_router,
    projects_router,
    web_legacy_router,
    web_router,
)
from .spa import register_spa

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
    # Origins stay explicit: the browser rejects "*" when allow_credentials is True.
    allow_origins=[
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        # Vite dev server for the React frontend.
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ProjectError)
async def handle_project_error(request: Any, exc: ProjectError) -> JSONResponse:
    """Turn a refused project or path into a 400 rather than a 500.

    ProjectError is raised for input the server declines to act on -- an id that does
    not resolve, a task path that escapes its project directory. That is a bad request,
    not a server fault, and it must not surface as a stack trace.
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(TaskLoadError)
async def handle_task_load_error(request: Any, exc: TaskLoadError) -> JSONResponse:
    """Report an unreadable task file instead of failing with a stack trace.

    422 rather than 500: the server is fine, one stored document is not, and the
    response says which file and which field so it can be fixed.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": f"Task file could not be loaded -- {exc}", "broken": exc.as_dict()},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Any, exc: RequestValidationError) -> JSONResponse:
    """Return a concise 400 response for request validation failures."""
    detail = "Invalid request payload"
    errors = exc.errors()
    if errors:
        first = errors[0]
        field_path = ".".join(
            str(part) for part in first.get("loc", []) if part not in {"body", "query"}
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
app.include_router(projects_router)

# Web pages are canonically project-scoped. The legacy router keeps the old
# unscoped URLs alive by redirecting into the resolved default project, so
# existing bookmarks work and there is one canonical URL per page.
app.include_router(web_router, prefix="/p/{project_id}")
app.include_router(web_legacy_router)

# Every task-facing router is mounted twice. The unscoped mount is registered first so
# existing callers, the CLI and the current GUI keep working against the default
# project; the scoped mount is the addressable form used across projects.
for _router in PROJECT_SCOPED_ROUTERS:
    app.include_router(_router, prefix="/api")
    app.include_router(_router, prefix="/api/projects/{project_id}")

# Registered last so the SPA catch-all cannot shadow the Jinja or API routers. Its
# asset mount is internally ordered before the shell fallback.
register_spa(app)
