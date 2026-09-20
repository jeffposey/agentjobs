"""Per-request counters that say how much work a request actually did.

Wall-clock time answers "was it slow"; it does not answer "why", and it is not
comparable between machines. The number that is comparable, and that the performance
work in task-130 is really about, is how many task files a single request parsed.
A request that parses the corpus four times is a defect whether or not the machine
running it happens to be fast enough to hide it.

**Why the counter is a mutable cell rather than an integer in a ContextVar.**
FastAPI runs synchronous route handlers in a worker thread, and the context is
*copied* into that thread. Setting an integer ContextVar inside the worker therefore
updates the copy and is invisible to the middleware that reads it afterwards -- the
first version of this module did exactly that and reported zero parses for every
request. A copied context still points at the same object, so storing a mutable
counter and incrementing its attribute crosses the threadpool hop correctly.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class _Counter:
    """Mutable integers, shared by reference across copied contexts."""

    __slots__ = ("parses", "corpus_loads")

    def __init__(self) -> None:
        self.parses = 0
        self.corpus_loads = 0


_counter: ContextVar[_Counter] = ContextVar("agentjobs_work_counter")


def _current() -> _Counter:
    """The counter for this context, created on first use."""
    try:
        return _counter.get()
    except LookupError:
        counter = _Counter()
        _counter.set(counter)
        return counter


def record_task_parse() -> None:
    """Count one task file read and parsed from disk."""
    _current().parses += 1


def task_parse_count() -> int:
    """How many task files have been parsed in the current context."""
    return _current().parses


def record_corpus_load() -> None:
    """Count one read of every task in a project, in either shape.

    Whole records and task-484's listing projection both count. They cost very
    differently -- 287 ms against 40 ms on this repository's backlog -- but the question
    the counter answers is how many times a request read the project, and reading it
    four times cheaply is the same defect as reading it four times expensively.
    """
    _current().corpus_loads += 1


def corpus_load_count() -> int:
    """How many times the whole corpus has been read in the current context.

    The number the dashboard's cost was really made of: nine loads to answer one
    request, none of which could see the other eight (task-485). It is reported on
    every response beside the parse count for the same reason that one is -- a
    wall-clock threshold stops meaning anything when the hardware changes, and a count
    does not.
    """
    return _current().corpus_loads


def reset_task_parses() -> None:
    """Install a fresh counter for the current context.

    Called once per request, so a header reports that request's work rather than a
    running total that only ever grows over the life of the server. Resets every
    counter here, not only the parses its name records.
    """
    _counter.set(_Counter())


class ParseTally:
    """Work done inside a measured block: task files parsed, corpora loaded."""

    __slots__ = ("_counter", "_started", "_started_loads", "_finished", "_finished_loads")

    def __init__(self, counter: _Counter) -> None:
        self._counter = counter
        self._started = counter.parses
        self._started_loads = counter.corpus_loads
        self._finished: int | None = None
        self._finished_loads: int | None = None

    def _finish(self) -> None:
        self._finished = self._counter.parses
        self._finished_loads = self._counter.corpus_loads

    @property
    def parses(self) -> int:
        """Files parsed since the block began; frozen once the block exits."""
        end = self._counter.parses if self._finished is None else self._finished
        return end - self._started

    @property
    def corpus_loads(self) -> int:
        """Whole-corpus loads since the block began; frozen once the block exits."""
        end = self._counter.corpus_loads if self._finished_loads is None else self._finished_loads
        return end - self._started_loads


@contextmanager
def count_task_parses() -> Iterator[ParseTally]:
    """Count the work the block did against the corpus.

    Used by tests to assert that one request parses each file at most once, and since
    task-485 that it loads the corpus at most once -- the assertions that stay
    meaningful when the hardware changes and a wall-clock threshold no longer means
    anything.
    """
    tally = ParseTally(_current())
    try:
        yield tally
    finally:
        tally._finish()
