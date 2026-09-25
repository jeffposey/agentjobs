"""Opt-in tolerance for schema a reader's copy of the models does not know.

Every process that talks to AgentJobs over HTTP carries its own copy of
``models_v2``. That copy is a *reader*, not an authority: the service validated the
task on write and again on read, so a document the service is willing to serve is by
definition legal. When the reader is older than the service, re-checking can only
produce false negatives -- and on 2026-08-19 it produced a total one. Adding
``AUTO = "auto"`` to ``models_v2.DispatchPosture`` (since replaced by ``MergeMode``) made task-107
unreadable to every process started before the change: the MCP client's
``task_handoff`` failed with ``log.12.posture: Input should be 'read_only',
'supervised' or 'autonomous'`` and ``retryable: false``, so an agent could not record
finished work against a task the service was serving over ``curl`` perfectly happily.

Adding a *field* does the same thing, one level over (task-445). task-375 added
``envelope`` and ``delivery`` to dispatch entries; a supervisor whose MCP server had
started before that merge then got ``internal_error`` -- ``log.10.delivery: Extra inputs
are not permitted`` -- from every write to a task dispatched after it. The write had
landed. Only the parse of the service's echo failed, so the error invited a retry that
recorded the same instruction twice.

Adding a member to an enum, or an optional field to a model, is backward-compatible by
construction -- old data stays valid. A strict reader inverts that and turns every
widening into a fleet-wide breaking change requiring every session to restart.

So a reader may ask to *degrade* instead: inside :func:`tolerant_schema_reader`, an
unknown enum value is kept as an opaque string, and a key a model does not declare is
left out of the parsed model rather than rejected. The caller is handed the list of what
it did not understand so the skew is still visible. A log entry's ``data`` is stored as
the raw mapping, so a field a newer writer put there survives the parse verbatim even
though the payload model checking it never heard of it.

**This is for clients, and only for clients.** It is off by default and must stay off
on the write path and in ``storage``:

* Writing an unknown enum value or an undeclared field must still be refused -- this is
  about what a reader will *accept*, never about what may be stored.
* A file that genuinely cannot be read must still be reported loudly (task-049).
  Tolerance covers unknown enum members and undeclared keys. Everything else -- a
  missing required field, a wrong type, a known field with an invalid value -- still
  fails exactly as before.

The switch is a :class:`~contextvars.ContextVar`, so it is scoped to the parse that
asked for it and is not visible to another thread or another task in the same process.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, List, NamedTuple, Optional

__all__ = [
    "UnknownSchema",
    "is_tolerant",
    "record_unknown_enum_value",
    "record_unknown_fields",
    "tolerant_schema_reader",
]


class UnknownSchema(NamedTuple):
    """One thing a tolerant reader met and could not interpret."""

    kind: str
    """``"value"`` for an enum member, ``"field"`` for a key a model does not declare."""

    owner: str
    """The enum or model class name."""

    name: str
    """The raw value, or the key."""


#: Where a tolerant reader accumulates what it did not understand. ``None`` -- the
#: default -- means tolerance is off and unknown schema is rejected as usual.
_sink: ContextVar[Optional[List[UnknownSchema]]] = ContextVar(
    "agentjobs_unknown_schema", default=None
)


@contextmanager
def tolerant_schema_reader() -> Iterator[List[UnknownSchema]]:
    """Accept unknown enum values and undeclared fields while active, collecting them.

    Yields the list the parse fills in, so a caller can warn about the skew it just
    absorbed rather than discovering it much later, or never. The list is empty on a
    matched reader and service, which is the ordinary case.

    Nesting is supported and each level gets its own list; the previous one is
    restored on exit, so an inner parse cannot swallow an outer parse's findings.
    """
    collected: List[UnknownSchema] = []
    token = _sink.set(collected)
    try:
        yield collected
    finally:
        _sink.reset(token)


def is_tolerant() -> bool:
    """Whether a tolerant parse is active. One lookup, so strict callers pay nothing."""
    return _sink.get() is not None


def _record(item: UnknownSchema) -> bool:
    collected = _sink.get()
    if collected is None:
        return False
    # Pydantic can attempt a value more than once while resolving a union, and a log
    # can legitimately carry the same unknown value in many entries. The warning is
    # about the skew, not about how often it appears.
    if item not in collected:
        collected.append(item)
    return True


def record_unknown_enum_value(enum_name: str, value: str) -> bool:
    """Note an unknown value, returning whether the caller may tolerate it.

    Called from :meth:`agentjobs.models_v2.ValueEnum._missing_`. Returns ``False``
    when no tolerant context is active, which is the default and means the value is
    rejected exactly as it has always been.
    """
    return _record(UnknownSchema("value", enum_name, value))


def record_unknown_fields(model_name: str, keys: Iterable[str]) -> bool:
    """Note keys a model does not declare, returning whether they may be dropped.

    Called from :class:`agentjobs.models_v2.StrictModel`'s before-validator. ``False``
    outside a tolerant context, where ``extra="forbid"`` then rejects them by name.
    """
    if _sink.get() is None:
        return False
    for key in keys:
        _record(UnknownSchema("field", model_name, key))
    return True
