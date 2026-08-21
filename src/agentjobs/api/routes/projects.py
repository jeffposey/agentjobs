"""Project registry endpoints and the cross-project task view."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from agentjobs.actors import default_user, load_actors
from agentjobs.dispatch.address import api_base_from_server, configured_api_base
from agentjobs.models_v2 import Ball, Lifecycle, Task
from agentjobs.project_setup import (
    build_project_config,
    ensure_mcp_server_entry,
    initialize_project,
)
from agentjobs.projects import (
    ProjectError,
    ProjectRegistry,
    load_project_config,
    slugify_project_id,
    validate_project_id,
)

from ..dependencies import get_registry, list_projects, project_config, storage_for

router = APIRouter(prefix="/api", tags=["projects"])

logger = logging.getLogger(__name__)


class ProjectRegistrationRequest(BaseModel):
    """Register a directory that is already an AgentJobs project."""

    path: str = Field(..., min_length=1)
    id: Optional[str] = Field(default=None, min_length=1)
    name: Optional[str] = Field(default=None, min_length=1)


class ProjectInitializationRequest(BaseModel):
    """Create a project in an existing directory, then register it."""

    path: str = Field(..., min_length=1)
    id: Optional[str] = Field(default=None, min_length=1)
    project_name: Optional[str] = Field(default=None, min_length=1)
    tasks_directory: str = Field(default="tasks", min_length=1)
    prompts_directory: str = Field(default="prompts", min_length=1)
    port: int = Field(default=8765, ge=1, le=65535)
    user: Optional[str] = Field(default=None, min_length=1)


class ProjectInspectionRequest(BaseModel):
    """A path to inspect before the user confirms a write or registration."""

    path: str = Field(..., min_length=1)


class ProjectInspection(BaseModel):
    """What the onboarding page should do with an inspected path."""

    path: str
    action: Literal["register", "initialize"]
    project_name: str
    suggested_id: str


class ProjectActor(BaseModel):
    """One actor a project's config defines."""

    id: str
    kind: str
    display_name: str


class ProjectResponse(BaseModel):
    """A registered project as exposed to API consumers."""

    id: str
    name: str
    root: str
    tasks_directory: str
    task_count: Optional[int]
    actors: List[ProjectActor] = Field(default_factory=list)
    default_user: Optional[str] = Field(
        default=None,
        description=(
            "The configured human. Present so a client can address a person; it is "
            "never an agent's mutation identity."
        ),
    )


def _resolved_directory(value: str) -> Path:
    """Resolve a submitted root and require that the user created it already."""
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ProjectError(f"Not a directory: {root}")
    return root


def _refuse_registration_conflict(registry: ProjectRegistry, root: Path, identifier: str) -> None:
    """Reject duplicate roots and ids instead of replacing a registry entry."""
    for existing in registry.list_projects():
        if existing.root.resolve() == root:
            raise ProjectError(f"{root} is already registered as {existing.id!r}.")
        if existing.id == identifier:
            raise ProjectError(
                f"Project id {identifier!r} is already registered for {existing.root}."
            )


def _registration_details(
    root: Path, project_id: Optional[str], name: Optional[str]
) -> tuple[str, str]:
    """Validate an existing config and derive the stored id and display name."""
    config = load_project_config(root, required=True)
    project_name = name or config.get("project_name") or root.name
    identifier = validate_project_id(project_id) if project_id else slugify_project_id(project_name)
    return identifier, project_name


def _actor_vocabulary(project: Any) -> tuple[List[ProjectActor], Optional[str]]:
    """Read a project's configured actors and its human default.

    Discovery has to carry these because an agent must supply an exact actor on every
    mutation and has no other way to learn the vocabulary. ``default_user`` is
    reported for addressing a person, not for an agent to adopt: inferring an actor
    from the human default is precisely the attribution bug actors.py exists to stop.

    A project whose config cannot be read reports an empty vocabulary rather than
    failing the listing, matching how a missing directory is already handled. Nothing
    is weakened by that: the authoritative validator reads the same config at mutation
    time, so a project that cannot be read here rejects unknown actors there anyway.
    """
    try:
        config = project_config(project)
    except (OSError, yaml.YAMLError):
        return [], None
    vocabulary = [
        ProjectActor(id=actor.id, kind=actor.kind, display_name=actor.display_name)
        for actor in load_actors(config).values()
    ]
    return vocabulary, default_user(config)


def _declare_mcp_server(request: Request, root: Path, port: int) -> None:
    """Give a project created from the web UI the same MCP wiring `init` writes.

    The address is taken from the socket this request arrived on before anything
    configured, for the reason `dispatch/address.py` sets out at length: an observed
    address cannot be stale, and this server *is* the one the new project's agents will
    be talking to. The declared answer and the project's own port follow, in that order.

    Registration is not failed over this. A project with no `.mcp.json` is the state
    every project was in before task-202 -- agents there fall back to the CLI, which
    works -- so a directory holding a malformed file of someone else's is logged and
    left alone rather than turned into a 500 on an otherwise successful init.
    """
    server = request.scope.get("server")
    observed = api_base_from_server(server[0], server[1]) if server else None
    base_url = observed or configured_api_base() or f"http://127.0.0.1:{port}"
    try:
        ensure_mcp_server_entry(root, base_url)
    except ProjectError as exc:
        logger.warning("No MCP server entry written for %s: %s", root, exc)


def _describe(project: Any, task_count: Optional[int]) -> ProjectResponse:
    """Render a project as an API payload."""
    actors, human = _actor_vocabulary(project)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        root=str(project.root),
        tasks_directory=str(project.tasks_dir()),
        task_count=task_count,
        actors=actors,
        default_user=human,
    )


@router.get("/projects", response_model=List[ProjectResponse])
async def get_projects() -> List[ProjectResponse]:
    """List every project this server can serve, with task counts.

    A project whose directory has gone missing is reported with a null task_count
    rather than failing the whole listing -- the registry is machine-local and a
    checkout can legitimately disappear.
    """
    payload: List[ProjectResponse] = []
    for project in list_projects():
        try:
            count: Optional[int] = len(storage_for(project).list_tasks())
        except OSError:
            count = None
        payload.append(_describe(project, count))
    return payload


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_project(
    payload: ProjectRegistrationRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ProjectResponse:
    """Register an existing AgentJobs project without changing its files."""
    root = _resolved_directory(payload.path)
    identifier, project_name = _registration_details(root, payload.id, payload.name)
    _refuse_registration_conflict(registry, root, identifier)
    project = registry.add(root, project_id=identifier, name=project_name)
    return _describe(project, None)


@router.post(
    "/projects/init",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initialize_and_register_project(
    request: Request,
    payload: ProjectInitializationRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ProjectResponse:
    """Initialize an existing untracked directory and register the new project."""
    root = _resolved_directory(payload.path)
    project_name = payload.project_name or root.name
    identifier = validate_project_id(payload.id) if payload.id else slugify_project_id(project_name)
    _refuse_registration_conflict(registry, root, identifier)
    config = build_project_config(
        project_name=project_name,
        tasks_directory=payload.tasks_directory,
        prompts_directory=payload.prompts_directory,
        port=payload.port,
        user=payload.user,
    )
    initialize_project(root, config, contain_directories=True)
    _declare_mcp_server(request, root, payload.port)
    project = registry.add(root, project_id=identifier, name=project_name)
    return _describe(project, 0)


@router.post("/projects/inspect", response_model=ProjectInspection)
async def inspect_project_path(
    payload: ProjectInspectionRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ProjectInspection:
    """Tell the UI whether confirmation should register or initialize a path."""
    root = _resolved_directory(payload.path)
    config_path = root / ".agentjobs" / "config.yaml"
    action: Literal["register", "initialize"]
    if config_path.exists():
        identifier, project_name = _registration_details(root, None, None)
        action = "register"
    else:
        project_name = root.name
        identifier = slugify_project_id(project_name)
        action = "initialize"
    _refuse_registration_conflict(registry, root, identifier)
    return ProjectInspection(
        path=str(root),
        action=action,
        project_name=project_name,
        suggested_id=identifier,
    )


@router.get("/all/tasks", response_model=List[Dict[str, Any]])
async def get_all_tasks(
    lifecycle_filter: Optional[Lifecycle] = Query(default=None, alias="lifecycle"),
    ball_filter: Optional[Ball] = Query(default=None, alias="ball"),
    project_filter: Optional[str] = Query(default=None, alias="project"),
) -> List[Dict[str, Any]]:
    """Every task across every project, each tagged with the project it belongs to.

    Mounted at ``/api/all/tasks`` rather than as a magic id under ``/api/tasks/``,
    because ``/api/tasks/all`` would be indistinguishable from a task whose id is
    literally "all" and would shadow it.

    Read-only by design. Writes always address one project explicitly, so there stays
    exactly one code path that mutates a file.
    """
    rows: List[Dict[str, Any]] = []
    for project in list_projects():
        if project_filter and project.id != project_filter:
            continue
        try:
            tasks: List[Task] = storage_for(project).list_tasks()
        except OSError:
            continue
        for task in tasks:
            if lifecycle_filter and task.lifecycle != lifecycle_filter:
                continue
            if ball_filter and task.ball != ball_filter:
                continue
            rows.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "task": task.model_dump(mode="json", exclude_none=True),
                }
            )
    return rows
