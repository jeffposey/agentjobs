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

from agentjobs.manager import TaskManager
from agentjobs.projects import (
    AmbiguousProjectError,
    Project,
    ProjectRegistry,
    UnknownProjectError,
)
from agentjobs.storage import TaskStorage, WebhookStorage
from agentjobs.webhooks import WebhookManager

TASKS_DIR_ENV = "AGENTJOBS_TASKS_DIR"
PROJECT_ROOT_ENV = "AGENTJOBS_PROJECT_ROOT"
_CONFIG_RELATIVE = Path(".agentjobs") / "config.yaml"
_TEMPLATES: Optional[Jinja2Templates] = None

_IMPLICIT_PROJECT_ID = "_local"
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


def resolve_project(project_id: str) -> Project:
    """Resolve an explicit project id from a request path."""
    if project_id == _IMPLICIT_PROJECT_ID:
        return _implicit_project()
    try:
        return get_registry().get(project_id)
    except UnknownProjectError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def try_resolve_default_project() -> Optional[Project]:
    """Resolve the default project, or None when it cannot be resolved without guessing.

    The non-raising form, so callers that want to offer a choice (the web project
    picker) do not have to catch an HTTPException to find out there is one.

    Note this must be used in preference to `ProjectRegistry.resolve_default` anywhere
    a default is wanted: the registry knows nothing about implicit single-project mode,
    so going straight to it reports "no projects" for an install that has one.
    """
    if _env_override_active():
        return _implicit_project()
    try:
        return get_registry().resolve_default()
    except AmbiguousProjectError:
        registered = get_registry().list_projects()
        # Nothing registered at all still means the working directory, as it always did.
        return None if registered else _implicit_project()


def resolve_default_project() -> Project:
    """Resolve the project that unscoped routes act on.

    Raises 409 rather than guessing when several projects are registered and none
    contains the working directory: serving the wrong project's tasks silently is a
    worse outcome than an error that names the ambiguity.
    """
    project = try_resolve_default_project()
    if project is None:
        # Name the candidates. An error that says only "ambiguous" leaves the caller
        # guessing at the very thing the server refused to guess at.
        known = ", ".join(candidate.id for candidate in get_registry().list_projects())
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
def _storage_for(project_id: str, tasks_dir: str) -> TaskStorage:
    """Create a cached TaskStorage for one project."""
    return TaskStorage(Path(tasks_dir))


@lru_cache(maxsize=32)
def _webhook_storage_for(project_id: str, webhooks_path: str) -> WebhookStorage:
    """Create a cached WebhookStorage for one project."""
    return WebhookStorage(Path(webhooks_path))


@lru_cache(maxsize=32)
def _webhook_manager_for(project_id: str, webhooks_path: str) -> WebhookManager:
    """Create a cached WebhookManager for one project."""
    return WebhookManager(_webhook_storage_for(project_id, webhooks_path))


def _tasks_dir_for(project: Project) -> Path:
    """Resolve a project's tasks directory, honouring the env override."""
    if project.id == _IMPLICIT_PROJECT_ID:
        return _resolve_tasks_dir()
    tasks_dir = project.tasks_dir()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


def storage_for(project: Project) -> TaskStorage:
    """TaskStorage scoped to one project."""
    return _storage_for(project.id, str(_tasks_dir_for(project)))


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
    """
    project_id = request.path_params.get("project_id")
    if project_id:
        return resolve_project(str(project_id))
    return resolve_default_project()


def get_project(request: Request) -> Project:
    """Provide the addressed project to a route."""
    return request_project(request)


def get_task_manager(request: Request) -> TaskManager:
    """Provide a TaskManager scoped to the project this request addresses."""
    return manager_for(request_project(request))


def get_webhook_manager(request: Request) -> WebhookManager:
    """Provide a WebhookManager scoped to the project this request addresses."""
    return webhook_manager_for(request_project(request))


def get_templates() -> Jinja2Templates:
    """Provide a shared Jinja2Templates instance for web views."""
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
