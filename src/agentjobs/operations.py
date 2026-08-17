"""Durable idempotency and optimistic-revision primitives for task mutations.

A client that times out cannot tell a lost request from a lost response. Retrying is
then the only sensible thing it can do, and without help that retry appends a second
log entry, or creates a second task, and the record quietly stops being true.

The fix is a caller-supplied ``operation_id`` recorded *in the task file* alongside the
entry the operation produced. Durability matters more than it first appears: an
in-memory cache in the MCP process would forget every operation the moment the process
restarted, which is exactly when a client is most likely to retry. Because the marker
lives in the log entry, replay detection survives a restart of anything.

Detection happens inside the same lock as the write. Checking beforehand would be the
double-claim race again, wearing a different hat.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from .models_v2 import Task

#: Reserved key inside ``LogEntry.data``. Manager-owned: an authored log entry may not
#: set it, or a caller could forge a replay marker and make a real write look like one
#: that already happened.
OPERATION_KEY = "operation"


class OperationConflictError(ValueError):
    """One operation id was reused for a materially different operation.

    Not retryable and nothing is written. The alternative -- treating it as a fresh
    operation -- would let a client that reuses ids by accident silently perform two
    different writes under one identity, which is worse than a loud refusal.
    """


class RevisionConflictError(ValueError):
    """The caller decided against a version of the task that has since moved on.

    Carries the current task so the caller can re-read and decide, rather than retry
    blindly against state it still has not seen.
    """

    def __init__(self, message: str, *, current_task: Optional[Task] = None) -> None:
        """Record the message and the state that made the decision stale."""
        super().__init__(message)
        self.current_task = current_task


@dataclass(frozen=True)
class Operation:
    """One idempotent mutation: what it is, who asked, and on what payload."""

    id: str
    kind: str
    actor: str
    payload: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        """A stable digest of the operation's normalized intent.

        Two calls with the same id are the same operation only if this matches. It
        covers the verb, the actor and the payload, so reusing an id for a different
        verb, a different actor, or a different body is a conflict rather than a
        silently accepted replay.
        """
        return fingerprint(self.kind, self.actor, self.payload)

    def marker(self) -> Dict[str, Any]:
        """The metadata stamped into the log entry this operation produces."""
        return {"id": self.id, "kind": self.kind, "fingerprint": self.fingerprint}


def fingerprint(kind: str, actor: str, payload: Mapping[str, Any]) -> str:
    """Digest an operation's normalized intent.

    ``default=str`` so datetimes and enums normalise instead of raising: a value that
    cannot be digested must still produce *some* stable string, because refusing to
    fingerprint would turn a legitimate retry into a hard failure.
    """
    canonical = json.dumps(
        {"kind": kind, "actor": actor, "payload": _normalise(payload)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _normalise(value: Any) -> Any:
    """Drop absent optional values so omitting a field equals sending it as null."""
    if isinstance(value, Mapping):
        return {key: _normalise(item) for key, item in sorted(value.items()) if item is not None}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def find_operation(task: Task, operation_id: str) -> Optional[Dict[str, Any]]:
    """Return the recorded marker for an operation id already applied to this task."""
    for entry in task.log:
        marker = entry.data.get(OPERATION_KEY)
        if isinstance(marker, Mapping) and marker.get("id") == operation_id:
            return dict(marker)
    return None


def replay_or_conflict(task: Task, operation: Optional[Operation]) -> bool:
    """Decide whether an operation has already been applied to this task.

    Returns True when the caller should be handed the current task unchanged. Raises
    :class:`OperationConflictError` when the id was reused for something else.
    """
    if operation is None:
        return False
    marker = find_operation(task, operation.id)
    if marker is None:
        return False
    if marker.get("fingerprint") != operation.fingerprint:
        raise OperationConflictError(
            f"Operation id {operation.id!r} was already used on task {task.id!r} for a "
            f"{marker.get('kind')!r} operation with a different payload. Nothing was "
            "written. Use a new operation_id, or resend the original request exactly."
        )
    return True


def stamp(data: Optional[Dict[str, Any]], operation: Optional[Operation]) -> Dict[str, Any]:
    """Attach an operation marker to log-entry data, refusing a forged one."""
    payload = dict(data or {})
    if OPERATION_KEY in payload:
        raise ValueError(
            f"{OPERATION_KEY!r} is reserved for manager-owned operation metadata and "
            "cannot be supplied by a caller."
        )
    if operation is not None:
        payload[OPERATION_KEY] = operation.marker()
    return payload


def check_revision(task: Task, expected_revision: Optional[datetime | str]) -> None:
    """Refuse a decision made against a version of the task that has since changed.

    Compared to the microsecond, because that is the resolution ``updated`` is stored
    at and anything coarser would call two genuinely different versions equal.
    """
    if expected_revision is None:
        return
    expected = _as_datetime(expected_revision)
    if expected is None:
        raise RevisionConflictError(
            f"expected_revision {expected_revision!r} is not a timestamp. Send the "
            "`updated` value from a prior read of this task.",
            current_task=task,
        )
    if _comparable(expected) != _comparable(task.updated):
        raise RevisionConflictError(
            f"Task {task.id!r} has changed since you read it (you expected "
            f"{_comparable(expected)}, it is now {_comparable(task.updated)}). Nothing "
            "was written. Re-read the task, decide again, and resend.",
            current_task=task,
        )


def _as_datetime(value: datetime | str) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _comparable(value: datetime) -> str:
    """Normalise a timestamp for comparison, tolerating tz-naive stored values."""
    return value.isoformat()
