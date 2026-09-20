"""The dashboard API consumed by the React client.

Reads, with one exception: acknowledging an attention episode is a write, and it is
here rather than in a router of its own because it is the same state the badge reads
and belongs beside it (task-422).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from agentjobs.attention import AttentionState, acknowledge, reconcile, waiting_path
from agentjobs.dashboard import build_dashboard_snapshot
from agentjobs.dispatch.config import machine_ceiling
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Task
from agentjobs.principals import Principal
from agentjobs.projects import Project

from ..dependencies import current_identity, get_project, get_principal, get_task_manager
from ..models import (
    AttentionAckRequest,
    AttentionEpisodeView,
    AttentionResponse,
    DashboardResponse,
    ReviewIdentity,
    TaskRead,
)

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_project),
    principal: Optional[Principal] = Depends(get_principal),
) -> DashboardResponse:
    """Return the dashboard projection, including its single next action.

    ``identity`` rides along for the same reason it rides along on the task detail and
    the playbook listing: the next-up panel offers a Dispatch button, a run has to be
    attributed to a real person, and a button that cannot name one must be disabled with
    the reason rather than pressable into a refusal.
    """
    # The preview is sized by the *machine*, not by this project: the slot board draws
    # one cell per run slot and each free cell has to offer a different task, so a
    # ceiling of six needs six (task-092). Read from the same function
    # `GET /api/runs/live` reports the ceiling with, so the board's cell count and the
    # supply of tasks for it cannot come from two different readings of one file.
    ceiling, _configured = machine_ceiling()
    snapshot = build_dashboard_snapshot(manager, preview_limit=ceiling)
    # The corpus the snapshot was built from, handed to the facts rather than letting
    # them list the project again in the other shape (task-485). `list_tasks` is free
    # here: the request's corpus scope already holds it, and this is what says so at the
    # call site instead of leaving it to a scope being open.
    facts = manager.dependency_facts(corpus=manager.list_tasks())
    identity = current_identity(project, principal)

    def read(task: Optional[Task]) -> Optional[TaskRead]:
        return TaskRead.from_task(task, facts[task.id]) if task is not None else None

    def reads(tasks: list[Task]) -> list[TaskRead]:
        return [TaskRead.from_task(task, facts[task.id]) for task in tasks]

    return DashboardResponse(
        **{
            **snapshot,
            "active_tasks": reads(snapshot["active_tasks"]),
            "waiting_tasks": reads(snapshot["waiting_tasks"]),
            "backlog_tasks": reads(snapshot["backlog_tasks"]),
            "next_task": read(snapshot["next_task"]),
            "queue_preview": reads(snapshot["queue_preview"]),
            "identity": ReviewIdentity(
                ok=identity.ok,
                user=identity.user,
                problem=identity.problem,
                detail=identity.detail,
            ),
        }
    )


def _attention_view(state: AttentionState, project_id: str) -> AttentionResponse:
    """Render reconciled attention state for a client."""
    if state.episode is None:
        return AttentionResponse(blocking=state.blocking, episode=None)
    lead = state.waiting[0] if state.waiting else None
    return AttentionResponse(
        blocking=state.blocking,
        episode=AttentionEpisodeView(
            id=state.episode.id,
            started_at=state.episode.started_at,
            acknowledged=state.episode.acknowledged,
            tasks=list(state.episode.members),
            lead_task_id=lead.id if lead else None,
            lead_task_title=lead.title if lead else None,
            # Computed once, server-side, because task-423 gave the rule a third caller
            # in a second language: a service worker rendering a push that arrived while
            # no page was running cannot import the React module that used to own it.
            deep_link=waiting_path(project_id, state.episode),
        ),
    )


@router.get("/attention", response_model=AttentionResponse)
async def get_attention(
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_project),
) -> AttentionResponse:
    """Return how many tasks are stopped waiting on a person, plus the episode.

    The header polls this on every surface, so it deliberately answers with one
    integer and one small object instead of the records behind them -- see
    :class:`AttentionResponse`. It is the same predicate the dashboard's own tile and
    the legacy header use, from the same function, because a badge that disagrees with
    the page it links to is worse than no badge.

    **This read reconciles**, which is the one thing about it worth knowing. The
    episode is a fact about the waiting set, so it is brought up to date here rather
    than on a clock of its own: the header was polling anyway, and a reconcile is
    idempotent, so polling cannot manufacture attention. Nothing in the answer depends
    on a notification having been delivered.
    """
    return _attention_view(reconcile(manager, project.id), project.id)


@router.post("/attention/ack", response_model=AttentionResponse)
async def acknowledge_attention(
    payload: AttentionAckRequest,
    manager: TaskManager = Depends(get_task_manager),
    project: Project = Depends(get_project),
) -> AttentionResponse:
    """Record that a person deliberately acted on the episode they were shown.

    Acknowledgment stops the *interruption*, not the indicator: the badge tracks the
    waiting set and stays up until it empties. What it buys is that the next task to
    stop on this person may interrupt again, which is the whole of the anti-fatigue
    rule (task-422's decision entry).

    An id that is no longer current is not an error -- see
    :func:`agentjobs.attention.acknowledge`. The caller gets the current state back and
    renders it.
    """
    return _attention_view(acknowledge(manager, project.id, payload.episode_id), project.id)
