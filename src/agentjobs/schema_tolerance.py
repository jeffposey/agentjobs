"""Opt-in tolerance for enum values a reader's copy of the schema does not know.

Every process that talks to AgentJobs over HTTP carries its own copy of
``models_v2``. That copy is a *reader*, not an authority: the service validated the
task on write and again on read, so a value the service is willing to serve is by
definition legal. When the reader is older than the service, re-checking can only
produce false negatives -- and on 2026-08-19 it produced a total one. Adding
``AUTO = "auto"`` to :class:`~agentjobs.models_v2.DispatchPosture` made task-107
unreadable to every process started before the change: the MCP client's
``task_handoff`` failed with ``log.12.posture: Input should be 'read_only',
'supervised' or 'autonomous'`` and ``retryable: false``, so an agent could not record
finished work against a task the service was serving over ``curl`` perfectly happily.

Adding a member to an enum is backward-compatible by construction -- old data stays
valid. A strict reader inverts that and turns every widening into a fleet-wide
breaking change requiring every session to restart.

So a reader may ask to *degrade* instead: inside :func:`tolerant_enum_values`, an
unknown enum value is kept as an opaque string rather than rejected, and the caller is
handed the list of what it did not understand so the skew is still visible.

**This is for clients, and only for clients.** It is off by default and must stay off
on the write path and in ``storage``:

* Writing an unknown enum value must still be refused -- this is about what a reader
  will *accept*, never about what may be stored.
* A file that genuinely cannot be read must still be reported loudly (task-049).
  Tolerance covers unknown *members* of a known enum. Everything else -- a missing
  required field, a wrong type, an unknown field under ``extra="forbid"`` -- still
  fails exactly as before.

The switch is a :class:`~contextvars.ContextVar`, so it is scoped to the parse that
asked for it and is not visible to another thread or another task in the same process.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, List, Optional, Tuple

__all__ = ["UnknownEnumValue", "record_unknown_enum_value", "tolerant_enum_values"]

#: One value a tolerant reader met and could not interpret: (enum name, raw value).
UnknownEnumValue = Tuple[str, str]

#: Where a tolerant reader accumulates what it did not understand. ``None`` -- the
#: default -- means tolerance is off and unknown values are rejected as usual.
_sink: ContextVar[Optional[List[UnknownEnumValue]]] = ContextVar(
    "agentjobs_unknown_enum_values", default=None
)


@contextmanager
def tolerant_enum_values() -> Iterator[List[UnknownEnumValue]]:
    """Accept unknown enum values while this context is active, collecting them.

    Yields the list the parse fills in, so a caller can warn about the skew it just
    absorbed rather than discovering it much later, or never. The list is empty on a
    matched reader and service, which is the ordinary case.

    Nesting is supported and each level gets its own list; the previous one is
    restored on exit, so an inner parse cannot swallow an outer parse's findings.
    """
    collected: List[UnknownEnumValue] = []
    token = _sink.set(collected)
    try:
        yield collected
    finally:
        _sink.reset(token)


def record_unknown_enum_value(enum_name: str, value: str) -> bool:
    """Note an unknown value, returning whether the caller may tolerate it.

    Called from :meth:`agentjobs.models_v2.ValueEnum._missing_`. Returns ``False``
    when no tolerant context is active, which is the default and means the value is
    rejected exactly as it has always been.
    """
    collected = _sink.get()
    if collected is None:
        return False
    entry = (enum_name, value)
    # Pydantic can attempt a value more than once while resolving a union, and a log
    # can legitimately carry the same unknown value in many entries. The warning is
    # about the skew, not about how often it appears.
    if entry not in collected:
        collected.append(entry)
    return True
