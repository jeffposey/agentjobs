"""Asking live sessions how they are getting on.

``DispatchRunner.poll_session`` decides what a session's state means and acts on it --
parking a permission prompt into a handoff, settling a finished session, recording a
cancellation. It was written to be called by a scheduler and, until this module, nothing
called it. A session therefore started, ran, finished, and left its run reading
``running`` forever with no ``dispatch_result`` on its task.

**This module finds the runs and hands them to that decision. It makes no decisions of
its own**, which is the whole point: the judgement in ``poll_session`` -- did the ball
move, has the staleness window passed, should the session be reaped -- has no second
implementation here to drift away from it.

Sessions only. A batch run is followed by its own supervisor thread inside the process
that spawned it, and needs nothing from here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
from agentjobs.dispatch.ledger import RunLock, RunRecord, live_runs, locks_root
from agentjobs.dispatch.runner import (
    DispatchRunError,
    DispatchRunner,
    RunDirectory,
    RunHandle,
    SessionPhase,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchMode
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.storage import TaskStorage

SESSION_POLL_SECONDS = 10.0
"""How often live sessions are asked how they are getting on.

Every tick costs one ``<runner> agents --json`` per project with a live session run, plus
one ``<runner> logs`` per run to refresh its transcript. Ten seconds keeps that
negligible while bounding how long a finished session can sit unreported, and it is the
**only** clock in this subsystem that spawns a process: the browser's live tail reads the
transcript this poller writes, so watching a run costs a file read no matter how many
people are watching (task-157, sc-9).
"""


@dataclass(frozen=True)
class PollResult:
    """What one poll of one run observed, for logging rather than control flow."""

    run_id: str
    phase: Optional[SessionPhase]
    detail: str

    @property
    def acted(self) -> bool:
        """True when the phase was terminal, so something was written to the task."""
        return self.phase in {
            SessionPhase.FINISHED,
            SessionPhase.STOPPED,
            SessionPhase.GONE,
        }


def _handle_from(home: Path, record: RunRecord) -> Optional[RunHandle]:
    """Rebuild the handle ``poll_session`` needs from what the run wrote to disk.

    ``dispatch_entry_id`` is the one field worth being careful about. ``_ball_moved``
    returns False without it, which would make every finished session look like it
    stopped without handing off -- a wrong and alarming outcome written to a task that
    did nothing wrong. A run missing it is skipped rather than guessed at.

    ``lock`` is the other one, and it was missing entirely until task-190. This handle
    is a *rebuild*: the object the dispatch created lives on the stack of the call that
    started the run, and by the time anything polls, that call has returned. So
    ``_finish_session``'s ``handle.release_lock()`` -- the release on the ordinary
    completion path for every session run there is -- was a silent no-op, because
    ``lock`` was ``None`` and the method is ``if self.lock is not None``. Every session
    the poller settled stranded its lock while the server that took it stayed up
    holding the descriptor, which is what two of the four locks found on 2026-08-20
    were. Attaching one here costs nothing (a lock is a task id and a path, not an open
    file) and restores the release to the path that actually concludes sessions.
    """
    directory = RunDirectory(path=record.path)
    meta = directory.read_meta()
    entry_id = meta.get("dispatch_entry_id")
    if not isinstance(entry_id, int):
        return None
    if not record.session_id:
        return None
    return RunHandle(
        run_id=record.run_id,
        task_id=record.task_id,
        mode=DispatchMode.SESSION,
        directory=directory,
        session_id=record.session_id,
        dispatch_entry_id=entry_id,
        lock=RunLock(
            task_id=record.task_id,
            path=locks_root(home) / f"{record.task_id}.lock",
            run_id=record.run_id,
        ),
    )


def _project_for(registry: ProjectRegistry, record: RunRecord) -> Optional[Project]:
    try:
        return registry.get(record.project_id)
    except ProjectError:
        return None


def poll_live_sessions(
    home: Path,
    *,
    registry: Optional[ProjectRegistry] = None,
    managers: Optional[Dict[str, TaskManager]] = None,
) -> List[PollResult]:
    """Poll every live session run once. Never raises; every failure becomes a result.

    A poll that throws would take down whatever schedules it, and the next tick would
    have settled the run anyway. Failures are therefore reported and dropped -- with one
    exception worth stating: a run that cannot be *resolved* is reported loudly rather
    than skipped silently, because a run nobody can resolve is exactly a run nobody will
    ever conclude.
    """
    registry = registry or ProjectRegistry(home=home)
    managers = managers or {}
    results: List[PollResult] = []

    for record in live_runs(home):
        if not record.is_session:
            continue

        handle = _handle_from(home, record)
        if handle is None:
            results.append(
                PollResult(record.run_id, None, "no session id or dispatch entry recorded")
            )
            continue

        manager = managers.get(record.project_id)
        project = _project_for(registry, record)
        if manager is None:
            if project is None:
                # Not silent on purpose. `manager_for` in the ledger drops this case,
                # and a run stamped with an id the registry does not hold -- the
                # implicit `_local` project, most often -- would then never be concluded
                # by anything.
                results.append(
                    PollResult(
                        record.run_id,
                        None,
                        f"project {record.project_id!r} is not in the registry, so this "
                        "run cannot be followed",
                    )
                )
                continue
            manager = TaskManager(TaskStorage(project.tasks_dir()))

        if project is None:
            results.append(
                PollResult(record.run_id, None, f"no root for project {record.project_id!r}")
            )
            continue

        try:
            resolution = assert_dispatch_permitted(record.project_id, home)
        except DispatchError as exc:
            # Dispatch being switched off must not orphan runs it already started.
            # Following a live run is not starting one, so this is reported and the run
            # is left live rather than concluded on a gate that says nothing about it.
            results.append(PollResult(record.run_id, None, f"dispatch no longer permitted: {exc}"))
            continue

        runner = DispatchRunner(
            manager=manager,
            resolution=resolution,
            project_root=project.root,
            home=home,
        )
        try:
            phase = runner.poll_session(handle)
        except (DispatchRunError, OSError) as exc:
            results.append(PollResult(record.run_id, None, f"poll failed: {exc}"))
            continue
        results.append(PollResult(record.run_id, phase, phase.value))

    return results


def _print_report(line: str) -> None:
    print(line, flush=True)


async def poll_sessions_forever(
    home: Path,
    *,
    interval: float = SESSION_POLL_SECONDS,
    report: Callable[[str], None] = _print_report,
) -> None:
    """Poll live sessions until cancelled. The loop `poll_session` was written to expect.

    Two details are deliberate:

    - **The first poll happens before the first sleep.** A session that ended while
      AgentJobs was down is then settled at startup rather than an interval later, which
      is what makes restarting clear it (task-157, sc-3) without `reconcile()` needing to
      learn about session phases.
    - **The work runs in a thread.** `poll_live_sessions` blocks on subprocesses, and on
      the event loop that would stall every request for as long as the runner takes to
      answer.

    Only *changes* are reported. A run that is still running says so once, not every ten
    seconds forever, so the server's output stays readable enough that the lines which do
    appear are worth reading.
    """
    seen: Dict[str, str] = {}
    while True:
        try:
            results = await asyncio.to_thread(poll_live_sessions, home)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - poll_live_sessions handles its own
            # A poller that dies takes session observability down with it and says
            # nothing, which is the failure this whole task exists to fix.
            report(f"Dispatch poll failed: {exc}")
            results = []
        current = {result.run_id: result.detail for result in results}
        for run_id, detail in current.items():
            if seen.get(run_id) != detail:
                report(f"Dispatch poll {run_id}: {detail}")
        seen = current
        await asyncio.sleep(interval)
