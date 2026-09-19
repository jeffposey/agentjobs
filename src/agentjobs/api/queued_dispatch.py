"""The machine's dispatch queue, read once per request and offered to every task read.

Since task-459 a dispatch can be authorised and then wait for a free slot. The slot
board shows the waiting entry; the task itself did not, so the page a person actually
opens said nothing was happening to a task the machine had already promised to start
(task-476).

**Why it is not a field on the record.** A queued dispatch deliberately does not claim
the task: ``dispatch.queue`` judges every gate when a slot frees, not at enqueue, which
is the whole of task-459's safety argument. The task genuinely is
``ready``/``agent``/``available`` until something starts. A flag written onto the record
would be a second copy of a fact the queue owns, and it would go stale the moment an
entry is cancelled, refused at start, or started -- none of which writes a counterpart
to the task.

**Why a context variable rather than an argument.** There are eight places in
``api/routes`` that build a ``TaskRead``, across five modules, and a site that forgets is
a surface that lies. Passing the queue to each of them is a fix repeated eight times and
forgotten on the ninth; ``TaskRead``'s validator reading a request-scoped binding is a
construction path that cannot forget. It is the shape ``_fill_self_clearing_wait``
already has, widened by the one thing this fact needs that the record does not carry:
which machine and which project is being asked about.

**What it costs.** One execution-store read per request, and only for a request that
builds a ``TaskRead`` at all -- the binding is lazy, so a dispatch route or a static file
pays nothing. A list of two hundred rows pays the same as a list of one, which is the
"one read shared across the rows" the task asked for rather than a cache with an
invalidation problem. An empty queue -- the ordinary case -- costs that one read and
stops: the incident book is only opened when this project has an entry in the queue to
judge against it.

Nothing here raises. The label is a convenience on a read surface, and an execution
store that cannot be opened must cost the label rather than the task.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Optional

from fastapi import Request

from agentjobs.models_v2 import QueuedDispatchState
from agentjobs.projects import default_home

_BINDING: ContextVar[Optional["QueuedDispatchBinding"]] = ContextVar(
    "agentjobs_queued_dispatch_binding", default=None
)


class QueuedDispatchBinding:
    """One request's answer to "is a dispatch of this task waiting for a slot".

    Lazy in both halves: the project is resolved on the first question asked and the
    queue is read once after that. A request that constructs no ``TaskRead`` -- which is
    most of them -- never touches either.
    """

    def __init__(self, request: Request) -> None:
        self._request = request
        self._states: Optional[Dict[str, QueuedDispatchState]] = None

    def for_task(self, task_id: str) -> Optional[QueuedDispatchState]:
        """The entry waiting to start this task, or ``None``."""
        if self._states is None:
            self._states = self._load()
        return self._states.get(task_id)

    def _load(self) -> Dict[str, QueuedDispatchState]:
        # Imported here rather than at module scope: `dispatch.queue` pulls in the
        # dispatch stack, and `api.models` is imported by everything that reads a task.
        from agentjobs.dispatch import queue as dispatch_queue
        from agentjobs.dispatch import start_pause

        try:
            home = default_home()
            entries = dispatch_queue.waiting(home)
        except Exception:  # noqa: BLE001 - a label may not cost the read; see the module docstring
            return {}
        if not entries:
            return {}
        project_id = self._project_id()
        if not project_id:
            return {}
        # The position is assigned over the machine's whole queue before the project
        # filter runs, for the reason the slot board's rail numbers its rows that way: a
        # task told it is second when two other projects' dispatches go first would be a
        # lie about when it starts.
        mine = [
            (index, entry)
            for index, entry in enumerate(entries, start=1)
            if entry.project_id == project_id
        ]
        if not mine:
            return {}
        try:
            incidents = start_pause.open_pauses(home)
        except Exception:  # noqa: BLE001 - as above
            incidents = {}
        states: Dict[str, QueuedDispatchState] = {}
        for position, entry in mine:
            pause = None
            if incidents:
                try:
                    pause = start_pause.pause_for(
                        home,
                        entry.project_id,
                        runner=entry.request.get("runner"),
                        group=entry.request.get("group"),
                        incidents=incidents,
                    )
                except Exception:  # noqa: BLE001 - as above
                    pause = None
            # One entry per task is the queue's own invariant (`AlreadyQueuedError`), so
            # the first is the only. `setdefault` rather than assignment so a store that
            # somehow held two would report the one that starts first.
            states.setdefault(
                entry.task_id,
                QueuedDispatchState(
                    queue_id=entry.queue_id,
                    position=position,
                    queued_at=entry.queued_at,
                    queued_by=entry.queued_by,
                    source=entry.source,
                    status=entry.status,
                    detail=entry.detail,
                    paused_by=pause.incident_id if pause else "",
                ),
            )
        return states

    def _project_id(self) -> str:
        """Which project this request addresses, or ``""`` when it cannot be told.

        The path parameter where there is one, exactly as ``request_project`` reads it,
        because every API router is mounted both unscoped and under
        ``/api/projects/{project_id}``. An unscoped request falls back to the positional
        default, resolved through the same visibility filter: a project this caller may
        not see answers ``None`` there, and the queue of a project whose tasks are hidden
        must not leak through a label.
        """
        from .dependencies import try_resolve_default_project
        from .dependencies import get_principal

        scoped = self._request.path_params.get("project_id")
        if scoped:
            return str(scoped)
        try:
            project = try_resolve_default_project(get_principal(self._request))
        except Exception:  # noqa: BLE001 - a label may not cost the read
            return ""
        return project.id if project is not None else ""


async def bind_queued_dispatches(request: Request) -> None:
    """Make this request's dispatch queue readable by every ``TaskRead`` it builds.

    Installed application-wide in :mod:`agentjobs.api.main`, so a route added tomorrow is
    covered the moment it is registered -- the reason ``enforce_capability`` is installed
    the same way. A dependency rather than middleware because middleware runs before
    routing and would not know the ``project_id`` path parameter.

    ``async def`` is load-bearing: FastAPI runs a synchronous dependency in a worker
    thread, which gets a *copy* of the context, and a context variable set there would be
    invisible to the endpoint. An async dependency is awaited in the request's own task,
    so what it sets is what the endpoint reads.

    Nothing is reset afterwards and nothing needs to be: each request is served in its
    own asyncio task, which copies the context at creation, so a binding cannot outlive
    the request that made it.
    """
    _BINDING.set(QueuedDispatchBinding(request))


def queued_dispatch_for(task_id: str) -> Optional[QueuedDispatchState]:
    """The dispatch waiting to start ``task_id`` in this request's project.

    ``None`` outside a request, which is what a unit test constructing a ``TaskRead`` by
    hand gets: no binding, no queue, no label.
    """
    binding = _BINDING.get()
    if binding is None:
        return None
    return binding.for_task(task_id)
