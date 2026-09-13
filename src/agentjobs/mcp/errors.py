"""The MCP tool error vocabulary.

Section 5 of ``docs/mcp-integration-design.md`` requires that a failing tool call
returns a *structured* error rather than prose, so an agent can branch on a code
instead of pattern-matching an English sentence. The codes are closed: a new failure
mode gets a new member here, not an ad-hoc string at a call site. A code the service sends
that this module does not know is still reported as ``internal_error``, but the message
names the code it replaced, so version skew between the MCP process and the service shows.

Domain classification -- turning a specific REST/manager failure into one of these
codes -- belongs to the mutation-safety work (task-113). This module owns the shape
and the retry policy only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ErrorCode(str, Enum):
    """Stable codes an agent may branch on."""

    INVALID_INPUT = "invalid_input"
    UNKNOWN_PROJECT = "unknown_project"
    UNKNOWN_ACTOR = "unknown_actor"
    TASK_NOT_FOUND = "task_not_found"
    BROKEN_TASK = "broken_task"
    INVALID_TRANSITION = "invalid_transition"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    QUEUE_BROKEN = "queue_broken"
    REVISION_CONFLICT = "revision_conflict"
    OPERATION_CONFLICT = "operation_conflict"
    LOCK_TIMEOUT = "lock_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"

    # The authorization family (task-426). The REST layer refuses with these since
    # task-332, and they are passed through verbatim: ALLAGENTS.md tells a run to branch
    # on `wrong_task` and `capability_denied`, which it cannot do if they arrive renamed.
    # The spellings are the service's -- `agentjobs.capabilities` and
    # `agentjobs.principals.Problem` -- and a test fails when the two sets disagree.
    CAPABILITY_DENIED = "capability_denied"
    WRONG_TASK = "wrong_task"
    WRONG_RUN = "wrong_run"
    ACTOR_MISMATCH = "actor_mismatch"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    NO_PRINCIPAL = "no_principal"
    NO_PROVEN_IDENTITY = "no_proven_identity"
    UNVERIFIED_RUN_CREDENTIAL = "unverified_run_credential"
    EXPIRED_RUN_CREDENTIAL = "expired_run_credential"


# Retryability is a property of the code, not a judgement made per call site. Only
# contention and transport faults are transient; every other code describes a state
# that an identical retry will reproduce exactly.
RETRYABLE_CODES = frozenset(
    {
        ErrorCode.LOCK_TIMEOUT,
        ErrorCode.SERVICE_UNAVAILABLE,
    }
)

#: What to do about each authorization refusal. Like retryability this is a property of
#: the code rather than of the call: the service's body carries a code and a sentence
#: saying what happened, and none of them is retryable, so without this an agent learns
#: *that* it was refused and not what to do next. A service-supplied ``suggested_action``
#: still wins where one is sent.
AUTHORIZATION_ACTIONS: Dict[ErrorCode, str] = {
    ErrorCode.WRONG_TASK: (
        "This task is not the one your run was dispatched to work, and a run may act only "
        "on its own. Work your own task; if this one needs attention, say so in a log "
        "entry on your own task, or file a new task."
    ),
    ErrorCode.CAPABILITY_DENIED: (
        "A run may not do this at all -- approving, dispatching, configuration and repairs "
        "belong to a person. Do not retry. Ask the human: hand your own task off with the "
        "request in the prompt."
    ),
    ErrorCode.WRONG_RUN: (
        "A run may read only its own output. Do not retry; ask the human if you need "
        "another run's transcript."
    ),
    ErrorCode.ACTOR_MISMATCH: (
        "Write as the actor your run was dispatched as. The log is append-only, so an "
        "attribution to anybody else is refused rather than recorded."
    ),
    ErrorCode.IDENTITY_UNRESOLVED: (
        "The service cannot work out which person is at this machine. That is "
        "configuration, not your request: tell the human, and see docs/authorization.md."
    ),
    ErrorCode.NO_PRINCIPAL: (
        "The service could not tell who is asking, so it refused. A retry is refused the "
        "same way; tell the human."
    ),
    ErrorCode.NO_PROVEN_IDENTITY: (
        "This request carried no identity the service accepts, so it refused. A retry is "
        "refused the same way; tell the human."
    ),
    ErrorCode.UNVERIFIED_RUN_CREDENTIAL: (
        "The run credential this session presented did not verify -- usually because the "
        "run has ended or was cancelled. Stop: a retry, and every other write from this "
        "session, is refused the same way. Say so in your final reply so the human can "
        "dispatch the task again."
    ),
    ErrorCode.EXPIRED_RUN_CREDENTIAL: (
        "This run's credential has expired. Stop: a retry, and every other write from "
        "this session, is refused the same way. Say so in your final reply so the human "
        "can dispatch the task again."
    ),
}

#: The authorization family, derived from the table so the two cannot drift.
AUTHORIZATION_CODES = frozenset(AUTHORIZATION_ACTIONS)


@dataclass(frozen=True)
class FieldError:
    """One rejected input field."""

    path: str
    message: str

    def to_payload(self) -> Dict[str, str]:
        """Serialise for the structured error content."""
        return {"path": self.path, "message": self.message}


@dataclass
class ToolError(Exception):
    """A tool failure carrying enough structure for an agent to act on it."""

    code: ErrorCode
    message: str
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    current_task: Optional[Dict[str, Any]] = None
    field_errors: List[FieldError] = field(default_factory=list)
    suggested_action: Optional[str] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def retryable(self) -> bool:
        """Whether an identical retry could plausibly succeed."""
        return self.code in RETRYABLE_CODES

    def to_payload(self) -> Dict[str, Any]:
        """Build the ``structuredContent`` body for an ``isError`` tool result.

        Optional keys are omitted rather than emitted as null, so a client reading
        ``current_task`` cannot confuse "not applicable" with "the task is empty".
        """
        payload: Dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.current_task is not None:
            payload["current_task"] = self.current_task
        if self.field_errors:
            payload["field_errors"] = [item.to_payload() for item in self.field_errors]
        if self.suggested_action is not None:
            payload["suggested_action"] = self.suggested_action
        return payload


#: JSON Schema for :meth:`ToolError.to_payload`. Published so tool output schemas and
#: the contract tests validate against one definition instead of two that drift.
ERROR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["code", "message", "retryable"],
    "properties": {
        "code": {"type": "string", "enum": [member.value for member in ErrorCode]},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "project_id": {"type": "string"},
        "task_id": {"type": "string"},
        "current_task": {"type": "object"},
        "field_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "message"],
                "properties": {
                    "path": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
        "suggested_action": {"type": "string"},
    },
}
