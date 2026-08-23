"""``playbooks_list``: the whole of the MCP playbook surface, and deliberately so.

``docs/playbooks-design.md`` decision P10. An MCP mutation is callable by an agent, and
an agent starting a playbook run is an agent causing a dispatch -- the transition the
dispatch design exists to forbid. So agents may discover what playbooks a project holds
and read what each one is for; **there is no ``playbook_run`` tool, and its absence is
the feature.** An agent that believes a groom pass is due raises it the way agents raise
everything: a question or a handoff a human reads.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Union

from mcp import types

from ..client import TaskClient, TaskClientError
from .read_tools import service_error
from .results import ToolOutput, read_only_annotations, success
from .routing import PROJECT_ID_SCHEMA, require_project_id, resolve_project
from .tools import ToolDefinition

_GATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["before", "what"],
    "properties": {
        "before": {"type": "string", "description": "The verb this gate stands before."},
        "what": {"type": "string", "description": "What must be true to pass it."},
    },
}

_PLAYBOOK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["name", "description", "target", "difficulty"],
    "properties": {
        "name": {"type": "string", "description": "Also the filename stem."},
        "description": {"type": "string"},
        "target": {
            "type": "string",
            "enum": ["project", "task"],
            "description": "What a run is aimed at: the whole project, or one task.",
        },
        "difficulty": {"type": "string", "enum": ["routine", "standard", "hard"]},
        "verbs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The manager verbs a run declares it will use.",
        },
        "gates": {
            "type": "array",
            "items": _GATE_SCHEMA,
            "description": "Where a run must stop for a human.",
        },
        "filename": {"type": "string"},
    },
}

_PROBLEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["filename", "message"],
    "properties": {
        "filename": {"type": "string"},
        "field": {"type": ["string", "null"]},
        "message": {"type": "string"},
    },
}


def build_playbooks_list(client: TaskClient) -> ToolDefinition:
    """Discovery only: what playbooks a project holds and what each is for."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        resolve_project(client, project_id)
        try:
            document = client.for_project(project_id).read_playbooks()
        except TaskClientError as exc:
            raise service_error(exc, project_id=project_id) from exc

        playbooks: List[Dict[str, Any]] = list(document.get("playbooks") or [])
        problems: List[Dict[str, Any]] = list(document.get("problems") or [])
        payload = {
            "directory": document.get("directory") or "",
            "exists": bool(document.get("exists")),
            "playbooks": playbooks,
            "problems": problems,
        }
        if not payload["exists"]:
            summary = f"{project_id} has no playbooks directory yet."
        elif playbooks:
            names = ", ".join(str(item.get("name")) for item in playbooks)
            summary = f"{len(playbooks)} playbook(s) in {project_id}: {names}."
        else:
            summary = f"{project_id} has a playbooks directory and no playbooks in it."
        if problems:
            summary += f" {len(problems)} file(s) in it did not validate."
        return success(payload, summary)

    return ToolDefinition(
        name="playbooks_list",
        title="List playbooks",
        description=(
            "List the playbooks a project holds: reusable briefs for recurring work, "
            "each declaring what a run may do and where it must stop for a human. "
            "Read-only. There is no tool that runs one, on purpose -- starting a run "
            "is a dispatch, and only a human starts a dispatch. If you think a "
            "playbook should run, say so in a question or a handoff instead."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id"],
            "properties": {"project_id": PROJECT_ID_SCHEMA},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["directory", "exists", "playbooks", "problems"],
            "properties": {
                "directory": {"type": "string"},
                "exists": {
                    "type": "boolean",
                    "description": (
                        "False when the project has no playbooks directory. Not an "
                        "error: it is the state a project starts in."
                    ),
                },
                "playbooks": {"type": "array", "items": _PLAYBOOK_SCHEMA},
                "problems": {
                    "type": "array",
                    "items": _PROBLEM_SCHEMA,
                    "description": (
                        "Files in the directory that are not valid playbooks, "
                        "reported beside the valid ones rather than hidden."
                    ),
                },
            },
        },
        annotations=read_only_annotations("List playbooks"),
        handler=handler,
    )


def playbook_tool_definitions(client: TaskClient) -> List[ToolDefinition]:
    """Every playbook tool. One, read-only, and that is the design."""
    return [build_playbooks_list(client)]
