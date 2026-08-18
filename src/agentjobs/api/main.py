"""FastAPI application setup for AgentJobs."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette import status

from agentjobs.__version__ import __version__
from agentjobs.instrumentation import reset_task_parses, task_parse_count
from agentjobs.projects import ProjectError, default_home
from agentjobs.storage import TaskLoadError, corpus_snapshot

from .routes import (
    PROJECT_SCOPED_ROUTERS,
    health_router,
    projects_router,
    web_legacy_router,
    web_router,
)
from .routes.status import MutationError, mutation_error_response
from .spa import register_spa

DESCRIPTION = (
    "REST API for interacting with AgentJobs tasks, including task "
    "management, status tracking, prompt coordination, and search."
)


def _reconcile_dispatch_runs() -> None:
    """Settle runs left behind by a previous process, at startup.

    This is what makes "a crashed run does not disappear silently" true rather than
    aspirational: a batch run still marked live means its supervisor died with the
    process that owned it, and it becomes an ``interrupted`` entry on its task with the
    ball handed to a human. A live *session* is deliberately the opposite -- it outlives
    us on purpose, so it is re-attached and left alone.

    Failures here are reported and never fatal. A server that refuses to start because
    it could not tidy up is worse than one that starts with the tidying undone, and the
    run directories are still on disk to reconcile next time.
    """
    from agentjobs.dispatch.ledger import DispatchLedger, LedgerError

    try:
        results = DispatchLedger(default_home()).reconcile()
    except (LedgerError, OSError) as exc:  # pragma: no cover - defensive
        print(f"Dispatch reconciliation skipped: {exc}", flush=True)
        return
    for result in results:
        print(f"Dispatch reconcile {result.run_id}: {result.detail}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run startup reconciliation once, before the first request is served."""
    _reconcile_dispatch_runs()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="AgentJobs API",
    description=DESCRIPTION,
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)

MEASUREMENT_HEADER = "X-Response-Time-Ms"
PARSE_COUNT_HEADER = "X-Task-Parses"


@app.middleware("http")
async def measure_request(request: Any, call_next: Any) -> Any:
    """Report how long a request took and how many task files it parsed.

    Two headers, on every response:

    - ``X-Response-Time-Ms`` -- wall time inside the application.
    - ``X-Task-Parses`` -- task files read and parsed from disk while serving it.

    The parse count is the more useful of the two. It says *why* a request was slow
    without attaching a profiler, and unlike a millisecond figure it means the same
    thing on a fast laptop and a loaded CI box: a request that parses a 112-file
    corpus four times is doing four times too much work on any hardware.

    The counter is reset per request rather than read as a running total, because a
    long-lived server would otherwise report a number that only ever grows.
    """
    reset_task_parses()
    started = time.perf_counter()
    # One parse of the corpus per request. The scope is entered here, around the whole
    # request, because that is the widest window in which the answer has to be
    # self-consistent and the narrowest one that fixes the repeated walks -- see
    # storage.corpus_snapshot for why it is not process-wide.
    with corpus_snapshot():
        response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers[MEASUREMENT_HEADER] = f"{elapsed_ms:.1f}"
    response.headers[PARSE_COUNT_HEADER] = str(task_parse_count())
    return response


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
    # Without this the browser hides the measurement headers from page scripts, so a
    # frontend served from the Vite dev server could not read its own timings.
    expose_headers=[MEASUREMENT_HEADER, PARSE_COUNT_HEADER],
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


@app.exception_handler(MutationError)
async def handle_mutation_error(request: Any, exc: MutationError) -> JSONResponse:
    """Return the structured refusal a mutation raised.

    Registered as a handler rather than caught per route, so all six mutation
    endpoints report failure in one shape without repeating a try/except six times.
    """
    return await mutation_error_response(request, exc)


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


def project_id_contract(project_id: str) -> str:
    """Expose the shared scoped-router path parameter to FastAPI and OpenAPI."""
    return project_id


# Every task-facing router is mounted twice. The unscoped mount is registered first so
# existing callers, the CLI and the current GUI keep working against the default
# project; the scoped mount is the addressable form used across projects.
for _router in PROJECT_SCOPED_ROUTERS:
    app.include_router(_router, prefix="/api")
    app.include_router(
        _router,
        prefix="/api/projects/{project_id}",
        dependencies=[Depends(project_id_contract)],
    )

# Registered last so the React catch-all cannot shadow the API or legacy Jinja
# compatibility routers. Its asset mount is internally ordered before the shell fallback.
register_spa(app)
