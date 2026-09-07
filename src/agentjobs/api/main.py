"""FastAPI application setup for AgentJobs."""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette import status

from agentjobs.__version__ import __version__
from agentjobs.environment import (
    SourceMismatchError,
    capture_source_identity,
    verify_source_or_die,
)
from agentjobs.instrumentation import reset_task_parses, task_parse_count
from agentjobs.projects import ProjectError, ProjectRegistry, default_home
from agentjobs.dispatch.credentials import verify_run_credential
from agentjobs.principals import set_run_credential_verifier
from agentjobs.storage import TaskLoadError, corpus_snapshot
from agentjobs.store_factory import close_databases, mark_server_process

from .authorization import Forbidden, enforce_capability
from .dependencies import PRINCIPAL_STATE_ATTR, resolve_request_principal
from .routes import (
    PROJECT_SCOPED_ROUTERS,
    health_router,
    projects_router,
    runs_router,
    web_legacy_router,
    web_router,
)
from .routes.status import MutationError, mutation_error_response
from .spa import register_spa

if TYPE_CHECKING:  # pragma: no cover - the runtime import stays inside the function
    from agentjobs.dispatch.ledger import DispatchLedger

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

    ledger = DispatchLedger(default_home())
    try:
        results = ledger.reconcile()
    except (LedgerError, OSError) as exc:  # pragma: no cover - defensive
        print(f"Dispatch reconciliation skipped: {exc}", flush=True)
        return
    for result in results:
        print(f"Dispatch reconcile {result.run_id}: {result.detail}", flush=True)
    _reap_finished_sessions(ledger)


def _reap_finished_sessions(ledger: "DispatchLedger") -> None:
    """Remove the job state of sessions that have already ended.

    A finished run still holds a pid in the session manager's ledger, and that is what
    this clears. Startup is where it happens, and it stays here now that a scheduler does
    exist: the poller below reaps each session as it settles it, so this pass only ever
    finds what was left behind by a process that died. Putting a second sweep on the
    interval would spawn processes to look for litter that has already been collected.
    `agentjobs dispatch reap` remains the on-demand form.

    (This docstring used to argue that no scheduler should exist at all. That was right
    about deleting directories and wrong about session state -- see the poller. It also
    used to say this removed worktrees, which stopped being true with task-186: dispatch
    no longer passes `-w`, so a dispatched session owns no worktree. See
    `DispatchLedger.reap`.)

    A **refused** reap is still printed rather than swallowed, because the reason for one
    is never obvious from here.
    """
    from agentjobs.dispatch.ledger import LedgerError

    try:
        results = ledger.reap_finished()
    except (LedgerError, OSError) as exc:  # pragma: no cover - defensive
        print(f"Dispatch reaping skipped: {exc}", flush=True)
        return
    for result in results:
        verb = "reaped" if result.stopped else "kept"
        print(f"Dispatch {verb} {result.run_id}: {result.detail}", flush=True)


def _verify_served_source() -> None:
    """Stop before serving anything if this process imported the wrong checkout.

    The editable install on this interpreter can point at a checkout that is not the one
    being served -- a worktree's `poetry install` rewrites it whenever `VIRTUAL_ENV` is
    set (task-194). Nothing downstream notices: the task files are read from the right
    place, `git log` in the served clone is correct, and every behaviour comes from an
    unmerged branch.

    This refuses rather than warns, because the whole character of the failure is that it
    is quiet. A dashboard that will not start is a five-second fix with the command in the
    error; a dashboard serving someone's branch cost a forensic session to even notice.
    The banner is printed as well as raised, since uvicorn's startup traceback is not
    where anyone looks first.
    """
    roots = [project.root for project in ProjectRegistry(default_home()).list_projects()]
    try:
        verify_source_or_die(roots)
    except SourceMismatchError as exc:
        print("\n" + "=" * 78, file=sys.stderr)
        print(exc, file=sys.stderr)
        print("=" * 78 + "\n", file=sys.stderr, flush=True)
        raise


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Reconcile once at startup, then follow live sessions for as long as we serve.

    The poller is the missing half of session dispatch. ``poll_session`` has always known
    what a session's state means and what to do about it, and nothing ever called it, so
    a session ran, finished, and left its run reading ``running`` for ever with no
    ``dispatch_result`` on its task (task-157).

    It is cancelled on shutdown and awaited: a poll caught mid-flight has already written
    whatever it decided, and letting the task be garbage-collected instead produces a
    "Task was destroyed but it is pending" line that looks like a fault and is not.
    """
    from agentjobs.dispatch.poller import poll_sessions_forever

    _verify_served_source()
    # Fix which code this process is running *before* it serves anything. Captured here
    # rather than on demand because the whole value of the answer is that a merge into
    # the served clone cannot change it -- see `capture_source_identity` (task-241).
    capture_source_identity()
    poller = asyncio.create_task(poll_sessions_forever(default_home()))
    _reconcile_dispatch_runs()
    try:
        yield
    finally:
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller
        # Let SQLite checkpoint the WAL and run PRAGMA optimize now rather than
        # leaving both to the next start. A restart is meant to be a pause a client
        # rides through, and a store that has to recover on open makes it longer.
        close_databases()


# Who a run credential proves you to be, installed over the verifier that verifies
# nothing (task-331). This is the line that splits loopback in two: until it runs, every
# local caller is the owner, including a dispatched agent. At import rather than in the
# lifespan so a TestClient that never enters the lifespan resolves runs the same way a
# served process does.
set_run_credential_verifier(verify_run_credential)

# This process is the one that may open the task database (task-273, entry 6). Declared
# at import rather than in the lifespan for the same reason as the line above: a
# TestClient that never enters the lifespan is still serving the application, and would
# otherwise be refused its own store.
#
# Importing this module *is* the declaration, and that is exact rather than approximate:
# the CLI names the app as a uvicorn import string, so the only processes that import it
# are the server and the tools that mount it in-process.
mark_server_process()

app = FastAPI(
    lifespan=lifespan,
    title="AgentJobs API",
    description=DESCRIPTION,
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    # The capability gate, installed once for the whole application rather than route by
    # route (task-332). Application-wide is what makes "no route left un-checked by
    # oversight" structural: a route added tomorrow is covered by this the moment it is
    # registered, and `tests/test_authorization.py` fails until somebody says which
    # capability it needs. A dependency rather than middleware, because middleware runs
    # before routing and would know neither the matched endpoint nor the path params.
    dependencies=[Depends(enforce_capability)],
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


@app.middleware("http")
async def resolve_principal_for_request(request: Any, call_next: Any) -> Any:
    """Resolve who is asking, once, before any handler runs.

    Middleware rather than a route dependency because "every request" is what the
    account system (task-066) is built on, and a dependency list covers routes rather
    than everything the application serves. Resolving here also means one answer per
    request: a handler and a future audit record cannot disagree about who was asking.

    **It only records.** Nothing is refused here, and nothing ever will be: the
    refusing is done by ``enforce_capability`` in ``api.authorization``, which reads
    what this stashed. Keeping resolution and enforcement apart is what makes a mistake
    in resolution show up as a wrong principal in a test rather than as a locked-out
    dashboard.
    """
    setattr(request.state, PRINCIPAL_STATE_ATTR, resolve_request_principal(request))
    return await call_next(request)


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


@app.exception_handler(Forbidden)
async def handle_forbidden(request: Any, exc: Forbidden) -> JSONResponse:
    """Render a capability or identity refusal, code and sentence both.

    The code is in the body rather than only in the status because 403 alone cannot tell
    "your credential is for another run" from "no principal holds this at all", and the
    two need different responses from whoever hit them. ``detail`` is carried as well as
    the code so every existing client -- which reads ``detail`` and nothing else -- shows
    the sentence rather than an empty box.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.body())


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
# Mounted once, unlike everything in PROJECT_SCOPED_ROUTERS: what is running on this
# machine is not a fact about any one project, and serving the same body under every
# spelling of /api/projects/{id}/... would be a URL asserting a scope the answer does
# not have (task-328).
app.include_router(runs_router)

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
