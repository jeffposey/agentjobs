"""The five read-only MCP tools.

Names and fields are fixed by section 4 of ``docs/mcp-integration-design.md`` and are
deliberately not collapsible into one generic query tool with a mode argument: a tool
list is the only documentation many agents read, so five named operations with real
schemas beat one whose behaviour depends on a string.

Everything here reads through ``TaskClient`` against the running service, so a read
sees exactly what a write would act on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Union

from mcp import types

from ..client import TaskClient, TaskClientError
from .errors import ErrorCode, FieldError, ToolError
from .results import ToolOutput, read_only_annotations, success
from .routing import (
    ACTOR_SCHEMA,
    PROJECT_ID_SCHEMA,
    PROJECT_SUMMARY_SCHEMA,
    project_payload,
    require_actor,
    require_project_id,
    resolve_project,
)
from .summaries import (
    BROKEN_TASK_SCHEMA,
    DEPENDENCY_FACTS_SCHEMA,
    TASK_DOCUMENT_SCHEMA,
    TASK_SUMMARY_SCHEMA,
    broken_task,
    dependency_facts,
    limited,
    summary_line,
    task_document,
    task_summary,
)
from .tools import ToolDefinition

#: Result-count bounds from the design. The default is a page an agent can actually
#: read; the ceiling stops one call from returning a corpus.
MIN_LIMIT = 1
MAX_LIMIT = 200
DEFAULT_LIMIT = 100

_LIMIT_SCHEMA: Dict[str, Any] = {
    "type": "integer",
    "minimum": MIN_LIMIT,
    "maximum": MAX_LIMIT,
    "default": DEFAULT_LIMIT,
    "description": "Maximum tasks to return. The result says whether it was truncated.",
}

_LIST_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tasks", "broken", "truncated"],
    "properties": {
        "tasks": {"type": "array", "items": TASK_SUMMARY_SCHEMA},
        "broken": {
            "type": "array",
            "items": BROKEN_TASK_SCHEMA,
            "description": (
                "Task files that exist but cannot be loaded. Reported beside valid "
                "tasks; a broken file is not a missing task."
            ),
        },
        "truncated": {"type": "boolean"},
    },
}


def _limit(arguments: Mapping[str, Any]) -> int:
    """Read the result limit, enforcing the accepted bounds."""
    raw = arguments.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message="limit must be an integer.",
            field_errors=[FieldError(path="limit", message="Must be an integer.")],
        )
    if not MIN_LIMIT <= raw <= MAX_LIMIT:
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message=f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}.",
            field_errors=[FieldError(path="limit", message="Out of range.")],
        )
    return int(raw)


def _service_error(
    exc: TaskClientError, *, project_id: str, task_id: Optional[str] = None
) -> ToolError:
    """Translate a transport-level client failure into a structured tool error.

    Domain classification of mutation failures belongs to the mutation-safety work.
    These are reads, so the only distinctions that matter here are "the task is not
    there", "one stored file will not parse", and "the service did not answer".
    """
    if exc.status_code == 404:
        return ToolError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
            suggested_action="Call tasks_list for this project to see the ids it holds.",
        )
    if exc.status_code == 422:
        # The service reports an unloadable stored document as 422 and names it. That
        # is a repairable file, not an absent task, and saying "not found" would send
        # the reader looking for something that is sitting right there.
        return ToolError(
            code=ErrorCode.BROKEN_TASK,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
            suggested_action=(
                "The file exists but does not validate against schema v2. Read it, fix "
                "the reported field, and re-run `agentjobs validate`."
            ),
        )
    if exc.status_code == 409:
        # Selection refuses to answer over a queue it cannot trust rather than
        # guessing from some other field, and the refusal names every offending task
        # and the repair command. Passing that through as `internal_error` would read
        # as "AgentJobs is broken" when the truth is "your corpus is, and here is the
        # fix" -- and would train a reader to ignore the one failure that otherwise
        # shows up as silently working the wrong task.
        return ToolError(
            code=ErrorCode.QUEUE_BROKEN,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
            suggested_action=(
                "Run `agentjobs queue check` to see the whole picture, then "
                "`agentjobs queue repair`. Do not pick a task by hand instead: the "
                "order is exactly what is in doubt."
            ),
        )
    if exc.status_code is None:
        return ToolError(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"The AgentJobs service did not answer: {exc}",
            project_id=project_id,
            task_id=task_id,
            suggested_action="Check that `agentjobs serve` is running, then retry.",
        )
    return ToolError(
        code=ErrorCode.INTERNAL_ERROR,
        message=str(exc),
        project_id=project_id,
        task_id=task_id,
    )


def _require_task_id(arguments: Mapping[str, Any]) -> str:
    """Read the task id a tool call must carry."""
    value = arguments.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message="task_id is required and must be a non-empty string.",
            field_errors=[FieldError(path="task_id", message="Required.")],
        )
    return value


# ---------------------------------------------------------------------------
# projects_list
# ---------------------------------------------------------------------------
def build_projects_list(client: TaskClient) -> ToolDefinition:
    """The only tool that does not take a project_id, because it supplies them."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        try:
            projects = client.projects()
        except TaskClientError as exc:
            raise _service_error(exc, project_id="") from exc
        payload = {"projects": [project_payload(project) for project in projects]}
        names = ", ".join(project.id for project in projects) or "none"
        return success(payload, f"{len(projects)} project(s): {names}.")

    return ToolDefinition(
        name="projects_list",
        title="List projects",
        description=(
            "List every AgentJobs project this service serves, with each project's "
            "configured actor vocabulary and its human default_user. Call this first: "
            "every other tool requires an exact project_id from here, including on a "
            "single-project installation. Never adopt default_user as your own actor."
        ),
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["projects"],
            "properties": {
                "projects": {"type": "array", "items": PROJECT_SUMMARY_SCHEMA},
            },
        },
        annotations=read_only_annotations("List projects"),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# tasks_list
# ---------------------------------------------------------------------------
def build_tasks_list(client: TaskClient) -> ToolDefinition:
    """List one project's tasks, with its unreadable files alongside."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        resolve_project(client, project_id)
        limit = _limit(arguments)
        scoped = client.for_project(project_id)
        try:
            records = scoped.read_tasks(
                lifecycle=arguments.get("lifecycle"),
                ball=arguments.get("ball"),
                priority=arguments.get("priority"),
                parent=arguments.get("parent"),
            )
            broken = scoped.read_broken_tasks()
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id) from exc

        rows = [task_summary(record, project_id=project_id) for record in records]
        rows, truncated = limited(rows, limit)
        payload = {
            "tasks": rows,
            "broken": [broken_task(item) for item in broken],
            "truncated": truncated,
        }
        return success(
            payload,
            summary_line(len(rows), "task", truncated=truncated, broken=len(broken)),
        )

    return ToolDefinition(
        name="tasks_list",
        title="List tasks",
        description=(
            "List tasks in one project, filtered by state. Task files that exist but "
            "cannot be loaded are returned in `broken` beside the valid tasks -- a "
            "broken file is a repairable record, never a missing task."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id"],
            "properties": {
                "project_id": PROJECT_ID_SCHEMA,
                "lifecycle": {"type": "string", "enum": ["draft", "ready", "active", "closed"]},
                "ball": {"type": "string", "enum": ["agent", "human", "external"]},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "parent": {
                    "type": "string",
                    "description": "Return only the children of this umbrella task.",
                },
                "limit": _LIMIT_SCHEMA,
            },
        },
        output_schema=_LIST_OUTPUT_SCHEMA,
        annotations=read_only_annotations("List tasks"),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# task_get
# ---------------------------------------------------------------------------
def build_task_get(client: TaskClient) -> ToolDefinition:
    """Return the whole record: the resumption contract in one payload."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        resolve_project(client, project_id)
        task_id = _require_task_id(arguments)
        try:
            detail = client.for_project(project_id).read_task_detail(task_id)
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc

        record = detail.get("task") or {}
        children: List[Dict[str, Any]] = [
            task_summary(child, project_id=project_id) for child in detail.get("children") or []
        ]
        payload = {
            "project_id": project_id,
            "task": task_document(record),
            "dependency_facts": dependency_facts(record),
            "subtasks": children,
        }
        prompt = record.get("ball_prompt") or "no current ask recorded"
        return success(
            payload,
            f"{task_id}: {record.get('display_status') or record.get('lifecycle')}. "
            f"Current ask -- {prompt}",
        )

    return ToolDefinition(
        name="task_get",
        title="Get a task",
        description=(
            "Return one task's complete schema-v2 record, its computed dependency "
            "facts, and its children. This is the resumption contract: read it before "
            "resuming work, and obey the newest handoff and every binding decision in "
            "its log. Reports an unreadable file as a broken task, not a missing one."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id", "task_id"],
            "properties": {"project_id": PROJECT_ID_SCHEMA, "task_id": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id", "task", "dependency_facts", "subtasks"],
            "properties": {
                "project_id": {"type": "string"},
                "task": TASK_DOCUMENT_SCHEMA,
                "dependency_facts": DEPENDENCY_FACTS_SCHEMA,
                "subtasks": {"type": "array", "items": TASK_SUMMARY_SCHEMA},
            },
        },
        annotations=read_only_annotations("Get a task"),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# tasks_search
# ---------------------------------------------------------------------------
def build_tasks_search(client: TaskClient) -> ToolDefinition:
    """Full-text search inside one project."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        resolve_project(client, project_id)
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message="query is required and must be a non-empty string.",
                project_id=project_id,
                field_errors=[FieldError(path="query", message="Required.")],
            )
        limit = _limit(arguments)
        scoped = client.for_project(project_id)
        try:
            records = scoped.read_search(query)
            broken = scoped.read_broken_tasks()
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id) from exc

        rows = [task_summary(record, project_id=project_id) for record in records]
        rows, truncated = limited(rows, limit)
        payload = {
            "tasks": rows,
            "broken": [broken_task(item) for item in broken],
            "truncated": truncated,
        }
        return success(
            payload,
            summary_line(
                len(rows), "match", plural="matches", truncated=truncated, broken=len(broken)
            ),
        )

    return ToolDefinition(
        name="tasks_search",
        title="Search tasks",
        description=(
            "Case-insensitive substring search within one project. Unreadable task "
            "files are reported alongside, because a search cannot look inside them "
            "and silence would imply they hold nothing."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id", "query"],
            "properties": {
                "project_id": PROJECT_ID_SCHEMA,
                "query": {"type": "string", "minLength": 1},
                "limit": _LIMIT_SCHEMA,
            },
        },
        output_schema=_LIST_OUTPUT_SCHEMA,
        annotations=read_only_annotations("Search tasks"),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# task_next
# ---------------------------------------------------------------------------
#: The section 9 explanation, in the shape the service returns it. Deliberately not
#: reshaped here: it is an explanation rather than a record, and a second definition
#: of it in this module would be free to drift from the one REST and the CLI render.
QUEUE_EXPLANATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task", "band", "queue_position", "empty_bands_above", "skipped"],
    "description": (
        "Why this task is next, and every open task the queue passed over to reach "
        "it. Read `skipped` before concluding the order is wrong: a task missing from "
        "the answer is usually blocked, claimed, or holding open children rather than "
        "mis-placed."
    ),
    "properties": {
        "task": {"type": ["string", "null"]},
        "band": {"type": ["string", "null"], "description": "The winner's priority band."},
        "queue_position": {
            "type": ["integer", "null"],
            "description": "Its place within that band.",
        },
        "empty_bands_above": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Higher bands checked first and found to hold no open work.",
        },
        "skipped": {
            "type": "array",
            "description": (
                "Open tasks ahead of the winner, each with the claimability rule that "
                "excluded it. Empty when nothing stood in front of it."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "position", "reason"],
                "properties": {
                    "task": {"type": "string"},
                    "position": {"type": ["integer", "null"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def _queue_line(queue: Mapping[str, Any]) -> str:
    """The one sentence about the order that a text-only client still sees."""
    band = queue.get("band")
    position = queue.get("queue_position")
    skipped = list(queue.get("skipped") or [])
    place = f"{band}/{position}" if band and position is not None else "an unrecorded position"
    if not skipped:
        return f"It stands at {place}, ahead of all other open work."
    names = "; ".join(f"{item.get('task')} ({item.get('reason')})" for item in skipped[:3])
    more = "" if len(skipped) <= 3 else f"; and {len(skipped) - 3} more"
    return f"It stands at {place}. The queue passed over {names}{more}."


def build_task_next(client: TaskClient) -> ToolDefinition:
    """Suggest the next claimable task, with the order that produced it. Never claims."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        priority = arguments.get("priority")
        scoped = client.for_project(project_id)
        try:
            record = scoped.read_next_task(priority=priority, agent=actor)
            # Fetched even when there is a winner, at the cost of a second call. The
            # alternative is an agent that disagrees with the answer and cannot see
            # what it is disagreeing with -- which is how a false `needs` edge gets
            # invented to express an opinion about order.
            queue = scoped.explain_next_task(priority=priority, agent=actor)
            if record is not None:
                explanation = (
                    f"{record.get('id')} is ready, eligible for {actor}, and has no "
                    f"unmet needs. It is NOT claimed; call task_claim to take it. "
                    f"{_queue_line(queue)} If you think something else should be "
                    f"first, move it with task_queue_move -- never add a needs "
                    f"dependency to express order."
                )
                payload: Dict[str, Any] = {
                    "task": task_summary(record, project_id=project_id),
                    "explanation": explanation,
                    "queue": queue,
                }
                return success(payload, explanation)
            explanation = _explain_no_work(scoped, actor=actor, priority=priority)
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id) from exc

        return success({"task": None, "explanation": explanation, "queue": queue}, explanation)

    return ToolDefinition(
        name="task_next",
        title="Next claimable task",
        description=(
            "Suggest the next task this actor could claim: ready, eligible, with no "
            "unmet needs, and first in the managed queue. It does NOT claim anything "
            "-- call task_claim separately. `queue` carries the band and position it "
            "won on, plus every open task passed over to reach it and the rule that "
            "excluded each. Work what this returns; if you disagree with the order, "
            "call task_queue_move rather than inventing a dependency. When nothing is "
            "available it explains why, distinguishing an empty backlog from work "
            "that is blocked, unreadable, or claimed by someone else."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id", "actor"],
            "properties": {
                "project_id": PROJECT_ID_SCHEMA,
                "actor": ACTOR_SCHEMA,
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["task", "explanation", "queue"],
            "properties": {
                "task": {"oneOf": [TASK_SUMMARY_SCHEMA, {"type": "null"}]},
                "explanation": {"type": "string"},
                "queue": QUEUE_EXPLANATION_SCHEMA,
            },
        },
        annotations=read_only_annotations("Next claimable task"),
        handler=handler,
    )


def _explain_no_work(scoped: TaskClient, *, actor: str, priority: Optional[str]) -> str:
    """Say why there is nothing to claim, in terms the reader can act on.

    "No task available" is the least useful true sentence here: a backlog that is
    empty, one entirely blocked on dependencies, one already claimed, and one whose
    files will not parse all produce it, and they need four different responses. This
    costs one extra listing call, which is worth it -- an agent that stops on a wrong
    conclusion costs far more than a round trip.
    """
    records = scoped.read_tasks()
    broken = scoped.read_broken_tasks()
    filtered = "" if priority is None else f" at {priority} priority"

    if not records and not broken:
        return f"This project has no tasks at all, so there is nothing to claim{filtered}."

    ready = [item for item in records if item.get("lifecycle") == "ready"]
    active = [item for item in records if item.get("lifecycle") == "active"]
    blocked = [item for item in ready if item.get("unmet_needs")]
    cyclic = [item for item in records if item.get("needs_cycles")]

    reasons = []
    if not ready and active:
        owners = sorted({(item.get("assignment") or {}).get("owner") or "?" for item in active})
        reasons.append(
            f"{len(active)} task(s) are already active, held by {', '.join(owners)}; "
            "no ready work remains"
        )
    elif not ready:
        reasons.append("no task is in the ready lifecycle")
    elif blocked and len(blocked) == len(ready):
        names = ", ".join(item.get("id") or "?" for item in blocked[:5])
        reasons.append(f"every ready task has unmet dependencies ({names})")
    elif ready:
        reasons.append(
            f"{len(ready)} ready task(s) exist but none is eligible for {actor!r}"
            f"{filtered} -- check assignment.eligible and whether an umbrella still "
            "has open children"
        )
    if cyclic:
        reasons.append(f"{len(cyclic)} task(s) sit in a dependency cycle and can never unblock")
    if broken:
        names = ", ".join(item.get("filename") or "?" for item in broken[:5])
        reasons.append(
            f"{len(broken)} task file(s) could not be read ({names}); claimable work "
            "may be hidden inside them"
        )
    return "Nothing to claim: " + "; ".join(reasons) + "."


def read_tool_definitions(client: TaskClient) -> List[ToolDefinition]:
    """Every read tool, in the order the design lists them."""
    return [
        build_projects_list(client),
        build_tasks_list(client),
        build_task_get(client),
        build_tasks_search(client),
        build_task_next(client),
    ]
