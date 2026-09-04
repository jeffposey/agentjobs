"""The spend caps, and the fact that they are what bounds the loop.

Design section 7 calls these a backstop against a bug in the dispatcher, on the strength
of section 2's claim that an agent-starts-agent cycle is *structurally impossible*. The
2026-08-21 dispatch audit tested that claim rather than believing it (finding P1-2) and
it did not hold: the API is unauthenticated on loopback, every dispatched agent is told
its address in the first sentence of its prompt, and one POST naming a configured human
writes an authorising entry indistinguishable from a click. The structural argument
covers the *design's* paths, not the machine's.

So these are not a backstop. **They are the bound**, and this module exists as its own
file to say so: while an agent on this machine can reach the dispatch API at all, what
stops a runaway is a counter, and a counter that binds one trigger out of three bounds
nothing. Until task-334 the per-task budgets were applied in ``auto.py``, to
``DispatchTrigger.AUTO`` only, because D3 held that "a human clicking Dispatch
repeatedly is a decision, not a malfunction" -- which assumes the server can tell a
human's click from an agent's, and it cannot.

Two things follow, and both are the whole of this module's content:

* **Every cap binds every trigger.** ``check_budget`` is called from ``dispatch_task``,
  the one chokepoint all three triggers pass through, rather than from any caller.
  D3's cost is accepted rather than argued away: a fourth manual dispatch of one task in
  a day is now refused, and the remedy is the refusal message.
* **A per-task cap cannot bound a machine.** Three tasks each dispatching at their own
  limit have no ceiling between them, so ``check_machine_budget`` counts dispatches on
  this machine in a rolling hour, from the run ledger -- the only count that sees every
  trigger, every project and every task at once.

Nothing here decides *whether* a loop may exist. It decides how long one runs before it
is refused, which is the honest version of the guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from agentjobs.dispatch.config import AutoDispatchLimits, DispatchLimits
from agentjobs.dispatch.ledger import RunRecord, list_runs
from agentjobs.dispatch.record_commit import commit_task_record
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchTrigger,
    LogEntryType,
    Task,
    utcnow,
)

DISPATCHER_ACTOR = "dispatcher"
"""Who writes a cap refusal. The reserved actor from task-069, never a human's id."""


@dataclass(frozen=True)
class CapRefusal:
    """One budget cap, refusing, in words that name it and its value."""

    #: Stable code, and the ``reason`` a refused dispatch is rendered under:
    #: ``per_task_per_day``, ``per_task_lifetime``, ``cooldown`` or ``machine_per_hour``.
    limit: str
    message: str
    #: Cooldown and the machine-wide hourly cap are transient -- waiting fixes them --
    #: so they do not park the task with a human. Exhausting a per-task count is not
    #: transient and does.
    parks_task: bool


#: Codes for every cap this module can refuse under, for callers that render them.
CAP_LIMITS = frozenset({"per_task_per_day", "per_task_lifetime", "cooldown", "machine_per_hour"})


# ----- the per-task caps ------------------------------------------------------


def last_dispatch_at(task: Task) -> Optional[datetime]:
    """When this task was last dispatched, or None if it never was.

    Read from the log rather than from a stored field, for the same reason
    ``Task.dispatch_count`` is derived: a second copy of a fact can disagree with the
    evidence for it, and here it would disagree in the direction that spends money.
    """
    stamps = [
        entry.ts if entry.ts.tzinfo else entry.ts.replace(tzinfo=timezone.utc)
        for entry in task.log
        if entry.type is LogEntryType.DISPATCH
    ]
    return max(stamps) if stamps else None


def check_budget(
    task: Task, limits: AutoDispatchLimits, *, now: Optional[datetime] = None
) -> Optional[CapRefusal]:
    """The first per-task cap this task has reached, or None when all three have room.

    Order matters only in what gets reported: the counts are checked before the
    cooldown, so a task that has genuinely exhausted its budget says so rather than
    telling someone to wait sixty seconds for a refusal that will not change.

    ``limits`` is still typed ``AutoDispatchLimits`` and still read from ``limits.auto:``
    in ``dispatch.yaml``. The name is historical and the key is kept deliberately: it is
    machine-local configuration, and renaming it would silently return a tuned machine to
    the defaults, which is the one direction a cap must never move by accident.
    """
    moment = now or utcnow()

    lifetime = task.dispatch_count
    if lifetime >= limits.per_task_lifetime:
        return CapRefusal(
            limit="per_task_lifetime",
            message=(
                f"{task.id} has been dispatched {lifetime} times, and the lifetime cap "
                f"is {limits.per_task_lifetime}. A task that reaches this has not been "
                "failing to run -- it has been running and not finishing, which is a "
                "fact about the task, not about the dispatcher."
            ),
            parks_task=True,
        )

    today = task.dispatches_since(moment - timedelta(days=1))
    if today >= limits.per_task_per_day:
        return CapRefusal(
            limit="per_task_per_day",
            message=(
                f"{task.id} has been dispatched {today} times in the last 24 hours, and "
                f"the daily cap is {limits.per_task_per_day}. Something about this task "
                "is not working; a fourth identical run will not discover what."
            ),
            parks_task=True,
        )

    previous = last_dispatch_at(task)
    if previous is not None:
        waited = (moment - previous).total_seconds()
        if waited < limits.cooldown_seconds:
            return CapRefusal(
                limit="cooldown",
                message=(
                    f"{task.id} was dispatched {int(waited)}s ago and the cooldown is "
                    f"{limits.cooldown_seconds}s. Nothing is wrong; this is the "
                    "dispatcher refusing to start two runs in the same breath."
                ),
                parks_task=False,
            )
    return None


# ----- the machine-wide cap ---------------------------------------------------


def dispatches_since(home: Path, moment: datetime) -> List[RunRecord]:
    """Every run this machine started at or after ``moment``, newest first.

    One run directory is created per dispatch and none are removed, so counting the
    directories *is* counting the dispatches -- across every project, every task and
    every trigger. That breadth is the point: a per-task cap cannot see the machine, and
    the machine is what a runaway spends.

    Reads through the ledger rather than scanning again here. ``list_runs`` parses every
    run directory the machine has ever had -- 16.7 ms for 137 of them, measured
    2026-09-04 -- which is why this is checked after every refusal that costs nothing,
    and is still three orders of magnitude below the process it is deciding whether to
    start. A run whose metadata has no readable ``started_at`` is not counted: it cannot
    be shown to be recent, and a cap that guessed would refuse dispatches on the strength
    of an unparseable file.
    """
    return [
        record
        for record in list_runs(home)
        if record.started_at is not None and record.started_at >= moment
    ]


def check_machine_budget(
    home: Path, limits: DispatchLimits, *, now: Optional[datetime] = None
) -> Optional[CapRefusal]:
    """The machine-wide hourly cap, refusing, or None while it has room.

    The gap this closes: the per-task caps bound one task, so N tasks each dispatching
    at their own limit have no ceiling at all. ``max_concurrent_runs`` does not close it
    either -- it bounds how many runs are alive at once, not how many are started, and a
    loop that starts and immediately fails a run never holds a slot for long enough to
    be refused by it.
    """
    moment = now or utcnow()
    started = dispatches_since(home, moment - timedelta(hours=1))
    if len(started) < limits.dispatches_per_hour:
        return None
    recent = ", ".join(
        f"{record.run_id} on {record.project_id or '?'}/{record.task_id or '?'}"
        for record in started[:5]
    )
    return CapRefusal(
        limit="machine_per_hour",
        message=(
            f"This machine has started {len(started)} runs in the last hour and the cap "
            f"is {limits.dispatches_per_hour}. Most recent: {recent}. Nothing about this "
            "task is wrong -- this is the one cap that counts the whole machine, and it "
            "is what bounds a dispatch loop that no per-task limit can see. Raise "
            "`limits.dispatches_per_hour` in ~/.agentjobs/dispatch.yaml if this is "
            "genuinely the work you meant to start."
        ),
        parks_task=False,
    )


# ----- saying so on the record ------------------------------------------------


def record_cap_refusal(
    manager: TaskManager,
    task: Task,
    refusal: CapRefusal,
    *,
    trigger: DispatchTrigger = DispatchTrigger.AUTO,
) -> None:
    """Write a tripped cap onto the task, loudly.

    A cap that refuses silently is worse than no cap: on the auto path the human clicked
    Approve, expected an agent, and would be left waiting for one that was never coming.
    So the limit is named in a log entry whatever started the dispatch -- an HTTP
    refusal is read once by whoever is holding the mouse and by nobody afterwards, and
    the record is what the next session reads.

    A count cap additionally parks the task with a person, because a task that burns its
    budget is reporting a problem with itself and nobody will look at it unless it asks
    them to. **Except on a manual dispatch**, where a person is at the keyboard reading
    the refusal in the same second: moving their ball for them would be the tool
    responding to a click by changing the thing they clicked on.
    """
    manager.add_log_entry(
        task.id,
        actor=DISPATCHER_ACTOR,
        type=LogEntryType.NOTE,
        body=(
            f"Dispatch ({trigger.value}) refused by the `{refusal.limit}` budget cap."
            f"\n\n{refusal.message}"
        ),
        data={
            "dispatch_refused": refusal.limit,
            "dispatch_trigger": trigger.value,
            "dispatch_count": task.dispatch_count,
        },
    )
    current = manager.get_task(task.id)
    if (
        refusal.parks_task
        and trigger is not DispatchTrigger.MANUAL
        and current is not None
        and current.is_open
        and current.ball is not Ball.HUMAN
    ):
        manager.handoff(
            task.id,
            actor=DISPATCHER_ACTOR,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt=(
                f"Dispatch has stopped starting runs for this task: it hit the "
                f"`{refusal.limit}` cap after {current.dispatch_count} dispatches "
                "without reaching a conclusion. Read the dispatch_result entries and "
                "decide what is actually wrong — the spec, the runner, or the task "
                "itself."
            ),
        )

    # No run was started, so there is no session and no run directory: this write has
    # nobody at all behind it, which makes it the most dangling of the lot (task-203).
    commit_task_record(
        manager, task.id, subject=f"record the `{refusal.limit}` dispatch cap refusal"
    )
