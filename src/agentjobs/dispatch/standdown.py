"""Taking an approved task from the session that asked for its review (task-312).

**The failure.** A dispatched session hands its work to ``human``/``review`` and stays
attached; its run stays live and keeps the task's run lock, which is correct -- the lock
is what stops a second run starting on a task somebody holds. The human approves. The
scripted finish asks for the same lock, loses, and declines ``locked``. Nothing merges,
nothing is written to the task, and the only way forward was for a person to cancel the
run -- whose cancellation then wrote *nobody was told what this task needs* over the
approval. Observed on task-021, task-022, task-292 and task-294; task-312 entry 14 is the
evidence and the reason each run was still live.

**The remedy is a transfer, not a cancellation.** The approval says the session's work is
finished, so the finish asks the session to stand down and takes the task from it:

1. The stand-down is **recorded in the execution journal first** -- requester, source
   ``internal_transfer``, reason, and this process's pid -- as a ``stand_down_requested``
   event. It sets no cancellation flag and bumps no control generation, so nothing that
   reads a Stop reads one here: no continuation is refused, no approval is withdrawn.
2. The session is **polled once**, by the same ``poll_session`` the poller uses. One that
   has already gone idle is settled on the spot; that alone is task-292's and task-294's
   case.
3. Otherwise it is **stopped** with the driver's own ``stop`` -- never a signal to a pid --
   and polled until the journal shows the run concluded. ``poll_session`` concludes a
   stood-down session ``completed``, before the auth-stall check that kept task-022's run
   live for ever.
4. **Only then is the lock taken.** The run lock is not weakened or bypassed at any
   point: the finish waits while the session can still write, and never merges beside it.

**If quiescence cannot be confirmed, nothing merges.** The run stays live and keeps the
lock; its directory records ``finish_pending`` and the task says so. The poller spawns the
finish again when it sees that run end, which is what makes this survive the finish
process giving up or dying.

**What is never displaced:** a person's interactive session, a batch run, another finish,
or a run with no journal record to write the transfer on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from agentjobs.actors import FINISHER
from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.approval import ApprovalReceipt
from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
from agentjobs.dispatch.ledger import LedgerError, find_run, read_task_lock_holder, write_status
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.models_v2 import LogEntryType
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

STAND_DOWN_CONFIRM_SECONDS = 90.0
"""How long the finish waits for a stopped session to be observed stopped.

``claude stop`` returns within seconds on a healthy daemon; the margin is for a busy
machine and a session mid-tool-call. Longer than this and the durable half takes over --
the poller re-spawns the finish when the run ends -- so waiting longer buys nothing."""

STAND_DOWN_POLL_SECONDS = 2.0

FINISH_PENDING = "finish_pending"
"""Written on a run whose stand-down could not be confirmed: the approval entry a finish
is waiting to act on once this run ends. Read by the poller."""


@dataclass(frozen=True)
class StandDown:
    """What asking the task's holder to stand down achieved."""

    released: bool
    reason: str
    detail: str
    run_id: Optional[str] = None


def stand_down_for_finish(
    *,
    manager: TaskManagerLike,
    project: Project,
    task_id: str,
    receipt: ApprovalReceipt,
    home: Path,
    confirm_seconds: Optional[float] = None,
    poll_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = dispatch_clock.sleep,
    monotonic: Callable[[], float] = dispatch_clock.monotonic,
) -> StandDown:
    """Transfer ``task_id`` from the session holding its lock to this finish, or say why not.

    ``released`` means the holding run is concluded and the lock may be taken. Never
    raises for anything a run, a driver or the journal can do; a refusal is a result.
    """
    confirm_seconds = STAND_DOWN_CONFIRM_SECONDS if confirm_seconds is None else confirm_seconds
    poll_seconds = STAND_DOWN_POLL_SECONDS if poll_seconds is None else poll_seconds
    from agentjobs.dispatch import journal
    from agentjobs.dispatch.poller import handle_from_record
    from agentjobs.dispatch.runner import DispatchRunError, DispatchRunner

    holder = read_task_lock_holder(home, task_id, project_id=project.id)
    if holder is None:
        return StandDown(True, "lock_free", "the task's lock was released while asking")
    if not holder.run_id or holder.is_finish or holder.is_runway:
        return StandDown(False, "held_by_finish", f"{task_id} is held by {holder.describe()}")
    run_id = holder.run_id
    try:
        record = find_run(home, run_id)
    except LedgerError as exc:
        return StandDown(False, "no_run_record", str(exc), run_id)
    if record.task_id != task_id or record.project_id != project.id:
        return StandDown(False, "not_this_task", f"run {run_id} belongs to another task", run_id)
    if record.is_interactive:
        return StandDown(
            False,
            "interactive",
            f"run {run_id} is a person's own session, which a finish never displaces",
            run_id,
        )
    if not record.is_session:
        return StandDown(False, "not_a_session", f"run {run_id} is a {record.mode} run", run_id)

    def ended() -> bool:
        liveness = journal.journal_liveness(home, run_id)
        return liveness is False

    if ended():
        return StandDown(True, "already_ended", f"run {run_id} had already ended", run_id)
    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError as exc:
        return StandDown(False, getattr(exc, "reason", "dispatch_error"), str(exc), run_id)
    try:
        journal.request_stand_down(
            home,
            record,
            requester=receipt.approver,
            source="internal_transfer",
            reason=(
                f"the approval in entry {receipt.entry_id} transfers {task_id} to the "
                "scripted finish"
            ),
            transfer_to="finish",
            holder_pid=os.getpid(),
        )
    except ExecutionStoreError as exc:
        return StandDown(False, "not_recordable", str(exc), run_id)

    _say(
        manager,
        task_id,
        (
            f"Run `{run_id}` is being stood down so the scripted finish can act on "
            f"{receipt.approver}'s approval (entry {receipt.entry_id}). **This is a transfer, "
            "not a cancellation**: the session's work was approved, and nothing it built is "
            "discarded. Nothing is merged until the session is confirmed stopped."
        ),
        {"finish_step": "stand_down", "run_id": run_id, "approval_entry": receipt.entry_id},
    )

    handle = handle_from_record(home, record)
    runner = DispatchRunner(
        manager=dispatch_manager_for(project),
        resolution=resolution,
        project_root=project.root,
        home=home,
    )

    def poll() -> None:
        if handle is None:
            return
        try:
            runner.poll_session(handle)
        except (DispatchRunError, OSError):
            return

    poll()
    if not ended():
        if handle is not None and handle.session_id:
            runner.stop_session(handle.session_id)
        deadline = monotonic() + confirm_seconds
        while True:
            poll()
            if ended() or monotonic() >= deadline:
                break
            sleep(poll_seconds)
    if ended():
        return StandDown(True, "stood_down", f"run {run_id} stood down", run_id)

    try:
        write_status(record, **{FINISH_PENDING: receipt.entry_id})
    except OSError:
        pass
    detail = (
        f"Run `{run_id}` was asked to stand down and could not be confirmed stopped within "
        f"{int(confirm_seconds)} seconds. It keeps this task, so **nothing was merged**; the "
        "finish starts again by itself when the poller sees the run end."
    )
    _say(
        manager,
        task_id,
        detail,
        {"finish_step": "stand_down_unconfirmed", "run_id": run_id},
    )
    return StandDown(False, "stand_down_unconfirmed", detail, run_id)


def transfer_in_progress(home: Path, run_id: str) -> bool:
    """Whether a finish is still alive and taking this run's task over.

    The poller's question before it spawns a finish of its own: a live requester with no
    ``finish_pending`` on the run is mid-transfer and will take the lock itself.
    """
    from datetime import datetime

    from agentjobs.dispatch import journal
    from agentjobs.dispatch.pids import recorded_process_alive
    from agentjobs.dispatch.runner import RunDirectory, runs_root

    def _moment(value: object) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    request = journal.stand_down(home, run_id)
    if request is None:
        return False
    meta = RunDirectory(path=runs_root(home) / run_id).read_meta()
    if meta.get(FINISH_PENDING) is not None:
        return False
    pid = request.get("holder_pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return False
    # The requester was running when it wrote the request, so a process at that number
    # created afterwards is a different one (task-505). Believing a stranger here means
    # the poller waits for a transfer nobody is performing.
    return recorded_process_alive(pid, recorded_at=_moment(request.get("requested_at")))


def _say(manager: TaskManagerLike, task_id: str, body: str, data: dict) -> None:
    try:
        manager.add_log_entry(
            task_id, actor=FINISHER, type=LogEntryType.PROGRESS, body=body, data=data
        )
    except Exception:  # noqa: BLE001 - the journal holds the transfer; the note is narration
        return


__all__ = [
    "FINISH_PENDING",
    "STAND_DOWN_CONFIRM_SECONDS",
    "StandDown",
    "stand_down_for_finish",
    "transfer_in_progress",
]
