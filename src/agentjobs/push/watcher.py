"""The loop that makes push independent of anybody looking at a screen (task-423).

The desktop notifier is driven by a page: the header polls ``/attention``, the episode
advances, and the browser draws a toast. That is enough for a person at their desk and
useless for the case this task exists for -- a laptop closed, a phone in a pocket, and
work that has just stopped on its owner.

So the server watches for itself. One task in the API lifespan, beside the dispatch
poller and for the same reason that one exists: a piece of the system that only worked
while something else was watching it was not working.

**It watches projects that have devices, and nothing else.** A project nobody has
opted in from is skipped entirely, so the cost of this loop on a machine with no push
subscriptions is one directory listing per tick.

**Reconciling is idempotent**, so this loop and any number of open pages advancing the
same episode cannot manufacture attention between them -- see
:func:`agentjobs.attention.reconcile`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..background import never_abandoned
from ..manager import TaskManager
from ..projects import Project, ProjectRegistry
from ..store_factory import task_manager_for
from .delivery import RoundResult, deliver_round
from .subscriptions import subscriptions_path

PUSH_POLL_SECONDS = 15.0
"""How often to look. Between the dispatch poller's cadence and a person's patience.

A handoff reaching a phone within fifteen seconds is indistinguishable from instant;
the cost of asking more often is a database read per project per tick for a person who
is, by construction, not at the keyboard.
"""


def poll_interval() -> float:
    """The tick, overridable from the environment for a test that cannot wait."""
    raw = os.environ.get("AGENTJOBS_PUSH_POLL_SECONDS")
    try:
        return max(1.0, float(raw)) if raw else PUSH_POLL_SECONDS
    except ValueError:
        return PUSH_POLL_SECONDS


def projects_with_devices(home: Optional[Path] = None) -> List[Project]:
    """Registered projects that have at least a device document.

    The file's existence is the filter rather than its contents: reading every one of
    them on every tick to find out that they are empty is the work this is avoiding.
    An empty document costs one wasted reconcile until somebody unsubscribes, which is
    the right way round.
    """
    registry = ProjectRegistry(home)
    found: List[Project] = []
    for project in registry.list_projects():
        try:
            path = subscriptions_path(project.id, home=registry.home)
        except ValueError:  # pragma: no cover - a registry id that is not a filename
            continue
        if path.exists():
            found.append(project)
    return found


def poll_once(home: Optional[Path] = None) -> List[RoundResult]:
    """One pass over every project with devices. Blocking; never raises for one project.

    A project whose store cannot be opened -- a checkout that has moved, a database
    mid-migration -- must not stop the others being served. The failure is returned to
    the caller as an absent result rather than as an exception, because the alternative
    is a loop that dies and takes notification with it silently, which is exactly the
    fault the dispatch poller was written to fix.
    """
    results: List[RoundResult] = []
    registry_home = ProjectRegistry(home).home
    for project in projects_with_devices(home):
        try:
            manager = task_manager_for(project)
            # Always through `task_manager_for`, which is the one place that resolves
            # which database a project is served from. It answers with a remote manager
            # outside the server process; this loop only ever runs inside one, so
            # anything else is a caller that should not have got here.
            if not isinstance(manager, TaskManager):  # pragma: no cover - server-only
                continue
            results.append(deliver_round(manager, project.id, home=registry_home))
        except Exception:  # noqa: BLE001 - one bad project must not silence the rest
            continue
    return results


_WATCHING = False


def is_watching() -> bool:
    """Whether a :func:`watch_forever` loop is running in this process.

    Reported by ``GET /api/projects/{id}/push`` rather than assumed, because the two
    cases where it is false are exactly the two where somebody would otherwise be
    debugging silence: a ``TestClient`` that never entered the lifespan, and a server
    whose loop died. A panel that claimed to be watching either way would be the same
    mistake as a badge that never reaches zero.
    """
    return _WATCHING


def _print_report(message: str) -> None:  # pragma: no cover - trivial
    print(message, flush=True)


async def watch_forever(
    home: Optional[Path] = None,
    *,
    interval: Optional[float] = None,
    report: Callable[[str], None] = _print_report,
) -> None:
    """Poll until cancelled, reporting only the rounds that did something.

    The work runs in a thread, and a shutdown waits for that thread: it reads SQLite and
    posts to an external service, and on the event loop either would stall every request
    for as long as it took. See :mod:`agentjobs.background` for why it is not abandoned.

    Only rounds that attempted a delivery are reported, and each episode is reported
    once. A loop that printed a line every fifteen seconds forever would make the
    server's output unreadable, which is the same judgement the dispatch poller makes
    about runs that are merely still running.
    """
    global _WATCHING
    tick = interval if interval is not None else poll_interval()
    announced: Dict[str, str] = {}
    _WATCHING = True
    try:
        await _watch(tick, home, announced, report)
    finally:
        _WATCHING = False


async def _watch(
    tick: float,
    home: Optional[Path],
    announced: Dict[str, str],
    report: Callable[[str], None],
) -> None:
    while True:
        try:
            # Never `asyncio.to_thread` for work that reads the store: cancelling
            # that await abandons the thread and the lifespan closes the databases out
            # from under it (task-467, `agentjobs.background`).
            results = await never_abandoned(poll_once, home)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - see poll_once
            report(f"Push poll failed: {exc}")
            results = []
        for result in results:
            if result.quiet:
                continue
            key = f"{result.project_id}:{result.episode_id}"
            if announced.get(key) != result.describe():
                announced[key] = result.describe()
                report(f"Push {result.describe()}")
        await asyncio.sleep(tick)
