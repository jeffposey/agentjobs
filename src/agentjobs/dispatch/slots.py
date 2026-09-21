"""Giving a machine slot back when the work ends, not when the process does (task-482).

A slot bounds how much of this machine agents may use at once. Until this module it was
held for as long as the run's *process* was readable as live, which is a different thing
from the run doing work: ``agentjobs finish`` merges the branch, closes the task and
exits, and the session that asked for it is still sitting there, in an exchange with the
person who dispatched it. Observed on 2026-09-19, ``run_b400b920``: the task read
Completed on the dashboard while the run read ``running`` and held one of three slots,
fifty minutes after its merge. With the ceiling at three, that is a third of the machine
held by a session with nothing to do.

**So the slot follows the work.** A run whose task has closed releases its slot at once
and stays live in every other respect -- listed, polled, stoppable, and usable by the
person talking to it. The alternative, keeping the accounting and relabelling the run on
every surface, was rejected: it leaves the bound measuring process lifetime, which is not
what the bound is for, and it would have to be re-argued the next time a session outlives
its task by an hour.

**Releasing cannot double-book the slot**, which is the objection that had to be answered
before choosing. A later dispatch does not re-enter this run: whether it forks the
conversation or messages the session where it stands, it is admitted as a *new* run with
its own id and its own slot (``dispatch/wake.py``). So the released slot is free exactly
once, and the next thing the session is asked to do pays for itself.

**Two callers, one function.** The scripted finish releases at the moment it closes the
task, because it knows that moment exactly; the poller sweeps every live run each tick,
because a task closed any other way -- an agent's own ``close``, a person's click -- is
the same state and deserves the same answer. Neither is the fallback of the other: the
finish makes it immediate, the sweep makes it general.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from agentjobs.dispatch import clock as dispatch_clock
from agentjobs.dispatch.ledger import RunRecord, live_runs, write_status
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

TASK_CLOSED = "task_closed"
"""The only reason a slot is released early today. A word rather than a sentence: it is
written to a run's meta and to the journal, and both are read by code."""


@dataclass(frozen=True)
class SlotRelease:
    """One run that gave its slot back, for the poller's report."""

    run_id: str
    task_id: str
    detail: str


def release_slot(home: Path, record: RunRecord, *, reason: str = TASK_CLOSED) -> bool:
    """Release ``record``'s slot, leaving the run live. True when this call did it.

    Both the journal and the run's ``meta.yaml`` are written, in that order. The journal
    is the authority -- it is what ``ExecutionStore.admit`` counts inside the transaction
    that refuses a dispatch -- and the meta is the projection every surface reads, the
    same relationship those two have had since task-264.

    A journal that cannot be written does not stop the projection. The consequence of
    writing only the meta is that the advisory count in ``dispatch/guards.py`` frees the
    slot and the admission still refuses it, which is the pre-existing behaviour and
    recoverable by the next sweep; the consequence of refusing to record anything is a
    slot held for ever.
    """
    if record.slot_released or not record.takes_slot or not record.is_live:
        return False
    from agentjobs.dispatch.journal import journal  # local: journal imports runner

    try:
        journal(home).release_slot(record.run_id, reason=reason)
    except ExecutionStoreError:
        pass
    write_status(
        record,
        slot_released_at=dispatch_clock.utcnow().isoformat(),
        slot_released_reason=reason,
    )
    return True


def release_slot_for_task(
    home: Path, *, project_id: str, task_id: str, reason: str = TASK_CLOSED
) -> List[str]:
    """Release the slot of every live run dispatched for ``task_id``. Never raises.

    Plural because the ledger is a directory scan and a machine that has somehow ended up
    with two live runs on one task should not have one of them silently kept; in practice
    the per-task ownership rule means there is at most one.

    Called by the scripted finish immediately after it closes the task, where "never
    raises" is the whole of the contract: the merge is done and the task is closed, and
    an accounting write that failed must not turn a finished finish into a failed one.
    """
    released: List[str] = []
    try:
        records = live_runs(home)
    except OSError:
        return released
    for record in records:
        if record.task_id != task_id or record.project_id not in (project_id, ""):
            continue
        try:
            if release_slot(home, record, reason=reason):
                released.append(record.run_id)
        except OSError:
            continue
    return released


def sweep_released_slots(
    home: Path,
    *,
    registry: Optional[ProjectRegistry] = None,
    managers: Optional[Dict[str, TaskManagerLike]] = None,
) -> List[SlotRelease]:
    """Release the slot of every live run whose task is closed. Never raises.

    One task read per slot-holding run per tick, and only for runs that still hold a
    slot -- a released one is skipped by ``takes_slot`` and is never read again.

    A task that cannot be read leaves its run alone. Failing to find a record is not
    evidence the work is over, and the cost of being wrong here is asymmetric: keeping a
    slot a moment longer is the state this module was written to shorten, while freeing
    one for a run that is still working would let a second agent start beside it.
    """
    registry = registry or ProjectRegistry(home=home)
    managers = managers or {}
    released: List[SlotRelease] = []
    for record in live_runs(home):
        if not record.takes_slot or not record.task_id:
            continue
        manager = managers.get(record.project_id)
        if manager is None:
            project: Optional[Project]
            try:
                project = registry.get(record.project_id)
            except ProjectError:
                continue
            try:
                manager = dispatch_manager_for(project)
            except Exception:  # noqa: BLE001 - a project that cannot be opened says nothing
                continue
        try:
            task = manager.get_task(record.task_id)
        except Exception:  # noqa: BLE001 - see the docstring
            continue
        if task is None or task.is_open:
            continue
        try:
            done = release_slot(home, record)
        except OSError as exc:
            released.append(SlotRelease(record.run_id, record.task_id, f"could not release: {exc}"))
            continue
        if done:
            released.append(
                SlotRelease(
                    record.run_id,
                    record.task_id,
                    f"released its slot: {record.task_id} is closed and the session is still open",
                )
            )
    return released
