"""The epic walks open on this machine, read once per request and offered to every task.

A walked epic is claimed and reads ``agent``/``work`` for as long as its walk is open,
because the walk is its supervisor. Nothing about the record says the supervisor is the
server rather than an agent editing it, so every chip drew it "Working" while the only
thing happening was children being started (task-591). This carries the walk onto the
task read so ``task_status`` can say "Walking", on every surface, from one fact.

One query, not one per row: ``open_walks`` is a single indexed read of the supervision
table, and it names the handful of epics being walked, so a list of five hundred rows
costs what a list of one does -- the shape :mod:`agentjobs.api.live_finish` has.

Nothing here raises, and nothing here writes, for the reasons that binding gives: a
label may not cost the read, and the reading half starts and holds nothing.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Optional

from fastapi import Request

from agentjobs.models_v2 import LiveWalkState
from agentjobs.projects import default_home

from .request_scope import request_project_id

_BINDING: ContextVar[Optional["LiveWalkBinding"]] = ContextVar(
    "agentjobs_live_walk_binding", default=None
)


class LiveWalkBinding:
    """One request's answer to "is an epic walk supervising this task". Lazy, like its siblings."""

    def __init__(self, request: Request) -> None:
        self._request = request
        self._walks: Optional[Dict[str, LiveWalkState]] = None

    def for_task(self, task_id: str, task_open: Optional[bool] = None) -> Optional[LiveWalkState]:
        # A closed epic is not being walked, whatever the supervision row says in the
        # second before the walk's own tick ends it.
        if task_open is False:
            return None
        if self._walks is None:
            self._walks = self._load()
        return self._walks.get(task_id)

    def _load(self) -> Dict[str, LiveWalkState]:
        project_id = request_project_id(self._request)
        if not project_id:
            return {}
        try:
            return live_walks(project_id)
        except Exception:  # noqa: BLE001 - a label may not cost the read; see the module docstring
            return {}


def live_walks(project_id: str) -> Dict[str, LiveWalkState]:
    """``{parent_task_id: state}`` for every open walk in ``project_id``."""
    # Imported here rather than at module scope, for the reason the sibling bindings
    # give: this pulls in the dispatch stack, and `api.models` is imported by everything
    # that reads a task.
    from agentjobs.dispatch.journal import journal

    return {
        walk.parent_task_id: LiveWalkState(walk_id=walk.walk_id, grounded=bool(walk.grounding))
        for walk in journal(default_home()).open_walks(project_id=project_id)
    }


async def bind_live_walks(request: Request) -> None:
    """Make this request's open walks readable by every task read it builds.

    Installed beside ``bind_live_finishes`` and for its reasons, ``async def`` included:
    a synchronous dependency runs in a worker thread with a copy of the context.
    """
    _BINDING.set(LiveWalkBinding(request))


def live_walk_for(task_id: str, task_open: Optional[bool] = None) -> Optional[LiveWalkState]:
    """The open walk supervising ``task_id``, or ``None``. ``None`` outside a request."""
    binding = _BINDING.get()
    if binding is None:
        return None
    return binding.for_task(task_id, task_open)
