"""Blocking background work that a shutdown is allowed to wait for.

``asyncio.to_thread`` hands work to an executor and gives the caller nothing but the
await. **Cancelling that await does not stop the thread** -- it only stops watching it.
For most work that is harmless; for work that reads SQLite it is not, because the
server's lifespan closes every database the instant the cancelled task returns, and a
poll still reading through one of those connections does not get an exception. SQLite is
a C library: it takes the process down with an access violation.

Found by task-467, and it was latent long before it. The dispatch poller and the push
watcher both run a blocking poll through ``to_thread`` and both read the task store, so
either could have crashed a shutdown at any time. What made it reliable was giving the
poller more store reading to do per tick: the poll stopped finishing inside the window
between a client's last request and its teardown, so a ``TestClient`` shutdown began
landing on a live read. Eight pytest workers died of it in one gate, in four different
files, with no traceback that named the cause.

The fix is not to make the poll faster. It is that **an abandoned thread is not a
shutdown**, so a cancellation waits for the work it started.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

__all__ = ["never_abandoned"]

T = TypeVar("T")


async def never_abandoned(work: Callable[..., T], *args: Any) -> T:
    """Run *work* in a thread; on cancellation, wait for it instead of walking away.

    Cancellation still propagates -- the caller's loop ends when it is told to. What
    changes is the order: the thread has finished touching whatever it was touching
    before the ``CancelledError`` leaves this function, so a caller that closes
    resources in its ``finally`` closes them after the last reader, not during it.

    A poll that fails on the way out has its exception consumed here rather than
    reported: the loop is already ending, nobody is listening, and an unretrieved
    exception on an abandoned task prints a warning that looks like the fault.
    """
    thread = asyncio.ensure_future(asyncio.to_thread(work, *args))
    try:
        return await asyncio.shield(thread)
    except asyncio.CancelledError:
        try:
            await asyncio.wait({thread})
        except asyncio.CancelledError:
            # Cancelled a second time while waiting. Nothing further can be done from
            # the event loop, and re-raising is honest about it.
            raise
        if thread.done() and not thread.cancelled():
            thread.exception()
        raise
