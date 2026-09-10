"""FastAPI dependency helpers for AgentJobs API.

Everything here used to be a ``maxsize=1`` cache resolved from the process working
directory, which is precisely what made AgentJobs single-project. The caches are now
keyed by project id, and the project comes from the request path (or, for the retained
unscoped routes, from the registry's default resolution).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from fastapi import HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from agentjobs.actors import Identity, human_identity
from agentjobs.exposure import Visibility, readable_by, visibility_of
from agentjobs.front_door import current_secret
from agentjobs.manager import TaskManager
from agentjobs.principals import Principal, Resolution, resolve_principal
from agentjobs.projects import (
    AmbiguousProjectError,
    Project,
    ProjectRegistry,
    UnknownProjectError,
)
from agentjobs.storage_config import load_storage_settings
from agentjobs.store_factory import (
    LOCAL_PROJECT_ID,
    TaskStoreBackend,
    local_database,
    open_store,
    server_process,
)
from agentjobs.webhooks import WebhookStorage
from agentjobs.webhooks import WebhookManager

TASKS_DIR_ENV = "AGENTJOBS_TASKS_DIR"
PROJECT_ROOT_ENV = "AGENTJOBS_PROJECT_ROOT"
_CONFIG_RELATIVE = Path(".agentjobs") / "config.yaml"
_TEMPLATES: Optional[Jinja2Templates] = None

_IMPLICIT_PROJECT_ID = LOCAL_PROJECT_ID
"""Id for the project implied by the environment rather than the registry.

When AGENTJOBS_TASKS_DIR or AGENTJOBS_PROJECT_ROOT is set, or when nothing is registered
at all, AgentJobs still serves the working directory the way it always did. That
single-project mode is modelled as one implicit project so the rest of the code has
exactly one shape to handle.

The leading underscore makes it illegal as a registry id (see `_ID_PATTERN` in
projects.py), so it can never collide with a real project. It must also be URL-safe:
this was "." until the web routes existed, and "/p/./tasks" normalises to "/p/tasks",
which silently broke single-project installs the moment pages became project-scoped.
"""


# ----- registry ---------------------------------------------------------------


@lru_cache(maxsize=1)
def get_registry() -> ProjectRegistry:
    """The machine-level project registry."""
    return ProjectRegistry()


def _env_override_active() -> bool:
    """True when the environment pins AgentJobs to one directory."""
    return bool(os.environ.get(TASKS_DIR_ENV) or os.environ.get(PROJECT_ROOT_ENV))


def _resolve_project_root() -> Path:
    """Resolve the project root directory for AgentJobs runtime."""
    root = os.environ.get(PROJECT_ROOT_ENV)
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd()


def _load_config(base_dir: Path) -> dict:
    """Load AgentJobs configuration from disk when present."""
    config_path = base_dir / _CONFIG_RELATIVE
    if not config_path.exists():
        return {}
    content = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(content) or {}


def _resolve_tasks_dir() -> Path:
    """Determine the tasks directory from env vars or configuration."""
    env_dir = os.environ.get(TASKS_DIR_ENV)
    if env_dir:
        path = Path(env_dir).expanduser()
        if not path.is_absolute():
            path = _resolve_project_root() / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    base_dir = _resolve_project_root()
    config = _load_config(base_dir)
    tasks_dir_value: Optional[str] = config.get("tasks_directory")
    if not tasks_dir_value:
        tasks_dir_value = "tasks"
    tasks_dir = Path(tasks_dir_value)
    if not tasks_dir.is_absolute():
        tasks_dir = base_dir / tasks_dir
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


def _implicit_project() -> Project:
    """The environment-implied project, for single-project mode."""
    root = _resolve_project_root()
    config = _load_config(root)
    return Project(
        id=_IMPLICIT_PROJECT_ID,
        name=config.get("project_name") or root.name,
        root=root,
    )


def list_projects() -> list[Project]:
    """Every project this server can serve.

    Registered projects when there are any and the environment is not pinning us to
    one directory; otherwise the single implicit project, so single-project installs
    behave exactly as they did before the registry existed.
    """
    if _env_override_active():
        return [_implicit_project()]
    registered = get_registry().list_projects()
    return registered if registered else [_implicit_project()]


def project_visibility(project: Project) -> Visibility:
    """Whether this project is served to remote principals. See `agentjobs.exposure`."""
    return visibility_of(project_config(project))


def project_visible_to(project: Project, principal: Optional[Principal]) -> bool:
    """True when this caller may be served this project at all."""
    return readable_by(project_visibility(project), principal)


def visible_projects(principal: Optional[Principal] = None) -> list[Project]:
    """Every project this caller may see, which for a local one is every project.

    The filtered form of :func:`list_projects`, and the one every cross-project surface
    calls. Passing ``None`` is not a way to see everything -- it is the answer for a
    request that resolved to nobody, and `exposure.readable_by` hides a local-only
    project from it.
    """
    return [project for project in list_projects() if project_visible_to(project, principal)]


def resolve_project(project_id: str, principal: Optional[Principal] = None) -> Project:
    """Resolve an explicit project id from a request path.

    A project this caller may not see answers **exactly** as an unregistered id does,
    down to the sentence: that is the ``_owned_run`` convention (``routes/dispatch.py``),
    and the reason is that a 403 here would confirm the hidden project exists to the one
    caller it is being hidden from. Every project-scoped route in the application
    resolves through here, so this one check is what makes a hidden project's tasks,
    runs, transcripts, webhooks, searches and queue absent together rather than one route
    at a time.
    """
    if project_id == _IMPLICIT_PROJECT_ID:
        project = _implicit_project()
    else:
        try:
            project = get_registry().get(project_id)
        except UnknownProjectError:
            raise _no_such_project(project_id, principal) from None
    if not project_visible_to(project, principal):
        raise _no_such_project(project_id, principal)
    return project


def _no_such_project(project_id: str, principal: Optional[Principal]) -> HTTPException:
    """The one 404 both "never registered" and "not yours to see" answer with.

    Assembled here rather than taken from ``UnknownProjectError`` because the registry's
    sentence names *every* registered id, which would disclose the hidden projects
    through the very response that is hiding them -- and because two sentences that are
    meant to be indistinguishable have to be built by one piece of code or they will
    eventually differ. The list is the caller's own visible set, so it stays as useful as
    it ever was to the person it is useful to.
    """
    known = ", ".join(project.id for project in visible_projects(principal)) or "none registered"
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unknown project {project_id!r}. Registered projects: {known}.",
    )


def try_resolve_default_project(principal: Optional[Principal] = None) -> Optional[Project]:
    """Resolve the default project, or None when it cannot be resolved without guessing.

    The non-raising form, so callers that want to offer a choice (the web project
    picker) do not have to catch an HTTPException to find out there is one.

    Note this must be used in preference to `ProjectRegistry.resolve_default` anywhere
    a default is wanted: the registry knows nothing about implicit single-project mode,
    so going straight to it reports "no projects" for an install that has one.

    ``principal`` filters the answer rather than the search: the default is resolved
    positionally, from where the server is running, and *then* checked. A caller who may
    not see the resolved default gets ``None`` -- there is no fallback to the next
    project it could see, because that would serve one project's tasks under a URL that
    named none, which is the guess this function exists to refuse.
    """
    project = _positional_default_project()
    if project is None or not project_visible_to(project, principal):
        return None
    return project


def _positional_default_project() -> Optional[Project]:
    """The default project before anybody asks who is looking at it."""
    if _env_override_active():
        return _implicit_project()
    try:
        return get_registry().resolve_default()
    except AmbiguousProjectError:
        registered = get_registry().list_projects()
        # Nothing registered at all still means the working directory, as it always did.
        return None if registered else _implicit_project()


def resolve_default_project(principal: Optional[Principal] = None) -> Project:
    """Resolve the project that unscoped routes act on.

    Raises 409 rather than guessing when several projects are registered and none
    contains the working directory: serving the wrong project's tasks silently is a
    worse outcome than an error that names the ambiguity.
    """
    project = try_resolve_default_project(principal)
    if project is None:
        # Name the candidates. An error that says only "ambiguous" leaves the caller
        # guessing at the very thing the server refused to guess at. The candidates are
        # this caller's visible ones: a hidden project is not an answer it could have
        # given, so naming it would be both useless and a disclosure.
        known = ", ".join(candidate.id for candidate in visible_projects(principal))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot resolve a default project: the working directory is inside none "
                f"of the registered projects ({known}). Address one explicitly, e.g. "
                "/api/projects/<id>/tasks."
            ),
        )
    return project


# ----- per-project components -------------------------------------------------
#
# Keyed by (project_id, path) rather than project_id alone: the id is what callers
# address, but the path is what the cache must actually be keyed on, so re-registering
# an id against a different directory cannot return the old directory's storage.


@lru_cache(maxsize=32)
def _storage_for(project_id: str, database: str) -> TaskStoreBackend:
    """Create a cached task store for one project.

    Keyed on the database path as well as the id, because the path is what an operator
    can change under a running server -- pointing a project at another file, or splitting
    it out of the machine's shared one. Without it in the key the server would keep
    serving the file it opened first while ``storage.yaml`` said otherwise.
    """
    project = get_registry().as_dict().get(project_id)
    if project is None:
        # The implicit single-project mode, and any project resolved from the
        # environment rather than the registry. It has no registry entry to carry a
        # database path, so the path was resolved from its directory instead.
        project = _implicit_project()
    # Held here rather than relied on from import time. This module *is* the server's,
    # so it is entitled to the declaration -- and taking it explicitly means the store
    # opens correctly however the application was constructed, instead of depending on
    # a module-level side effect that a host or a test runner can have undone.
    with server_process():
        return open_store(project, database=Path(database))


@lru_cache(maxsize=32)
def _webhook_storage_for(project_id: str, webhooks_path: str) -> WebhookStorage:
    """Create a cached WebhookStorage for one project."""
    return WebhookStorage(Path(webhooks_path))


@lru_cache(maxsize=32)
def _webhook_manager_for(project_id: str, webhooks_path: str) -> WebhookManager:
    """Create a cached WebhookManager for one project."""
    return WebhookManager(_webhook_storage_for(project_id, webhooks_path))


def _tasks_dir_for(project: Project) -> Path:
    """Resolve a project's tasks directory, honouring the env override.

    Still resolved for a project whose records are rows, and deliberately: it is where
    ``agentjobs storage export`` writes, and in the implicit single-project mode it is
    what names the project's database.

    The directory is not created here. A project that has never held task files should
    not have an empty ``tasks/`` conjured into its checkout by the act of serving it.
    """
    if project.id == _IMPLICIT_PROJECT_ID:
        return _resolve_tasks_dir()
    return project.tasks_dir()


def _database_for(project: Project) -> Path:
    """Which file this project's rows are in.

    Registered projects are answered by the machine's storage configuration. The
    implicit project is not in it -- it is a directory somebody pointed the server at --
    so its file is named from that directory, which is the only identity it has.
    """
    if project.id == _IMPLICIT_PROJECT_ID:
        return local_database(_resolve_tasks_dir())
    return load_storage_settings().database_for(project.id)


def storage_for(project: Project) -> TaskStoreBackend:
    """The task store scoped to one project."""
    return _storage_for(project.id, str(_database_for(project)))


def webhook_manager_for(project: Project) -> WebhookManager:
    """WebhookManager scoped to one project."""
    return _webhook_manager_for(project.id, str(project.webhooks_path()))


def manager_for(project: Project) -> TaskManager:
    """TaskManager scoped to one project."""
    return TaskManager(storage_for(project), webhook_manager_for(project))


# ----- FastAPI dependencies ---------------------------------------------------


def request_project(request: Request) -> Project:
    """Resolve the project a request addresses.

    Each API router is mounted twice -- once unscoped at ``/api`` and once at
    ``/api/projects/{project_id}`` -- so the same handlers serve both. Reading the
    project from the path parameters here is what makes that possible: one dependency,
    one set of routes, and no duplicated handler bodies to drift apart.

    It is also the single place a project's exposure is enforced for scoped routes
    (task-333). Every handler behind both mounts asks this for its project, so a project
    a caller may not see is absent from all of them at once rather than from whichever
    ones somebody remembered to check.
    """
    principal = get_principal(request)
    project_id = request.path_params.get("project_id")
    if project_id:
        return resolve_project(str(project_id), principal)
    return resolve_default_project(principal)


def get_project(request: Request) -> Project:
    """Provide the addressed project to a route."""
    return request_project(request)


def get_task_manager(request: Request) -> TaskManager:
    """Provide a TaskManager scoped to the project this request addresses."""
    return manager_for(request_project(request))


def get_task_storage(request: Request) -> TaskStoreBackend:
    """Provide task storage scoped to the project this request addresses."""
    return storage_for(request_project(request))


def get_webhook_manager(request: Request) -> WebhookManager:
    """Provide a WebhookManager scoped to the project this request addresses."""
    return webhook_manager_for(request_project(request))


def project_config(project: Project) -> dict:
    """The config of the project a request addresses.

    Read per call rather than cached: config is hand-edited, and a stale actor list
    after adding yourself to it is exactly the kind of "why is it still wrong" that
    costs an hour. It is one small YAML file.
    """
    if project.id == _IMPLICIT_PROJECT_ID:
        return _load_config(_resolve_project_root())
    return _load_config(project.root)


def current_identity(project: Project, principal: Optional[Principal] = None) -> Identity:
    """Who the GUI acts as for this project, or why it cannot tell.

    ``principal`` is what makes this per request rather than per project (task-330): a
    remote caller's proven login resolves through the machine's identity registry, so
    two people can use one dashboard and each be recorded as themselves. Omitted, it
    resolves the machine owner, which is the right answer for a caller with no request
    behind it -- the CLI, a script -- and is what every call site did before principals
    existed.
    """
    return human_identity(project_config(project), principal)


def current_user(project: Project, principal: Optional[Principal] = None) -> Optional[str]:
    """The actor id the GUI acts as for this project, or None if unresolvable."""
    return current_identity(project, principal).user


def request_identity(request: Request) -> Identity:
    """Who this request acts as, resolved from its own principal."""
    return current_identity(request_project(request), get_principal(request))


def get_current_user(request: Request) -> Optional[str]:
    """Provide the acting user to a route."""
    return request_identity(request).user


def get_templates() -> Jinja2Templates:
    """Provide templates for the legacy server-rendered compatibility routes."""
    global _TEMPLATES
    if _TEMPLATES is None:
        template_dir = Path(__file__).parent / "templates"
        _TEMPLATES = Jinja2Templates(directory=str(template_dir))
    return _TEMPLATES


def reset_dependency_cache() -> None:
    """Clear cached storage when environment configuration changes."""
    get_registry.cache_clear()
    _storage_for.cache_clear()
    _webhook_storage_for.cache_clear()
    _webhook_manager_for.cache_clear()
    global _TEMPLATES
    _TEMPLATES = None


# ----- who is asking -----------------------------------------------------------
#
# Resolution only. Nothing here refuses a request; `api.authorization.enforce_capability`
# reads what this resolves and is the one place that does. See agentjobs.principals and
# agentjobs.capabilities.

PRINCIPAL_STATE_ATTR = "principal_resolution"
"""Where the middleware stashes the request's resolution.

On ``request.state`` rather than in a context variable so that anything with the
request in hand -- a handler, an exception handler, a future audit record -- can name
who was asking without a second resolution and without a global.
"""


def resolve_request_principal(request: Request) -> Resolution:
    """Resolve who is asking, from the socket the request arrived on and its headers.

    The adapter, and deliberately the whole of it: the trust rule lives in
    ``agentjobs.principals`` where it can be tested without a transport. ``request.client``
    is the immediate peer -- the socket, which cannot be forwarded or rewritten -- and
    is why a header claiming an identity is only believed when it arrives by the path
    the front door controls.

    The secret is read here rather than held as a module-level slot the way the run
    credential verifier is (task-244). A slot would have to be installed at import,
    which fixes the answer to whatever was true when the server started -- so a server
    started before the proxy would treat every remote caller as the owner until somebody
    restarted it. :func:`agentjobs.front_door.current_secret` caches the file read, so
    reading it per request costs a clock comparison and makes start order irrelevant.
    """
    client = request.client
    return resolve_principal(
        client_host=client.host if client is not None else None,
        headers=request.headers,
        front_door_secret=current_secret(),
    )


def get_principal_resolution(request: Request) -> Resolution:
    """The resolution for this request, resolved once.

    Falls back to resolving on the spot when the middleware did not run, so a route
    exercised through a bare ASGI call or a partially-wired test app still gets an
    answer rather than a default one.
    """
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Resolution):
        return cached
    return resolve_request_principal(request)


def get_principal(request: Request) -> Optional[Principal]:
    """The principal this request resolved to, or ``None`` when none did.

    ``None`` is the honest answer and not a defect: a caller with no proven identity
    has no principal, and substituting one would be the silent default task-064
    removed.
    """
    return get_principal_resolution(request).principal
