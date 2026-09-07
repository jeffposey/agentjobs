"""Auto-dispatch: a human approval that starts an agent without a second click.

This was the only place the budget caps bound, on the argument that runaway needs a
*cycle* and manual dispatch has none -- every turn of the wheel costing a human click, so
refusing one would be the tool second-guessing its owner about his own money (design
section 7, decision D3). **Task-334 moved them to ``dispatch/budget.py`` and applied
them to every trigger**, because D3's premise assumes the server can tell a human's click
from an agent's and the 2026-08-21 audit showed it cannot. Nothing about the caps is
decided here any more; this module is one caller of them.

**The human-clocked rule is not weakened here.** An approval is a human act, so it may
cause one dispatch. The handoff that ends the resulting run is written by an agent, so it
causes nothing -- there is no second turn, and the wheel stops. That is enforced by
`assert_human_clocked` in ``guards.py``, which this module calls rather than
reimplements, and it is tested directly rather than assumed: see
``tests/test_auto_dispatch.py``. What it does *not* do is make a loop impossible; see
that module's docstring for what it does and does not buy.

**Nothing here ever raises into the caller.** Auto-dispatch is a consequence of an
approval, not a part of it: an approval that already succeeded must not turn into an
error response because a run could not start. Every failure path returns an outcome and,
where it matters, writes what happened onto the task. That is why ``dispatch_task``'s
``BudgetCapError`` is caught below rather than allowed out, and why the outcome a
refused auto-dispatch returns is unchanged by the move.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from agentjobs.dispatch.budget import (
    DISPATCHER_ACTOR,
    CapRefusal,
    check_budget,
    last_dispatch_at,
    record_cap_refusal,
)
from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
from agentjobs.dispatch.guards import BudgetCapError, DispatchRequest, dispatch_task
from agentjobs.dispatch.runner import DispatchRunError
from agentjobs.models_v2 import Ball, BallReason, DispatchTrigger, Task
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

__all__ = [
    "DISPATCHER_ACTOR",
    "AutoDispatchOutcome",
    "CapRefusal",
    "check_budget",
    "last_dispatch_at",
    "maybe_auto_dispatch",
    "record_cap_refusal",
]
"""The budget names are re-exported rather than left behind.

They lived here until task-334 and are imported from here by tests and by anyone reading
the design's section 7 alongside the code. Re-exporting costs a line and saves a reader
discovering that ``check_budget`` moved by getting an ImportError; ``budget.py`` is where
they are defined and where the reasoning lives."""


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
    recorded: bool = False
    """Whether this outcome has already written itself onto the task record.

    Two paths have: a dispatch that started wrote its dispatch entry, and a tripped
    budget cap wrote its refusal through ``record_cap_refusal``. Every other path has
    said nothing at all, which is what task-384 found. The rule there is *exactly one*
    entry per human handback, and a caller cannot honour it without being told which
    outcomes have already spoken -- writing twice is as wrong as writing nothing, it
    just fails more loudly.
    """

    @property
    def considered(self) -> bool:
        """True unless auto-dispatch was switched off or the task was not eligible."""
        return self.reason not in {"not_enabled", "not_eligible", "not_configured"}


def _skipped(reason: str, detail: str = "", *, recorded: bool = False) -> AutoDispatchOutcome:
    """An outcome that started nothing, for a reason that is not a failure."""
    return AutoDispatchOutcome(started=False, reason=reason, detail=detail, recorded=recorded)


# ----- the trigger ------------------------------------------------------------


def maybe_auto_dispatch(
    *,
    manager: TaskManagerLike,
    project: Project,
    project_config: Dict[str, object],
    task: Task,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    caused_by: Optional[int] = None,
    now: Optional[datetime] = None,
) -> AutoDispatchOutcome:
    """Start an agent, if this project asked for that and every limit allows it.

    Called after a human action that has already been recorded. ``task`` is the record
    *after* that write, so its newest log entry is the human act being reacted to --
    which is what makes the human-clocked check meaningful rather than circular.

    Off by default and configured only in machine-local ``~/.agentjobs/dispatch.yaml``,
    which no browser can write. Merging this file changes nothing anywhere until a
    person edits that file by hand.

    ``api_base`` is forwarded to ``dispatch_task`` unchanged, so an auto-dispatch and the
    manual dispatch it sits beside resolve the same address by construction rather than
    by two copies of a default staying in step.

    ``caused_by`` names the entry the dispatch is attributed to, and defaults to letting
    ``resolve_causing_entry`` take the newest -- correct for every caller that runs in
    the same breath as the click. A caller that runs *later*, once AgentJobs has written
    an entry of its own on top, must name the human's entry: the check it satisfies is
    ``assert_human_clocked``, unchanged, on the entry it names (task-384).
    """
    if task.ball is not Ball.AGENT or not task.is_open:
        # Requesting changes hands to agent/revise and is eligible; rejecting closes the
        # task and is not. Checked on the resulting record rather than on which endpoint
        # was called, so a verb added later is covered without anyone remembering to.
        return _skipped("not_eligible", f"ball is {task.ball}, lifecycle {task.lifecycle}")

    if task.ball_reason is BallReason.HOLD:
        # The one agent-side reason that is not workable. Reading the ball alone was
        # enough while every agent reason meant "get on with it"; `hold` is the human
        # saying stop, so auto-dispatching on it would start a run in the same breath
        # as the click that told the last one to halt. task-231 added the value for
        # exactly this reason -- a hold recorded as `revise` was eligible here.
        return _skipped("on_hold", f"{task.id} is on hold; a human must release it first")

    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError as exc:
        # Every gate from task-068, including the sentinel. A machine with dispatch off
        # is the normal case and is not worth writing to a task record.
        return _skipped(getattr(exc, "reason", "dispatch_error"), str(exc))

    if not resolution.settings.auto_dispatch:
        return _skipped("not_enabled", f"{project.id} has auto_dispatch off")

    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config,
            request=DispatchRequest(
                task_id=task.id, trigger=DispatchTrigger.AUTO, caused_by=caused_by
            ),
            home=home,
            api_base=api_base,
            now=now,
        )
    except (DispatchError, DispatchRunError) as exc:
        # Includes `not_human_clocked`, which is the case that matters: if the entry
        # that moved this ball was an agent's, nothing starts, and no amount of
        # configuration changes that.
        #
        # And it includes every budget cap, which used to be checked here instead. The
        # outcome is deliberately identical either way -- `reason` is still the cap's own
        # code and `detail` is still its message -- because a caller of this function
        # branches on those and none of them should have to know the check moved
        # (task-334).
        #
        # `recorded` distinguishes the cap from the rest: `record_cap_refusal` has
        # already written the refusal onto the task by the time `BudgetCapError` reaches
        # here, so a caller that writes its own entry for an unrecorded outcome must not
        # write a second one for this (task-384).
        return _skipped(
            getattr(exc, "reason", "dispatch_failed"),
            str(exc),
            recorded=isinstance(exc, BudgetCapError),
        )

    return AutoDispatchOutcome(
        started=True,
        reason="dispatched",
        detail=f"Auto-dispatched run {handle.run_id}.",
        run_id=handle.run_id,
        recorded=True,
    )
