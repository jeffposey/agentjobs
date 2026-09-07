"""Delivering a human's handback to the agent it is addressed to, and saying what happened.

A human clicks Request Changes. The ball moves to ``agent``/``revise`` with their
feedback in the prompt, and that write is correct and complete. This module is what
happens *next* -- and until task-384 the answer, on the most common path there is, was
nothing at all, recorded nowhere.

**The path that failed, exactly.** ``after_human_handoff`` called ``maybe_auto_dispatch``,
which called ``dispatch_task``, which raised ``LiveRunExistsError`` because the session
that asked for the review was still alive. ``maybe_auto_dispatch`` turns every refusal
into a returned outcome, ``after_human_handoff`` discarded it, and no branch of either
wrote anything to the task. From the dashboard the task read *Revising (claude)* beside a
live run, which is the reading most likely to make somebody wait.

**A live run for this task is the normal case at this moment, not an edge case.** Every
Request Changes arrives while the session that handed the work over is still up: the
notification that brings the human to the page is the handoff itself. The run becomes
terminal only when the poller next observes the session idle, up to
``SESSION_POLL_SECONDS`` later -- so the window in which a click meets a live run is
precisely the window in which a human is most likely to click. That makes the failure a
race by construction rather than a property of any one afternoon.

**So the remedy is to reach the session that is already there, not to start a rival.**
That session holds the whole context of the work and owns a worktree and a branch; a
second one would bootstrap a second worktree, re-derive what the first already knows, and
at worst collide with it on the same branch. Task-234 already built the wake -- a dispatch
whose previous session's conversation still exists resumes it with ``--resume`` rather
than starting cold -- and task-241 already built the shape for approvals. Request Changes,
the button next to Approve, had no equivalent. This module is that equivalent, and it
reuses both rather than growing a third mechanism.

**Nothing here decides whether a session is finished.** That judgement lives in
``DispatchRunner.poll_session`` and is asked, not reimplemented: this module polls the
live run once, immediately, instead of waiting for the poller's next tick. A session the
poller would settle is settled by the same code on the same evidence; a session it would
leave alone is left alone. One decision, one place.

**And every outcome writes exactly one log entry.** That is the half that turns a future
occurrence from a mystery into a sentence, and it is why ``HandbackOutcome`` carries
``recorded``: ``dispatch_task`` writes its own dispatch entry and a tripped budget cap
writes its own refusal, so the caller must be able to tell an outcome that has already
spoken from one that has not. A path that writes twice is as wrong as one that writes
nothing -- it just fails more loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agentjobs.dispatch.auto import DISPATCHER_ACTOR, AutoDispatchOutcome, maybe_auto_dispatch
from agentjobs.dispatch.config import (
    DispatchError,
    DispatchResolution,
    assert_dispatch_permitted,
)
from agentjobs.dispatch.guards import actor_kind, resolve_machine_home
from agentjobs.dispatch.ledger import RunRecord, live_runs, run_health
from agentjobs.models_v2 import Ball, BallReason, LogEntry, LogEntryType, Task
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

__all__ = [
    "HandbackOutcome",
    "deliver_handback",
    "pending_handback",
    "record_handback",
]

QUIET_REASONS = frozenset(
    {
        "not_eligible",
        "not_enabled",
        "not_configured",
        "disabled",
        "project_not_enabled",
    }
)
"""Outcomes that write nothing, because they are configuration rather than events.

A machine that does not dispatch, or a project that never opted in, refuses every
handback there will ever be. An entry per refusal would put a note on every human action
on every such project, and a log nobody can skim is a log nobody reads -- which is the
same failure as silence, arrived at from the other side.

``sentinel`` is deliberately **not** here. It is a temporary stop somebody put in place
by hand, and the cost of having left it there is exactly what they want to be told.
"""


# ----- what happened ----------------------------------------------------------


@dataclass(frozen=True)
class HandbackOutcome:
    """What the delivery did, and whether it has already said so on the record.

    ``recorded`` is not decoration. The caller's contract is *exactly one* entry per
    handback, and two of the paths below write their own: a dispatch that started writes
    the dispatch entry, and a budget cap writes its refusal. Every other path has said
    nothing yet and the caller must say it.
    """

    delivered: bool
    reason: str
    detail: str = ""
    run_id: Optional[str] = None
    recorded: bool = False

    @property
    def considered(self) -> bool:
        """True unless the handback was never a candidate for delivery. See ``QUIET_REASONS``."""
        return self.reason not in QUIET_REASONS


def _outcome(reason: str, detail: str = "", *, run_id: Optional[str] = None) -> HandbackOutcome:
    """An outcome that delivered nothing and has not written anything yet."""
    return HandbackOutcome(delivered=False, reason=reason, detail=detail, run_id=run_id)


def _from_auto(outcome: AutoDispatchOutcome) -> HandbackOutcome:
    """Carry an auto-dispatch outcome through unchanged, including who has written what."""
    return HandbackOutcome(
        delivered=outcome.started,
        reason=outcome.reason,
        detail=outcome.detail,
        run_id=outcome.run_id,
        recorded=outcome.recorded,
    )


# ----- is there a handback waiting --------------------------------------------


def pending_handback(
    task: Task,
    project_config: Dict[str, object],
    *,
    after_entry: Optional[int] = None,
) -> Optional[LogEntry]:
    """The human handoff this task is waiting on an agent to act on, or ``None``.

    Answered from the record rather than from which endpoint was called, so a verb added
    later is covered without anybody remembering to add it here -- the same reasoning
    ``maybe_auto_dispatch`` gives for reading the resulting ball.

    ``after_entry`` is the poller's question and nobody else's: *did a human move this
    ball after the run I am settling was dispatched?* Without it, a session that finished
    the work it was dispatched for would look like one with feedback waiting, and every
    settling run would trigger a dispatch of itself.
    """
    if not task.is_open or task.ball is not Ball.AGENT:
        return None
    if task.ball_reason is BallReason.HOLD:
        # The one agent-side reason that is not workable: a hold is the human saying
        # stop. Waking a session on it would deliver "carry on" in the same breath as
        # the click that said halt (task-231).
        return None
    for entry in reversed(task.log):
        if entry.type is not LogEntryType.HANDOFF:
            continue
        if after_entry is not None and entry.id <= after_entry:
            return None
        actor = actor_kind(project_config, entry.actor)
        return entry if actor is not None and actor.is_human else None
    return None


# ----- the live run in the way ------------------------------------------------


def _task_runs(home: Path, task_id: str) -> List[RunRecord]:
    """Every live run recorded for this task. Normally none or one."""
    return [record for record in live_runs(home) if record.task_id == task_id]


def _settle_if_idle(
    manager: TaskManagerLike,
    project: Project,
    record: RunRecord,
    home: Path,
    resolution: DispatchResolution,
) -> None:
    """Poll one live session run now, so a finished one stops being in the way.

    The poller would do this within ``SESSION_POLL_SECONDS``; asking now removes the race
    rather than shortening it. It is the *same* call the poller makes -- so a session that
    is genuinely working is left alone here for exactly the reason it is left alone there,
    and this module holds no opinion of its own about when a session is done.

    Every failure is swallowed. A handback that could not poll is a handback that finds
    the run still live and says so, which is a worse answer than delivering and a much
    better one than an exception out of an HTTP request that already succeeded.
    """
    from agentjobs.dispatch.poller import handle_from_record  # local: poller imports guards
    from agentjobs.dispatch.runner import DispatchRunError, DispatchRunner

    if not record.is_session or record.is_interactive:
        # An interactive run is somebody's own chat window. It is not ours to stop, and
        # a person sitting in a session is the one holder a handback should never
        # displace (task-354).
        return
    handle = handle_from_record(home, record)
    if handle is None:
        return
    runner = DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=project.root,
        home=home,
    )
    try:
        runner.poll_session(handle)
    except (DispatchRunError, OSError):
        return


def _mark_pending(record: RunRecord, entry_id: int) -> None:
    """Note on the run that feedback is waiting for it, so a surface can say so.

    The whole of ac-6: a task at ``agent``/``revise`` beside a live run has to be
    distinguishable from one being worked, on the surface a person actually reads.
    ``run_health`` renders this ahead of ``working`` -- both are true and this is the one
    that answers the reader's question.

    Written straight to the run's meta rather than held in the server's memory, because
    a promise that dies with the process is the failure this task is about. Best effort:
    a run directory that cannot be written is not a reason to fail a click that already
    succeeded, and the log entry carries the same fact either way.
    """
    from agentjobs.dispatch.ledger import write_status

    try:
        write_status(record, handback_pending=entry_id)
    except OSError:  # pragma: no cover - an unwritable run directory
        return


def _blocked_body(task: Task, records: List[RunRecord]) -> str:
    """What the record says when a live run stands between the click and the agent."""
    named = ", ".join(f"`{record.run_id}` ({run_health(record)})" for record in records)
    return (
        f"The ball moved to the agent, and {task.id} already has a live run: {named}. "
        "Nothing new was started -- one live run per task, always, because a second "
        "would put two agents on the same branch with the same task record.\n\n"
        "That run has not finished its turn, so it has not been given this feedback "
        "yet. It will be delivered when the run settles: the poller notices a session "
        "that has gone idle, records the run's result, and the handback is woken into "
        "that same session with its context intact. Nothing else is needed from you.\n\n"
        "If the run never settles, its own health above is what says so."
    )


# ----- the trigger ------------------------------------------------------------


def deliver_handback(
    *,
    manager: TaskManagerLike,
    project: Project,
    project_config: Dict[str, object],
    task: Task,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    caused_by: Optional[int] = None,
    now: Optional[datetime] = None,
) -> HandbackOutcome:
    """Get a human's handback to the agent, and return what happened to it.

    Never raises. The human's click has already been written by the time this runs, and
    an approval that succeeded must not become an error response because a run could not
    start -- the argument ``auto.py`` makes, applying unchanged to every other verb that
    hands the ball back.

    ``caused_by`` names the log entry the resulting dispatch is attributed to. The HTTP
    routes leave it ``None``, because the human's own handoff *is* the newest entry at
    that moment and ``resolve_causing_entry`` finds it. The poller cannot: by the time it
    delivers, the run's terminal ``dispatch_result`` is newer, and a dispatch attributed
    to AgentJobs' own entry is refused as an agent's -- correctly. So it names the human's
    entry explicitly, which satisfies ``assert_human_clocked`` rather than bypassing it:
    the dispatch still traces to a human act, and to the *right* one.
    """
    waiting = pending_handback(task, project_config)
    if waiting is None:
        return _outcome(
            "not_eligible",
            f"ball is {task.ball}/{task.ball_reason}, lifecycle {task.lifecycle}",
        )

    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError as exc:
        # Every gate from task-068, including the sentinel, and resolved here rather than
        # left to `maybe_auto_dispatch` for one reason: a machine that may not dispatch
        # may not settle somebody's session either, and `_settle_if_idle` runs first.
        return _outcome(getattr(exc, "reason", "dispatch_error"), str(exc))
    if not resolution.settings.auto_dispatch:
        return _outcome("not_enabled", f"{project.id} has auto_dispatch off")

    root = resolve_machine_home(home, resolution)
    running = _task_runs(root, task.id)
    for record in running:
        _settle_if_idle(manager, project, record, root, resolution)
    # Re-read rather than reason about what the poll did. `poll_session` returns a phase,
    # but the question here is the ledger's answer to "is anything still live", and that
    # is a directory scan whose result a phase only predicts.
    running = _task_runs(root, task.id)
    if running:
        for record in running:
            _mark_pending(record, waiting.id)
        return _outcome(
            "live_run_exists",
            _blocked_body(task, running),
            run_id=running[0].run_id,
        )

    return _from_auto(
        maybe_auto_dispatch(
            manager=manager,
            project=project,
            project_config=project_config,
            task=task,
            home=home,
            api_base=api_base,
            caused_by=caused_by,
            now=now,
        )
    )


def record_handback(
    manager: TaskManagerLike,
    task: Task,
    outcome: HandbackOutcome,
) -> None:
    """Write the one entry an outcome owes the record, or nothing when it already has.

    Separated from ``deliver_handback`` because the poller and the HTTP routes want the
    same sentence written under different circumstances, and because a function that both
    decides and narrates is one whose narration is untestable without its decision.
    """
    if not outcome.considered or outcome.recorded:
        return
    if outcome.delivered:  # pragma: no cover - a delivery always records its dispatch
        return
    manager.add_log_entry(
        task.id,
        actor=DISPATCHER_ACTOR,
        type=LogEntryType.NOTE,
        body=outcome.detail or f"The handback was not delivered: `{outcome.reason}`.",
        data={
            "handback_refused": outcome.reason,
            "handback_run_id": outcome.run_id,
        },
    )
