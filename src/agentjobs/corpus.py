"""One read of the task corpus per request, instead of one per question asked of it.

A dashboard asks the corpus nine separate questions -- what is active, what is waiting,
what is claimable, which dependencies are met, which parents have open children, which
records failed to import -- and before task-485 each of those questions loaded every
task in the project from scratch. Nine loads of 488 tasks and 5,545 log entries is 1.6
seconds of the 2 the endpoint took on an idle machine, and none of the nine can see that
the other eight exist.

This module is the scope that lets them. Inside :func:`corpus_scope` the first load is
kept and the rest are answered from it; outside one, nothing is kept at all.

**It is a scope, not a cache.** There is no expiry to tune and no staleness window to
reason about, because the memo cannot outlive the block that opened it: a later request
opens its own and starts from the database. A write inside the block calls
:func:`discard`, so a handler that saves a task and then reads the corpus reads the
corpus it just changed. That is the property a time-based cache in front of a slow query
would not have given us, and the reason this is not one.

**Why the snapshot is an object in the ContextVar rather than a dict.** For the reason
``instrumentation`` records: FastAPI runs a synchronous handler in a worker thread and
*copies* the context into it, so a ContextVar set inside the worker is invisible to the
middleware outside. A copied context still points at the same object, so the scope is
opened by installing one object and closed by emptying it -- and emptying it is what
makes a thread that outlived the request (a dispatch watcher holding a copied context)
fall back to loading rather than reading a snapshot that stopped being true.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Hashable, Iterator, Optional, TypeVar

T = TypeVar("T")


class _Snapshot:
    """What one scope kept, and whether that scope is still open."""

    __slots__ = ("entries", "closed")

    def __init__(self) -> None:
        self.entries: Dict[Hashable, Any] = {}
        self.closed = False


_scope: ContextVar[Optional[_Snapshot]] = ContextVar("agentjobs_corpus_scope", default=None)


@contextmanager
def corpus_scope() -> Iterator[None]:
    """Answer repeated corpus reads inside this block from a single load.

    Re-entrant: a nested scope defers to the outer one rather than starting a second
    snapshot, so a helper that opens one is safe to call from a request that already
    has one open.
    """
    existing = _scope.get()
    if existing is not None and not existing.closed:
        yield
        return
    snapshot = _Snapshot()
    token = _scope.set(snapshot)
    try:
        yield
    finally:
        # Emptied *and* marked closed, in that order, so a context copied into a thread
        # that outlives this block neither holds the records alive nor reads them.
        snapshot.entries.clear()
        snapshot.closed = True
        _scope.reset(token)


def memoised(key: Hashable, produce: Callable[[], T]) -> T:
    """``produce()``, once per key per open scope; every time when there is no scope."""
    snapshot = _scope.get()
    if snapshot is None or snapshot.closed:
        return produce()
    try:
        return snapshot.entries[key]  # type: ignore[no-any-return]
    except KeyError:
        value = produce()
        snapshot.entries[key] = value
        return value


def discard() -> None:
    """Forget what the open scope kept, because the corpus has just changed.

    Called by the store's writes. A read after a write in the same request must see the
    write, which is the whole difference between a scope and a cache.
    """
    snapshot = _scope.get()
    if snapshot is not None:
        snapshot.entries.clear()


def scope_is_open() -> bool:
    """Whether a scope is currently keeping anything. Used by tests and the gate."""
    snapshot = _scope.get()
    return snapshot is not None and not snapshot.closed
