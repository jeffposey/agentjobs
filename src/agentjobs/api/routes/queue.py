"""The project's queue: the ordered backlog, and the two ways to fix a broken one.

Design section 10's project-level rows. Everything here reads or repairs the *whole*
order rather than one task's place in it, which is why it is its own module: the
per-task verbs are `queue-move` and `reprioritize` in ``status.py``, beside the other
verbs they behave exactly like.

Mounted like every other task-facing router -- unscoped at ``/api`` and again under
``/api/projects/{project_id}`` -- so ``GET /api/projects/agentjobs/queue`` and
``GET /api/queue`` are the same handler and cannot drift apart.
"""

from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Depends, Query

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Priority
from agentjobs.projects import Project
from agentjobs.storage import TaskLockTimeout

from .status import acting_actor, get_acting_project, lock_timeout_error
from ..dependencies import get_task_manager
from ..models import (
    QueueAssignmentRead,
    QueueCompactRequest,
    QueueCompactResponse,
    QueueMaintenanceRequest,
    QueueRepairResponse,
    QueueResponse,
)

router = APIRouter(tags=["queue"])


def _renumbered(band: str, moved: List[Any]) -> List[QueueAssignmentRead]:
    """Turn the manager's ``(task_id, position)`` pairs into read models."""
    return [
        QueueAssignmentRead(task=task_id, band=band, position=position)
        for task_id, position in moved
    ]


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    agent: str
    | None = Query(
        default=None,
        description=(
            "Judge claimability for this agent, so a task restricted to somebody else "
            "is marked with that as its reason. Omit it to judge for any agent."
        ),
    ),
    manager: TaskManager = Depends(get_task_manager),
) -> QueueResponse:
    """The whole ordered backlog, band by band. This is the list a human reviews.

    Every band appears, including the empty ones: "critical is empty" is a fact a
    reader of an ordered backlog wants stated rather than inferred from a heading that
    is not there. Every open task carries its claimability and, when it is not
    claimable, the rule that excluded it -- the same sentence ``/tasks/next/explain``
    gives, from the same place, so the two can never disagree.

    **This does not 409 on a broken queue**, unlike ``/tasks/next``. It is one of the
    two deliberate exceptions in design section 8: you have to be able to see a broken
    queue in order to fix it, so the offending bands still render and ``problems``
    names what is wrong beside ``repair_command``.
    """
    return QueueResponse.model_validate(manager.queue_listing(agent=agent).as_dict())


@router.post("/queue/repair", response_model=QueueRepairResponse)
async def repair_queue(
    payload: QueueMaintenanceRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> QueueRepairResponse:
    """Make a broken queue into a queue again, and say exactly what was guessed.

    Operates on a corrupt corpus by definition, so it reads the raw files rather than
    loaded tasks -- the records it most needs are the ones the open-task-has-a-position
    rule refuses to load. Open tasks with no usable position, and the losing claimants
    of a shared one, go to the bottom of their band ordered by ``created`` then id.

    It never invents an opinion it does not have. A duplicate position carries no
    record of who was meant to be first, so that tie-break is arbitrary by necessity --
    and every task it touched comes back in ``assigned``, which is what makes the guess
    reviewable rather than silent.
    """
    acting_actor(project, payload.actor)
    try:
        report = manager.repair_queue()
    except TaskLockTimeout as exc:
        raise lock_timeout_error(exc, held="queue") from exc
    return QueueRepairResponse(
        assigned=[
            QueueAssignmentRead(
                task=assignment.task_id, band=assignment.band, position=assignment.position
            )
            for assignment in report.assigned
        ],
        rebalanced=list(report.rebalanced),
        unrepairable=list(report.unrepairable),
        changed=report.changed,
        report=report.render(),
    )


@router.post("/queue/compact", response_model=QueueCompactResponse)
async def compact_queue(
    payload: QueueCompactRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_acting_project),
) -> QueueCompactResponse:
    """Renumber one band back to 100, 200, 300..., changing nobody's place.

    Purely cosmetic, and explicit only -- there is no automatic compaction, because a
    background process quietly rewriting forty task files is exactly the kind of thing
    that should require somebody to ask for it. One band per request for the same
    reason: compacting the whole corpus is four decisions, not one.
    """
    acting_actor(project, payload.actor)
    band = Priority(payload.band)
    try:
        moved = manager.compact_band(band)
    except TaskLockTimeout as exc:
        raise lock_timeout_error(exc, held="queue") from exc
    return QueueCompactResponse(band=band.value, moved=_renumbered(band.value, moved))
