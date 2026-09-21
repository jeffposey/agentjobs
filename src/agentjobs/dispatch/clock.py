"""The dispatch subsystem's one time source (task-518).

**Nothing under ``agentjobs.dispatch`` reads the machine's clock directly.** It calls
:func:`utcnow`, :func:`monotonic` or :func:`sleep` here, and those read whichever
:class:`Clock` is installed. Outside a test that is :class:`SystemClock`, which does
exactly what the calls it replaced did.

**Why one source rather than an injectable parameter per call.** Most of this subsystem
already accepted a ``clock=`` argument before this module existed, and the flake that
motivated it was in a file that did::

     881:  clock: Callable[[], datetime] = utcnow           <- injectable
    1737:  monotonic: Callable[[], float] = time.monotonic  <- injectable

     315:  opened_at=_parse(row["opened_at"]) or utcnow()   <- not injectable
     318:  next_probe_at=_parse(row["next_probe_at"]) or utcnow()

A reviewer reading the first two would conclude the file was done. The four fallbacks
below them called the real clock anyway, so a test driving simulated time had two
timelines that agreed on an idle machine and drifted on a busy one. **A partly injected
clock is worse than none**, because it looks finished. Reading through one function
makes "is this file converted" a question a grep answers.

**What is deliberately still real.** A subprocess takes real time to start and real time
to die, and the poller's own loop interval is a real wait by a live process. Those are
not decisions about a threshold; they are the machine doing work, and faking them would
prove nothing (task-325 is the subject there). What reads through here is what *decides*:
when a probe is due, when a stall has lasted long enough to escalate, how long a
stand-down may take to confirm, whether a spend window has closed.

**Installing one is a test's job and nobody else's.** :func:`installed` is a context
manager and :data:`INSTALLED` is the name a fixture monkeypatches; both put the previous
clock back, so nothing is left installed by accident and the production default is never
replaced in a process that is doing work.
"""

from __future__ import annotations

import threading
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """What the dispatch subsystem asks about time. Three questions, no more.

    ``now`` for anything stamped or compared against a stored moment, ``monotonic`` for
    measuring an interval that must not move when the wall clock does, and ``sleep`` for
    a wait whose length is a decision rather than a process's own latency.
    """

    def now(self) -> datetime:
        """The current moment, timezone-aware and in UTC."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never going backwards."""

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` before returning."""


class SystemClock:
    """The machine's own clock: what every call here did before this module existed."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return _time.monotonic()

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)


SYSTEM = SystemClock()

INSTALLED: Clock = SYSTEM
"""The clock this process reads. Swap it with :func:`installed`, or -- in a harness whose
lifetime is a fixture's rather than a block's -- with ``monkeypatch.setattr`` on this
name, which pytest undoes at teardown. Read through :func:`utcnow`, :func:`monotonic` and
:func:`sleep` rather than directly, so a swap reaches call sites that already hold a
reference to nothing but this module."""

_lock = threading.Lock()


def clock() -> Clock:
    """The clock this process reads. :data:`SYSTEM` unless a test installed another."""
    return INSTALLED


@contextmanager
def installed(replacement: Clock) -> Iterator[Clock]:
    """Install ``replacement`` for the duration of the block, then put the old one back.

    Process-wide rather than per thread, because the thing being tested spans threads --
    a poller tick, a runner following a session, a finish waiting on the merge runway --
    and a clock only one of them could see would be the two-timeline bug again.
    """
    global INSTALLED
    with _lock:
        previous = INSTALLED
        INSTALLED = replacement
    try:
        yield replacement
    finally:
        with _lock:
            INSTALLED = previous


def utcnow() -> datetime:
    """The current moment in UTC, from the installed clock."""
    return INSTALLED.now()


def monotonic() -> float:
    """A monotonic reading, from the installed clock."""
    return INSTALLED.monotonic()


def sleep(seconds: float) -> None:
    """Wait, on the installed clock -- which under a skipping clock is not a wait."""
    INSTALLED.sleep(seconds)


__all__ = [
    "Clock",
    "INSTALLED",
    "SYSTEM",
    "SystemClock",
    "clock",
    "installed",
    "monotonic",
    "sleep",
    "utcnow",
]
