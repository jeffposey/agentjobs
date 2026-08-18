"""Auto-dispatch: a human approval that starts an agent without a second click.

This is the last piece of the loop and the only one where the budget caps bind, and
both facts follow from the same observation. Runaway needs a *cycle*: agent finishes,
something starts an agent, repeat. Manual dispatch has no cycle -- every turn of the
wheel costs a human click, so refusing one would be the tool second-guessing its owner
about his own money (design section 7, decision D3). Auto-dispatch is the only place a
cycle could form, so it is the only place the numbers are enforced.

**The human-clocked rule is not weakened here, and that is the whole safety argument.**
An approval is a human act, so it may cause one dispatch. The handoff that ends the
resulting run is written by an agent, so it causes nothing -- there is no second turn,
and the wheel stops. That is enforced by `assert_human_clocked` in ``guards.py``, which
this module calls rather than reimplements, and it is tested directly rather than
assumed: see ``tests/test_auto_dispatch.py``.

The caps are therefore a backstop against a bug in *this file*, not the primary
defence. They are still specified concretely, because "we have a structural argument"
is exactly the sentence people write before an incident.

**Nothing here ever raises into the caller.** Auto-dispatch is a consequence of an
approval, not a part of it: an approval that already succeeded must not turn into an
error response because a run could not start. Every failure path returns an outcome and,
where it matters, writes what happened onto the task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from agentjobs.dispatch.config import (
    AutoDispatchLimits,
    DispatchError,
    assert_dispatch_permitted,
)
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.runner import DispatchRunError
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchTrigger,
    LogEntryType,
    Task,
    utcnow,
)
from agentjobs.projects import Project

DISPATCHER_ACTOR = "dispatcher"
"""Who writes a cap refusal. The reserved actor from task-069, never a human's id."""


# ----- what happened ----------------------------------------------------------


@dataclass(frozen=True)
class AutoDispatchOutcome:
    """What auto-dispatch did, for a caller that wants to say so.

    Returned rather than raised, always. ``started`` and ``reason`` are the two things
    worth branching on: everything else is for humans reading a log line.
    """

    started: bool
    reason: str
    detail: str = ""
    run_id: Optional[str] = None

    @property
    def considered(self) -> bool:
        """True unless auto-dispatch was switched off or the task was not eligible."""
        return self.reason not in {"not_enabled", "not_eligible", "not_configured"}


def _skipped(reason: str, detail: str = "") -> AutoDispatchOutcome:
    """An outcome that started nothing, for a reason that is not a failure."""
    return AutoDispatchOutcome(started=False, reason=reason, detail=detail)


# ----- the caps ---------------------------------------------------------------


@dataclass(frozen=True)
class CapRefusal:
    """One budget cap, refusing, in words that name it and its value."""

    #: Stable code: ``per_task_per_day``, ``per_task_lifetime`` or ``cooldown``.
    limit: str
    message: str
    #: Cooldown is transient -- waiting fixes it -- so it does not park the task with a
    #: human. Exhausting a count is not transient and does.
    parks_task: bool


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
    """The first cap this task has reached, or None when all three have room.

    Order matters only in what gets reported: the counts are checked before the
    cooldown, so a task that has genuinely exhausted its budget says so rather than
    telling someone to wait sixty seconds for a refusal that will not change.
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


# ----- the trigger ------------------------------------------------------------


def maybe_auto_dispatch(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    task: Task,
    home: Optional[Path] = None,
    api_base: str = "http://localhost:8765",
    now: Optional[datetime] = None,
) -> AutoDispatchOutcome:
    """Start an agent, if this project asked for that and every limit allows it.

    Called after a human action that has already been recorded. ``task`` is the record
    *after* that write, so its newest log entry is the human act being reacted to --
    which is what makes the human-clocked check meaningful rather than circular.

    Off by default and configured only in machine-local ``~/.agentjobs/dispatch.yaml``,
    which no browser can write. Merging this file changes nothing anywhere until a
    person edits that file by hand.
    """
    if task.ball is not Ball.AGENT or not task.is_open:
        # Requesting changes hands to agent/revise and is eligible; rejecting closes the
        # task and is not. Checked on the resulting record rather than on which endpoint
        # was called, so a verb added later is covered without anyone remembering to.
        return _skipped("not_eligible", f"ball is {task.ball}, lifecycle {task.lifecycle}")

    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError as exc:
        # Every gate from task-068, including the sentinel. A machine with dispatch off
        # is the normal case and is not worth writing to a task record.
        return _skipped(getattr(exc, "reason", "dispatch_error"), str(exc))

    if not resolution.settings.auto_dispatch:
        return _skipped("not_enabled", f"{project.id} has auto_dispatch off")

    refusal = check_budget(task, resolution.limits.auto, now=now)
    if refusal is not None:
        _record_cap_refusal(manager, task, refusal)
        return AutoDispatchOutcome(started=False, reason=refusal.limit, detail=refusal.message)

    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config,
            request=DispatchRequest(task_id=task.id, trigger=DispatchTrigger.AUTO),
            home=home,
            api_base=api_base,
        )
    except (DispatchError, DispatchRunError) as exc:
        # Includes `not_human_clocked`, which is the case that matters: if the entry
        # that moved this ball was an agent's, nothing starts, and no amount of
        # configuration changes that.
        return _skipped(getattr(exc, "reason", "dispatch_failed"), str(exc))

    return AutoDispatchOutcome(
        started=True,
        reason="dispatched",
        detail=f"Auto-dispatched run {handle.run_id}.",
        run_id=handle.run_id,
    )


def _record_cap_refusal(manager: TaskManager, task: Task, refusal: CapRefusal) -> None:
    """Write a tripped cap onto the task, loudly.

    A cap that refuses silently is worse than no cap: the human clicked Approve,
    expected an agent, and would be left waiting for one that was never coming. So the
    limit is named in a log entry, and a count cap additionally parks the task with a
    person -- because a task that burns its budget is reporting a problem with itself,
    and nobody will look at it unless it asks them to.
    """
    manager.add_log_entry(
        task.id,
        actor=DISPATCHER_ACTOR,
        type=LogEntryType.NOTE,
        body=f"Auto-dispatch refused by the `{refusal.limit}` budget cap.\n\n{refusal.message}",
        data={"auto_dispatch_refused": refusal.limit, "dispatch_count": task.dispatch_count},
    )
    if not refusal.parks_task:
        return
    current = manager.get_task(task.id)
    if current is None or not current.is_open or current.ball is Ball.HUMAN:
        return
    manager.handoff(
        task.id,
        actor=DISPATCHER_ACTOR,
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=(
            f"Auto-dispatch has stopped starting runs for this task: it hit the "
            f"`{refusal.limit}` cap after {current.dispatch_count} dispatches without "
            "reaching a conclusion. Read the dispatch_result entries and decide what is "
            "actually wrong — the spec, the runner, or the task itself. Manual dispatch "
            "still works and is not capped."
        ),
    )
