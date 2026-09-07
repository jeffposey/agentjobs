"""Runs for sessions a person is sitting in (task-354).

Until this module, a task worked from a chat session had no run record at all, by
design: ``registration`` refused interactive sessions because the poller's protections
-- park, stop, settle -- would eventually end a session somebody was typing into. The
cost of that design was measured on 2026-09-06: task-352 was claimed and being worked,
and the dashboard showed *Runs 0*, no session log on the task, and a Dispatch button
offering to start a second agent on it. Jeff: "do you even know what tasks are being
worked on?"

**An interactive run is a run record the poller never acts on.** It is written by the
claim -- the one act every session performs -- when the claim carries the session's
identity, and it says: this session, in this directory, is working this task. What the
record buys is visibility: the Runs badge, the Runs tab, the slot board and the task
page all read the ledger, so a record is what makes the work show up, and the task's
transcript panel can find the session's own JSONL from the session id it carries.

What it deliberately does not buy:

- **A run slot.** ``limits.max_concurrent_runs`` exists to stop a click starting an
  agent the machine cannot afford. A session Jeff is typing into is not that, and
  refusing his dispatch because he is chatting would be the wrong refusal. So
  ``slot_runs`` leaves these out and ``occupied`` counts without them.
- **Any of the poller's judgements.** Nothing here parks a permission prompt, settles
  a finish without a handoff, or calls the driver's ``stop``. An interactive run ends
  for two reasons only: the task moved on (the ball left the agent, or the task
  closed), or the session is no longer in the driver's ledger. Neither writes to the
  task; the task record already says everything the person did.

The per-task "one live run" rule still holds for these, and that is the fix for the
Dispatch button: a dispatch aimed at a task an interactive session holds is refused
``live_run_exists``, exactly as it would be for a dispatched one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
from agentjobs.dispatch.ledger import (
    RunLockTimeout,
    RunRecord,
    acquire_run_lock,
    conclude_interactive,
    live_runs,
    read_run,
)
from agentjobs.dispatch.runner import (
    DispatchRunError,
    DispatchRunner,
    RunDirectory,
    git_head,
    new_run_id,
)
from agentjobs.models_v2 import Ball, DispatchMode, DispatchOutcome, Lifecycle, Task
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.session_identity import SessionIdentity
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

ORIGIN_CLAIMED = "claimed"
"""The claim wrote the record: the ordinary way an interactive run begins."""

ORIGIN_REGISTERED = "registered"
"""``agentjobs run register`` wrote it, for a session that claimed some other way."""


def start_interactive_run(
    *,
    home: Path,
    project: Project,
    task: Task,
    identity: Optional[SessionIdentity],
    actor: str,
    origin: str = ORIGIN_CLAIMED,
    caused_by: Optional[int] = None,
) -> Optional[RunRecord]:
    """Write the run record for a session that has just claimed ``task``, or nothing.

    ``None`` is an ordinary answer and never an error: no identity (a human at the
    keyboard, a caller that sent none), a task that is not active, or a task that
    already has a live run -- a replayed claim, or a dispatched run whose agent claimed
    on arrival. Every one of those is a state in which writing a record would either
    invent a session or put two runs on one task.
    """
    if identity is None or not identity.session_id.strip():
        return None
    if task.lifecycle is not Lifecycle.ACTIVE:
        return None
    for run in live_runs(home):
        if run.task_id == task.id:
            return None

    run_id = new_run_id()
    try:
        acquire_run_lock(home, task.id, run_id=run_id, timeout=1.0)
    except RunLockTimeout:
        # Something else holds the task -- a finish, a dispatch in its first second.
        # Whatever it is, it is what the record would have said, so nothing is lost.
        return None

    directory = RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": task.id,
            "project_id": project.id,
            "mode": DispatchMode.INTERACTIVE.value,
            "driver": identity.driver,
            "agent": actor,
            # No posture: AgentJobs did not choose this session's permission envelope
            # and cannot read it. `origin` is what a reader should key on instead.
            "origin": origin,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "session_id": identity.session_id,
            "cwd": identity.cwd,
            "git_head": git_head(project.root),
            "caused_by": caused_by,
            "argv": [],
        },
    )
    return read_run(directory.path)


@dataclass(frozen=True)
class SweepResult:
    """What one pass over the interactive runs did to one of them."""

    run_id: str
    concluded: bool
    detail: str


def settle_for_task(home: Path, task: Optional[Task], task_id: str) -> List[SweepResult]:
    """End this task's interactive runs if the task has moved on. Called after a verb.

    The rule, in one place: **an interactive run is live while its task is `active` with
    the ball on an agent**, which is exactly the state a claim puts it in. Every verb
    that leaves that state is the session saying it is done with this task for now --
    a handoff to a human, a release back to the pool, a close -- and the record says so
    the moment the verb lands rather than a poll later.

    Deliberately not "the task is open": a released task is open, `ready`, and owned by
    nobody, and a card still showing a session working it would be showing work that has
    been given away.
    """
    results: List[SweepResult] = []
    if task is not None and task.lifecycle is Lifecycle.ACTIVE and task.ball is Ball.AGENT:
        return results
    for record in live_runs(home):
        if record.task_id != task_id or not record.is_interactive:
            continue
        conclude_interactive(home, record, DispatchOutcome.COMPLETED, detail=_why_over(task))
        results.append(SweepResult(record.run_id, True, _why_over(task)))
    return results


def _why_over(task: Optional[Task]) -> str:
    """The sentence the record carries about why this run stopped."""
    if task is None:
        return "the task record is gone"
    if task.lifecycle is Lifecycle.CLOSED:
        return "the task was closed"
    if task.lifecycle is not Lifecycle.ACTIVE:
        return "the task was released back to the pool"
    return "the ball left the agent"


def sweep_interactive_runs(
    home: Path,
    *,
    registry: Optional[ProjectRegistry] = None,
    managers: Optional[Dict[str, TaskManagerLike]] = None,
) -> List[SweepResult]:
    """One pass over every live interactive run, ending the ones that are over.

    Two checks, in the order that costs less. The task's state is a file read and
    catches the common ending -- the session handed off or closed the task through a
    path that did not call :func:`settle_for_task`. The driver's ledger is a subprocess,
    read once per project per sweep and only for a run that survived the first check; a
    session missing from it has ended, and its record is concluded ``session_ended``
    without a word written to the task.

    **The listing comes from the project's own configured runner**, which is the same
    resolution the poller makes, rather than from a default ``claude``: the driver is a
    project's choice, and a sweep that assumed one would be asking the wrong program
    whether somebody's session is alive.

    Any failure to look leaves every run alone. Declaring live work dead on a failed
    lookup is the one mistake here that the next tick cannot undo.
    """
    registry = registry or ProjectRegistry(home=home)
    managers = managers or {}
    results: List[SweepResult] = []
    rows_by_project: Dict[str, Optional[List[Dict[str, object]]]] = {}

    for record in live_runs(home):
        if not record.is_interactive:
            continue

        try:
            project: Optional[Project] = registry.get(record.project_id)
        except ProjectError:
            project = None
        manager = managers.get(record.project_id)
        if manager is None and project is not None:
            manager = dispatch_manager_for(project)
        if manager is not None and record.task_id:
            settled = settle_for_task(home, manager.get_task(record.task_id), record.task_id)
            if settled:
                results.extend(settled)
                continue

        if not record.session_id or project is None:
            continue
        if record.project_id not in rows_by_project:
            rows_by_project[record.project_id] = _session_rows(home, project)
        rows = rows_by_project[record.project_id]
        if rows is None:
            results.append(SweepResult(record.run_id, False, "could not read the session ledger"))
            continue
        if _session_listed(rows, record.session_id):
            results.append(SweepResult(record.run_id, False, "session still running"))
            continue
        conclude_interactive(
            home,
            record,
            DispatchOutcome.SESSION_ENDED,
            detail="the session is no longer in the driver's ledger",
        )
        results.append(SweepResult(record.run_id, True, "session ended"))
    return results


def _session_rows(home: Path, project: Project) -> Optional[List[Dict[str, object]]]:
    """Every session this project's runner currently lists, or ``None`` if it could not
    be asked. ``None`` and "no sessions" are different answers and only one of them
    means anything has ended."""
    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError:
        # Dispatch being off for this project says nothing about whether a person's
        # session is open, so it must not conclude one.
        return None
    runner = DispatchRunner(
        manager=dispatch_manager_for(project),
        resolution=resolution,
        project_root=project.root,
        home=home,
    )
    try:
        return runner.ledger(scoped=False)
    except DispatchRunError:
        return None


def _session_listed(rows: List[Dict[str, object]], session_id: str) -> bool:
    """Whether the driver's ledger still lists this session, under either of its names.

    Background rows carry a short ``id`` and the full ``sessionId``; interactive rows
    carry only the full one. An interactive record stores the full uuid, so the match
    is exact on ``sessionId`` and, for a record written with the short form, a prefix
    match is accepted too -- a session id is the first group of its uuid.
    """
    for row in rows:
        full = str(row.get("sessionId") or "")
        short = str(row.get("id") or "")
        if session_id in (full, short):
            return True
        if full and full.startswith(session_id):
            return True
    return False
