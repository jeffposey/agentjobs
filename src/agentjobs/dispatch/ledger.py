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
    TERMINAL_STATUSES,
    finish_stamped,
    resolve_executable,
    runs_root,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, DispatchMode, DispatchOutcome
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.storage import TaskStorage

LOCKS_DIRNAME = ".locks"
"""Run locks live under the runs root. A leading dot cannot collide with a run id."""

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


@dataclass(frozen=True)
class LockHolder:
    """Who a lock file says is holding it, as far as the file can be read.

    Both fields are optional because both can be missing from a file written by an
    older version, and a lock nobody can attribute must still be *describable* -- the
    refusal a human reads is built from this.
    """

    pid: Optional[int] = None
    run_id: str = ""
    text: str = ""

    @classmethod
    def parse(cls, text: str) -> "LockHolder":
        """Read ``pid=<n> run=<id>``, tolerating anything else."""
        fields: Dict[str, str] = {}
        for token in text.split():
            key, _, value = token.partition("=")
            if _:
                fields[key] = value
        pid: Optional[int] = None
        try:
            pid = int(fields["pid"])
        except (KeyError, ValueError):
            pid = None
        return cls(pid=pid, run_id=fields.get("run", ""), text=text.strip())

    def describe(self) -> str:
        """The holder as a sentence fragment, for a message a human reads."""
        parts = []
        if self.run_id:
            parts.append(f"run {self.run_id}")
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        return ", ".join(parts) or (self.text or "nothing recorded")


def process_alive(pid: int) -> bool:
    """Whether a process with this id exists right now.

    ``os.kill(pid, 0)`` is the Unix idiom and is **not** available here: on Windows
    CPython implements ``os.kill`` with ``TerminateProcess`` for every signal that is
    not a console event, so the usual liveness probe would kill the process it is
    asking about. Windows therefore goes through ``OpenProcess`` and
    ``GetExitCodeProcess``.

    Pid reuse means a ``True`` here can be wrong -- some unrelated process may have
    inherited the number. That is the safe direction: a wrongly-alive answer refuses a
    lock, and the ledger check above it is what actually clears one. Nothing is
    reclaimed on this answer alone unless the lock names no run at all.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # exists, owned by somebody else
            return True
        return True

    import ctypes

    still_active = 259
    query_limited_information = 0x1000
    # getattr rather than ctypes.windll.kernel32: the attribute only exists on
    # Windows, so the direct spelling is a type error wherever mypy is not run here.
    kernel32 = getattr(ctypes, "windll").kernel32
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell; treat as alive, which refuses rather than clears
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def stale_lock_reason(home: Path, holder: LockHolder) -> Optional[str]:
    """Why this holder is not holding anything any more, or ``None`` if it may be.

    Two rules, in this order, and the order is the whole design:

    1. **If the lock names a run, that run's record decides, and nothing else does.**
       A terminal run releases the lock; a live one keeps it however dead the recorded
       pid looks. The pid must not get a vote here, because a CLI dispatch starts a
       session that deliberately outlives the process that started it -- the recorded
       pid is *expected* to be gone while the session runs, and reclaiming on that
       would hand a second agent to a task that already has one. That is the failure
       this lock exists to prevent, and it would be worse than the leak.
    2. **If the lock names no run, the pid decides.** That is the narrow window between
       taking the lock and the run existing to be named; a holder that died inside it
       left a lock for a run that never started.

    Anything else -- a named run whose directory has gone, a live pid, an unreadable
    file -- is left alone. "I cannot tell" refuses.

    Note what is deliberately absent: no clock. Nothing here expires, so there is no
    lease to renew and no window in which a slow run loses a lock it is still using.
    A lock is cleared only against a positive statement on disk that its run is over.
    """
    if holder.run_id:
        directory = runs_root(home) / holder.run_id
        if not directory.is_dir():
            return None
        record = read_run(directory)
        if record.is_live:
            return None
        return f"its run {holder.run_id} is {record.outcome or record.status}"
    if holder.pid is not None and not process_alive(holder.pid):
        return f"the process that took it (pid {holder.pid}) is gone, and it named no run"
    return None


@dataclass
class RunLock:
    """An exclusive claim on one task, held for the lifetime of its run.

    The storage lock protects a read-modify-write lasting microseconds; a run lasts half
    an hour. Same primitive, different lifetime, which is why it cannot simply be a
    ``with`` block around ``mutate_task``.

    **The claim is the file's existence, not an open descriptor.** ``acquire_run_lock``
    closes the descriptor before returning, so a lock can be released by anything
    holding its path -- which is what lets the session poller release one it did not
    take. Keeping the descriptor open bought nothing (no advisory lock is taken on it)
    and cost two real things: on Windows an open handle blocks ``unlink``, so a leaked
    lock could not be deleted while the leaking process lived, and a long-lived server
    accumulated one handle for every run it had ever started. Both observed 2026-08-20;
    see task-190.
    """

    task_id: str
    path: Path
    run_id: str = ""

    def adopt(self, run_id: str) -> None:
        """Name the run this lock is held for, once there is a run to name.

        The lock is taken before the run exists -- that is the point of taking it, so
        two dispatches cannot both get as far as spawning. So the run id is written a
        moment later, and until it is, the lock is attributable only by pid. Every lock
        file on this machine before task-190 read ``run=`` empty for exactly that
        reason, which is why "is this lock's run over?" was not a question the code
        could ask.
        """
        self.run_id = run_id
        try:
            self.path.write_text(_holder_text(run_id), encoding="ascii")
        except OSError:  # pragma: no cover - the lock was cleared underneath us
            pass

    def release(self) -> None:
        """Delete the lock. Safe to call twice, and safe to call late.

        A lock file that has come to name a *different* run is left alone. That is not
        defensive padding: a run's terminal write and its lock release are two steps,
        and between them a second dispatch can legitimately reclaim the lock and take
        it for a new run. Releasing blind would delete the new run's lock and let a
        third dispatch in beside it.
        """
        holder = read_lock_holder(self.path)
        if holder is not None and self.run_id and holder.run_id and holder.run_id != self.run_id:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:  # pragma: no cover - already cleaned up
            pass
        except OSError:  # pragma: no cover - a peer is mid-unlink
            pass


def _holder_text(run_id: str) -> str:
    """What a lock file says about who holds it."""
    return f"pid={os.getpid()} run={run_id}"


def read_lock_holder(path: Path) -> Optional[LockHolder]:
    """The holder recorded in a lock file, or ``None`` when there is no such file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:  # pragma: no cover - unreadable but present
        return LockHolder()
    return LockHolder.parse(text)


def acquire_run_lock(
    home: Path, task_id: str, *, run_id: str = "", timeout: float = LOCK_TIMEOUT_SECONDS
) -> RunLock:
    """Take the run lock for one task, or raise saying who is holding it and why.

    ``O_CREAT|O_EXCL``, the same primitive task-055 chose and for the same reasons:
    exclusive create is atomic on every filesystem this runs on, it needs no dependency,
    and it behaves identically on Windows and Unix. A second locking convention would be
    a second set of stale-lock failure modes to learn.

    Windows reports contention two ways -- ``FileExistsError`` and, for a file whose
    delete has not finished, ``PermissionError``. Both are contention and both are
    retried; treating the second as fatal made a losing claimant crash with "Permission
    denied" instead of being told something else held it.

    **A lock whose holder can be shown not to be holding anything is reclaimed, once,
    before the timeout is consulted** (task-190). This does not weaken the argument for
    a primitive with no automatic release -- nothing releases on a timer, and there is
    still no lease to renew. It replaces the part of that argument that did not survive
    contact: "a named file tells you what to delete" was true only for someone who knew
    the directory existed, and false on Windows for as long as the leaking process
    lived, because the descriptor it never closed blocked the delete. See
    ``stale_lock_reason`` for what counts as evidence, and note the asymmetry -- being
    unable to tell refuses.
    """
    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.lock"
    deadline = time.monotonic() + timeout
    reclaimed = False
    while True:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            pass
        except OSError as exc:  # pragma: no cover - unexpected filesystem failure
            if exc.errno != errno.EEXIST:
                raise
        else:
            # Written and closed immediately. The claim is the file, not the handle.
            try:
                os.write(handle, _holder_text(run_id).encode("ascii"))
            finally:
                os.close(handle)
            return RunLock(task_id=task_id, path=path, run_id=run_id)

        holder = read_lock_holder(path)
        if holder is not None and not reclaimed:
            reason = stale_lock_reason(home, holder)
            if reason is not None:
                # Once. A lock that is retaken and judged stale a second time within one
                # acquisition means something is creating them faster than they can be
                # judged, and looping on that would be a busy-wait dressed as recovery.
                reclaimed = True
                RunLock(task_id=task_id, path=path, run_id=holder.run_id).release()
                continue

        if time.monotonic() >= deadline:
            raise RunLockTimeout(_lock_refusal(home, task_id, path, holder))
        time.sleep(LOCK_POLL_SECONDS)


def _lock_refusal(home: Path, task_id: str, path: Path, holder: Optional[LockHolder]) -> str:
    """What to tell whoever was refused, in terms they can act on.

    This message reaches a browser -- ``dispatch_task`` turns the exception into
    ``LiveRunExistsError`` and the task page renders it verbatim -- so it must not name
    a remedy only a shell can reach. It used to end "delete the file to clear it",
    which told a person the one thing they could neither find nor do (task-190):
    ``~/.agentjobs/runs/.locks/`` is undiscoverable from the app, and on Windows the
    delete failed anyway while the holding process lived.
    """
    if holder is None:  # pragma: no cover - the lock vanished between the two reads
        return (
            f"{task_id} could not take its run lock, and the lock was gone by the time "
            "the refusal was written. Try again."
        )
    if holder.run_id:
        return (
            f"{task_id} is held by run {holder.run_id}, which has not reported that it "
            "finished. Cancel that run and this task is dispatchable again; it appears "
            "under this task's runs."
        )
    return (
        f"{task_id} is held by a dispatch that started ({holder.describe()}) and has not "
        "yet said which run it became, so it cannot be shown to be over. If it is "
        "genuinely stuck, restarting AgentJobs clears locks whose runs have ended."
    )


@dataclass(frozen=True)
class StaleLock:
    """One lock reclaimed by the startup sweep, for reporting."""

    task_id: str
    run_id: str
    reason: str


def release_stale_locks(home: Path) -> List[StaleLock]:
    """Delete every lock whose holder can be shown not to be holding anything.

    The counterpart to reclaiming at acquisition, and it exists because acquisition is
    the wrong and only place to find out. A leaked lock is silent until somebody tries
    to dispatch that task, which may be days later and is certainly not when they can
    do anything about it. Sweeping at startup means a restart -- the event this project
    prescribes after every merge, and the event that used to *cause* the leak -- is
    what heals it.

    Same evidence as ``stale_lock_reason``, so there is no second rule to drift.
    """
    directory = locks_root(home)
    if not directory.is_dir():
        return []
    released: List[StaleLock] = []
    for path in sorted(directory.glob("*.lock")):
        holder = read_lock_holder(path)
        if holder is None:
            continue
        reason = stale_lock_reason(home, holder)
        if reason is None:
            continue
        RunLock(task_id=path.stem, path=path, run_id=holder.run_id).release()
        released.append(StaleLock(task_id=path.stem, run_id=holder.run_id, reason=reason))
    return released


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
    finished_at: Optional[datetime] = None
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
        """How long this run has been going, or how long it ran for.

        A concluded run is measured to its finish time, never to the current clock: its
        duration is a fact about the past and must read the same however long afterwards
        you look at it.

        A concluded run with no recorded finish time reports ``None`` -- "unknown" in the
        web UI, ``-`` in the CLI. Those are runs that ended before finish times were
        recorded, or ended in a way nothing wrote down. Measuring them from now would be
        the bug this method exists to fix, and inventing a plausible number would be
        worse than admitting the record does not say.
        """
        if self.started_at is None:
            return None
        if self.is_live:
            moment = now or datetime.now(timezone.utc)
            return (moment - self.started_at).total_seconds()
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


def _as_moment(value: object) -> Optional[datetime]:
    """Read a timestamp out of run metadata, naive values read as UTC.

    An unparseable timestamp is absent rather than an error: run metadata is a file on
    disk that a person may have edited, and one bad field should not make the run
    unreadable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
        started_at=_as_moment(meta.get("started_at")),
        finished_at=_as_moment(meta.get("finished_at")),
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
    meta_path.write_text(
        yaml.safe_dump(finish_stamped(meta, fields), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
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
        managers: Optional[Dict[str, TaskManager]] = None,
    ) -> None:
        self.home = Path(home)
        self.registry = registry or ProjectRegistry(home=self.home)
        #: Managers supplied by a caller that has already resolved the project, keyed by
        #: project id. Consulted before the registry, because the registry is not the
        #: only way a project exists: a server started with AGENTJOBS_PROJECT_ROOT serves
        #: an implicit project the registry has never heard of. Resolving through the
        #: registry alone meant a cancellation there had nowhere to write its result, so
        #: the run ended and the task record never learnt of it -- exactly the silence
        #: this subsystem is built to prevent. Found by the browser path, 2026-08-18.
        self.managers = dict(managers or {})
        #: How to reach the session manager. Overridable so tests can stand in a fake,
        #: and so a runner that is not Claude Code can be driven by the same code.
        self.session_command = session_command or ["claude"]

    # ----- resolution --------------------------------------------------------

    def manager_for(self, record: RunRecord) -> Optional[TaskManager]:
        """The TaskManager owning this run's task, or None when it cannot be resolved."""
        supplied = self.managers.get(record.project_id)
        if supplied is not None:
            return supplied
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
        """Ask a run to stop. Session mode delegates; batch mode signals.

        The flag goes down **before** anything is killed, and that order is the whole
        point. A batch run's supervisor is blocked in ``wait()`` until the kill, so it
        cannot reach its own terminal write before the flag exists -- and when it wakes
        to a non-zero exit it defers instead of overwriting this cancellation with
        ``failed``. Writing the flag afterwards would leave exactly the race it removes.
        """
        write_status(record, cancel_requested=True)
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

        # After the runs, never before: concluding an orphaned run is what turns its
        # status terminal, and a lock is judged against that status. Sweeping first
        # would find every one of those locks still held by a run reading `running` and
        # leave the whole set behind -- which is the leak, one restart later.
        for stale in release_stale_locks(self.home):
            results.append(
                StopResult(
                    stale.run_id or f"{stale.task_id} lock",
                    True,
                    f"released the run lock on {stale.task_id}: {stale.reason}",
                )
            )
        return results

    # ----- reaping -----------------------------------------------------------

    def reap(self, record: RunRecord) -> StopResult:
        """Remove a finished session's job state, freeing the pid it still holds.

        **That is now the whole job, and the narrowing is deliberate (task-186).** While
        dispatch passed ``-w``, a session owned a worktree, ``claude rm`` deleted it, and
        its *refusal* to delete one holding uncommitted changes was the useful half: it
        meant a run had produced work nobody had looked at. Dispatch cannot pass ``-w``
        any more -- see ``posture_flags`` -- so a dispatched session owns no worktree and
        there is nothing here for that refusal to fire on. Verified rather than assumed:
        ``claude rm`` on a worktree-less background session exits 0 and prints
        ``removed <id>``, so this is a narrowing, not a silent no-op. Freeing the pid a
        finished session still holds in the manager's ledger is real work and remains
        worth doing on its own.

        What is no longer covered: the worktree a dispatched agent makes for itself
        (``../aj-<nnn>``, per ALLAGENTS.md) is outside AgentJobs' knowledge entirely.
        Removing it is the agent's own closing step and ``git worktree list`` is the
        inventory. AgentJobs deliberately does not go looking for directories it did not
        create in order to delete them.

        A refusal is still surfaced and never forced -- ``-f`` here would delete exactly
        the thing worth keeping -- because a session this did not start can still own a
        worktree, and because a refusal can also be a transient Windows file handle
        (observed 2026-08-19; the retry seconds later succeeded).
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
        finished = datetime.now(timezone.utc)
        write_status(
            record,
            status="cancelled",
            outcome=outcome.value,
            finished_at=finished.isoformat(),
        )
        manager = self.manager_for(record)
        if manager is None or not record.task_id:
            write_status(record, unattributed=f"no task record for {record.task_id!r}")
            return
        task = manager.get_task(record.task_id)
        if task is None:
            write_status(record, unattributed=f"task {record.task_id!r} not found")
            return

        # From the instant just written to the run, not from the record in hand: that
        # record was read before the status changed and still describes a live run.
        duration = (
            (finished - record.started_at).total_seconds()
            if record.started_at is not None
            else None
        )
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
