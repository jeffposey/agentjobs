"""What ran, what is running, and how to stop it.

``claude agents --json`` is a ledger, but it is not *ours*. It knows a session's pid,
cwd, status and state; it does not know which AgentJobs task the session belongs to,
which dispatch caused it, who authorised that dispatch, or anything at all about runs
that have finished -- ``claude rm`` deletes the row. So AgentJobs keeps its own, keyed by
task, and correlates the two by session id.

One directory per run under ``~/.agentjobs/runs/<run_id>/``, and **no shared index
file**. Each directory is written only by the run that owns it, so listing is a scan and
there is nothing contended to lock. Same reasoning as one YAML file per task.

The two modes diverge here more than anywhere else, and the divergence is deliberate:

- **Batch runs never outlive their supervisor.** On startup, anything non-terminal is
  declared ``interrupted`` and handed to a human. Pid adoption was rejected -- pids are
  reused, matching start times needs a dependency, and the result would be an orphaned
  autonomous agent editing a repository with nothing supervising it and no kill switch.
- **Session runs deliberately do outlive it, and reconciliation re-attaches.** Neither
  half of that objection survives for a session: it is looked up by id in a manager that
  outlives us, and ``claude stop <id>`` works whether or not AgentJobs is running.
  Killing live sessions because ``agentjobs serve`` restarted would destroy real work --
  including a session someone is mid-conversation with on their phone -- for no safety
  gain.
"""

from __future__ import annotations

import errno
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agentjobs.dispatch.config import sentinel_path
from agentjobs.dispatch.runner import (
    META_FILENAME,
    resolve_executable,
    runs_root,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, DispatchMode, DispatchOutcome
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.storage import TaskStorage

LOCKS_DIRNAME = ".locks"
"""Run locks live under the runs root. A leading dot cannot collide with a run id."""

TERMINAL_STATUSES = frozenset({"finished", "cancelled", "failed"})
"""Statuses meaning nothing is executing. Everything else counts as live."""

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.01


class RunLockTimeout(Exception):
    """Another run holds this task's lock, or a dead one left it behind."""


class LedgerError(Exception):
    """A ledger operation could not be completed."""


# ----- the per-task run lock --------------------------------------------------


def locks_root(home: Path) -> Path:
    """Directory holding one lock file per task with a live run."""
    return runs_root(home) / LOCKS_DIRNAME


@dataclass
class RunLock:
    """An exclusive claim on one task, held for the lifetime of its run.

    The storage lock protects a read-modify-write lasting microseconds; a run lasts half
    an hour. Same primitive, different lifetime, which is why it cannot simply be a
    ``with`` block around ``mutate_task``.
    """

    task_id: str
    path: Path
    handle: int

    def release(self) -> None:
        """Release the lock. Safe to call twice."""
        if self.handle < 0:
            return
        try:
            os.close(self.handle)
        except OSError:  # pragma: no cover - already closed
            pass
        self.handle = -1
        try:
            self.path.unlink()
        except FileNotFoundError:  # pragma: no cover - already cleaned up
            pass


def acquire_run_lock(
    home: Path, task_id: str, *, run_id: str = "", timeout: float = LOCK_TIMEOUT_SECONDS
) -> RunLock:
    """Take the run lock for one task, or raise naming the file that is holding it.

    ``O_CREAT|O_EXCL``, the same primitive task-055 chose and for the same reasons:
    exclusive create is atomic on every filesystem this runs on, it needs no dependency,
    and it behaves identically on Windows and Unix. A second locking convention would be
    a second set of stale-lock failure modes to learn.

    Windows reports contention two ways -- ``FileExistsError`` and, for a file whose
    delete has not finished, ``PermissionError``. Both are contention and both are
    retried; treating the second as fatal made a losing claimant crash with "Permission
    denied" instead of being told something else held it.

    A stale lock times out with a message naming the file, rather than hanging. That is
    the whole point of choosing a primitive with no automatic release: a hang tells you
    nothing and a named file tells you what to delete.
    """
    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            pass
        except OSError as exc:  # pragma: no cover - unexpected filesystem failure
            if exc.errno != errno.EEXIST:
                raise
        if time.monotonic() >= deadline:
            holder = _read_text(path)
            raise RunLockTimeout(
                f"{task_id} already has a run holding {path} "
                f"({holder or 'no holder recorded'}). Either it is still going, or a "
                "run died and left the lock behind -- delete the file to clear it."
            )
        time.sleep(LOCK_POLL_SECONDS)
    os.write(handle, f"pid={os.getpid()} run={run_id}".encode("ascii"))
    return RunLock(task_id=task_id, path=path, handle=handle)


def _read_text(path: Path) -> str:
    """Best-effort read, for putting a lock's holder into an error message."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


# ----- the ledger itself ------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """One run, as the ledger knows it."""

    run_id: str
    path: Path
    task_id: str = ""
    project_id: str = ""
    mode: str = ""
    posture: str = ""
    status: str = "unknown"
    outcome: Optional[str] = None
    session_id: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    caused_by: Optional[int] = None
    argv: List[str] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        """True while nothing has declared this run over."""
        return self.status not in TERMINAL_STATUSES

    @property
    def is_session(self) -> bool:
        return self.mode == DispatchMode.SESSION.value

    def elapsed_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """How long this run has been going, or ran for."""
        if self.started_at is None:
            return None
        moment = now or datetime.now(timezone.utc)
        return (moment - self.started_at).total_seconds()


def _as_optional_int(value: object) -> Optional[int]:
    """Read an int out of run metadata, which is a YAML mapping of anything."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_run(directory: Path) -> RunRecord:
    """Read one run directory. An unreadable meta yields a live record, not an absence.

    Deliberate: a run that cannot be read cannot be shown to have ended, and treating it
    as finished would let a second run start beside it and would hide a crash.
    """
    meta: Dict[str, object] = {}
    meta_path = directory / META_FILENAME
    if meta_path.is_file():
        try:
            loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, yaml.YAMLError):
            meta = {}
    started = meta.get("started_at")
    parsed: Optional[datetime] = None
    if isinstance(started, str):
        try:
            parsed = datetime.fromisoformat(started)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    argv = meta.get("argv")
    return RunRecord(
        run_id=str(meta.get("run_id") or directory.name),
        path=directory,
        task_id=str(meta.get("task_id") or ""),
        project_id=str(meta.get("project_id") or ""),
        mode=str(meta.get("mode") or ""),
        posture=str(meta.get("posture") or ""),
        status=str(meta.get("status") or "unknown"),
        outcome=str(meta["outcome"]) if meta.get("outcome") else None,
        session_id=str(meta["session_id"]) if meta.get("session_id") else None,
        pid=_as_optional_int(meta.get("pid")),
        started_at=parsed,
        caused_by=_as_optional_int(meta.get("caused_by")),
        argv=[str(item) for item in argv] if isinstance(argv, list) else [],
    )


def list_runs(home: Path) -> List[RunRecord]:
    """Every run this machine has a directory for, newest first."""
    root = runs_root(home)
    if not root.is_dir():
        return []
    records = [
        read_run(directory)
        for directory in root.iterdir()
        if directory.is_dir() and directory.name != LOCKS_DIRNAME
    ]
    return sorted(
        records,
        key=lambda record: record.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def live_runs(home: Path) -> List[RunRecord]:
    """Runs nothing has declared over."""
    return [record for record in list_runs(home) if record.is_live]


def find_run(home: Path, run_id: str) -> RunRecord:
    """One run by id, or an error naming what is there instead."""
    directory = runs_root(home) / run_id
    if not directory.is_dir():
        known = ", ".join(record.run_id for record in list_runs(home)[:5]) or "none"
        raise LedgerError(f"No run {run_id!r}. Recent runs: {known}.")
    return read_run(directory)


def write_status(record: RunRecord, **fields: object) -> None:
    """Merge fields into a run's meta.yaml."""
    meta_path = record.path / META_FILENAME
    meta: Dict[str, object] = {}
    if meta_path.is_file():
        try:
            loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, yaml.YAMLError):
            meta = {}
    meta.update(fields)
    meta_path.write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=False), encoding="utf-8"
    )


# ----- stopping things --------------------------------------------------------


@dataclass(frozen=True)
class StopResult:
    """What happened when a run was asked to stop."""

    run_id: str
    stopped: bool
    detail: str


class DispatchLedger:
    """Reads and acts on the machine's runs, writing outcomes back to their tasks.

    Holds a registry so it can resolve a run's project back to the manager that owns its
    task record -- a run knows its project id, and a terminal entry has to land on the
    right task file.
    """

    def __init__(
        self,
        home: Path,
        *,
        registry: Optional[ProjectRegistry] = None,
        session_command: Optional[List[str]] = None,
    ) -> None:
        self.home = Path(home)
        self.registry = registry or ProjectRegistry(home=self.home)
        #: How to reach the session manager. Overridable so tests can stand in a fake,
        #: and so a runner that is not Claude Code can be driven by the same code.
        self.session_command = session_command or ["claude"]

    # ----- resolution --------------------------------------------------------

    def manager_for(self, record: RunRecord) -> Optional[TaskManager]:
        """The TaskManager owning this run's task, or None when it cannot be resolved."""
        try:
            project: Project = self.registry.get(record.project_id)
        except ProjectError:
            return None
        return TaskManager(TaskStorage(project.tasks_dir()))

    def _session(self, *args: str) -> subprocess.CompletedProcess:
        """Run a session-manager subcommand. argv is a list; there is no shell."""
        argv = [resolve_executable(self.session_command[0]), *self.session_command[1:], *args]
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # An unreachable session manager is a LedgerError, never an uncaught OSError.
            # Callers decide what to do about not being able to look; a traceback out of
            # startup reconciliation would take the server down over a missing binary.
            raise LedgerError(f"Could not run `{' '.join(argv[:2])} {args[0]}`: {exc}") from exc

    def session_ledger(self) -> List[Dict[str, object]]:
        """Every background session the CLI knows about, machine-wide."""
        completed = self._session("agents", "--json", "--all")
        if completed.returncode != 0:
            raise LedgerError(
                f"Could not read the session ledger: {(completed.stderr or '').strip()[:300]}"
            )
        import json

        try:
            loaded = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Session ledger was not JSON: {exc}") from exc
        if isinstance(loaded, dict):
            loaded = loaded.get("agents") or loaded.get("sessions") or []
        return [row for row in loaded if isinstance(row, dict)]

    # ----- cancellation ------------------------------------------------------

    def cancel(self, run_id: str, *, actor: str = "dispatcher") -> StopResult:
        """Stop one run, by whichever means its mode calls for."""
        record = find_run(self.home, run_id)
        if not record.is_live:
            return StopResult(run_id, False, f"already {record.outcome or record.status}")
        result = self._stop(record)
        self._conclude(record, DispatchOutcome.CANCELLED, actor=actor, body=result.detail)
        return result

    def _stop(self, record: RunRecord) -> StopResult:
        """Ask a run to stop. Session mode delegates; batch mode signals."""
        if record.is_session:
            return self._stop_session(record)
        return self._stop_batch(record)

    def _stop_session(self, record: RunRecord) -> StopResult:
        """`claude stop <id>`, and nothing else.

        Deliberately not reimplemented with signals. The session manager owns the
        process; going around it with a pid would leave its ledger claiming a session
        that no longer exists, and would lose the conversation that `stop` preserves.
        """
        if not record.session_id:
            return StopResult(record.run_id, False, "no session id recorded")
        try:
            completed = self._session("stop", record.session_id)
        except LedgerError as exc:
            return StopResult(record.run_id, False, str(exc))
        if completed.returncode == 0:
            return StopResult(record.run_id, True, f"stopped session {record.session_id}")
        return StopResult(
            record.run_id,
            False,
            f"`stop {record.session_id}` exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:200]}",
        )

    def _stop_batch(self, record: RunRecord) -> StopResult:
        """Signal the process group, then kill the tree.

        The tree, not the process: an agent that shelled out to pytest must not leave the
        pytest behind. Windows is the platform this runs on, so it is the reference path
        rather than the port.
        """
        if record.pid is None:
            return StopResult(record.run_id, False, "no pid recorded")
        from agentjobs.dispatch.runner import _kill_tree  # local: same subsystem

        _kill_tree(record.pid)
        return StopResult(record.run_id, True, f"killed process tree at pid {record.pid}")

    def stop_everything(self, *, actor: str = "dispatcher") -> List[StopResult]:
        """The panic button: refuse all new runs, then stop every live one.

        The sentinel is written **first**. If stopping takes a while -- a batch run using
        its grace period -- nothing new may start in the meantime, which is the whole
        point of pressing it.
        """
        sentinel = sentinel_path(self.home)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            f"written by 'agentjobs dispatch stop' at {datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        results = []
        for record in live_runs(self.home):
            result = self._stop(record)
            self._conclude(record, DispatchOutcome.CANCELLED, actor=actor, body=result.detail)
            results.append(result)
        return results

    # ----- reconciliation ----------------------------------------------------

    def reconcile(self, *, actor: str = "dispatcher") -> List[StopResult]:
        """Settle every run left behind by a previous process.

        Called at startup. Batch and session are opposites here, on purpose -- see the
        module docstring.
        """
        results: List[StopResult] = []
        sessions: Optional[Dict[str, Dict[str, object]]] = None

        for record in live_runs(self.home):
            if record.is_session:
                if sessions is None:
                    try:
                        rows = self.session_ledger()
                    except LedgerError as exc:
                        # Cannot tell whether the sessions survived, so leave them alone
                        # rather than declaring live work dead on a failed lookup.
                        results.append(
                            StopResult(record.run_id, False, f"ledger unreadable: {exc}")
                        )
                        sessions = {}
                        continue
                    sessions = {
                        str(row.get("id")): row for row in rows if row.get("id") is not None
                    }
                if record.session_id and record.session_id in sessions:
                    results.append(
                        StopResult(record.run_id, False, "session still running; re-attached")
                    )
                    continue
                self._conclude(
                    record,
                    DispatchOutcome.INTERRUPTED,
                    actor=actor,
                    body=(
                        "This session is no longer known to the session manager, so it "
                        "cannot be followed or resumed. It was recorded as live when "
                        "AgentJobs last stopped."
                    ),
                )
                results.append(StopResult(record.run_id, True, "session gone; marked interrupted"))
                continue

            # Batch: the supervisor died with the process that owned it.
            self._conclude(
                record,
                DispatchOutcome.INTERRUPTED,
                actor=actor,
                body=(
                    "A batch run was still marked live when AgentJobs restarted. Batch "
                    "runs do not outlive their supervisor, so whatever it was doing "
                    "stopped without reporting. Its output is in the run directory."
                ),
            )
            results.append(StopResult(record.run_id, True, "batch run marked interrupted"))
        return results

    # ----- reaping -----------------------------------------------------------

    def reap(self, record: RunRecord) -> StopResult:
        """Remove a finished session's job state, freeing the pid it still holds.

        ``claude rm`` **refuses when the session's worktree holds uncommitted changes**,
        and that refusal is a signal rather than an obstacle: it means a run produced
        work nobody has looked at. It is surfaced, never forced -- ``-f`` here would
        delete exactly the thing worth keeping.
        """
        if not record.session_id:
            return StopResult(record.run_id, False, "no session id recorded")
        try:
            completed = self._session("rm", record.session_id)
        except LedgerError as exc:
            return StopResult(record.run_id, False, str(exc))
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode == 0:
            write_status(record, reaped=True)
            return StopResult(record.run_id, True, f"removed session {record.session_id}")
        detail = output.strip()[:300] or f"exited {completed.returncode}"
        write_status(record, reap_blocked=detail)
        return StopResult(record.run_id, False, f"not removed: {detail}")

    def reap_finished(self) -> List[StopResult]:
        """Reap every session run that has reached a terminal state and not been reaped."""
        results = []
        for record in list_runs(self.home):
            if not record.is_session or record.is_live:
                continue
            meta_path = record.path / META_FILENAME
            if meta_path.is_file() and "reaped: true" in _read_text(meta_path):
                continue
            results.append(self.reap(record))
        return results

    # ----- writing the outcome back to the task ------------------------------

    def _conclude(
        self,
        record: RunRecord,
        outcome: DispatchOutcome,
        *,
        actor: str,
        body: Optional[str] = None,
    ) -> None:
        """Write a run's terminal entry to its task, and hand the ball to a human.

        A run reaching a terminal state without a task-visible record is the failure this
        whole subsystem is designed against, so this happens even when the task cannot be
        resolved -- in which case the run's own meta records why, and the run at least
        stops counting as live.
        """
        write_status(record, status="cancelled", outcome=outcome.value)
        manager = self.manager_for(record)
        if manager is None or not record.task_id:
            write_status(record, unattributed=f"no task record for {record.task_id!r}")
            return
        task = manager.get_task(record.task_id)
        if task is None:
            write_status(record, unattributed=f"task {record.task_id!r} not found")
            return

        duration = record.elapsed_seconds()
        manager.record_dispatch_result(
            record.task_id,
            actor=actor,
            run_id=record.run_id,
            outcome=outcome,
            re=_dispatch_entry_id(task, record.run_id),
            duration_seconds=duration,
            log_path=str(record.path),
            body=body,
        )
        task = manager.get_task(record.task_id)
        if task is not None and task.is_open and task.ball is not Ball.HUMAN:
            manager.handoff(
                record.task_id,
                actor=actor,
                ball=Ball.HUMAN,
                ball_reason=BallReason.DECISION,
                ball_prompt=(
                    f"Run {record.run_id} ended `{outcome.value}` and nobody was told "
                    "what this task needs. Read the dispatch_result entry, then either "
                    "dispatch again or take it on yourself."
                ),
            )


def _dispatch_entry_id(task: object, run_id: str) -> Optional[int]:
    """The id of the dispatch entry this run belongs to, so the result threads to it."""
    for entry in reversed(getattr(task, "log", [])):
        if entry.type.value == "dispatch" and entry.data.get("run_id") == run_id:
            return int(entry.id)
    return None
