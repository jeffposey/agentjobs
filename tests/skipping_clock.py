"""A clock a test owns, which advances itself when nothing is runnable (task-518).

This is the technique Temporal's time-skipping test server uses, without Temporal
(task-516 settled build-versus-buy and that decision stands). Workflow code there never
calls ``datetime.now()`` and never sleeps against the machine; it reads the runtime's
clock and asks the runtime for timers, and the test server then **jumps the clock to the
next deadline whenever every workflow is blocked on a timer and nothing else is
runnable**. A thirty-day wait finishes in milliseconds, and it does so because the
waiting is a fact the runtime knows about -- not because a test patched something.

Here, the runtime is :mod:`agentjobs.clock`. Everything in the dispatch
subsystem that decides *when* reads through it, so installing one of these is enough:

    with skipping_clock() as fake:
        ...                       # production code, unchanged, at whatever speed

**No test calls ``advance()``.** That is the whole difference between this and the
``FakeClock`` it replaces. A hand-advanced clock puts the schedule in the test, so it can
only skip the moments its author predicted, and a threshold the production code consults
that the author did not think of is read against a timeline nobody moved. That is the
shape of the bug this exists to remove, not a smaller version of it.

**How it decides nothing is runnable.** Every thread that will wait on this clock
registers as a *waiter* -- the test's own thread is registered by the context manager,
and :meth:`SkippingClock.waiter` registers any other. A waiter is *blocked* exactly while
it is inside :meth:`SkippingClock.sleep`, with a deadline. When the number of blocked
waiters reaches the number of registered ones, no registered thread can make progress
without time moving, so the clock jumps to the earliest deadline and wakes whoever it
reached. A waiter that is computing rather than sleeping holds the clock still, which is
what makes the jump safe.

The rejected alternative, and why: a watchdog that advances after some quiet period of
*real* time. It is simpler and it reintroduces exactly the failure mode being removed --
the quiet period is a wall-clock measurement, so a loaded machine makes it fire while a
waiter is still computing, and the clock jumps a deadline the code had not yet reached.
The whole point is a rule that does not consult the machine.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from agentjobs import clock as dispatch_clock

#: How far a skipping clock starts from the wall clock, and the answer is **not at all**.
#:
#: Task-414's ``FakeClock`` started two hours ahead, for a good reason that has since gone
#: away: anything stamped with real time -- a task log entry from the manager, an execution
#: attempt's admission -- was then always in the fake clock's past, however slowly a loaded
#: machine ran the test. Since task-518 those stamps read this clock too, so there is
#: nothing left to stay ahead of.
#:
#: And the skew actively breaks one thing, which is why it is zero rather than merely
#: unnecessary. **A process's creation time is an OS fact and cannot be faked.** Task-505's
#: pid-reuse guard asks whether the process now at a recorded pid was created *after* the
#: moment that pid was recorded -- and with the clock two hours ahead, every process on the
#: machine was created before every recorded moment, so a stranger holding a dead child's
#: pid read as the original. That is task-505's defect reintroduced by the harness, and it
#: turned `test_an_unknown_launch_keeps_its_ownership_when_another_dispatch_runs` red under
#: a contended gate while it passed alone.
#:
#: So: a clock a test owns may skip forward freely, and must start where the machine is.
DEFAULT_SKEW = timedelta(0)


class ClockDeadlock(AssertionError):
    """Every waiter is blocked and none of them is waiting for time.

    Raised rather than hung, because a hang in a suite is a twenty-minute timeout and a
    stack trace nobody kept. It means a thread registered as a waiter and then blocked on
    something this clock cannot see -- a real lock, a socket, a subprocess.
    """


class SkippingClock:
    """A :class:`~agentjobs.clock.Clock` that moves when nothing else can."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self.origin = start if start is not None else datetime.now(timezone.utc) + DEFAULT_SKEW
        self._offset = 0.0
        self._condition = threading.Condition()
        self._registered = 0
        self._deadlines: Dict[int, float] = {}
        #: Every jump this clock made, oldest first, as (from, to) offsets. A test that
        #: wants to assert the code really waited reads this rather than a wall clock.
        self.jumps: List[tuple] = []

    # -- the Clock protocol ----------------------------------------------------------

    def now(self) -> datetime:
        """The current moment. Reading does not move it -- only a wait does."""
        with self._condition:
            return self.origin + timedelta(seconds=self._offset)

    def monotonic(self) -> float:
        """Seconds of this clock's own time since its origin."""
        with self._condition:
            return self._offset

    def sleep(self, seconds: float) -> None:
        """Block until this clock reaches ``now + seconds``, moving it if it must.

        The move happens here, in the last waiter to block, because that is the moment
        the condition becomes true: every registered thread is inside this method, so
        none of them can make progress until the clock moves.
        """
        if seconds <= 0:
            return
        key = threading.get_ident()
        with self._condition:
            deadline = self._offset + seconds
            self._deadlines[key] = deadline
            try:
                while self._offset < deadline:
                    if len(self._deadlines) >= max(self._registered, 1):
                        self._jump_locked()
                    else:
                        if not self._condition.wait(timeout=30.0):
                            raise ClockDeadlock(
                                f"{self._registered - len(self._deadlines)} waiter(s) "
                                "neither blocked on this clock nor making progress"
                            )
            finally:
                self._deadlines.pop(key, None)
            self._condition.notify_all()

    # -- registration ----------------------------------------------------------------

    @contextmanager
    def waiter(self) -> Iterator["SkippingClock"]:
        """Register the calling thread as something the clock must wait for.

        A thread that is *not* registered is invisible: the clock will jump past it. That
        is correct for a thread doing real work against a real resource -- a subprocess,
        a socket -- and wrong for one running production code with thresholds in it, so
        anything spawned to run the code under test registers here.
        """
        with self._condition:
            self._registered += 1
            self._condition.notify_all()
        try:
            yield self
        finally:
            with self._condition:
                self._registered -= 1
                self._condition.notify_all()

    # -- what a test reads afterwards ------------------------------------------------

    def at(self, seconds: float) -> datetime:
        """The moment ``seconds`` after this clock's origin, whatever it reads now."""
        return self.origin + timedelta(seconds=seconds)

    @property
    def elapsed(self) -> float:
        """Seconds this clock has covered since its origin. Never wall-clock seconds."""
        with self._condition:
            return self._offset

    # -- internals -------------------------------------------------------------------

    def _jump_locked(self) -> None:
        target = min(self._deadlines.values())
        if target > self._offset:
            self.jumps.append((self._offset, target))
            self._offset = target
        self._condition.notify_all()


def install(monkeypatch: Any, start: Optional[datetime] = None) -> SkippingClock:
    """Install a skipping clock for the rest of this test, undone at teardown.

    For a harness built inside a fixture, where the clock has to outlive the call that
    installs it and ``with`` would mean indenting every scenario. The calling thread is
    not registered as a waiter and does not need to be: with none registered, a wait is
    a wait by the only thread there is, so the clock moves at once.
    """
    fake = SkippingClock(start)
    monkeypatch.setattr(dispatch_clock, "INSTALLED", fake)
    return fake


@contextmanager
def skipping_clock(start: Optional[datetime] = None) -> Iterator[SkippingClock]:
    """Install a :class:`SkippingClock` over the dispatch subsystem for this block.

    The calling thread is registered, so a single-threaded test -- which is nearly all of
    them -- finds that a production wait of any length returns at once, having moved the
    clock by exactly the amount the production code asked for.
    """
    fake = SkippingClock(start)
    with dispatch_clock.installed(fake), fake.waiter():
        yield fake
