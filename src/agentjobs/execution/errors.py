"""What the execution store says when it cannot do what it was asked.

Every one of these means **nothing was committed**. That is the contract the durable
execution design (docs/agent-dispatch-design.md, section 9a) puts on disk failure, lock
contention and a stale owner alike: a caller that catches one must not report the
transition it attempted as having happened, and must not release anything on the strength
of it. The classes exist so a caller can tell *retry shortly* from *stop and repair* from
*somebody else owns this now* without matching on SQLite's message text itself.
"""

from __future__ import annotations


class ExecutionStoreError(RuntimeError):
    """Base class. Nothing the failed call attempted was committed."""


class StoreBusy(ExecutionStoreError):
    """Another process held the database past the busy timeout. Retryable."""


class StorageFailure(ExecutionStoreError):
    """The disk refused: full, read-only, I/O error, or a corrupt file. Not retryable
    by waiting; the class a person repairs (``storage_failure`` in the failure table)."""


class HistoryIncompatible(ExecutionStoreError):
    """The store or an execution's history was written by a version this code does not
    implement. Reads may continue; every mutation is refused (``history_incompatible``)."""


class StaleOwner(ExecutionStoreError):
    """The caller's ownership epoch or control generation is no longer current.

    Raised instead of writing, so a coordinator that lost ownership -- to a restart, a
    takeover or a cancellation -- cannot publish a result over the newer owner's.
    """


class ActivityConflict(ExecutionStoreError):
    """An activity id was reused with a different input. Stable ids name one effect."""


class OwnershipConflict(ExecutionStoreError):
    """A different live attempt already owns this project/task."""


class CapacityExhausted(ExecutionStoreError):
    """Admission found every machine slot occupied, or the hourly start cap reached.

    ``holders`` names the attempts in the way, so the refusal a person reads can say where
    to go rather than only how many.
    """

    def __init__(self, message: str, *, holders: tuple = (), limit: str = "slots") -> None:
        super().__init__(message)
        self.holders = holders
        self.limit = limit


class QueueFull(ExecutionStoreError):
    """The machine's dispatch queue is at its configured length cap (task-459).

    A bound rather than a courtesy: an unbounded queue of authorised dispatches is a
    promise to spend money the person who made it can no longer see the end of.
    """

    def __init__(self, message: str, *, depth: int = 0, limit: int = 0) -> None:
        super().__init__(message)
        self.depth = depth
        self.limit = limit


class AlreadyQueued(ExecutionStoreError):
    """This project/task already has an entry waiting in the dispatch queue.

    One waiting entry per task, for the same reason there is one live run per task: two
    would start two agents on one repository the moment two slots freed together.
    """

    def __init__(self, message: str, *, queue_id: str = "") -> None:
        super().__init__(message)
        self.queue_id = queue_id


class OwnerModeConflict(ExecutionStoreError):
    """A controller of one mode tried to act on a run another mode owns (legacy/durable)."""


__all__ = [
    "ActivityConflict",
    "AlreadyQueued",
    "CapacityExhausted",
    "ExecutionStoreError",
    "HistoryIncompatible",
    "OwnerModeConflict",
    "OwnershipConflict",
    "QueueFull",
    "StaleOwner",
    "StorageFailure",
    "StoreBusy",
]
