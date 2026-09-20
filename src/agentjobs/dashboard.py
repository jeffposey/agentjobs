"""Dashboard projection shared by the React API and legacy Jinja compatibility views."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    List,
    Optional,
    Sequence,
    TypedDict,
    TypeVar,
)

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    LabelledTask,
    Lifecycle,
    Outcome,
    RecentLogEntry,
    Task,
    TaskCard,
)
from agentjobs.queue import REPAIR_COMMAND, QueueCorruptionError, problem_dicts


RecordT = TypeVar("RecordT", bound=LabelledTask)
"""Whichever shape of record a panel was handed, filtered and sorted but not converted.

Every predicate and every sort key below reads a field both shapes carry, which is what
lets the snapshot be built from cards while the attention poll keeps passing records
through the same functions (task-498).
"""


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
    active_tasks: List[TaskCard]
    recent_updates: List[RecentUpdate]
    waiting_tasks: List[TaskCard]
    backlog_tasks: List[TaskCard]
    next_task: Optional[TaskCard]
    queue_preview: List[TaskCard]
    next_action: str
    broken_files: List[Dict[str, Any]]
    queue_broken: Optional[QueueBroken]


QUEUE_PREVIEW_LIMIT = 3
"""The floor on how many claimable tasks the dashboard offers.

Three, because the panel's job is to make *starting work* a single gesture, and a machine
whose ``max_concurrent_runs`` is greater than one can usefully start more than one. It is
deliberately not a task list -- ``/tasks`` is that -- and a preview long enough to need
scanning has become the thing this panel was introduced to replace (task-337).

A **floor** rather than the whole answer since task-092: the slot board draws one cell
per run slot and each free cell offers a *different* task, so a machine with a ceiling
above three needs more than three. ``build_dashboard_snapshot`` takes a ``preview_limit``
and the API route passes the machine's ceiling; nothing shrinks below this number, so a
caller that knows nothing about the machine -- the legacy Jinja view, a test -- gets
exactly what it got before.

``next_task`` is its head, and stays in the contract: it is what ``agentjobs next``
answers, what the why-this-one disclosure explains, and what several callers already read.
"""


def blocks_human(task: LabelledTask) -> bool:
    """Return whether work is stopped because a person holds the ball."""
    return task.ball is Ball.HUMAN and task.lifecycle is not Lifecycle.DRAFT


def awaits_human_input(task: LabelledTask) -> bool:
    """Return whether an unstarted draft is parked on a person."""
    return task.ball is Ball.HUMAN and task.lifecycle is Lifecycle.DRAFT


def deferred_to_child(
    task: LabelledTask, children: Sequence[LabelledTask]
) -> Optional[LabelledTask]:
    """The open child that is already holding this task's ask, if one is (task-467).

    A parent whose child sits at ``human``/``review`` is not a second thing to do. The
    person has one click, on the child; the parent is waiting for the consequence of it.
    Counting both is what made one approval read as two asks on 2026-09-18, and what let
    a parent that nothing ever retracted keep the badge lit afterwards.

    Deliberately narrow. Only an **open** child, and only one holding the ball with a
    **human**: a child an agent is working says nothing about whether the parent needs
    a person, and a closed one says nothing at all.
    """
    for child in children:
        if child.is_open and child.ball is Ball.HUMAN:
            return child
    return None


def human_waiting(
    tasks: Sequence[RecordT], children_of: Callable[[str], Sequence[LabelledTask]]
) -> List[RecordT]:
    """The waiting set itself: what a person is holding up, in inbox order.

    One function so that the badge, the notification and the dashboard panel cannot
    disagree -- which they did before task-422, and would again the moment a second
    caller grew its own copy of :func:`deferred_to_child`.

    ``children_of`` is a lookup rather than a manager because the two callers reach
    children differently: the dashboard already holds every task and can build the map
    once, while the attention poll holds only the handful of human-held candidates and
    asks for each one's subtasks.
    """
    return _inbox_order(
        [
            task
            for task in tasks
            if blocks_human(task) and deferred_to_child(task, children_of(task.id)) is None
        ]
    )


def attention_waiting(
    tasks: Sequence[RecordT],
    children_of: Callable[[str], Sequence[LabelledTask]],
    stalled_ids: Collection[str] = (),
) -> List[RecordT]:
    """The whole waiting set: what a person holds, plus what nobody is working.

    The second half is task-499's. A claimed task whose agent died reads ``agent`` on
    every surface AgentJobs has, so it looks exactly like a task being worked; only the
    owner can re-dispatch it, take it over or let it sit, which makes it work waiting on
    a person in the same sense a review is. ``stalled_ids`` is decided by
    :mod:`agentjobs.stalled` and passed in, because it needs the machine's run ledger and
    this module is a projection over task records -- the same line
    :func:`build_dashboard_snapshot` draws around ``preview_limit``.

    One function, because the badge, the notification, the phone and the dashboard's own
    panel all render this set and a surface that disagreed with the page it links to is
    the defect ``tests/test_attention_tiers.py`` was written about. A task that is both
    human-held and stalled appears once.
    """
    waiting: Dict[str, RecordT] = {task.id: task for task in human_waiting(tasks, children_of)}
    for task in tasks:
        if task.id in stalled_ids:
            waiting.setdefault(task.id, task)
    return _inbox_order(list(waiting.values()))


def human_waiting_tasks(manager: TaskManager) -> List[Task]:
    """Every task a person is actually holding up, in the order the inbox shows them.

    The set behind the badge number, given a name because task-422 needs the records
    rather than only the count: a notification has to say *which* task when there is
    one, and open the filtered list when there are several. Same predicate and same
    order as the dashboard's own panel, so an alert and the page it leads to cannot
    disagree about what is waiting or which of them is first.
    """
    return human_waiting(manager.list_tasks(ball=Ball.HUMAN), manager.get_subtasks)


def count_blocking_human(manager: TaskManager) -> int:
    """The badge number: tasks where a person is actually holding work up.

    One function rather than one per surface. The legacy Jinja header, the React
    header and the dashboard's own "Needs you" tile all render this number, and
    ``tests/test_attention_tiers.py`` exists because they once each computed it --
    a parked draft raised the same red badge as a branch at the merge gate, so the
    badge never reached zero and could only be read by opening it.

    Only ``blocks_human``: a draft is backlog, not a blockage.
    """
    return len(human_waiting_tasks(manager))


def _inbox_order(tasks: List[RecordT]) -> List[RecordT]:
    """Order human-held tasks by urgency, then most recently touched."""
    return sorted(tasks, key=lambda task: (task.priority_rank(), -task.updated.timestamp()))


def _sort_active_tasks(tasks: List[RecordT]) -> List[RecordT]:
    """Order in-flight work by urgency, then most recently touched."""
    return sorted(
        (task for task in tasks if task.lifecycle in (Lifecycle.READY, Lifecycle.ACTIVE)),
        key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
    )


RECENT_UPDATES_LIMIT = 10
"""How many entries the recent-updates panel shows.

A product decision, and the number the bounded read is asked for. It was a literal
inside the flattening loop when the panel's cost was the whole corpus; now that the
store is told what the panel needs, it is the argument.
"""


def _collect_recent_updates(entries: Sequence[RecentLogEntry]) -> List[RecentUpdate]:
    """Render the newest entries the store returned as the panel's rows.

    **The finding is no longer done here.** This used to flatten every task's log and
    take the ten newest with ``nlargest``, which meant the snapshot had to load every
    record in the project -- 5,871 log entries on this repository's own backlog, to draw
    ten lines (task-498). The store answers the question directly now; what is left is
    the rendering, and only these ten bodies are ever split.
    """
    return [
        {
            "task_id": entry.task_id,
            "task_title": entry.task_title,
            "timestamp": entry.ts,
            "summary": (
                (entry.body or "").strip().splitlines()[0]
                if (entry.body or "").strip()
                else entry.type.value
            ),
            "author": entry.actor,
        }
        for entry in entries
    ]


def _next_action(
    *,
    blocking: Sequence[LabelledTask],
    backlog: Sequence[LabelledTask],
    next_task: Optional[LabelledTask],
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


def build_dashboard_snapshot(
    manager: TaskManager,
    *,
    preview_limit: Optional[int] = None,
    stalled_ids: Collection[str] = (),
) -> DashboardSnapshot:
    """Compute every dashboard value once for all presentation clients.

    ``stalled_ids`` is the same kind of caller-supplied machine fact as
    ``preview_limit``: which claimed tasks have nobody on them is read off the run
    ledger in the user's home, and a projection over task records must not reach for
    that itself. The route computes it and passes it, so the panel this page draws and
    the badge in its header count the same set.

    ``preview_limit`` is how many claimable tasks the caller can put in front of a
    person. It is a machine fact rather than a project one -- the slot board offers one
    task per free run slot -- so the API route reads it from ``machine_ceiling`` and
    passes it here rather than this module reaching for the dispatch config, which would
    make a projection over task files depend on a file in the user's home. Clamped up to
    :data:`QUEUE_PREVIEW_LIMIT`, so no caller can ask for less than the panel has always
    shown, and ``None`` means exactly that number.
    """
    limit = max(QUEUE_PREVIEW_LIMIT, preview_limit or 0)
    # Cards, not records. Every panel here draws a title, a badge, a place in line and a
    # summary line; the one consumer that wanted a log -- the recent-updates panel --
    # asks the store for the ten entries it shows instead of being handed every log in
    # the project to pick them out of (task-498).
    tasks = manager.list_task_cards()
    children_by_parent: Dict[str, List[TaskCard]] = defaultdict(list)
    for task in tasks:
        if task.parent:
            children_by_parent[task.parent].append(task)
    waiting_tasks = attention_waiting(
        tasks, lambda task_id: children_by_parent.get(task_id, ()), stalled_ids
    )
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
        #
        # Over the corpus already in hand rather than through `claimable_tasks`, which
        # would read the project again as whole records. The selection rules read only
        # fields a card carries, so the frontier comes back as cards (task-498).
        queue_preview = manager.claimable_over(tasks)[:limit]
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
        "recent_updates": _collect_recent_updates(manager.recent_log_entries(RECENT_UPDATES_LIMIT)),
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
