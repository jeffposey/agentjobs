"""Project routing and actor attribution for MCP tools.

Two rules from section 3 of ``docs/mcp-integration-design.md``, enforced in one place
so no individual tool can decide otherwise:

**A project is always named, never inferred.** Task ids are unique only inside a
project, so a tool that guessed -- from the MCP process working directory, from the
last project used, from the only project that happens to be registered -- would write
a session's work into whichever repository it happened to be standing in. There is no
current-project state here, and adding some would defeat the point.

**An actor is always supplied, never derived.** The actor lands in an append-only log
that nothing ever rewrites. Deriving it from the model name, the OS user, the MCP
client, or the project's ``default_user`` would file an agent's work under a person,
which is the specific attribution failure ``actors.py`` was written to prevent.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ..client import ProjectSummary, TaskClient, TaskClientError
from .errors import ErrorCode, FieldError, ToolError

#: Reusable JSON Schema fragments. Tools compose these so every schema states the
#: same requirement in the same words.
PROJECT_ID_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Exact project id from projects_list. Required on every task tool, including "
        "single-project installations. It is never inferred."
    ),
}

ACTOR_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Actor id from the project's configured vocabulary, as returned by "
        "projects_list. This is written to the task log. Do not send a model name, "
        "an OS user, or the project's default_user."
    ),
}


def require_project_id(arguments: Mapping[str, Any]) -> str:
    """Read the project id a tool call must carry."""
    value = arguments.get("project_id")
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message="project_id is required and must be a non-empty string.",
            field_errors=[FieldError(path="project_id", message="Required.")],
            suggested_action=("Call projects_list and pass the exact id of the project you mean."),
        )
    return value


def resolve_project(client: TaskClient, project_id: str) -> ProjectSummary:
    """Look up a project, or fail with an error naming the valid ids."""
    try:
        return client.get_project(project_id)
    except TaskClientError as exc:
        if exc.status_code == 404:
            raise ToolError(
                code=ErrorCode.UNKNOWN_PROJECT,
                message=str(exc),
                project_id=project_id,
                suggested_action="Call projects_list and use one of the ids it returns.",
            ) from exc
        raise ToolError(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"Could not reach the AgentJobs service to resolve a project: {exc}",
            project_id=project_id,
            suggested_action="Check that the AgentJobs service is running, then retry.",
        ) from exc


def require_actor(arguments: Mapping[str, Any], project: ProjectSummary) -> str:
    """Read and validate the actor a mutating tool call must carry.

    Checked here as well as at the REST layer, which is the authority. This copy
    exists to fail before a write is attempted and to name the project's configured
    actors in the error, which is what lets an agent correct itself instead of
    retrying the same wrong id.

    A project that configures no actors accepts any id, matching ``validate_actor``:
    a fresh ``agentjobs init`` has not decided who its actors are yet, and rejecting
    everything until it does would make the product unusable before it is configured.
    """
    value = arguments.get("actor")
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message="actor is required and must be a non-empty string.",
            project_id=project.id,
            field_errors=[FieldError(path="actor", message="Required.")],
            suggested_action=_actor_guidance(project),
        )
    known = project.actor_ids
    if known and value not in known:
        raise ToolError(
            code=ErrorCode.UNKNOWN_ACTOR,
            message=(
                f"{value!r} is not an actor in project {project.id!r}. "
                f"Configured actors: {', '.join(sorted(known))}."
            ),
            project_id=project.id,
            field_errors=[FieldError(path="actor", message="Not a configured actor.")],
            suggested_action=_actor_guidance(project),
        )
    return value


def _actor_guidance(project: ProjectSummary) -> str:
    """Say which actor to use, without nominating one."""
    agents = sorted(actor.id for actor in project.actors if actor.kind != "human")
    if agents:
        return (
            f"Use the agent actor you are running as. Agents configured in "
            f"{project.id!r}: {', '.join(agents)}."
        )
    if project.actors:
        return (
            f"Project {project.id!r} configures no agent actors. Add one to 'actors:' "
            "in its .agentjobs/config.yaml with 'kind: agent'."
        )
    return (
        f"Project {project.id!r} configures no actors, so any id is accepted. Send a "
        "stable id for yourself; it is written to the task log permanently."
    )


def scoped_client(client: TaskClient, project_id: str) -> TaskClient:
    """Return a client addressing exactly one project.

    A thin pass-through, present so tool handlers never build a URL and never hold a
    scoped client past the call that created it.
    """
    return client.for_project(project_id)


def project_payload(project: ProjectSummary) -> Dict[str, Any]:
    """Render a project for a structured tool result."""
    return {
        "id": project.id,
        "name": project.name,
        "root": project.root,
        "tasks_directory": project.tasks_directory,
        "task_count": project.task_count,
        "actors": [
            {"id": actor.id, "kind": actor.kind, "display_name": actor.display_name}
            for actor in project.actors
        ],
        "default_user": project.default_user,
    }


#: JSON Schema for one entry of :func:`project_payload`.
PROJECT_SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "name", "root", "tasks_directory", "actors"],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "root": {"type": "string"},
        "tasks_directory": {"type": "string"},
        "task_count": {"type": ["integer", "null"]},
        "actors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "display_name"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "display_name": {"type": "string"},
                },
            },
        },
        "default_user": {
            "type": ["string", "null"],
            "description": ("The configured human. Never adopt this as an agent's actor id."),
        },
    },
}


def optional_string(arguments: Mapping[str, Any], name: str) -> Optional[str]:
    """Read an optional string argument, treating blank as absent."""
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message=f"{name} must be a non-empty string when supplied.",
            field_errors=[FieldError(path=name, message="Must be a non-empty string.")],
        )
    return value
