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

**A run that has already handed off is not waited for** (task-574). A session that
handed its work to review with a sandbox still running as a background job never reads
``idle``, so it never settles, and waiting for it delivered nothing, ever. It is waiting,
not working, so the feedback goes to it in place on the peer channel; failing that it is
stood down and a fresh run carries the feedback; failing that a person is told why. See
``_deliver_to_handed_off_run``.

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
from typing import Any, Dict, List, Optional, Tuple

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
    open_tasks: Tuple[str, ...] = ()
    """The unmet `needs` dependencies, when that is why nothing started (task-150)."""

    recorded: bool = False

    @property
    def considered(self) -> bool:
        """True unless the handback was never a candidate for delivery. See ``QUIET_REASONS``."""
        return self.reason not in QUIET_REASONS


def _outcome(
    reason: str,
    detail: str = "",
    *,
    run_id: Optional[str] = None,
    open_tasks: Tuple[str, ...] = (),
) -> HandbackOutcome:
    """An outcome that delivered nothing and has not written anything yet."""
    return HandbackOutcome(
        delivered=False, reason=reason, detail=detail, run_id=run_id, open_tasks=open_tasks
    )


def _from_auto(outcome: AutoDispatchOutcome) -> HandbackOutcome:
    """Carry an auto-dispatch outcome through unchanged, including who has written what."""
    return HandbackOutcome(
        delivered=outcome.started,
        reason=outcome.reason,
        detail=outcome.detail,
        run_id=outcome.run_id,
        open_tasks=outcome.open_tasks,
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


def _task_runs(home: Path, task_id: str, *, project_id: str) -> List[RunRecord]:
    """Every live run recorded for this project's task. Normally none or one."""
    from agentjobs.dispatch.journal import same_task  # local: journal imports runner

    return [record for record in live_runs(home) if same_task(record, project_id, task_id)]


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


# ----- a run that handed off and is still up (task-574) ------------------------

IN_PLACE_STUB = (
    "You handed task `{task_id}` off, and it has come back to you. This message reached "
    "your running session directly: it is the same run (`{run_id}`), and the worktree, "
    "the branch and everything you verified are still yours. Do not start the task over "
    "and do not take a second worktree.\n\n"
    "{payload_frame}\n\n"
    "{feedback}\n\n"
    "Act on it, then hand the task off again the way you did before. Anything you left "
    "running for the review, such as a sandbox, is still running: restart it if your "
    "change needs it, and say so in the handoff. The task record at {api_base} has the "
    "full entry.\n\n"
    "**If you cannot account for the state you left behind** -- your worktree is gone, "
    "your branch is not where you left it, or your own account of this task no longer "
    "matches what is on disk -- do not guess and do not improvise a recovery. Say so on "
    "the task and hand the ball back."
)
"""What a handed-off session is told when feedback reaches it where it stands.

Not ``wake.WAKE_STUB``: that one announces a resumed session under a new run id, and
this is neither -- the process and the run are the ones that asked for the review."""

UNDELIVERABLE_REASON = "handback_undeliverable"


def _handed_off_at(task: Task, baseline: int, before: int) -> Optional[LogEntry]:
    """The handoff to a person this run made before ``before``, or ``None``.

    Only the newest handoff counts, and only if it is newer than the run's baseline: a
    run whose last move of the ball was not to a person is working, not waiting.
    """
    for entry in reversed(task.log):
        if entry.id >= before:
            continue
        if entry.id <= baseline:
            return None
        if entry.type is LogEntryType.HANDOFF:
            return entry if (entry.data or {}).get("ball") == Ball.HUMAN.value else None
    return None


def _in_place_message(
    task: Task,
    project_config: Dict[str, object],
    run_id: str,
    baseline: int,
    waiting: LogEntry,
    api_base: str,
) -> str:
    """The handback as the running session will read it: every message since, newest last."""
    from agentjobs.dispatch.approval import (
        author_is_human,
        ball_prompt_author,
        human_handoffs_since,
    )
    from agentjobs.dispatch.wake import BALL_PROMPT_LIMIT, payload_frame

    author = ball_prompt_author(task)
    frame = payload_frame(author, is_human=bool(author) and author_is_human(project_config, author))
    newest = (task.ball_prompt or "").strip() or (
        "(The task record carries no ball prompt. Read the newest handoff entry before "
        "doing anything.)"
    )
    if len(newest) > BALL_PROMPT_LIMIT:
        newest = newest[:BALL_PROMPT_LIMIT].rstrip() + (
            "\n\n(truncated -- the whole entry is on the task record)"
        )
    earlier = [
        f"From {entry.actor} (entry {entry.id}):\n\n{(entry.body or '').strip()}"
        for entry in human_handoffs_since(task, project_config, after_entry=baseline)
        if entry.id < waiting.id and (entry.body or "").strip()
    ]
    if earlier:
        newest = (
            "Earlier messages since you handed off, oldest first -- all of them still "
            "apply:\n\n" + "\n\n---\n\n".join(earlier) + "\n\n---\n\nAnd the newest:\n\n" + newest
        )
    return IN_PLACE_STUB.format(
        task_id=task.id,
        run_id=run_id,
        payload_frame=frame,
        feedback=newest,
        api_base=api_base,
    )


def _deliver_to_handed_off_run(
    manager: TaskManagerLike,
    project: Project,
    project_config: Dict[str, object],
    task: Task,
    waiting: LogEntry,
    record: RunRecord,
    home: Path,
    resolution: DispatchResolution,
    api_base: Optional[str] = None,
) -> Optional[HandbackOutcome]:
    """Get feedback to a live run that had already handed off, or say why it cannot go.

    **The occurrence** (task-563, run_220c2439): the session handed off for review with
    its review sandbox running as a background job. Claude Code does not report a session
    with work in flight as ``idle``, so ``poll_session`` read it ``RUNNING`` for as long as
    the sandbox ran, and the handback waited for a settle that never came.

    The session handed off, so it is waiting rather than working. In order:

    1. **Deliver in place**, on task-451's peer channel. The run continues under the same
       record, and the record says the message landed.
    2. **Otherwise stand it down**: stop the session and conclude its run ``completed``
       once it reads stopped, then return ``None`` so the ordinary path dispatches a
       fresh run -- which resumes a copy of the conversation when one can be resumed.
    3. **Otherwise the ball goes to a person**, with the reason. A task at
       ``agent``/``revise`` with nobody acting on it is the one outcome this refuses.

    ``None`` without a note also means "not this case": an interactive run, a batch run,
    a run that has not handed off, or one whose record cannot be followed.
    """
    from agentjobs.dispatch.address import resolve_api_base
    from agentjobs.dispatch.ledger import write_status
    from agentjobs.dispatch.poller import handle_from_record  # local: poller imports guards
    from agentjobs.dispatch.runner import (
        HANDBACK_DELIVERED_AT,
        HANDBACK_DELIVERED_ENTRY,
        DispatchRunner,
    )
    from agentjobs.dispatch.config import RunnerDriver

    if not record.is_session or record.is_interactive:
        return None
    handle = handle_from_record(home, record)
    if handle is None or handle.dispatch_entry_id is None or not handle.session_id:
        return None
    meta = handle.directory.read_meta()
    baseline = handle.dispatch_entry_id
    delivered_before = meta.get(HANDBACK_DELIVERED_ENTRY)
    if isinstance(delivered_before, int) and delivered_before > baseline:
        baseline = delivered_before
    handed = _handed_off_at(task, baseline, waiting.id)
    if handed is None:
        return None
    runner = DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=project.root,
        home=home,
    )
    if runner.runner.driver is not RunnerDriver.CLAUDE:
        return None

    receipt = _DeliveryReceipt.open(home, project.id, task, waiting)
    if receipt is not None:
        uncertain = receipt.refuse_if_uncertain()
        if uncertain is not None:
            return uncertain
        receipt.intend()

    message = _in_place_message(
        task,
        project_config,
        record.run_id,
        baseline,
        waiting,
        resolve_api_base(api_base, home=home),
    )
    attempt = runner.deliver_to_live_session(handle.session_id, message)
    if attempt.delivered:
        write_status(
            record,
            **{
                HANDBACK_DELIVERED_ENTRY: waiting.id,
                HANDBACK_DELIVERED_AT: runner.clock().isoformat(),
                # The poller's own marker for "these messages have been given to this
                # run", so settling it later never delivers them a second time.
                "delivered_through_entry": waiting.id,
                "handback_pending": None,
            },
        )
        manager.add_log_entry(
            task.id,
            actor=DISPATCHER_ACTOR,
            type=LogEntryType.NOTE,
            body=(
                f"Delivered entry {waiting.id} **in place** to run `{record.run_id}`, "
                f"session `{handle.session_id}`, which handed off in entry {handed.id} and "
                "was still running -- kept busy by a background job, so it never read "
                "idle. Same session, same worktree, same branch; nothing new was "
                f"started. The peer channel reported: {attempt.detail}"
            ),
            data={
                "handback_delivered": "in_place",
                "handback_run_id": record.run_id,
                "handback_entry": waiting.id,
            },
        )
        outcome = HandbackOutcome(
            delivered=True,
            reason="delivered_in_place",
            detail=attempt.detail,
            run_id=record.run_id,
            recorded=True,
        )
        if receipt is not None:
            receipt.settle(outcome, manager)
        return outcome

    if receipt is not None:
        receipt.settle(_outcome("in_place_missed", attempt.detail, run_id=record.run_id), manager)
    released = runner.release_handed_off_session(
        handle,
        body=(
            f"Stood down so feedback in entry {waiting.id} could reach a fresh run: the "
            f"session had handed off (entry {handed.id}) and was still running, and the "
            f"message could not be delivered to it in place ({attempt.detail}). Not a "
            "cancellation -- its work reached the handoff, and nothing it built was "
            "discarded."
        ),
    )
    if released:
        manager.add_log_entry(
            task.id,
            actor=DISPATCHER_ACTOR,
            type=LogEntryType.NOTE,
            body=(
                f"Entry {waiting.id} could not be delivered in place to run "
                f"`{record.run_id}` ({attempt.detail}), so that session was stopped and its "
                "run concluded. A fresh run is dispatched next and carries the feedback."
            ),
            data={
                "handback_delivered": "stood_down",
                "handback_run_id": record.run_id,
                "handback_entry": waiting.id,
            },
        )
        return None
    return _outcome(
        UNDELIVERABLE_REASON,
        (
            f"Your feedback in entry {waiting.id} did not reach an agent. Run "
            f"`{record.run_id}` (session `{handle.session_id}`) handed off in entry "
            f"{handed.id} and is still running, so nothing new could start beside it; "
            f"the message could not be delivered to it in place ({attempt.detail}), and "
            "the session could not be confirmed stopped. Attach to it or stop it, then "
            "request changes again."
        ),
        run_id=record.run_id,
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
    running = _task_runs(root, task.id, project_id=project.id)
    for record in running:
        _settle_if_idle(manager, project, record, root, resolution)
    # Re-read rather than reason about what the poll did. `poll_session` returns a phase,
    # but the question here is the ledger's answer to "is anything still live", and that
    # is a directory scan whose result a phase only predicts.
    running = _task_runs(root, task.id, project_id=project.id)
    for record in running:
        handed = _deliver_to_handed_off_run(
            manager, project, project_config, task, waiting, record, root, resolution, api_base
        )
        if handed is not None:
            return handed
    still_running = _task_runs(root, task.id, project_id=project.id)
    if len(still_running) < len(running) and caused_by is None:
        # A run was stood down, and its conclusion and the note saying so are now newer
        # than the human's handoff. The fresh dispatch is still that handoff's, so it is
        # named rather than resolved from the newest entry -- the poller's reasoning above.
        caused_by = waiting.id
    running = still_running
    if running:
        for record in running:
            _mark_pending(record, waiting.id)
        return _outcome(
            "live_run_exists",
            _blocked_body(task, running),
            run_id=running[0].run_id,
        )

    receipt = _DeliveryReceipt.open(root, project.id, task, waiting)
    if receipt is not None:
        uncertain = receipt.refuse_if_uncertain()
        if uncertain is not None:
            return uncertain
        receipt.intend()
    outcome = _from_auto(
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
    if receipt is not None:
        receipt.settle(outcome, manager)
    return outcome


# ----- whether a message reached anyone (task-312, durable-2) ---------------------

AMBIGUOUS_REASONS = frozenset({"dispatch_failed"})
"""Outcomes that do not say whether a work instruction was sent.

``dispatch_failed`` is what ``maybe_auto_dispatch`` reports for a ``DispatchRunError``, and
a runner can raise that *after* the agent was launched -- a session whose id could not be
read back is a session that may be running the instruction. Every other refusal is raised
before anything is launched."""


@dataclass
class _DeliveryReceipt:
    """The journal's record of one handback's delivery: intent before, result after.

    Persisted as an activity whose id is the handback's own source event, so the same
    message is the same activity however many processes try to deliver it. An intent
    with no result is **not** permission to send again: it is reconciled against the task
    log, where a delivered wake always leaves a dispatch entry naming this handback as its
    cause, and anything that cannot be reconciled stays ``unknown``.
    """

    home: Path
    project_id: str
    task: Task
    entry: LogEntry
    execution_id: str
    epoch: int

    @classmethod
    def open(
        cls, home: Path, project_id: str, task: Task, entry: LogEntry
    ) -> Optional["_DeliveryReceipt"]:
        """``None`` when there is no execution to record on, or the journal is unreadable."""
        from agentjobs.dispatch.journal import journal
        from agentjobs.execution.errors import ExecutionStoreError

        try:
            execution = journal(home).latest_execution(project_id, task.id)
        except ExecutionStoreError:
            return None
        if execution is None:
            return None
        return cls(
            home, project_id, task, entry, execution.execution_id, execution.controller_epoch
        )

    @property
    def activity_id(self) -> str:
        from agentjobs.dispatch.approval import source_event

        event = source_event(self.project_id, self.task.id, self.entry)
        return f"deliver:{self.project_id}:{event.source_event_id}"

    def _store(self) -> Any:
        from agentjobs.dispatch.journal import journal

        return journal(self.home)

    def _existing(self) -> Optional[Any]:
        from agentjobs.execution.errors import ExecutionStoreError

        try:
            for activity in self._store().activities(self.execution_id):
                if activity.activity_id == self.activity_id:
                    return activity
        except ExecutionStoreError:
            return None
        return None

    def _reconciled_run(self) -> Optional[str]:
        """The run a wake started for this handback, read from the task log, or ``None``."""
        for item in self.task.log:
            if item.type is LogEntryType.DISPATCH and item.data.get("caused_by") == self.entry.id:
                return str(item.data.get("run_id") or "") or None
        return None

    def refuse_if_uncertain(self) -> Optional[HandbackOutcome]:
        """Refuse a second send when an earlier one's result is not known."""
        from agentjobs.execution.errors import ExecutionStoreError

        prior = self._existing()
        if prior is None or prior.state not in {"intended", "unknown", "still_running"}:
            return None
        run_id = self._reconciled_run()
        try:
            if run_id is not None:
                self._store().record_result(
                    self.activity_id,
                    state="applied",
                    owner_epoch=self.epoch,
                    result={"run_id": run_id, "reconciled_from": "dispatch entry"},
                )
                return _outcome(
                    "already_delivered",
                    f"Entry {self.entry.id} was already delivered to run `{run_id}`; it was "
                    "not sent again.",
                    run_id=run_id,
                )
            if prior.state != "unknown":
                self._store().record_result(
                    self.activity_id,
                    state="unknown",
                    owner_epoch=self.epoch,
                    result=prior.result,
                    error_class="effect_unknown",
                )
        except ExecutionStoreError:
            pass
        return _outcome(
            "delivery_uncertain",
            f"An earlier attempt to deliver entry {self.entry.id} to an agent has no recorded "
            "result, and nothing on this task shows that it arrived. It was **not sent "
            "again**: a second work instruction to a session that already has the first "
            "would be acted on twice.\n\n"
            "Check the agent's own session for that message. If it never arrived, send it "
            "again from the task page -- a new message is a new delivery.",
        )

    def intend(self) -> None:
        from agentjobs.execution.errors import ExecutionStoreError

        try:
            activity, created = self._store().record_intent(
                self.activity_id,
                execution_id=self.execution_id,
                kind="deliver_signal",
                input={"entry_id": self.entry.id, "task_id": self.task.id},
                owner_epoch=self.epoch,
            )
            if not created and activity.state != "intended":
                self._store().record_result(
                    self.activity_id, state="intended", owner_epoch=self.epoch
                )
        except ExecutionStoreError:
            return

    def settle(self, outcome: HandbackOutcome, manager: TaskManagerLike) -> None:
        from agentjobs.dispatch.approval import dispose, human_handoffs_since, project_config_for
        from agentjobs.execution.errors import ExecutionStoreError

        if outcome.delivered:
            state, error = "applied", None
        elif outcome.reason in AMBIGUOUS_REASONS:
            state, error = "unknown", "effect_unknown"
        else:
            state, error = "not_applied", None
        try:
            self._store().record_result(
                self.activity_id,
                state=state,
                owner_epoch=self.epoch,
                result={"reason": outcome.reason, "run_id": outcome.run_id},
                error_class=error,
            )
        except ExecutionStoreError:
            pass
        if not outcome.delivered or outcome.run_id is None:
            return
        # Every human message the wake carried is consumed by the run it started, not
        # only the newest (durable-1): each keeps its own row and its own disposition.
        fresh = manager.get_task(self.task.id) or self.task
        previous = _previous_dispatch_entry(fresh, outcome.run_id)
        for message in human_handoffs_since(
            fresh, project_config_for(self.home, self.project_id), after_entry=previous
        ):
            if message.id > self.entry.id:
                continue
            dispose(
                self.home,
                self.project_id,
                fresh,
                message,
                status="consumed",
                disposition={"by": f"run:{outcome.run_id}", "delivered_with": self.entry.id},
            )


def _previous_dispatch_entry(task: Task, run_id: str) -> Optional[int]:
    """The newest dispatch entry before the one that started ``run_id``."""
    previous: Optional[int] = None
    for item in task.log:
        if item.type is not LogEntryType.DISPATCH:
            continue
        if item.data.get("run_id") == run_id:
            return previous
        previous = item.id
    return previous


def _blocked_prompt(outcome: HandbackOutcome) -> str:
    """The ask a task waiting on other work carries while it waits."""
    named = ", ".join(outcome.open_tasks) or "another task"
    return (
        f"Blocked on {named}. The answer above was recorded and nothing was started: "
        "this task cannot be claimed until those close, at which point it becomes "
        "workable again without anybody doing anything here."
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

    **Every refusal reason was checked for the same defect and only one had it**
    (task-150 section 4). The question is whether a refusal leaves the ball somewhere
    nothing will ever act on:

    * ``unmet_dependencies`` -- **it did**, and this is the fix. The ball moves to
      ``external``/``dependency``.
    * ``delivery_uncertain`` -- already moved the ball to a person, for this exact reason
      (task-340). Unchanged.
    * ``live_run_exists`` -- the ball is with an agent and an agent really is there, and
      it has *not* handed off, so it is working; the poller delivers the message when that
      run settles. A live run that **had** handed off is not this case since task-574: it
      is delivered to in place, or stood down, or it is ``handback_undeliverable``.
    * ``handback_undeliverable`` -- a handed-off run that could neither be reached in
      place nor stopped. Nobody will act, so the ball goes to a person, as
      ``delivery_uncertain`` does.
    * ``on_hold`` -- ``agent``/``hold`` is a person saying stop, which is a state they
      chose and will leave themselves. Moving it would undo the click.
    * the budget caps -- ``record_cap_refusal`` already parks a count cap on a person and
      deliberately leaves a transient one (cooldown, hourly) alone. Unchanged.
    * ``not_enabled``, ``not_configured``, ``disabled``, ``project_not_enabled``,
      ``not_eligible`` -- configuration rather than events, and they write nothing at all
      (``QUIET_REASONS``). A project with auto-dispatch off expects a person to dispatch
      by hand, so the ball sitting with the agent is the accurate reading.
    * ``sentinel`` -- a temporary stop somebody put in place by hand and will lift.
      Writing a note and leaving the ball is what they want to be told.
    * ``claim_lost``, ``owner_mismatch``, ``task_closed``, ``dispatch_failed`` and the
      remaining gate refusals -- each names a condition somebody has to look at, and each
      keeps its note. They are **not** changed here: none of them is the "nothing will
      ever act on this" shape, and widening the fix to cover them would be moving balls on
      a guess rather than on the case that was observed.
    """
    if not outcome.considered or outcome.recorded:
        return
    if outcome.delivered:  # pragma: no cover - a delivery always records its dispatch
        return
    if outcome.reason in {"delivery_uncertain", UNDELIVERABLE_REASON}:
        # Nobody is working the task and nobody will be until a person looks, so the ball
        # names a person rather than an agent that does not exist (task-340's invariant).
        manager.handoff(
            task.id,
            actor=DISPATCHER_ACTOR,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt=outcome.detail,
        )
        return
    if outcome.reason == "unmet_dependencies":
        # **The defect task-150 section 4 records, fixed at the point it happened.** The
        # human's answer moved the ball to `agent`/`answer`; the dispatcher then could not
        # claim the task because a dependency was open, wrote a note saying so, and left
        # the ball where it was. Nothing in the world would ever act on that task, and it
        # read as work in progress on every surface.
        #
        # One write, not two: the handoff carries the refusal as its body. Two writes
        # would leave a window in which the record said an agent had it and gave no
        # reason, which is the state this is fixing.
        #
        # `external`/`dependency` and not `human`/`decision`, because there is nothing for
        # a person to decide -- the task is waiting on other work and will become
        # workable when that work closes, which is exactly what `external` means in
        # docs/agent-workflow.md's External Block rule.
        manager.handoff(
            task.id,
            actor=DISPATCHER_ACTOR,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.DEPENDENCY,
            ball_prompt=_blocked_prompt(outcome),
            body=outcome.detail or f"The handback was not delivered: `{outcome.reason}`.",
        )
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
