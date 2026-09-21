"""The finishes running on this machine, read once per request and offered to every task.

Approving a task on a project with ``finish.enabled`` spawns a scripted finish: rebase,
full gate, ``--no-ff`` merge, rebuild, restart, verify -- three to four minutes on one
click. **Nothing about the task record changes to say so.** The ball is handed to
``agent``/``work`` and stays there for the whole attempt, so ``display_status`` computed
"In progress" and every list, board and count showed a task mid-merge as an ordinary
agent-held one. The only honest surface was the task page's ``FinishPanel``, which polls
the per-task read (task-321); this is the same fact, made cheap enough to put on a row
(task-509).

**Why it is not a field on the record.** For the reason
:mod:`agentjobs.api.queued_dispatch` gives about a queued dispatch, and one more: the
finish is a separate process that may be killed, or whose machine may reboot mid-gate.
A flag written onto the record at spawn would then say "finishing" forever, while the
reading half already knows how to tell a holder that is working from one whose process
is gone -- ``finish_status`` derives liveness from the run lock, which is what makes
``interrupted`` a state a reader is told about rather than a spinner that never stops.

**Why one scan rather than a lookup per row.** ``read_finish_status`` reads up to
``SCAN_LIMIT`` finish directories to find the one attempt it was asked about, so a list
of 500 tasks calling it per row would do that 500 times -- about six seconds, measured
in :func:`agentjobs.dispatch.finish_status.live_finishes`, for a fact that is ``None``
for all but at most one or two rows. ``live_finishes`` turns the question round: one
scan says which tasks *might* be finishing, and only those are confirmed. A list of 500
rows then costs the same 12ms a list of one does.

Nothing here raises, and nothing here writes. The reading half stays the reading half:
it starts nothing, waits for nothing and holds no lock, so a hundred browsers polling a
list while a finish runs changes nothing about how that finish goes.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Dict, Optional, Set

from fastapi import Request

from agentjobs.models_v2 import LiveFinishState
from agentjobs.projects import default_home

from .request_scope import request_project_id

_BINDING: ContextVar[Optional["LiveFinishBinding"]] = ContextVar(
    "agentjobs_live_finish_binding", default=None
)


class LiveFinishBinding:
    """One request's answer to "is a finish running against this task's branch".

    Lazy in both halves, as the queued-dispatch binding is: the project is resolved on
    the first question asked and the finishes are scanned once after that. A request
    that constructs no task read -- which is most of them -- never touches either.
    """

    def __init__(self, request: Request) -> None:
        self._request = request
        self._states: Optional[Dict[str, LiveFinishState]] = None
        self._closed_by: Set[str] = set()

    def for_task(self, task_id: str, task_open: Optional[bool] = None) -> Optional[LiveFinishState]:
        """The finish running against this task right now, or ``None``.

        A **closed** task gets one only from the finish that plausibly closed it --
        ``finish_status.did_the_closing``, the same rule the task page's panel applies,
        so the chip and the panel cannot disagree (task-514). Such a finish goes on
        working for a second or two afterwards, removing the worktree and deleting the
        branch, and a chip there is right. A finish holding a closed task having merged
        nothing and closed nothing cannot be that one, and a row reading "Finishing"
        beside a task reading "Completed" is the disagreement task-506 spent twenty
        minutes being.
        """
        if self._states is None:
            self._states = self._load()
        state = self._states.get(task_id)
        if state is not None and task_open is False and task_id not in self._closed_by:
            return None
        return state

    def _load(self) -> Dict[str, LiveFinishState]:
        # Imported here rather than at module scope, for the reason the sibling binding
        # gives: this pulls in the dispatch stack, and `api.models` is imported by
        # everything that reads a task.
        from agentjobs.dispatch.finish_status import (
            STEP_MEANING,
            did_the_closing,
            live_finishes,
        )

        project_id = request_project_id(self._request)
        if not project_id:
            return {}
        try:
            home = default_home()
            statuses = live_finishes(home, project_id)
        except Exception:  # noqa: BLE001 - a label may not cost the read; see the module docstring
            return {}
        self._closed_by = {
            task_id
            for task_id, status in statuses.items()
            if did_the_closing(status.merge_commit, status.steps)
        }
        return {
            task_id: LiveFinishState(
                finish_id=status.finish_id,
                state=status.state,
                started_at=status.started_at,
                current_step=status.current_step,
                step_meaning=STEP_MEANING.get(status.current_step, ""),
                branch=status.branch,
            )
            for task_id, status in statuses.items()
        }


async def bind_live_finishes(request: Request) -> None:
    """Make this request's live finishes readable by every task read it builds.

    Installed application-wide in :mod:`agentjobs.api.main`, beside
    ``bind_queued_dispatches`` and for the same reasons -- a route added tomorrow is
    covered the moment it is registered, and a dependency rather than middleware because
    middleware runs before routing and would not know the ``project_id`` path parameter.

    ``async def`` is load-bearing: FastAPI runs a synchronous dependency in a worker
    thread, which gets a *copy* of the context, so a context variable set there would be
    invisible to the endpoint.
    """
    _BINDING.set(LiveFinishBinding(request))


def live_finish_for(task_id: str, task_open: Optional[bool] = None) -> Optional[LiveFinishState]:
    """The finish running against ``task_id`` in this request's project.

    ``None`` outside a request, which is what a unit test constructing a read model by
    hand gets: no binding, no scan, no label. ``task_open`` is the row's own answer to
    "is this task still open"; see :meth:`LiveFinishBinding.for_task`.
    """
    binding = _BINDING.get()
    if binding is None:
        return None
    return binding.for_task(task_id, task_open)
