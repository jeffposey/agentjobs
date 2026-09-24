"""The health of the run working each task right now, read once per request (task-570).

A task reads "Working" whenever an agent holds its ball on an active record, and that is
all it says: the session behind it may be producing output, parked on a prompt, silent,
or gone. The run board has always known which, through ``ledger.run_health``; the task
list did not, so it had nothing to back a chip that claims *something is happening now*.
This carries the run board's word onto the task read, from the same function, so an
animated chip and the run tile beside it are one fact rather than two.

**Why the run locks rather than ``live_runs``.** ``live_runs`` reads the meta of every
run directory this machine has ever had -- 416 of them on the day this was written --
to find the handful still going. A task's live run holds that task's run lock for as
long as it lives, so the lock directory already *is* the list of live runs, keyed by
project and task, and only the runs it names are read. The cost follows live locks,
never tasks or rows, exactly as :mod:`agentjobs.api.live_finish`'s scan does.

**What it does not report.** A lock held by a scripted finish or the merge runway names
no run: a finish is ``live_finish``'s fact, not this one. And ``finishing`` is never
passed to ``run_health`` here, for the same reason -- a finishing task says so through
its own field, which outranks this one in ``task_status``.

Nothing here raises, and nothing here writes, for the reasons the live-finish binding
gives: a label may not cost the read, and the reading half starts and holds nothing.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional

from fastapi import Request

from agentjobs.projects import default_home

from .request_scope import request_project_id

_BINDING: ContextVar[Optional["LiveRunBinding"]] = ContextVar(
    "agentjobs_live_run_binding", default=None
)


class LiveRunBinding:
    """One request's answer to "what is the run on this task doing". Lazy, like its siblings."""

    def __init__(self, request: Request) -> None:
        self._request = request
        self._health: Optional[Dict[str, str]] = None

    def for_task(self, task_id: str) -> Optional[str]:
        if self._health is None:
            self._health = self._load()
        return self._health.get(task_id)

    def _load(self) -> Dict[str, str]:
        project_id = request_project_id(self._request)
        if not project_id:
            return {}
        try:
            return live_run_health(default_home(), project_id)
        except Exception:  # noqa: BLE001 - a label may not cost the read; see the module docstring
            return {}


def live_run_health(home: Path, project_id: str) -> Dict[str, str]:
    """``{task_id: run_health}`` for every task in ``project_id`` a live run holds.

    Only dispatch locks that name a run count. ``live_lock_holders`` has already dropped
    every lock whose run has concluded, by the rule ``stale_lock_reason`` states, so a
    run read here is one nothing has declared over.
    """
    # Imported here rather than at module scope, for the reason the sibling bindings
    # give: this pulls in the dispatch stack, and `api.models` is imported by everything
    # that reads a task.
    from agentjobs.dispatch.ledger import (
        live_lock_holders,
        read_run,
        run_health,
        runs_root,
        split_lock_name,
    )

    health: Dict[str, str] = {}
    for name, holder in live_lock_holders(home):
        if holder.is_runway or holder.is_finish or not holder.run_id:
            continue
        project, task_id = split_lock_name(name)
        if project != project_id:
            continue
        directory = runs_root(home) / holder.run_id
        if not directory.is_dir():
            continue
        record = read_run(directory)
        if record.is_live:
            health[task_id] = run_health(record)
    return health


async def bind_live_runs(request: Request) -> None:
    """Make this request's live-run health readable by every task read it builds.

    Installed beside ``bind_live_finishes`` and for its reasons, ``async def`` included:
    a synchronous dependency runs in a worker thread with a copy of the context.
    """
    _BINDING.set(LiveRunBinding(request))


def live_run_health_for(task_id: str) -> Optional[str]:
    """The health of the live run holding ``task_id``, or ``None``.

    ``None`` outside a request, and for a task no live run holds.
    """
    binding = _BINDING.get()
    if binding is None:
        return None
    return binding.for_task(task_id)
