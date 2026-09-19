"""A cancelled poll is waited for, not abandoned (task-467).

The crash this pins had no traceback that named it. Eight pytest workers died with
``Windows fatal exception: access violation`` across four unrelated files, and the only
thing in the stack was a poll reading SQLite in one thread while the server's lifespan
closed that database in another. SQLite is a C library: a connection used after it is
closed does not raise, it takes the process down.

``asyncio.to_thread`` is what made it possible. It hands the work to an executor and
gives the caller only the await, so cancelling the await stops the *watching* and not
the work -- and the lifespan then closes every database the instant the cancelled task
returns. The fix is that a cancellation waits for the thread it started.

Latent since the poller was written; task-467 made it reliable by giving each tick more
of the store to read, which is why it is fixed here rather than filed.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from agentjobs.background import never_abandoned


@pytest.mark.asyncio
async def test_a_cancelled_call_waits_for_its_thread() -> None:
    started = threading.Event()
    finished = threading.Event()

    def slow() -> str:
        started.set()
        # Long enough that an abandoning implementation is still inside it when the
        # cancellation is handled, which is the whole condition being tested.
        threading.Event().wait(0.4)
        finished.set()
        return "done"

    task = asyncio.ensure_future(never_abandoned(slow))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set(), "the cancellation walked away from a thread still running"


@pytest.mark.asyncio
async def test_an_ordinary_call_returns_its_value() -> None:
    assert await never_abandoned(lambda value: value * 2, 21) == 42


@pytest.mark.asyncio
async def test_a_failure_reaches_the_caller_unchanged() -> None:
    def boom() -> None:
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        await never_abandoned(boom)


@pytest.mark.asyncio
async def test_a_failure_during_cancellation_is_consumed_rather_than_warned_about() -> None:
    """Nobody is listening by then, and an unretrieved exception looks like the fault."""
    started = threading.Event()

    def fails_slowly() -> None:
        started.set()
        threading.Event().wait(0.4)
        raise RuntimeError("injected on the way out")

    task = asyncio.ensure_future(never_abandoned(fails_slowly))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
