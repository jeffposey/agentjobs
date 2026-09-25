"""Getting a stopped finish's repair to a live run that is waiting, not working (task-569).

**The occurrence** (task-368, fin_8cf34319, 2026-09-24). A dispatched session handed its
work to review with its review sandbox running as a Claude Code background job. The owner
approved; the scripted finish went red at the gate and escalated to ``agent``/``work``;
the escalation found the session's run live and deferred to it (``live_run``). Nothing
delivered the escalation to that session, and the run never ended -- the sandbox held it
open -- so ``resolve_deferred_escalation``, which fires only when the run ends, never
fired either. The task read "In progress" with nobody acting until the owner messaged
the session half an hour later.

A run that has handed off for review is waiting, not working, and the finisher has just
handed it the ball. So the escalation goes to it, in order:

1. **Wake it in place** on task-451's peer channel, with the escalation's prompt. Same
   session, same worktree, same branch, and the run keeps going under the same record.
2. **Otherwise stand it down** -- stop the session and conclude its run ``completed`` --
   and let the escalation dispatch a repair session, which resumes a copy of the
   conversation where one can be resumed. Only when the transcript shows nobody has
   typed into the session since its handoff: a person talking to it is not an idle
   session, and it is never stopped from under them.
3. **Otherwise say so**, and the caller parks the ball with a person.

Task-574 is the same three steps for Request Changes; this is them for a stopped finish.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agentjobs.actors import FINISHER
from agentjobs.dispatch.config import DispatchResolution
from agentjobs.dispatch.ledger import RunRecord, write_status
from agentjobs.models_v2 import Ball, LogEntry, LogEntryType, Task
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

WOKEN = "woke_idle_run"
STOOD_DOWN = "stood_down_idle_run"
UNREACHABLE = "idle_run_unreachable"
"""The three things this can do, as the finish's ``escalation_dispatch`` records them."""

WAKE_STUB = (
    "This is the **same session you were already running on task `{task_id}`** (run "
    "`{run_id}`). You handed it off for review in entry {handoff_id}; it was approved, and "
    "the scripted finish that approval started **stopped**. The ball is back with you. "
    "Everything you established earlier still stands: your worktree, your branch, and what "
    "you verified. Do not start the task over and do not take a second worktree.\n\n"
    "The finisher's handoff, verbatim:\n\n{prompt}\n\n"
    "The task record at {api_base} has the full entries. When the repair is done, record "
    "it on the task and hand the ball on as that handoff says. If you cannot account for "
    "the state you left behind, say so on the task and hand the ball back."
)
"""What the woken session is told. The finisher's prompt is the payload, carried verbatim."""


@dataclass(frozen=True)
class IdleRun:
    """A live session run that handed its task to a person and has been waiting since."""

    record: RunRecord
    handoff: LogEntry
    """The run's own review handoff: the newest ball move it made."""


@dataclass(frozen=True)
class Takeover:
    """What getting the escalation to an idle run achieved."""

    path: str
    detail: str


def idle_after_review_handoff(
    record: RunRecord,
    task: Task,
    project_config: Dict[str, Any],
    home: Path,
    *,
    own_run: str,
) -> Optional[IdleRun]:
    """The evidence that ``record`` is waiting behind a review handoff, or ``None``.

    ``None`` means "it may be acting", and the caller defers to it exactly as before:

    - it is the run this finish belongs to (a ``--merge-mode-release`` from inside the run),
      which is mid-turn by definition and reads the finish's answer itself;
    - it is a person's interactive session, or not a session at all;
    - its record cannot be followed back to its dispatch entry;
    - its newest move of the ball was not a handoff to a person.

    The run's own moves are told apart from the approval and the finisher's by actor:
    a human's entries and the finisher's are skipped, and the newest remaining handoff
    after the run's dispatch entry is the run's.
    """
    from agentjobs.dispatch.guards import actor_kind
    from agentjobs.dispatch.poller import handle_from_record  # local: poller imports guards

    if own_run and record.run_id == own_run:
        return None
    if not record.is_session or record.is_interactive or not record.session_id:
        return None
    handle = handle_from_record(home, record)
    if handle is None or handle.dispatch_entry_id is None:
        return None
    for entry in reversed(task.log):
        if entry.id <= handle.dispatch_entry_id:
            return None
        if entry.type is not LogEntryType.HANDOFF or entry.actor == FINISHER:
            continue
        kind = actor_kind(project_config, entry.actor)
        if kind is not None and kind.is_human:
            continue
        if (entry.data or {}).get("ball") == Ball.HUMAN.value:
            return IdleRun(record=record, handoff=entry)
        return None
    return None


def human_turn_since(session_id: str, moment: datetime) -> Optional[bool]:
    """Whether a person typed into the session after ``moment``; ``None`` if unknowable.

    Only a person's own turn counts. Tool results and a background job's notifications
    arrive as ``user`` records too, and every session writes one straight after its
    handoff call returns, so counting any user record would make every session look
    spoken to. ``None`` -- no transcript found, or it could not be read -- is not
    evidence that nobody did, and the caller treats it as a reason not to stop anything.
    """
    from agentjobs.dispatch.auth import (
        TAIL_BYTES,
        _belongs_to,
        _entries,
        _moment,
        _tail_lines,
        session_log_path,
    )
    from agentjobs.dispatch.session_question import _is_human_turn

    path = session_log_path(session_id)
    if path is None:
        return None
    try:
        lines = _tail_lines(path, TAIL_BYTES)
    except OSError:  # pragma: no cover - the transcript went away mid-read
        return None
    since = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    for entry in _entries(lines):
        if entry.get("type") != "user" or not _belongs_to(entry, session_id):
            continue
        at = _moment(entry.get("timestamp"))
        if at is not None and at > since and _is_human_turn(entry):
            return True
    return False


def take_over_idle_run(
    *,
    manager: TaskManagerLike,
    project: Project,
    task: Task,
    idle: IdleRun,
    home: Path,
    resolution: DispatchResolution,
    api_base: Optional[str],
) -> Takeover:
    """Wake ``idle``'s session with the escalation, or stand it down, or say neither worked.

    ``STOOD_DOWN`` means the run is concluded and its lock released, so the caller may
    dispatch the repair. Never raises for anything a session, a driver or the peer
    channel can do; the caller's own guard covers the rest.
    """
    from agentjobs.dispatch.address import resolve_api_base
    from agentjobs.dispatch.poller import handle_from_record
    from agentjobs.dispatch.runner import (
        HANDBACK_DELIVERED_AT,
        HANDBACK_DELIVERED_ENTRY,
        DispatchRunner,
    )
    from agentjobs.dispatch.wake import BALL_PROMPT_LIMIT
    from agentjobs.store_factory import dispatch_manager_for

    record = idle.record
    handle = handle_from_record(home, record)
    session = record.session_id or ""
    if handle is None or not session:  # pragma: no cover - checked by the caller's finder
        return Takeover(UNREACHABLE, f"run `{record.run_id}` has no session to reach")
    escalation = _newest_finisher_handoff(task)
    prompt = (task.ball_prompt or "").strip()
    if len(prompt) > BALL_PROMPT_LIMIT:
        prompt = prompt[:BALL_PROMPT_LIMIT].rstrip() + (
            "\n\n(truncated -- the whole entry is on the task record)"
        )
    runner = DispatchRunner(
        manager=dispatch_manager_for(project),
        resolution=resolution,
        project_root=project.root,
        home=home,
    )
    message = WAKE_STUB.format(
        task_id=task.id,
        run_id=record.run_id,
        handoff_id=idle.handoff.id,
        prompt=prompt,
        api_base=resolve_api_base(api_base, home=home),
    )

    attempt = runner.deliver_to_live_session(session, message)
    if attempt.delivered:
        # The run carries on under the same record, so its work is measured from here:
        # the review handoff it made before is what this escalation answers, and a settle
        # must not read that as the repair being handed off (task-574's marker).
        write_status(
            record,
            **{
                HANDBACK_DELIVERED_ENTRY: escalation.id if escalation else idle.handoff.id,
                HANDBACK_DELIVERED_AT: runner.clock().isoformat(),
            },
        )
        detail = (
            f"Woke run `{record.run_id}` (session `{session}`) **in place** with this "
            f"escalation. It handed off in entry {idle.handoff.id} and was still running but "
            "idle -- kept alive by a background job -- so it would never have acted or "
            f"ended on its own. Same session, worktree and branch. The peer channel "
            f"reported: {attempt.detail}"
        )
        _say(manager, task.id, detail, WOKEN, record.run_id)
        return Takeover(WOKEN, detail)

    spoken_to = human_turn_since(session, idle.handoff.ts)
    if spoken_to is not False:
        why = (
            "somebody has typed into it since its handoff"
            if spoken_to
            else "its transcript cannot be read, so nobody typing into it can be ruled out"
        )
        return Takeover(
            UNREACHABLE,
            (
                f"run `{record.run_id}` (session `{session}`) handed off in entry "
                f"{idle.handoff.id} and is still running. The escalation could not be "
                f"delivered to it in place ({attempt.detail}), and it was not stopped "
                f"because {why}."
            ),
        )

    released = runner.release_handed_off_session(
        handle,
        body=(
            f"Stood down so a repair session could take over the stopped scripted finish: "
            f"the session had handed off (entry {idle.handoff.id}) and was still running, "
            f"and the escalation could not be delivered to it in place ({attempt.detail}). "
            "Not a cancellation -- its work reached the handoff and was approved, and "
            "nothing it built is discarded."
        ),
    )
    if released:
        detail = (
            f"Run `{record.run_id}` (session `{session}`) handed off in entry "
            f"{idle.handoff.id} and was idle, and the escalation could not be delivered to "
            f"it in place ({attempt.detail}), so it was **stood down** and its run "
            "concluded. A repair session is dispatched next."
        )
        _say(manager, task.id, detail, STOOD_DOWN, record.run_id)
        return Takeover(STOOD_DOWN, detail)
    return Takeover(
        UNREACHABLE,
        (
            f"run `{record.run_id}` (session `{session}`) handed off in entry "
            f"{idle.handoff.id} and is still running. The escalation could not be delivered "
            f"to it in place ({attempt.detail}), and the session could not be confirmed "
            "stopped, so nothing new could start beside it."
        ),
    )


def _newest_finisher_handoff(task: Task) -> Optional[LogEntry]:
    return next(
        (
            entry
            for entry in reversed(task.log)
            if entry.type is LogEntryType.HANDOFF and entry.actor == FINISHER
        ),
        None,
    )


def _say(manager: TaskManagerLike, task_id: str, body: str, path: str, run_id: str) -> None:
    try:
        manager.add_log_entry(
            task_id,
            actor=FINISHER,
            type=LogEntryType.NOTE,
            body=body,
            data={"escalation_takeover": path, "run_id": run_id},
        )
    except Exception:  # noqa: BLE001 - the finish's meta still records the path taken
        return


__all__ = [
    "STOOD_DOWN",
    "UNREACHABLE",
    "WOKEN",
    "IdleRun",
    "Takeover",
    "human_turn_since",
    "idle_after_review_handoff",
    "take_over_idle_run",
]
