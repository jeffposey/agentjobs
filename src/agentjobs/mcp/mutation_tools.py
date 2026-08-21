"""The ten mutation MCP tools.

Every one is a thin, typed domain verb over ``TaskClient``. None of them reimplements
a lifecycle rule: the manager owns those, and a second copy here would eventually
disagree with the first and be wrong in a way nobody could see.

What the schemas do enforce is *shape*, and they are deliberately narrow. There is no
``set_lifecycle``, no ``set_ball``, no ``set_queue_position``, no generic patch, no
``save_yaml``, no batch, and no ``create_and_claim``. The state axes move only through
claim, handoff, release and close, the order moves only through ``task_queue_move``,
and the schemas make an invalid holder/reason pair or a bare position number
unrepresentable rather than warning about it in prose -- a warning is advice, a schema
is a wall.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from mcp import types

from ..client import MutationResult, TaskClient, TaskClientError
from ..models_v2 import MANAGER_WRITTEN_LOG_TYPES, LogEntryType
from .errors import ErrorCode, FieldError, ToolError
from .results import ToolOutput, mutation_annotations, success
from .routing import (
    ACTOR_SCHEMA,
    PROJECT_ID_SCHEMA,
    require_actor,
    require_project_id,
    resolve_project,
)
from .summaries import TASK_DOCUMENT_SCHEMA, task_document
from .tools import ToolDefinition

OPERATION_ID_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "format": "uuid",
    "minLength": 1,
    "description": (
        "Caller-generated UUID for this attempt. Resending the same request with the "
        "same id replays the original result instead of writing twice, so it is safe "
        "to retry after a timeout. Generate a fresh one per distinct operation."
    ),
}

REVISION_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "description": (
        "The `updated` value from your most recent read of this task. The request is "
        "refused if the task changed since, so a decision made on stale content "
        "cannot silently discard someone else's work."
    ),
}

MUTATION_RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "operation_id", "replayed", "task", "warnings"],
    "properties": {
        "project_id": {"type": "string"},
        "operation_id": {"type": ["string", "null"]},
        "replayed": {
            "type": "boolean",
            "description": (
                "True when this operation had already been applied: nothing was "
                "written and no log entry was added."
            ),
        },
        "task": TASK_DOCUMENT_SCHEMA,
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Post-commit side effects that failed, such as webhook delivery. A "
                "warning never means the task write failed."
            ),
        },
    },
}

#: Log entry types a caller may author. `handoff` is absent because the manager writes it
#: from the handoff verb; `transition`, `dispatch` and `dispatch_result` are absent
#: because they are manager-written (`MANAGER_WRITTEN_LOG_TYPES`) -- letting a tool forge
#: one would put an event in the record that never happened. Derived from the model
#: rather than restated, so a type added there cannot quietly become postable here.
AUTHORED_LOG_TYPES = [
    entry_type.value
    for entry_type in LogEntryType
    if entry_type not in MANAGER_WRITTEN_LOG_TYPES and entry_type is not LogEntryType.HANDOFF
]

#: The only fields a content patch may touch. Everything else -- the state axes, the
#: log, identity, timestamps -- is absent from the schema rather than rejected by a
#: check, so an attempt to set one fails before any handler runs.
CONTENT_FIELDS: Dict[str, Any] = {
    "title": {"type": "string", "minLength": 1},
    "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    "category": {"type": "string", "minLength": 1},
    "effort": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "parent": {"type": ["string", "null"]},
    "spec": {"type": "object"},
    "acceptance": {"type": "array", "items": {"type": "object"}},
    "deliverables": {"type": "array", "items": {"type": "object"}},
    "dependencies": {"type": "array", "items": {"type": "object"}},
    "links": {"type": "array", "items": {"type": "object"}},
    "branches": {"type": "array", "items": {"type": "object"}},
}

#: Holder/reason pairs, as a discriminated union. Written as a union rather than two
#: free enums so that `human/work` -- which reads fine as separate fields -- simply
#: does not validate.
HANDOFF_TARGET_SCHEMA: Dict[str, Any] = {
    "description": "Who acts next and why. agent/available is task_release, not a handoff.",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["ball", "reason", "prompt"],
            "properties": {
                "ball": {"const": "agent"},
                "reason": {
                    "type": "string",
                    "enum": ["work", "revise", "answer", "redirect", "hold"],
                },
                "prompt": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["ball", "reason", "prompt"],
            "properties": {
                "ball": {"const": "human"},
                "reason": {
                    "type": "string",
                    "enum": ["spec", "review", "decision", "approval", "input"],
                },
                "prompt": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["ball", "reason", "prompt"],
            "properties": {
                "ball": {"const": "external"},
                "reason": {"type": "string", "enum": ["dependency", "service"]},
                "prompt": {"type": "string", "minLength": 1},
            },
        },
    ],
}

#: Where a task lands in its band, as a discriminated union for the same reason the
#: handoff target is one: `before` together with `top` reads fine as separate fields
#: and means nothing, so it simply does not validate.
#:
#: There is no position number here, and that is the whole design. A caller that could
#: write one would be choosing a place without knowing what else is in the band, which
#: is how two open tasks end up sharing a position -- corruption the queue refuses to
#: answer over. Name a neighbour or an end; the server does the arithmetic under the
#: queue lock, where it is the only writer.
QUEUE_PLACEMENT_SCHEMA: Dict[str, Any] = {
    "description": (
        "Where the task lands in its band. Exactly one placement, named relative to "
        "another task or to an end of the band. No position number is accepted."
    ),
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["where", "neighbour"],
            "properties": {
                "where": {"const": "before"},
                "neighbour": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The task this one should come immediately ahead of.",
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["where", "neighbour"],
            "properties": {
                "where": {"const": "after"},
                "neighbour": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The task this one should come immediately behind.",
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["where"],
            "properties": {"where": {"const": "top"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["where"],
            "properties": {"where": {"const": "bottom"}},
        },
    ],
}

_CREATE_PROPERTIES: Dict[str, Any] = {
    "project_id": PROJECT_ID_SCHEMA,
    "actor": ACTOR_SCHEMA,
    "operation_id": OPERATION_ID_SCHEMA,
    "id": {"type": "string", "description": "Optional explicit id; generated when omitted."},
    "title": {"type": "string", "minLength": 1},
    "summary": {
        "type": "string",
        "minLength": 1,
        "description": "One or two sentences orienting a reader with no other context.",
    },
    "description": {"type": "string", "minLength": 1, "description": "The working spec."},
    "intent": {"type": "string"},
    "constraints": {"type": "string"},
    "out_of_scope": {"type": "string"},
    "context": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "why"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "why": {"type": "string", "minLength": 1},
            },
        },
    },
    "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    "category": {"type": "string"},
    "eligible": {"type": "array", "items": {"type": "string"}},
    "effort": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "parent": {"type": "string"},
    "acceptance": {"type": "array", "items": {"type": "object"}},
    "deliverables": {"type": "array", "items": {"type": "object"}},
    "dependencies": {"type": "array", "items": {"type": "object"}},
    "links": {"type": "array", "items": {"type": "object"}},
}

_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "actor", "operation_id", "title", "summary", "description"],
    "properties": _CREATE_PROPERTIES,
}


def _require(arguments: Mapping[str, Any], name: str) -> str:
    """Read a required non-empty string argument."""
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            code=ErrorCode.INVALID_INPUT,
            message=f"{name} is required and must be a non-empty string.",
            field_errors=[FieldError(path=name, message="Required.")],
        )
    return value


def _service_error(
    exc: TaskClientError, *, project_id: str, task_id: Optional[str] = None
) -> ToolError:
    """Translate a client failure into the structured tool error.

    The REST layer already classified the failure and sent back a stable code, so this
    passes it through rather than re-deriving one from the message. Re-deriving would
    be a second copy of the taxonomy, free to disagree with the first.
    """
    code = exc.code
    if code is not None:
        try:
            resolved = ErrorCode(code)
        except ValueError:  # pragma: no cover - a code this build does not know
            resolved = ErrorCode.INTERNAL_ERROR
        return ToolError(
            code=resolved,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
            current_task=exc.current_task,
            field_errors=[
                FieldError(path=str(item.get("path", "")), message=str(item.get("message", "")))
                for item in exc.field_errors
            ],
            suggested_action=exc.suggested_action,
        )
    if exc.status_code is None:
        return ToolError(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"The AgentJobs service did not answer: {exc}",
            project_id=project_id,
            task_id=task_id,
            suggested_action="Check that `agentjobs serve` is running, then retry.",
        )
    if exc.status_code == 404:
        return ToolError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
        )
    if exc.status_code == 400:
        return ToolError(
            code=ErrorCode.INVALID_INPUT,
            message=str(exc),
            project_id=project_id,
            task_id=task_id,
        )
    return ToolError(
        code=ErrorCode.INVALID_TRANSITION,
        message=str(exc),
        project_id=project_id,
        task_id=task_id,
    )


def _result_payload(result: MutationResult, project_id: str) -> Dict[str, Any]:
    """Shape a MutationResult for a structured tool result."""
    return {
        "project_id": project_id,
        "operation_id": result.operation_id,
        "replayed": result.replayed,
        "task": task_document(
            result.task.model_dump(mode="json", by_alias=True, exclude_none=True)
        ),
        "warnings": list(result.warnings),
    }


def _mutation_summary(result: MutationResult, verb: str) -> str:
    """One sentence for a client that does not read structured results."""
    prefix = "Already applied" if result.replayed else verb
    task = result.task
    state = task.display_status if hasattr(task, "display_status") else task.lifecycle.value
    return f"{prefix}: {task.id} is now {state}."


def _mutation_tool(
    name: str,
    title: str,
    description: str,
    input_schema: Dict[str, Any],
    handler: Any,
    *,
    destructive: bool = False,
) -> ToolDefinition:
    """Declare one mutation tool with the shared result schema and annotations."""
    return ToolDefinition(
        name=name,
        title=title,
        description=description,
        input_schema=input_schema,
        output_schema=MUTATION_RESULT_SCHEMA,
        annotations=mutation_annotations(title, destructive=destructive),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
#: Arguments the create tools consume themselves. Everything else is task content and
#: passes straight through: the REST create payload is flat -- intent, constraints,
#: context and the rest are top-level fields it folds into the spec itself -- so there
#: is nothing here to regroup.
_CREATE_CONTROL_ARGS = frozenset({"project_id", "actor", "operation_id", "title"})


def _create_kwargs(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """The content fields of a create call, ready to hand to the client."""
    return {key: value for key, value in arguments.items() if key not in _CREATE_CONTROL_ARGS}


def _build_create(client: TaskClient, *, ready: bool) -> Any:
    """Build a create handler pinned to one starting lifecycle."""

    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        operation_id = _require(arguments, "operation_id")
        title = _require(arguments, "title")
        _require(arguments, "summary")
        _require(arguments, "description")
        scoped = client.for_project(project_id)
        try:
            task = scoped.operations.create(
                actor=actor,
                operation_id=operation_id,
                title=title,
                lifecycle="ready" if ready else "draft",
                **_create_kwargs(arguments),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id) from exc

        payload = {
            "project_id": project_id,
            "operation_id": operation_id,
            # A create that resolved to an existing task is indistinguishable from the
            # original call by design, so this reports the outcome rather than
            # guessing which of the two happened.
            "replayed": False,
            "task": task_document(task.model_dump(mode="json", by_alias=True, exclude_none=True)),
            "warnings": [],
        }
        state = "ready for an agent to claim" if ready else "draft, awaiting its spec"
        return success(payload, f"Created {task.id} ({state}).")

    return handler


# ---------------------------------------------------------------------------
# The state verbs
# ---------------------------------------------------------------------------
def _build_promote(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        revision = _require(arguments, "expected_revision")
        try:
            result = client.for_project(project_id).operations.promote(
                task_id,
                actor=actor,
                operation_id=operation_id,
                expected_revision=revision,
                body=arguments.get("body"),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(_result_payload(result, project_id), _mutation_summary(result, "Promoted"))

    return handler


def _build_claim(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        try:
            result = client.for_project(project_id).operations.claim(
                task_id, actor=actor, operation_id=operation_id
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(_result_payload(result, project_id), _mutation_summary(result, "Claimed"))

    return handler


def _build_release(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        try:
            result = client.for_project(project_id).operations.release(
                task_id, actor=actor, operation_id=operation_id, body=arguments.get("body")
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(_result_payload(result, project_id), _mutation_summary(result, "Released"))

    return handler


def _build_handoff(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        revision = _require(arguments, "expected_revision")
        target = arguments.get("target")
        if not isinstance(target, Mapping):
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message="target is required and must name a ball, reason and prompt.",
                project_id=project_id,
                task_id=task_id,
                field_errors=[FieldError(path="target", message="Required.")],
            )
        try:
            result = client.for_project(project_id).operations.handoff(
                task_id,
                actor=actor,
                operation_id=operation_id,
                expected_revision=revision,
                ball=str(target.get("ball")),
                ball_reason=str(target.get("reason")),
                ball_prompt=str(target.get("prompt")),
                body=arguments.get("body"),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(_result_payload(result, project_id), _mutation_summary(result, "Handed off"))

    return handler


def _build_close(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        revision = _require(arguments, "expected_revision")
        outcome = _require(arguments, "outcome")
        try:
            result = client.for_project(project_id).operations.close(
                task_id,
                actor=actor,
                operation_id=operation_id,
                expected_revision=revision,
                outcome=outcome,
                body=arguments.get("body"),
                archive=bool(arguments.get("archive", False)),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(_result_payload(result, project_id), _mutation_summary(result, "Closed"))

    return handler


def _build_log_append(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        body = _require(arguments, "body")
        entry_type = arguments.get("type", "note")
        data = arguments.get("data") or {}
        if not isinstance(data, Mapping):
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message="data must be a JSON object.",
                project_id=project_id,
                task_id=task_id,
                field_errors=[FieldError(path="data", message="Must be an object.")],
            )
        try:
            result = client.for_project(project_id).operations.append_log(
                task_id,
                actor=actor,
                operation_id=operation_id,
                type=str(entry_type),
                body=body,
                re=arguments.get("re"),
                data=dict(data),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        return success(
            _result_payload(result, project_id),
            (
                "Already applied"
                if result.replayed
                else f"Appended a {entry_type} entry to {task_id}."
            ),
        )

    return handler


def _build_update_content(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        revision = _require(arguments, "expected_revision")
        patch = arguments.get("patch")
        if not isinstance(patch, Mapping) or not patch:
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message="patch is required and must set at least one field.",
                project_id=project_id,
                task_id=task_id,
                field_errors=[FieldError(path="patch", message="Required.")],
            )
        try:
            task = client.for_project(project_id).operations.update_content(
                task_id,
                actor=actor,
                operation_id=operation_id,
                expected_revision=revision,
                **dict(patch),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        payload = {
            "project_id": project_id,
            "operation_id": operation_id,
            "replayed": False,
            "task": task_document(task.model_dump(mode="json", by_alias=True, exclude_none=True)),
            "warnings": [],
        }
        return success(payload, f"Updated {', '.join(sorted(patch))} on {task_id}.")

    return handler


def _build_queue_move(client: TaskClient) -> Any:
    async def handler(arguments: Mapping[str, Any]) -> Union[ToolOutput, types.CallToolResult]:
        project_id = require_project_id(arguments)
        project = resolve_project(client, project_id)
        actor = require_actor(arguments, project)
        task_id = _require(arguments, "task_id")
        operation_id = _require(arguments, "operation_id")
        revision = _require(arguments, "expected_revision")
        placement = arguments.get("placement")
        if not isinstance(placement, Mapping) or not placement.get("where"):
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "placement is required: name where the task goes, as "
                    '{"where": "before"|"after", "neighbour": "task-id"} or '
                    '{"where": "top"|"bottom"}.'
                ),
                project_id=project_id,
                task_id=task_id,
                field_errors=[FieldError(path="placement", message="Required.")],
            )
        where = str(placement.get("where"))
        neighbour = placement.get("neighbour")
        if where in ("before", "after") and not isinstance(neighbour, str):
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message=f"placement.where '{where}' needs the neighbour it is relative to.",
                project_id=project_id,
                task_id=task_id,
                field_errors=[FieldError(path="placement.neighbour", message="Required.")],
            )
        try:
            result = client.for_project(project_id).operations.queue_move(
                task_id,
                actor=actor,
                operation_id=operation_id,
                expected_revision=revision,
                before=neighbour if where == "before" else None,
                after=neighbour if where == "after" else None,
                top=where == "top",
                bottom=where == "bottom",
                with_children=bool(arguments.get("with_children", False)),
                body=arguments.get("body"),
            )
        except TaskClientError as exc:
            raise _service_error(exc, project_id=project_id, task_id=task_id) from exc
        task = result.task
        # The new place, not the lifecycle: a move does not change state, so the
        # shared summary would report "is now active" and say nothing about what the
        # caller actually asked for.
        summary = (
            "Already applied"
            if result.replayed
            else f"Moved {task.id} to {task.priority.value}/{task.queue_position}."
        )
        return success(_result_payload(result, project_id), summary)

    return handler


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def _verb_schema(
    *,
    extra: Optional[Dict[str, Any]] = None,
    also_required: Sequence[str] = (),
    revision: bool = False,
) -> Dict[str, Any]:
    """Build the input schema shared by the state verbs.

    Every mutation carries the same four: the project, the task, who is acting, and
    the operation id that makes a retry safe. ``revision`` adds the fifth for verbs
    that act on content the caller has already read.
    """
    properties: Dict[str, Any] = {
        "project_id": PROJECT_ID_SCHEMA,
        "task_id": {"type": "string", "minLength": 1},
        "actor": ACTOR_SCHEMA,
        "operation_id": OPERATION_ID_SCHEMA,
    }
    required = ["project_id", "task_id", "actor", "operation_id"]
    if revision:
        properties["expected_revision"] = REVISION_SCHEMA
        required.append("expected_revision")
    properties.update(extra or {})
    required.extend(also_required)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def mutation_tool_definitions(client: TaskClient) -> List[ToolDefinition]:
    """Every mutation tool, in the order the design lists them."""
    return [
        _mutation_tool(
            "task_create_draft",
            "Create a draft task",
            (
                "Create a task that still needs its spec finished. It is born "
                "draft/human/spec: the ball sits with a human, and there is no way to "
                "create a task in any other state, because doing so would skip the "
                "transitions the log exists to record."
            ),
            _CREATE_SCHEMA,
            _build_create(client, ready=False),
        ),
        _mutation_tool(
            "task_create_ready",
            "Create a ready task",
            (
                "Create a task ready for an agent to claim. It is born "
                "ready/agent/available and is NOT claimed -- call task_claim after "
                "this if you intend to work it yourself."
            ),
            _CREATE_SCHEMA,
            _build_create(client, ready=True),
        ),
        _mutation_tool(
            "task_promote",
            "Promote a draft",
            (
                "Declare a draft's spec finished: draft becomes ready/agent/available "
                "and claimable. This is the only exit from draft -- task_handoff moves "
                "the ball and deliberately leaves the lifecycle alone, so a drafted "
                "task stays unclaimable until this is called. Completeness is not "
                "checked here; that is what agentjobs validate is for."
            ),
            _verb_schema(revision=True, extra={"body": {"type": "string"}}),
            _build_promote(client),
        ),
        _mutation_tool(
            "task_claim",
            "Claim a task",
            (
                "Take ownership of a ready task. One winner: a task already claimed, "
                "blocked by unmet dependencies, restricted to other actors, or holding "
                "open children is refused with a structured reason."
            ),
            _verb_schema(),
            _build_claim(client),
        ),
        _mutation_tool(
            "task_release",
            "Release a task",
            (
                "Return a claimed task to the pool: active becomes ready/agent/"
                "available. This is how work goes back, not a handoff -- "
                "agent/available is deliberately absent from task_handoff."
            ),
            _verb_schema(extra={"body": {"type": "string"}}),
            _build_release(client),
        ),
        _mutation_tool(
            "task_handoff",
            "Hand off a task",
            (
                "Move the ball, with the ask that travels with it. The target is a "
                "discriminated union, so an invalid holder/reason pair such as "
                "human/work does not validate at all. Requires expected_revision: a "
                "handoff decided against a stale read is refused."
            ),
            _verb_schema(
                revision=True,
                extra={"target": HANDOFF_TARGET_SCHEMA, "body": {"type": "string"}},
                also_required=["target"],
            ),
            _build_handoff(client),
        ),
        _mutation_tool(
            "task_close",
            "Close a task",
            (
                "End a task with an outcome. Destructive in the sense that it ends "
                "open work; the record itself remains, and remains recoverable in Git."
            ),
            _verb_schema(
                revision=True,
                extra={
                    "outcome": {
                        "type": "string",
                        "enum": ["completed", "cancelled", "superseded", "duplicate"],
                    },
                    "body": {"type": "string"},
                    "archive": {"type": "boolean", "default": False},
                },
                also_required=["outcome"],
            ),
            _build_close(client),
            destructive=True,
        ),
        _mutation_tool(
            "task_log_append",
            "Append a log entry",
            (
                "Append one authored entry to the task's append-only log. Record "
                "progress, decisions with their rejected alternative, and open "
                "questions here -- the log is what lets a session with no other "
                "context resume the work. `transition` and `handoff` are not "
                "available: the manager writes those itself."
            ),
            _verb_schema(
                extra={
                    "type": {
                        "type": "string",
                        "enum": AUTHORED_LOG_TYPES,
                        "default": "note",
                    },
                    "body": {"type": "string", "minLength": 1},
                    "re": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Id of an earlier entry this one answers.",
                    },
                    "data": {"type": "object"},
                },
                also_required=["body"],
            ),
            _build_log_append(client),
        ),
        _mutation_tool(
            "task_update_content",
            "Update task content",
            (
                "Edit authoring content: title, priority, category, effort, tags, "
                "parent, spec, acceptance, deliverables, dependencies, links, "
                "branches. The state axes and the log are absent from the schema, not "
                "merely rejected -- they move only through the domain verbs. Whole "
                "nested collections are replaced, matching the REST patch contract."
            ),
            _verb_schema(
                revision=True,
                extra={
                    "patch": {
                        "type": "object",
                        "additionalProperties": False,
                        "minProperties": 1,
                        "properties": CONTENT_FIELDS,
                    }
                },
                also_required=["patch"],
            ),
            _build_update_content(client),
        ),
        _mutation_tool(
            "task_queue_move",
            "Move a task in the queue",
            (
                "Change where a task stands in its priority band -- the only way the "
                "order changes. Use this when you disagree with what task_next "
                "returned: move the task you think should be first, so the next "
                "session inherits the decision instead of re-arguing it. Never add a "
                "needs dependency to express order; dependencies are prerequisites, "
                "and a false one deadlocks the graph and lies to every reader of it. "
                "Placement names a neighbour or an end of the band, never a number: "
                "the server computes the position under the queue lock, because a "
                "caller choosing one blind is how two tasks come to share it."
            ),
            _verb_schema(
                revision=True,
                extra={
                    "placement": QUEUE_PLACEMENT_SCHEMA,
                    "with_children": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Move this task's open children with it, keeping their "
                            "order among themselves."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Why the order changed. It becomes the queue_move log "
                            "entry, which is the only record of the reasoning."
                        ),
                    },
                },
                also_required=["placement"],
            ),
            _build_queue_move(client),
        ),
    ]
