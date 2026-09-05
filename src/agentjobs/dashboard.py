"""Dashboard projection shared by the React API and legacy Jinja compatibility views."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, Lifecycle, Outcome, Task
from agentjobs.queue import REPAIR_COMMAND, QueueCorruptionError, problem_dicts


class RecentUpdate(TypedDict):
    """A compact log entry rendered by the dashboard."""

    task_id: str
    task_title: str
    timestamp: datetime
    summary: str
    author: str


class QueueBroken(TypedDict):
    """Why the queue could not be read, and the command that repairs it."""

    problems: List[Dict[str, Any]]
    repair_command: str


class DashboardSnapshot(TypedDict):
    """The complete read model shared by both dashboard clients."""

    stats: Dict[str, int]
    active_tasks: List[Task]
    recent_updates: List[RecentUpdate]
    waiting_tasks: List[Task]
    backlog_tasks: List[Task]
    next_task: Optional[Task]
    queue_preview: List[Task]
    next_action: str
    broken_files: List[Dict[str, Any]]
    queue_broken: Optional[QueueBroken]


QUEUE_PREVIEW_LIMIT = 3
"""How many claimable tasks the "next up" panel offers.

Three, because the panel's job is to make *starting work* a single gesture, and a machine
whose ``max_concurrent_runs`` is greater than one can usefully start more than one. It is
deliberately not a task list -- ``/tasks`` is that -- and a preview long enough to need
scanning has become the thing this panel was introduced to replace (task-337).

``next_task`` is its head, and stays in the contract: it is what ``agentjobs next``
answers, what the why-this-one disclosure explains, and what several callers already read.
"""


def blocks_human(task: Task) -> bool:
    """Return whether work is stopped because a person holds the ball."""
    return task.ball is Ball.HUMAN and task.lifecycle is not Lifecycle.DRAFT


def awaits_human_input(task: Task) -> bool:
    """Return whether an unstarted draft is parked on a person."""
    return task.ball is Ball.HUMAN and task.lifecycle is Lifecycle.DRAFT


def count_blocking_human(manager: TaskManager) -> int:
    """The badge number: tasks where a person is actually holding work up.

    One function rather than one per surface. The legacy Jinja header, the React
    header and the dashboard's own "Needs you" tile all render this number, and
    ``tests/test_attention_tiers.py`` exists because they once each computed it --
    a parked draft raised the same red badge as a branch at the merge gate, so the
    badge never reached zero and could only be read by opening it.

    Only ``blocks_human``: a draft is backlog, not a blockage.
    """
    return sum(1 for task in manager.list_tasks(ball=Ball.HUMAN) if blocks_human(task))


def _inbox_order(tasks: List[Task]) -> List[Task]:
    """Order human-held tasks by urgency, then most recently touched."""
    return sorted(tasks, key=lambda task: (task.priority_rank(), -task.updated.timestamp()))


def _sort_active_tasks(tasks: List[Task]) -> List[Task]:
    """Order in-flight work by urgency, then most recently touched."""
    return sorted(
        (task for task in tasks if task.lifecycle in (Lifecycle.READY, Lifecycle.ACTIVE)),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


def _collect_recent_updates(tasks: List[Task]) -> List[RecentUpdate]:
    """Flatten task logs into the ten newest dashboard updates."""
    updates: List[RecentUpdate] = []
    for task in tasks:
        for entry in task.log:
            body = (entry.body or "").strip()
            updates.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "timestamp": entry.ts,
                    "summary": body.splitlines()[0] if body else entry.type.value,
                    "author": entry.actor,
                }
            )
    updates.sort(key=lambda record: record["timestamp"], reverse=True)
    return updates[:10]


def _next_action(
    *,
    blocking: List[Task],
    backlog: List[Task],
    next_task: Optional[Task],
    queue_broken: bool,
    total: int,
) -> str:
    """Choose the dashboard's single call to action; first match wins.

    ``queue_broken`` sits below the alert rung and above ``next_up``, because a corrupt
    queue falsifies exactly one of these answers. "Two tasks are blocked on you" is read
    off the ball and is still true; "this one is next" is read off an order that does not
    exist, and the honest reply there is to say the queue is broken and print the repair
    command. Without this rung a corrupt corpus reports "nothing claimable", which is a
    lie of a particularly bad kind -- it looks like an empty backlog.

    The broken-queue *banner* is not this decision. It renders above the panel whatever
    the panel says, the way unreadable task files already do: the ladder chooses one
    call to action, and corruption is a fact about the corpus rather than a call to
    action competing with the others.

    **``backlog`` now sits below ``next_up``** (task-337, answering task-092). It used to
    outrank it, so a project with drafts parked on a person answered "what do I do next"
    with a table of every one of them -- 31 rows, each reading ``spec``, under a sentence
    admitting that nothing is blocked by any of them. That is a filing cabinet, not a
    call to action, and it appeared on precisely the days when nothing needed the reader
    at all. Starting work is the better answer whenever there is work that can be
    started, so the calm rungs swapped: ``next_up`` offers the head of the queue with a
    way to dispatch it, and the backlog is what the page says when there is nothing
    claimable to offer instead.

    The alert rung is untouched and still strict -- an alarm must never compete with a
    nudge, which is what task-081 fixed. What changed is the order of the two calm rungs,
    neither of which is an alarm. The backlog stays traceable either way through the
    unconditional "+N in backlog" link on the statistics card, which is the same trace
    that has always covered it on the days an alert suppressed it.
    """
    if blocking:
        return "blocked"
    if queue_broken:
        return "queue_broken"
    if next_task is not None:
        return "next_up"
    if backlog:
        return "backlog"
    return "empty_project" if total == 0 else "nothing_claimable"


def build_dashboard_snapshot(manager: TaskManager) -> DashboardSnapshot:
    """Compute every dashboard value once for all presentation clients."""
    tasks = manager.list_tasks()
    waiting_tasks = _inbox_order([task for task in tasks if blocks_human(task)])
    backlog_tasks = _inbox_order([task for task in tasks if awaits_human_input(task)])
    # Selection refuses to guess an order it cannot justify (design section 8), and
    # that refusal is a RuntimeError which no route handler catches -- so before
    # task-207 one duplicated position took the whole dashboard down with a 500. The
    # dashboard is the surface that has to *say* the queue is broken, so it carries the
    # breakage rather than raising over it, and every panel that does not depend on the
    # order keeps rendering.
    queue_broken: Optional[QueueBroken] = None
    try:
        # The whole claimable frontier rather than its head, and one call rather than
        # two: `get_next_task` *is* `claimable_tasks()[0]`, so asking for both would run
        # the same scan twice and -- worse -- give the panel and the disclosure beside it
        # two chances to disagree about what is first.
        queue_preview = manager.claimable_tasks()[:QUEUE_PREVIEW_LIMIT]
    except QueueCorruptionError as error:
        queue_preview = []
        queue_broken = {
            "problems": problem_dicts(error.problems),
            "repair_command": REPAIR_COMMAND,
        }
    next_task = queue_preview[0] if queue_preview else None
    stats = {
        "total": len(tasks),
        "in_progress": sum(
            1 for task in tasks if task.lifecycle is Lifecycle.ACTIVE and task.ball is Ball.AGENT
        ),
        "blocked": sum(1 for task in tasks if task.ball is Ball.EXTERNAL),
        "waiting_for_human": len(waiting_tasks),
        "awaiting_input": len(backlog_tasks),
        "completed": sum(1 for task in tasks if task.outcome is Outcome.COMPLETED),
    }
    return {
        "stats": stats,
        "active_tasks": _sort_active_tasks(tasks),
        "recent_updates": _collect_recent_updates(tasks),
        "waiting_tasks": waiting_tasks,
        "backlog_tasks": backlog_tasks,
        "next_task": next_task,
        "queue_preview": queue_preview,
        "next_action": _next_action(
            blocking=waiting_tasks,
            backlog=backlog_tasks,
            next_task=next_task,
            queue_broken=queue_broken is not None,
            total=len(tasks),
        ),
        "broken_files": [error.as_dict() for error in manager.load_errors()],
        "queue_broken": queue_broken,
    }
