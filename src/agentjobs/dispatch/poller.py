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

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.config import (
    DispatchError,
    assert_dispatch_permitted,
    resolve_for_observation,
)
from agentjobs.dispatch.ledger import RunLock, RunRecord, live_runs, run_lock_path
from agentjobs.dispatch.runner import (
    DispatchRunError,
    DispatchRunner,
    RunDirectory,
    RunHandle,
    SessionPhase,
)
from agentjobs.models_v2 import DispatchMode
from agentjobs.background import never_abandoned
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

TERMINAL_PHASES = frozenset({SessionPhase.FINISHED, SessionPhase.STOPPED, SessionPhase.GONE})
"""The phases after which the run is over and its task's lock is free again.

The same set ``PollResult.acted`` names, hoisted so the handback delivery below and that
property cannot come to disagree about what "this run has ended" means.
"""

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
        return self.phase in TERMINAL_PHASES


def handle_from_record(home: Path, record: RunRecord) -> Optional[RunHandle]:
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
            path=run_lock_path(home, record.task_id, project_id=record.project_id),
            run_id=record.run_id,
            project_id=record.project_id,
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
    managers: Optional[Dict[str, TaskManagerLike]] = None,
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

    # Interactive runs first, and through their own sweep rather than `poll_session`
    # (task-354): the sweep ends a record whose task moved on or whose session is gone,
    # and never parks, stops or settles anything. `phase` is None because these have no
    # session phase -- nothing observed them, something merely checked they exist.
    from agentjobs.dispatch.interactive import sweep_interactive_runs  # local: same subsystem

    for swept in sweep_interactive_runs(home, registry=registry, managers=managers):
        results.append(PollResult(swept.run_id, None, swept.detail))

    from agentjobs.dispatch.journal import controller_driven  # local: journal imports runner

    for record in live_runs(home):
        if not record.is_session:
            continue
        if controller_driven(home, record.run_id):
            # The durable controller follows this run (task-416). Two followers of one run
            # is the double-settle the journal's compare-and-set exists to lose, and the
            # controller is the one that can also recover it.
            continue
        results.extend(follow_session(home, record, registry=registry, managers=managers))

    results.extend(_release_finished_slots(home, registry, managers))
    results.extend(_shadow_journal(home, registry, managers))
    results.extend(_drive_controller(home, registry, managers))
    results.extend(_recover_parked(home, registry, managers))
    results.extend(_resume_interrupted_finishes(home, registry, managers))
    results.extend(_retract_resolved_asks(registry, managers))
    results.extend(_sweep_idle_sessions(home))
    return results


def _release_finished_slots(
    home: Path, registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """Free the slot of every live run whose task has closed (task-482).

    After the session polls, so a run that ended this tick is already terminal and is not
    swept; before everything that decides whether to *start* something, so a slot freed
    here is free for this tick's queue rather than the next one's.

    ``phase`` is None for the same reason the interactive sweep's is: nothing observed the
    session, something merely read its task.
    """
    from agentjobs.dispatch.slots import sweep_released_slots

    try:
        released = sweep_released_slots(home, registry=registry, managers=managers)
    except Exception as exc:  # noqa: BLE001 - the sweep must never take the poller down
        return [PollResult("slot-release", None, f"failed: {exc}")]
    return [PollResult(item.run_id, None, item.detail) for item in released]


RETRACTION_SWEEP_SECONDS = 300.0
"""How often the retraction sweep actually runs, whatever the poll interval is.

It is a repair, not a heartbeat. The thing it corrects is a ball left standing for
minutes or hours, so five minutes is not a compromise -- and reading every project's
human-held tasks on every ten-second tick forever would be paying a recurring cost for
a rare event. The same reasoning, and the same number, as the idle-session sweep.
"""

_last_retraction_sweep = 0.0
"""When this process last swept. Process-local on purpose: a second server sweeping
independently changes nothing, because the sweep is idempotent and reports only what it
actually corrected."""


def _retract_resolved_asks(
    registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """Take back an ask whose reason has been resolved (task-467).

    Late in the tick, after the walks above have had their say, so a parent a walk has
    just corrected itself is already correct and is not found here twice.

    This is the half of the attention rule that nothing used to own: an epic parent
    handed to a person because a child was in review stayed there after the child
    merged, because the walk that wrote it had already stopped and nothing was watching.
    A permanent member of the waiting set keeps the badge lit *and*, under the
    one-alert-per-episode rule, stops the next genuinely new wait from ever interrupting.
    So the sweep runs on the clock rather than on somebody noticing, which is the whole
    of the second clause.
    """
    from agentjobs.models_v2 import Ball
    from agentjobs.retraction import retract

    global _last_retraction_sweep
    now = dispatch_clock.monotonic()
    if _last_retraction_sweep and now - _last_retraction_sweep < RETRACTION_SWEEP_SECONDS:
        return []
    _last_retraction_sweep = now

    results: List[PollResult] = []
    try:
        projects = registry.list_projects()
    except ProjectError as exc:
        return [PollResult("retraction", None, f"no projects to sweep: {exc}")]
    for project in projects:
        # Every registered project, not only the ones that happen to have a run this
        # tick: a stale ask outlives the run that wrote it by definition, and a sweep
        # that only visited busy projects would never reach the epic that went quiet.
        try:
            manager = managers.get(project.id) or dispatch_manager_for(project)
            # Only the tasks that could possibly be findings, because this runs on every
            # tick forever: the sweep's first condition is `ball is human`, so reading
            # the whole corpus would deserialise every log in every project to discard
            # almost all of it. Children are then asked for per candidate, and there are
            # rarely more than a handful of candidates.
            lines = retract(
                manager.list_tasks(ball=Ball.HUMAN),
                manager.get_subtasks,
                handoff=manager.handoff,
                log=manager.add_log_entry,
            )
        except Exception as exc:  # noqa: BLE001 - a sweep must never take the poller down
            results.append(PollResult("retraction", None, f"{project.id}: failed: {exc}"))
            continue
        results.extend(PollResult("retraction", None, f"{project.id}: {line}") for line in lines)
    return results


def _sweep_idle_sessions(home: Path) -> List[PollResult]:
    """Stop idle, resumable Claude sessions, only when ``idle_sessions.enforce`` is on (task-447).

    After everything else, so a run settled this tick has already had its session reaped by
    the runner and is not the sweep's business. Throttled inside the module to one sweep per
    five minutes; with enforcement off it reads the config and records nothing new.
    """
    from agentjobs.dispatch.idle_sessions import tick

    try:
        lines = tick(home)
    except Exception as exc:  # noqa: BLE001 - the sweep must never take the poller down
        return [PollResult("idle-sessions", None, f"failed: {exc}")]
    return [
        PollResult(subject, None, detail)
        for subject, _, detail in (line.partition(": ") for line in lines)
    ]


def _recover_parked(
    home: Path, registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """Probe, wake and confirm runs parked on a login or quota refusal (task-417).

    After every session has been polled, so a stall found this tick has already joined its
    incident and an immediate probe can run in the same tick. A probe blocks for at most
    its 30-second timeout, and only while an incident is open and due.
    """
    from agentjobs.dispatch.auth_recovery import tick

    try:
        lines = tick(home, registry=registry, managers=managers)
    except Exception as exc:  # noqa: BLE001 - recovery must never take the poller down
        return [PollResult("auth-recovery", None, f"failed: {exc}")]
    return [
        PollResult(subject, None, detail)
        for subject, _, detail in (line.partition(": ") for line in lines)
    ]


def _resume_interrupted_finishes(
    home: Path, registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """Start the finish a dead finish process still owes its task (task-443).

    Last in the tick, so every run above has been settled first: a posture finish's run
    that ended this tick has released its lock, and the settle has had its say about it.
    """
    from agentjobs.dispatch.finish_resume import resume_interrupted_finishes

    try:
        decisions = resume_interrupted_finishes(home, registry=registry, managers=managers)
    except Exception as exc:  # noqa: BLE001 - it never raises; this is the belt to that
        return [PollResult("finish-resume", None, f"failed: {exc}")]
    return [PollResult(item.subject, None, item.detail) for item in decisions]


def _drive_controller(
    home: Path, registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """One pass of the durable controller (task-416), after the legacy work of the tick.

    It acts only on executions it drives, so on a machine whose controller has never been
    switched on it reads the open executions and does nothing else.
    """
    from agentjobs.dispatch.controller import controller_tick  # local: controller imports poller

    from agentjobs.dispatch.epic import advance_hosted_walks

    report = controller_tick(home, registry=registry, managers=managers)
    lines = list(report.lines)

    def resolve(project_id: str) -> Optional[tuple]:
        project = _project_for_id(registry, project_id)
        if project is None:
            return None
        manager = managers.get(project_id) or dispatch_manager_for(project)
        return manager, project

    # Walks detached to the server (task-416, epic-5): one step each, rebuilt from their
    # record, so no session has to stay alive to wait on an epic's children.
    lines.extend(advance_hosted_walks(home, resolve=resolve))
    return [
        PollResult(subject, None, detail)
        for subject, _, detail in (line.partition(": ") for line in lines)
    ]


def _project_for_id(registry: ProjectRegistry, project_id: str) -> Optional[Project]:
    try:
        return registry.get(project_id)
    except ProjectError:
        return None


def follow_session(
    home: Path,
    record: RunRecord,
    *,
    registry: ProjectRegistry,
    managers: Dict[str, TaskManagerLike],
    runner_name: Optional[str] = None,
) -> List[PollResult]:
    """Poll one live session run and act on what it says. Never raises.

    The one implementation of "follow a session", shared by the legacy poller and the
    durable controller's observe activity, so the judgement in ``poll_session`` still has
    no second copy to drift from.
    """
    handle = handle_from_record(home, record)
    if handle is None:
        return [PollResult(record.run_id, None, "no session id or dispatch entry recorded")]

    manager = managers.get(record.project_id)
    project = _project_for(registry, record)
    if manager is None:
        if project is None:
            # Not silent on purpose. `manager_for` in the ledger drops this case, and a run
            # stamped with an id the registry does not hold -- the implicit `_local`
            # project, most often -- would then never be concluded by anything.
            return [
                PollResult(
                    record.run_id,
                    None,
                    f"project {record.project_id!r} is not in the registry, so this "
                    "run cannot be followed",
                )
            ]
        manager = dispatch_manager_for(project)

    if project is None:
        return [PollResult(record.run_id, None, f"no root for project {record.project_id!r}")]

    results: List[PollResult] = []
    try:
        resolution = assert_dispatch_permitted(record.project_id, home)
    except DispatchError as refused:
        # Launching being switched off must not stop runs already going from being
        # followed (task-416, design section 9a). The gate answers "may a run start";
        # settling, stall reports and Stop confirmation are not starts, and a handback
        # a settle delivers goes back through every gate on its own.
        try:
            resolution = resolve_for_observation(record.project_id, home, runner=runner_name)
        except DispatchError as exc:
            return [PollResult(record.run_id, None, f"cannot be followed: {exc}")]
        results.append(
            PollResult(
                record.run_id,
                None,
                f"launching is refused ({refused}); following the run anyway",
            )
        )

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
        return results
    results.append(PollResult(record.run_id, phase, phase.value))
    if phase in TERMINAL_PHASES:
        results.extend(_deliver_pending_handback(home, project, manager, record, handle))
    return results


def _shadow_journal(
    home: Path, registry: ProjectRegistry, managers: Dict[str, TaskManagerLike]
) -> List[PollResult]:
    """Keep the execution journal's inbox current and replay open executions, in shadow.

    Once per tick and after every session has been polled, so nothing here can delay a
    settle. It performs no activity (task-264); it reports only when it imported something
    or could not.
    """
    from agentjobs.dispatch.journal import shadow_tick  # local: journal imports runner

    def resolve(project_id: str) -> Optional[TaskManagerLike]:
        supplied = managers.get(project_id)
        if supplied is not None:
            return supplied
        try:
            return dispatch_manager_for(registry.get(project_id))
        except Exception:  # noqa: BLE001 - an unresolvable project imports nothing
            return None

    report = shadow_tick(home, resolve)
    lines: List[PollResult] = [PollResult("journal", None, error) for error in report.errors]
    if report.imported:
        lines.append(PollResult("journal", None, f"imported {report.imported} task-log entries"))
    return lines


def _deliver_pending_handback(
    home: Path,
    project: Project,
    manager: TaskManagerLike,
    record: RunRecord,
    handle: RunHandle,
) -> List[PollResult]:
    """Give the run that just settled its waiting feedback, in the session it settled.

    **This is the half a click cannot do for itself** (task-384). A human clicks Request
    Changes while the session that asked for the review is still up, which is the normal
    case and not an edge one: the notification that brought them to the page *was* the
    handoff. The route refuses to start a rival run -- correctly -- and the feedback then
    has nowhere to go until something notices the session has gone quiet. The poller is
    the only thing that ever notices, so it is the only thing that can finish the job.

    A supervisor that re-derives what to do from the record rather than remembering it,
    which is what makes this survive a server restart: the pending handback is a property
    of the task and the run, not of a promise some earlier request made.

    ``after_entry`` is the load-bearing argument. Without it every settling run would
    look like one with feedback waiting -- its own dispatch handed the ball to it -- and
    the poller would dispatch each task once more for nothing. With it, only a human
    handoff written *after* this run was dispatched counts, which is exactly the click
    that arrived while the run was still up.
    """
    from agentjobs.dispatch.handback import deliver_handback, pending_handback, record_handback

    task = manager.get_task(record.task_id)
    if task is None:
        return []
    try:
        config = project.load_config()
    except ProjectError:  # pragma: no cover - a project whose config cannot be read
        return []
    # A recovery resume already carried every human message up to this entry (task-417),
    # so settling the run must not deliver them a second time.
    delivered = handle.directory.read_meta().get("delivered_through_entry")
    after = handle.dispatch_entry_id
    if isinstance(delivered, int) and (after is None or delivered > after):
        after = delivered
    entry = pending_handback(task, config, after_entry=after)
    if entry is None:
        return []
    finishing = _finish_an_approval(home, project, task, config, record, entry.id)
    if finishing is not None:
        return [PollResult(record.run_id, None, finishing)]
    outcome = deliver_handback(
        manager=manager,
        project=project,
        project_config=config,
        task=task,
        home=home,
        caused_by=entry.id,
    )
    record_handback(manager, task, outcome)
    if not outcome.considered:
        return []
    return [
        PollResult(
            record.run_id,
            None,
            f"handback from entry {entry.id}: {outcome.reason}",
        )
    ]


def _finish_an_approval(
    home: Path,
    project: Project,
    task: object,
    config: Dict[str, object],
    record: RunRecord,
    entry_id: int,
) -> Optional[str]:
    """Start the finish an approval is owed, instead of waking the session (task-312).

    An approval that arrived while this run was live is the one handback that is not
    addressed to the session: it is addressed to the scripted finish, which could not
    start because this run held the task. Now the run has ended, that finish is started
    -- and the session is not woken to merge by hand beside it.

    ``None`` means this is not that case, and ordinary handback delivery proceeds: the
    waiting handoff is not a standing approval, the machine offers no finish, or a finish
    is alive and already taking the task over.
    """
    from agentjobs.dispatch.approval import standing_approval_for
    from agentjobs.dispatch.finish import finish_is_offered, resume_approved_finish
    from agentjobs.dispatch.standdown import transfer_in_progress

    try:
        receipt = standing_approval_for(home, task, config, project_id=project.id)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - an unreadable approval is ordinary delivery's to handle
        return None
    if receipt is None or receipt.entry_id != entry_id:
        return None
    if not finish_is_offered(project.id, home):
        return None
    if transfer_in_progress(home, record.run_id):
        return f"approval from entry {entry_id}: the finish taking it over is still running"
    spawned = resume_approved_finish(
        project_id=project.id, task_id=record.task_id, approver=receipt.approver, home=home
    )
    if spawned is None:
        # Fall through to ordinary delivery: the session carrying the clearance is a
        # worse merge than the scripted one, and a far better one than nobody at all.
        return None
    return f"approval from entry {entry_id}: started the scripted finish"


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
    - **The work runs in a thread, and a shutdown waits for it.** `poll_live_sessions`
      blocks on subprocesses, and on the event loop that would stall every request for
      as long as the runner takes to answer. It also reads SQLite, which is why the
      thread is never abandoned on cancellation -- see :mod:`agentjobs.background`.

    Only *changes* are reported. A run that is still running says so once, not every ten
    seconds forever, so the server's output stays readable enough that the lines which do
    appear are worth reading.
    """
    seen: Dict[str, str] = {}
    while True:
        try:
            # Not `asyncio.to_thread`: cancelling that await abandons the thread, and
            # the lifespan closes every database as soon as this task returns. See
            # `agentjobs.background` for the crash that taught us (task-467).
            results = await never_abandoned(poll_live_sessions, home)
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
