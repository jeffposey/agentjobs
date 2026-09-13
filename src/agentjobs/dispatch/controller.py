"""The durable execution controller: performing the journal's proposals (task-416).

``execution.coordinator.advance_execution`` replays an execution and records what should
happen next. This module is where those intents meet the world -- the session listing,
the batch worker's process, the dispatch gates, the task record -- through one adapter per
intent kind, each following design section 9a's crash-window contract:

* **Intent before effect, result after.** The intent is already committed when an adapter
  runs. An adapter that cannot establish an outcome records nothing, and the next tick
  proposes the same activity id again; one that can, records ``applied``, ``not_applied``
  or ``unknown``. ``unknown`` is never quietly turned into ``not_applied`` by waiting.
* **Only an execution the controller drives.** ``execution.controlled_by`` is set at
  admission when this machine's ``execution.controller`` is ``active``; everything else
  is followed by the legacy poller exactly as before, and neither follower touches the
  other's runs.
* **Recovery goes through production code.** Following a session is
  ``poller.follow_session`` -- the same ``poll_session`` judgement the poller uses. A
  relaunch is ``guards.dispatch_task``, so the kill switch, a hold, a Stop, the ceiling,
  the recorded runner and every budget cap bind a retry exactly as they bind a click.

**What each driver can prove, stated rather than assumed** (:data:`CAPABILITIES`). The
Claude session listing returns the ``--name`` a dispatch launches with -- read off
``claude agents --json --all`` on Claude Code 2.1.270, 2026-09-13, where this very run's
row carried ``agentjobs/task-416@6be7e05b`` -- so a launch whose launcher died after
spawning can be positively found. Its *absence* proves nothing: a launcher orphaned by its
coordinator can still register a session. So an unmatched launch with the launch marker
written is ``effect_unknown`` after the reconcile deadline, with one escalation, and
nothing is relaunched. A Codex App Server thread is owned by the process that started it
and has no listing to correlate against, so its ambiguous launch is unknown outright; the
Codex persisted-thread and explicit-fresh-start contracts are untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentjobs.dispatch import journal as journal_adapter
from agentjobs.dispatch.config import (
    DispatchConfigError,
    DispatchDisabledError,
    DispatchError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    Posture,
    ProjectNotEnabledError,
    RecordedRunnerUnavailableError,
    assert_dispatch_permitted,
    load_dispatch_config,
    resolve_for_observation,
)
from agentjobs.dispatch.ledger import (
    DispatchLedger,
    RunRecord,
    process_alive,
    process_identity,
    read_run,
    write_status,
)
from agentjobs.execution import reducer
from agentjobs.execution.coordinator import MODE_ACTIVE, advance_execution
from agentjobs.execution.errors import ExecutionStoreError, StaleOwner
from agentjobs.execution.reducer import Intent
from agentjobs.execution.store import Attempt, Execution, ExecutionStore
from agentjobs.models_v2 import Ball, BallReason, DispatchOutcome, LogEntryType
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

CONTROLLER_ACTOR = "dispatcher"
"""Who the controller writes as. The reserved actor, never a human's id."""

HANDED_OFF = "handed_off"
"""Not a failure: the task moved on (closed, handed to a person, claimed by another run),
so the execution has nothing left to recover and closes without writing to the task."""

OPERATION_NAMESPACE = uuid.UUID("0f41c2a0-4160-4d1e-9a16-c0a7e011e4a1")


@dataclass(frozen=True)
class DriverCapabilities:
    """What one driver's own store can prove about a launch a coordinator lost track of."""

    driver: str
    mode: str
    correlation: str
    """``session_name`` (the listing returns the name the launch carried), ``worker_receipt``
    (pid plus creation time recorded by the launcher) or ``none``."""
    authoritative_absence: bool
    """Whether "not in the store" proves "never started". False for every driver here."""
    evidence: str


CAPABILITIES: Mapping[Tuple[str, str], DriverCapabilities] = {
    ("claude", "session"): DriverCapabilities(
        driver="claude",
        mode="session",
        correlation="session_name",
        authoritative_absence=False,
        evidence=(
            "`claude agents --json --all` returns each background session's `name`, and a "
            "dispatch launches with `--name <project>/<task>@<run>` (verified on Claude Code "
            "2.1.270, 2026-09-13). A launcher orphaned by its coordinator can still register, "
            "so absence from the listing proves nothing."
        ),
    ),
    ("codex", "session"): DriverCapabilities(
        driver="codex",
        mode="session",
        correlation="none",
        authoritative_absence=False,
        evidence=(
            "An App Server thread is owned by the process that started it and has no "
            "machine-wide listing, so a lost launch cannot be correlated."
        ),
    ),
}


def capabilities_for(driver: str, mode: str) -> DriverCapabilities:
    """The capability row for a driver and mode; every batch runner shares one."""
    if mode == "batch":
        return DriverCapabilities(
            driver=driver,
            mode="batch",
            correlation="worker_receipt",
            authoritative_absence=False,
            evidence=(
                "The launcher records the worker's pid with its creation time the instant the "
                "process exists. A pid alone is never adopted; a process whose creation time "
                "differs is not the worker."
            ),
        )
    return CAPABILITIES.get(
        (driver, mode),
        DriverCapabilities(driver, mode, "none", False, "No correlation capability is known."),
    )


# ----- what one tick did ---------------------------------------------------------


@dataclass
class ControllerReport:
    lines: List[str] = field(default_factory=list)

    def say(self, subject: str, detail: str) -> None:
        self.lines.append(f"{subject}: {detail}")


# ----- the controller ----------------------------------------------------------------


def _parse(moment: Optional[str]) -> Optional[datetime]:
    if not moment:
        return None
    try:
        value = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _meta(record: RunRecord) -> Dict[str, Any]:
    from agentjobs.dispatch.runner import RunDirectory

    try:
        return dict(RunDirectory(path=record.path).read_meta())
    except Exception:  # noqa: BLE001 - an unreadable meta proves nothing
        return {}


class Controller:
    """Advances every execution the controller drives, once per :meth:`tick`.

    Stateless across processes by construction: everything it acts on is read from the
    journal and the world at the start of each tick, so the server after a restart picks
    up exactly where the process that died left off. The only in-memory state is the
    controller epoch it holds per execution, which fences a result computed before another
    process took the execution over.

    Every collaborator that touches the outside world is injectable, for the tests and for
    nothing else -- ``dispatch`` is ``guards.dispatch_task`` in the application.
    """

    def __init__(
        self,
        home: Path,
        *,
        registry: Optional[ProjectRegistry] = None,
        managers: Optional[Dict[str, TaskManagerLike]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        alive: Callable[[int], bool] = process_alive,
        identity: Callable[[int], Optional[str]] = process_identity,
        dispatch: Optional[Callable[..., Any]] = None,
        api_base: Optional[str] = None,
    ) -> None:
        self.home = Path(home)
        self.registry = registry or ProjectRegistry(home=self.home)
        self.managers = dict(managers or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.alive = alive
        self.identity = identity
        self._dispatch = dispatch
        self.api_base = api_base
        self._epochs: Dict[str, int] = {}

    # ----- plumbing -------------------------------------------------------------

    @property
    def store(self) -> ExecutionStore:
        return journal_adapter.journal(self.home)

    def project(self, project_id: str) -> Optional[Project]:
        try:
            return self.registry.get(project_id)
        except ProjectError:
            return None

    def manager(self, project_id: str) -> Optional[TaskManagerLike]:
        supplied = self.managers.get(project_id)
        if supplied is not None:
            return supplied
        project = self.project(project_id)
        if project is None:
            return None
        try:
            return dispatch_manager_for(project)
        except Exception:  # noqa: BLE001 - an unopenable store resolves nothing
            return None

    def record(self, run_id: str) -> Optional[RunRecord]:
        from agentjobs.dispatch.runner import runs_root

        directory = runs_root(self.home) / run_id
        return read_run(directory) if directory.is_dir() else None

    def settings(self):  # type: ignore[no-untyped-def]
        try:
            config = load_dispatch_config(self.home)
        except DispatchError:
            config = None
        from agentjobs.dispatch.config import DispatchConfig

        return config or DispatchConfig()

    def epoch_for(self, execution: Execution) -> int:
        held = self._epochs.get(execution.execution_id)
        if held is not None and held == execution.controller_epoch:
            return held
        epoch = self.store.claim_controller(execution.execution_id, owner_mode=execution.owner_mode)
        self._epochs[execution.execution_id] = epoch
        return epoch

    def result(
        self,
        intent: Intent,
        execution: Execution,
        state: str,
        *,
        result: Optional[Mapping[str, Any]] = None,
        error_class: Optional[str] = None,
    ) -> None:
        self.store.record_result(
            intent.activity_id,
            state=state,
            owner_epoch=self._epochs[execution.execution_id],
            result=result,
            error_class=error_class,
        )

    # ----- the tick -------------------------------------------------------------

    def tick(self) -> ControllerReport:
        """Fire due timers, then advance every open execution the controller drives.

        Never raises. A failure acting on one execution becomes a report line and leaves
        its proposal in place for the next tick, which is the recoverable direction.
        """
        report = ControllerReport()
        try:
            store = self.store
            for timer in store.due_timers(now=self.clock(), owner_prefix="execution:"):
                if store.fire_timer(timer.timer_id):
                    report.say(timer.execution_id or timer.timer_id, f"timer {timer.kind} fired")
            executions = [e for e in store.executions(open_only=True) if e.controller_driven]
        except ExecutionStoreError as exc:
            report.say("controller", f"journal unavailable: {exc}")
            return report
        for execution in executions:
            try:
                self.advance(execution, report)
            except StaleOwner as exc:
                self._epochs.pop(execution.execution_id, None)
                report.say(execution.execution_id, f"ownership moved; re-read next tick ({exc})")
            except Exception as exc:  # noqa: BLE001 - reported; the proposal stays for next tick
                report.say(execution.execution_id, f"{type(exc).__name__}: {exc}")
        return report

    def advance(self, execution: Execution, report: ControllerReport) -> None:
        """One execution: replay, record the proposals, perform each."""
        self.epoch_for(execution)
        current = self.store.execution(execution.execution_id) or execution
        advanced = advance_execution(self.store, execution.execution_id, mode=MODE_ACTIVE)
        for intent in advanced.intents:
            performer = getattr(self, f"perform_{intent.kind}", None)
            if performer is None:
                continue  # admit/launch belong to dispatch; deliver_signal to task-312
            detail = performer(current, intent)
            if detail:
                report.say(execution.execution_id, f"{intent.kind}: {detail}")

    # ----- launch reconciliation ------------------------------------------------

    def perform_launch_reconcile(self, execution: Execution, intent: Intent) -> Optional[str]:
        """Establish what happened to an admitted attempt whose launch was never confirmed."""
        run_id = str(intent.input["run_id"])
        attempt = self.store.attempt(run_id)
        if attempt is None or not attempt.is_live or attempt.state != "admitted":
            return None
        settings = self.settings().execution
        now = self.clock()
        admitted = _parse(attempt.admitted_at) or now
        age = (now - admitted).total_seconds()
        holder_alive = attempt.holder_pid is not None and self.alive(int(attempt.holder_pid))
        record = self.record(run_id)
        meta = _meta(record) if record is not None else {}

        if record is not None and record.session_id:
            return self._adopt_session(execution, intent, attempt, record, record.session_id)
        if record is not None and record.pid is not None and not record.is_session:
            return self._adopt_batch(execution, intent, attempt, record, meta)

        if not meta.get("launch_attempted_at"):
            # No marker: the launcher never ran. Only the process that admitted the
            # attempt could still run it, so while that process lives this waits.
            if holder_alive:
                if age >= settings.reconcile_deadline_seconds:
                    return self._unknown(
                        execution,
                        intent,
                        f"the process that admitted {run_id} (pid {attempt.holder_pid}) is "
                        f"alive and has not launched it in {int(age)}s",
                    )
                return None
            return self._not_applied_launch(execution, intent, attempt, record)

        if holder_alive and age < settings.launch_observation_seconds:
            return None  # the launcher is very probably still running
        driver = str(meta.get("driver") or (record.mode if record else "") or "claude")
        mode = record.mode if record is not None else ""
        capability = capabilities_for(driver, mode)
        if capability.correlation == "session_name" and record is not None:
            return self._correlate_session(
                execution, intent, attempt, record, meta, age, holder_alive
            )
        if age < settings.reconcile_deadline_seconds:
            return None
        return self._unknown(
            execution,
            intent,
            f"{run_id}'s launcher ran and recorded no worker, and the {driver} {mode} driver "
            f"cannot say whether one exists: {capability.evidence}",
        )

    def _correlate_session(
        self,
        execution: Execution,
        intent: Intent,
        attempt: Attempt,
        record: RunRecord,
        meta: Mapping[str, Any],
        age: float,
        holder_alive: bool,
    ) -> Optional[str]:
        from agentjobs.dispatch.runner import DispatchRunError, session_name

        deadline = self.settings().execution.reconcile_deadline_seconds
        name = str(
            meta.get("session_name")
            or session_name(record.project_id, record.task_id, record.run_id)
        )
        runner = self._runner_for(execution, record)
        if runner is None:
            return None
        try:
            rows = runner.ledger(include_finished=True)
        except DispatchRunError as exc:
            # Unreadable is not empty. Retried each tick until the deadline, then unknown.
            self._observer_failure(execution, record.run_id, str(exc))
            if age >= deadline:
                return self._unknown(
                    execution,
                    intent,
                    f"the session listing could not be read: {exc}",
                    error_class="observer_unavailable",
                )
            return None
        matches = [row for row in rows if str(row.get("name") or "") == name]
        if len(matches) == 1:
            session = str(matches[0].get("id") or matches[0].get("sessionId") or "")
            if session:
                return self._adopt_session(execution, intent, attempt, record, session[:8])
        if len(matches) > 1:
            return self._unknown(
                execution, intent, f"{len(matches)} sessions carry this run's name {name!r}"
            )
        if age >= deadline and not holder_alive:
            return self._unknown(
                execution,
                intent,
                f"no session named {name!r} is listed {int(age)}s after its launch, and the "
                "listing cannot prove one was never started",
            )
        return None

    def _adopt_session(
        self,
        execution: Execution,
        intent: Intent,
        attempt: Attempt,
        record: RunRecord,
        session_id: str,
    ) -> str:
        """A session exists for this attempt. Follow it, or stop it if nothing can.

        With a dispatch entry on the task the run is followable and simply reattaches.
        Without one, no poll can ever settle it (task-394's ``_abandon_unfollowable``), so
        it is stopped -- and only a stop the listing confirms makes it quiescent enough to
        conclude and try again.
        """
        meta = _meta(record)
        if isinstance(meta.get("dispatch_entry_id"), int):
            write_status(record, session_id=session_id, status="running")
            self.store.mark_launched(record.run_id, session_id=session_id)
            self.result(
                intent, execution, "applied", result={"session_id": session_id, "reattached": True}
            )
            return f"reattached session {session_id}"
        runner = self._runner_for(execution, record)
        stopped = False
        if runner is not None:
            try:
                stopped = runner.stop_session(session_id)
                listed = {str(row.get("id")) for row in runner.ledger()}
                stopped = stopped and session_id not in listed
            except Exception:  # noqa: BLE001 - an unconfirmed stop is not quiescence
                stopped = False
        if not stopped:
            return self._unknown(
                execution,
                intent,
                f"session {session_id} exists for {record.run_id} with no dispatch entry, and "
                "stopping it could not be confirmed",
            )
        write_status(
            record,
            session_id=session_id,
            abandoned_session=session_id,
            abandoned_session_stopped=True,
        )
        self.store.mark_launched(record.run_id, session_id=session_id)
        self.conclude(
            record,
            DispatchOutcome.CRASHED,
            body=(
                f"The launch produced session {session_id} but the dispatch was never recorded, "
                "so nothing could follow it. It was stopped and confirmed gone before this "
                "attempt was concluded."
            ),
        )
        self.result(
            intent, execution, "applied", result={"session_id": session_id, "stopped": True}
        )
        return f"stopped unfollowable session {session_id}"

    def _adopt_batch(
        self,
        execution: Execution,
        intent: Intent,
        attempt: Attempt,
        record: RunRecord,
        meta: Mapping[str, Any],
    ) -> str:
        self.store.mark_launched(record.run_id)
        self.result(intent, execution, "applied", result={"pid": record.pid})
        return f"batch worker pid {record.pid} was recorded; following it"

    def _not_applied_launch(
        self, execution: Execution, intent: Intent, attempt: Attempt, record: Optional[RunRecord]
    ) -> str:
        """Nothing was launched and nothing can launch it now: release it and let it retry."""
        conclusion = self.store.conclude(
            attempt.run_id,
            outcome=DispatchOutcome.CRASHED.value,
            status="failed",
            concluded_by="controller: admitted, never launched, and its admitting process is gone",
            refund=True,
            retry_owed=True,
            failure_class=reducer.LAUNCH_NOT_APPLIED,
        )
        if conclusion.won and record is not None:
            write_status(record, status="failed", outcome=DispatchOutcome.CRASHED.value)
        self.result(intent, execution, "not_applied", result={"refunded": True})
        return f"{attempt.run_id} never launched; its reservation is refunded"

    def _unknown(
        self,
        execution: Execution,
        intent: Intent,
        detail: str,
        *,
        error_class: str = reducer.EFFECT_UNKNOWN,
    ) -> str:
        self.result(
            intent, execution, "unknown", result={"detail": detail}, error_class=error_class
        )
        return f"unknown ({error_class}): {detail}"

    def _observer_failure(self, execution: Execution, run_id: str, detail: str) -> None:
        """Keep a durable trace of an observer failure, bounded to one per ten minutes."""
        bucket = int(self.clock().timestamp() // 600)
        try:
            self.store.append_event(
                execution.execution_id,
                "observed",
                {"phase": "observer_unavailable", "run_id": run_id, "detail": detail[:500]},
                source_id=f"observer:{run_id}:{bucket}",
            )
        except ExecutionStoreError:
            pass

    def _runner_for(self, execution: Execution, record: RunRecord):  # type: ignore[no-untyped-def]
        from agentjobs.dispatch.runner import DispatchRunner

        project = self.project(record.project_id)
        manager = self.manager(record.project_id)
        if project is None or manager is None:
            return None
        try:
            resolution = resolve_for_observation(
                record.project_id, self.home, runner=execution.envelope.get("runner")
            )
        except DispatchError:
            return None
        return DispatchRunner(
            manager=manager, resolution=resolution, project_root=project.root, home=self.home
        )

    # ----- observing a working attempt -----------------------------------------

    def perform_observe(self, execution: Execution, intent: Intent) -> Optional[str]:
        from agentjobs.dispatch.poller import follow_session

        run_id = str(intent.input["run_id"])
        attempt = self.store.attempt(run_id)
        if attempt is None or not attempt.is_live:
            return None
        record = self.record(run_id)
        if record is None:
            return None
        if record.is_session:
            results = follow_session(
                self.home,
                record,
                registry=self.registry,
                managers=self.managers,
                runner_name=execution.envelope.get("runner"),
            )
            failures = [r.detail for r in results if r.detail.startswith("poll failed")]
            if failures:
                self._observer_failure(execution, run_id, failures[0])
            return "; ".join(r.detail for r in results if r.detail) or None
        return self._observe_batch(execution, attempt, record)

    def _observe_batch(
        self, execution: Execution, attempt: Attempt, record: RunRecord
    ) -> Optional[str]:
        """A batch attempt: leave it to its supervisor while that lives, else prove the worker.

        The worker is identified by its pid *and* its creation time. Only a match is the
        worker; anything else -- gone, or a reused pid -- means the worker cannot still be
        writing, which is the quiescence a retry needs. Nothing in the working tree is
        touched either way: dirty paths are named in the result and left for whoever works
        the task next.
        """
        from agentjobs.dispatch.runner import _kill_tree, uncommitted_paths

        meta = _meta(record)
        supervisor = meta.get("supervisor_pid") or attempt.holder_pid
        if isinstance(supervisor, int) and self.alive(supervisor):
            return None
        pid = record.pid
        receipt = meta.get("pid_identity")
        running = pid is not None and self.alive(pid)
        same = (
            pid is not None
            and running
            and isinstance(receipt, str)
            and self.identity(pid) == receipt
        )
        limits = self.settings().limits
        started = record.started_at or _parse(attempt.admitted_at)
        elapsed = (self.clock() - started).total_seconds() if started else 0.0
        if running and not same and not isinstance(receipt, str):
            return (
                f"pid {pid} is alive and this run recorded no process identity, so it can "
                "neither be adopted nor declared gone"
            )
        if same:
            if elapsed < limits.run_timeout_seconds:
                return None
            assert pid is not None
            _kill_tree(int(pid))
            if self.alive(int(pid)) and self.identity(int(pid)) == receipt:
                return f"worker pid {pid} outlived its wall clock and did not stop when killed"
            self.conclude(
                record,
                DispatchOutcome.TIMEOUT,
                body=(
                    f"Terminated after the {limits.run_timeout_seconds}s wall-clock limit by the "
                    "durable controller; the supervisor that started it had already gone."
                ),
            )
            return f"worker pid {pid} timed out and was stopped"
        project = self.project(record.project_id)
        dirty = uncommitted_paths(project.root) if project is not None else None
        manager = self.manager(record.project_id)
        task = manager.get_task(record.task_id) if manager is not None else None
        entry = meta.get("dispatch_entry_id")
        moved = (
            task is not None
            and isinstance(entry, int)
            and any(e.id > entry and e.type.value in {"handoff", "transition"} for e in task.log)
        )
        preserved = (
            f" Uncommitted work was left exactly as found: {', '.join(sorted(dirty)[:10])}."
            if dirty
            else ""
        )
        if moved:
            self.conclude(
                record,
                DispatchOutcome.COMPLETED,
                body=(
                    "The worker finished and moved the ball; its supervisor had gone, so the "
                    f"durable controller recorded the ending.{preserved}"
                ),
            )
            return "worker finished after its supervisor died; recorded completed"
        self.conclude(
            record,
            DispatchOutcome.INTERRUPTED,
            body=(
                "The batch worker is gone and so is the supervisor that started it; nothing can "
                f"still be writing for this run.{preserved}"
            ),
        )
        return "batch worker gone; concluded interrupted"

    def conclude(self, record: RunRecord, outcome: DispatchOutcome, *, body: str) -> bool:
        """The terminal transition for one attempt, projected onto its task and meta.

        Unlike ``DispatchLedger._conclude`` this hands nothing to a person: whether a human
        is needed is the reducer's decision once the ending is recorded, and a recoverable
        ending keeps the task with the agent.
        """
        from agentjobs.dispatch.ledger import _dispatch_entry_id
        from agentjobs.dispatch.record_commit import commit_task_record

        manager = self.manager(record.project_id)
        task = manager.get_task(record.task_id) if manager is not None else None
        finished = self.clock()
        duration = (finished - record.started_at).total_seconds() if record.started_at else None
        projection = (
            journal_adapter.result_projection(
                record,
                outcome,
                actor=CONTROLLER_ACTOR,
                re=_dispatch_entry_id(task, record.run_id),
                duration_seconds=duration,
                body=body,
            )
            if task is not None
            else None
        )
        conclusion = journal_adapter.claim_conclusion(
            self.home, record, outcome, concluded_by="controller", projection=projection
        )
        if not conclusion.won:
            return False
        write_status(
            record,
            status=journal_adapter.status_for(outcome),
            outcome=outcome.value,
            finished_at=finished.isoformat(),
        )
        if manager is not None and projection is not None:
            journal_adapter.deliver_projection(self.home, manager, projection)
            commit_task_record(
                manager, record.task_id, subject=f"record run {record.run_id} as {outcome.value}"
            )
        return True

    # ----- Stop ---------------------------------------------------------------

    def perform_stop(self, execution: Execution, intent: Intent) -> Optional[str]:
        """Finish a Stop whose requester died between recording it and confirming it."""
        run_id = str(intent.input["run_id"])
        attempt = self.store.attempt(run_id)
        record = self.record(run_id)
        if attempt is None or not attempt.is_live or record is None:
            return None
        ledger = DispatchLedger(self.home, registry=self.registry, managers=self.managers)  # type: ignore[arg-type]
        if record.is_session and not record.session_id:
            return self._stop_unrecorded_session(execution, record, ledger)
        stopped = ledger._stop(record)
        if record.is_session and not stopped.stopped:
            return f"stop not confirmed: {stopped.detail}"
        if not record.is_session and record.pid is not None and self.alive(record.pid):
            return f"stop not confirmed: pid {record.pid} is still alive"
        ledger._conclude(
            record, DispatchOutcome.CANCELLED, actor=CONTROLLER_ACTOR, body=stopped.detail
        )
        return f"stopped: {stopped.detail}"

    def _stop_unrecorded_session(
        self, execution: Execution, record: RunRecord, ledger: DispatchLedger
    ) -> str:
        """A Stop on a launch nobody recorded a session for: the ``effect_unknown`` case.

        There is no id to stop, so the confirmation ``DispatchLedger.cancel`` needs cannot
        come from ``stop <id>`` (task-312 concludes only a confirmed Stop). It comes from
        the driver's listing instead, by the name the launch carried: every session under
        that name is stopped and the listing re-read, and only a readable listing showing
        none of them live concludes the run. The person's Stop is what makes this
        acceptable where the controller alone escalated -- they were asked to look, and
        asked for the run to end.
        """
        from agentjobs.dispatch.runner import DispatchRunError, session_name

        runner = self._runner_for(execution, record)
        if runner is None:
            return "stop not confirmed: no runner to read the listing with"
        name = str(
            _meta(record).get("session_name")
            or session_name(record.project_id, record.task_id, record.run_id)
        )
        try:
            for row in runner.ledger(include_finished=True):
                if str(row.get("name") or "") == name and row.get("state") != "stopped":
                    runner.stop_session(str(row.get("id")))
            still = [
                row
                for row in runner.ledger()
                if str(row.get("name") or "") == name and row.get("state") != "stopped"
            ]
        except DispatchRunError as exc:
            return f"stop not confirmed: the listing could not be read ({exc})"
        if still:
            return f"stop not confirmed: {len(still)} session(s) named {name!r} still listed"
        ledger._conclude(
            record,
            DispatchOutcome.CANCELLED,
            actor=CONTROLLER_ACTOR,
            body=(
                f"Stopped. No session under this run's name ({name}) is live in the driver's "
                "listing, so nothing launched for it is still writing."
            ),
        )
        return f"stopped: no live session named {name}"

    # ----- between attempts -----------------------------------------------------

    def perform_schedule_retry(self, execution: Execution, intent: Intent) -> str:
        from datetime import timedelta

        delay = int(intent.input["delay_seconds"])
        timer_id = str(intent.input["timer_id"])
        self.store.set_timer(
            timer_id,
            owner=f"execution:{execution.execution_id}",
            kind="retry",
            execution_id=execution.execution_id,
            due_at=self.clock() + timedelta(seconds=delay),
            payload={
                "round": intent.input["round"],
                "attempt_no": intent.input["attempt_no"],
                "delay_seconds": delay,
            },
        )
        self.result(
            intent, execution, "applied", result={"timer_id": timer_id, "delay_seconds": delay}
        )
        return f"attempt {intent.input['attempt_no']} due in {delay}s"

    def perform_observe_policy(self, execution: Execution, intent: Intent) -> str:
        """Record what the world permits now, before the side effect that depends on it."""
        observation = self.observe_policy(execution)
        self.store.append_event(
            execution.execution_id,
            "policy_observed",
            {
                **observation,
                "round": intent.input["round"],
                "attempt_no": intent.input["attempt_no"],
            },
            source_id=f"policy:{intent.activity_id}",
        )
        self.result(intent, execution, "applied", result=observation)
        verdict = "permitted" if observation["permitted"] else f"refused ({observation['class']})"
        return f"{verdict}: {observation['detail']}"

    def observe_policy(self, execution: Execution) -> Dict[str, Any]:
        """Stop/hold, enablement, the ceiling in force, the recorded runner and remaining caps."""
        from agentjobs.dispatch.budget import check_budget, check_machine_budget
        from agentjobs.dispatch.guards import effective_live_runs, live_runs

        observed: Dict[str, Any] = {"observed_at": self.clock().isoformat()}

        def verdict(permitted: bool, klass: Optional[str], detail: str) -> Dict[str, Any]:
            return {"permitted": permitted, "class": klass, "detail": detail, "observed": observed}

        manager = self.manager(execution.project_id)
        task = manager.get_task(execution.task_id) if manager is not None else None
        if task is None or not task.is_open:
            return verdict(False, HANDED_OFF, f"{execution.task_id} is closed or missing")
        observed[
            "ball"
        ] = f"{task.ball.value if task.ball else None}/{task.ball_reason.value if task.ball_reason else None}"
        if task.ball is not Ball.AGENT:
            return verdict(
                False,
                HANDED_OFF,
                f"{task.id}'s ball is with {task.ball.value if task.ball else 'nobody'}",
            )
        if task.ball_reason is BallReason.HOLD:
            return verdict(False, reducer.POLICY_WAIT, f"{task.id} is on hold")
        runner = execution.envelope.get("runner")
        group = execution.envelope.get("group")
        if not isinstance(runner, str) or not runner:
            return verdict(
                False, reducer.POLICY_REVOKED, "the envelope recorded no runner to continue"
            )
        try:
            resolution = assert_dispatch_permitted(
                execution.project_id,
                self.home,
                recorded=(runner, group if isinstance(group, str) else None),
            )
        except RecordedRunnerUnavailableError as exc:
            return verdict(False, reducer.POLICY_REVOKED, str(exc))
        except (
            DispatchSentinelError,
            DispatchDisabledError,
            ProjectNotEnabledError,
            DispatchNotConfiguredError,
        ) as exc:
            observed["launch_gate"] = getattr(exc, "reason", type(exc).__name__)
            return verdict(False, reducer.POLICY_WAIT, str(exc))
        except (DispatchConfigError, DispatchError) as exc:
            return verdict(False, reducer.POLICY_REVOKED, str(exc))
        observed["runner"] = resolution.runner.name
        ceiling = resolution.settings.ceiling
        observed["ceiling"] = ceiling.value
        try:
            granted = Posture(str(execution.envelope.get("posture")))
            if granted.rank > ceiling.rank:
                observed["posture_clamped_to"] = ceiling.value
        except ValueError:
            pass
        refusal = check_budget(task, resolution.limits.auto, now=self.clock())
        if refusal is None:
            refusal = check_machine_budget(self.home, resolution.limits, now=self.clock())
        if refusal is not None:
            observed["cap"] = refusal.limit
            klass = {
                "cooldown": reducer.COOLDOWN,
                "machine_per_hour": reducer.CAPACITY_WAIT,
            }.get(refusal.limit, reducer.BUDGET_EXHAUSTED)
            return verdict(False, klass, refusal.message)
        holding = [
            run for run in effective_live_runs(self.home, live_runs(self.home)) if run.takes_slot
        ]
        observed["slots"] = f"{len(holding)}/{resolution.limits.max_concurrent_runs}"
        if len(holding) >= resolution.limits.max_concurrent_runs:
            return verdict(
                False,
                reducer.CAPACITY_WAIT,
                f"all {resolution.limits.max_concurrent_runs} slot(s) are taken",
            )
        observed["dispatches_before"] = task.dispatch_count
        return verdict(
            True, None, f"runner {resolution.runner.name}, ceiling {ceiling.value}, caps have room"
        )

    def perform_relaunch(self, execution: Execution, intent: Intent) -> str:
        """The next paid attempt, through ``dispatch_task`` and every gate it runs."""
        from agentjobs.dispatch.budget import CapRefusal  # noqa: F401 - documents the mapping below
        from agentjobs.dispatch.envelope import GrantStoppedError
        from agentjobs.dispatch.guards import (
            AlreadyAdmittedError,
            BudgetCapError,
            ConcurrencyLimitError,
            DispatchRefused,
            DispatchRequest,
            LiveRunExistsError,
            TaskOnHoldError,
            dispatch_task,
        )
        from agentjobs.dispatch.runner import DispatchRunError
        from agentjobs.models_v2 import DispatchTrigger

        attempt_no = int(intent.input["attempt_no"])
        project = self.project(execution.project_id)
        manager = self.manager(execution.project_id)
        if project is None or manager is None:
            return "project unresolvable this tick"
        caused_by = self._authorising_entry(execution, manager)
        request = DispatchRequest(
            task_id=execution.task_id,
            trigger=DispatchTrigger.AUTO,
            caused_by=caused_by,
            continues_execution_id=execution.execution_id,
            admission_operation_id=f"{execution.execution_id}:attempt:{attempt_no}",
        )
        starter = self._dispatch or dispatch_task
        try:
            handle = starter(
                manager=manager,
                project=project,
                project_config=project.load_config(),
                request=request,
                home=self.home,
                api_base=self.api_base,
                # The budget caps judge the same moment the policy observation did.
                now=self.clock(),
            )
        except AlreadyAdmittedError as exc:
            self.result(
                intent, execution, "applied", result={"run_id": exc.attempt.run_id, "already": True}
            )
            return f"attempt {attempt_no} was already admitted as {exc.attempt.run_id}"
        except ConcurrencyLimitError as exc:
            return self._refused(execution, intent, reducer.CAPACITY_WAIT, exc)
        except BudgetCapError as exc:
            klass = {"cooldown": reducer.COOLDOWN, "machine_per_hour": reducer.CAPACITY_WAIT}.get(
                exc.refusal.limit, reducer.BUDGET_EXHAUSTED
            )
            return self._refused(execution, intent, klass, exc)
        except LiveRunExistsError as exc:
            return self._refused(execution, intent, HANDED_OFF, exc)
        except TaskOnHoldError as exc:
            return self._refused(execution, intent, reducer.POLICY_WAIT, exc)
        except (
            DispatchSentinelError,
            DispatchDisabledError,
            ProjectNotEnabledError,
            DispatchNotConfiguredError,
        ) as exc:
            return self._refused(execution, intent, reducer.POLICY_WAIT, exc)
        except (GrantStoppedError, RecordedRunnerUnavailableError) as exc:
            return self._refused(execution, intent, reducer.POLICY_REVOKED, exc)
        except DispatchRunError as exc:
            # Admitted and then failed to launch: dispatch_task concluded the attempt, and
            # that conclusion carries its own class. The relaunch itself happened.
            self.result(intent, execution, "applied", result={"launch_failed": str(exc)[:500]})
            return f"attempt {attempt_no} was admitted and its launch failed: {exc}"
        except (DispatchRefused, DispatchError) as exc:
            return self._refused(execution, intent, reducer.POLICY_REVOKED, exc)
        run_id = getattr(handle, "run_id", None)
        self.result(intent, execution, "applied", result={"run_id": run_id})
        return f"attempt {attempt_no} started as {run_id}"

    def _refused(self, execution: Execution, intent: Intent, klass: str, exc: Exception) -> str:
        self.result(
            intent, execution, "not_applied", result={"detail": str(exc)[:1000]}, error_class=klass
        )
        return f"refused ({klass}): {exc}"

    def _authorising_entry(self, execution: Execution, manager: TaskManagerLike) -> Optional[int]:
        """The human entry the execution's first attempt was caused by -- its grant.

        The journal's ``authorised`` event is the record, written before the launch; the
        task's dispatch entries are the fallback for an attempt that got that far.
        """
        recorded = journal_adapter.authorising_entry(self.home, execution.execution_id)
        if recorded is not None:
            return recorded
        task = manager.get_task(execution.task_id)
        if task is None:
            return None
        runs = {attempt.run_id for attempt in self.store.attempts_for(execution.execution_id)}
        for entry in task.log:
            if entry.type is LogEntryType.DISPATCH and isinstance(entry.data, dict):
                if entry.data.get("run_id") in runs and isinstance(
                    entry.data.get("caused_by"), int
                ):
                    return int(entry.data["caused_by"])
        return None

    # ----- the one human action ---------------------------------------------------

    def perform_escalate(self, execution: Execution, intent: Intent) -> str:
        klass = str(intent.input.get("failure_class") or reducer.EFFECT_UNKNOWN)
        run_id = intent.input.get("run_id")
        close = bool(intent.input.get("close"))
        manager = self.manager(execution.project_id)
        wrote = False
        if klass != HANDED_OFF and manager is not None:
            task = manager.get_task(execution.task_id)
            if task is not None and task.is_open and task.ball is not Ball.HUMAN:
                manager.handoff(
                    execution.task_id,
                    actor=CONTROLLER_ACTOR,
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.DECISION,
                    ball_prompt=escalation_prompt(
                        klass, execution, run_id, self._last_detail(execution)
                    ),
                    operation_id=str(uuid.uuid5(OPERATION_NAMESPACE, intent.activity_id)),
                )
                wrote = True
                from agentjobs.dispatch.record_commit import commit_task_record

                commit_task_record(
                    manager,
                    execution.task_id,
                    subject=f"escalate {execution.execution_id} ({klass})",
                )
        if close:
            self.store.close_execution(
                execution.execution_id,
                outcome="superseded" if klass == HANDED_OFF else "escalated",
                reason=klass,
                failure_class=klass,
            )
        self.result(
            intent,
            execution,
            "applied",
            result={"handoff": wrote, "closed": close},
            error_class=klass,
        )
        return f"{klass}: {'handed to a person' if wrote else 'nothing for a person to do'}"

    def _last_detail(self, execution: Execution) -> str:
        for event in reversed(self.store.events(execution.execution_id)):
            if event.kind == "activity_result":
                result = event.payload.get("result") or {}
                if isinstance(result, Mapping) and result.get("detail"):
                    return str(result["detail"])
            if event.kind == "policy_observed" and event.payload.get("detail"):
                return str(event.payload["detail"])
        return ""


ESCALATION_ACTIONS: Mapping[str, str] = {
    reducer.EFFECT_UNKNOWN: (
        "Check `claude agents --all` for a session with this run's name. If one is there, stop "
        "it; then press Stop on the run. Nothing is relaunched until the run is concluded."
    ),
    reducer.ATTEMPTS_EXHAUSTED: (
        "Read the dispatch_result entries to see why each worker vanished, then dispatch the "
        "task again or take it on yourself."
    ),
    reducer.BUDGET_EXHAUSTED: (
        "Decide whether this task deserves more runs than its cap; raise the cap in "
        "~/.agentjobs/dispatch.yaml or take the task on yourself."
    ),
    reducer.POLICY_REVOKED: "Dispatch the task again to grant a new run, or take it on yourself.",
    reducer.POLICY_WAIT: (
        "Dispatch stayed refused through every retry window. Re-enable dispatch for the project "
        "(or clear the hold) and dispatch the task again."
    ),
    reducer.WORKER_FAILED: "Read the run's output and the dispatch_result, fix the cause, and dispatch again.",
    reducer.TIMED_OUT: "Read the run's output: it ran out of wall clock. Split the task or raise the limit.",
    reducer.SPEC_GAP: "The run ended without saying what it needs. Tighten the spec, then dispatch again.",
}


def escalation_prompt(klass: str, execution: Execution, run_id: Any, detail: str) -> str:
    action = ESCALATION_ACTIONS.get(
        klass, "Read the dispatch_result entries and decide what the task needs."
    )
    what = f" Detail: {detail}" if detail else ""
    return (
        f"Automatic recovery of execution {execution.execution_id} stopped (`{klass}`) at run "
        f"{run_id or 'none'}.{what} {action}"
    )


def controller_tick(
    home: Path,
    *,
    registry: Optional[ProjectRegistry] = None,
    managers: Optional[Dict[str, TaskManagerLike]] = None,
    api_base: Optional[str] = None,
) -> ControllerReport:
    """One pass of the controller, for the server's poll loop and ``agentjobs execution tick``."""
    return Controller(home, registry=registry, managers=managers, api_base=api_base).tick()


__all__ = [
    "CAPABILITIES",
    "Controller",
    "ControllerReport",
    "DriverCapabilities",
    "ESCALATION_ACTIONS",
    "HANDED_OFF",
    "capabilities_for",
    "controller_tick",
    "escalation_prompt",
]
