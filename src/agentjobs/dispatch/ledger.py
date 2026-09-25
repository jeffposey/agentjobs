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
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from agentjobs.dispatch import program
from agentjobs import clock as dispatch_clock
from agentjobs.dispatch import proctree
from agentjobs.dispatch.config import write_sentinel
from agentjobs.dispatch.record_commit import commit_task_record
from agentjobs.dispatch.credentials import revoke_run_credential
from agentjobs.dispatch.runner import (
    META_FILENAME,
    TERMINAL_STATUSES,
    finish_stamped,
    resolve_executable,
    runs_root,
)
from agentjobs.dispatch.atomic_yaml import merge_yaml_atomically
from agentjobs.dispatch.phases import RUN_ID_ENV
from agentjobs.dispatch.pids import (  # noqa: F401 - re-exported; see the note below
    process_alive,
    process_identity,
)
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.execution.store import Attempt
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchOutcome,
    legacy_allow_automerge,
    LogEntryType,
    merge_mode_text,
)
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.taskfiles import load_yaml
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

LOCKS_DIRNAME = ".locks"
"""Run locks live under the runs root. A leading dot cannot collide with a run id."""

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.01

RUNWAY_PREFIX = "runway-"
"""Reserved lock-name prefix for the repo-scoped finish runway (task-223).

Runway locks share the directory with per-task locks because they are the same
primitive with the same stale-lock rules, and a second locks directory would be a
second set of failure modes to learn. They cannot collide with a task's lock: a task id
that began ``runway-`` would have to be a task literally named for this, and the
prefix is reserved here so nobody creates one by accident."""

RUNWAY_TIMEOUT_SECONDS = 3600.0
"""How long a finish waits for the runway before giving up.

Ten seconds is right for the per-task lock, whose contention means *somebody else is
already doing this task* and is therefore a refusal. Runway contention means the
opposite -- the queue is working -- so waiting is the correct behaviour and the bound
has to cover a real queue: three children each rebasing, gating and merging at the
measured ~4 minutes of a scripted finish, plus the outlier gate under three-way
contention that ENGINEERING.md records at about six. An hour is well clear of that and
still finite, because a wait with no bound is a hang."""

KIND_DISPATCH = "dispatch"
KIND_FINISH = "finish"
KIND_RUNWAY = "runway"
"""What kind of thing holds a lock in this directory.

Two of them take a *task's* lock and they are opposites, which is why the lock has to say
which (task-298). A **dispatch** starts a session that outlives the process that started it,
so its pid is expected to die while the work goes on and the run record is the only
authority on whether it is over. A **finish** is the process: it holds the lock for one
attempt, in the foreground of its own detached invocation, and it never becomes a run --
it has a finish id, not a run id.

Before this, the refusal a human read was written for the first case and shown for both.
It called a healthy finish "a dispatch that ... has not yet said which run it became",
which is a permanent truth about every finish, described as an anomaly -- and then
offered a server restart, which mid-merge is the most harmful thing available. Observed
twice on 2026-08-23 and read, reasonably, as the product being broken.

An empty kind means a lock file written before this existed; it is treated as a
dispatch, which is what every such file was.

A **runway** is the third and is not keyed on a task at all: one per repository, held by
whichever finish is currently rebasing, gating and merging there (task-223). It shares
this directory and these stale-lock rules because it is the same primitive; it behaves
like a finish in every respect except its key, which is why ``stale_lock_reason`` reaches
the same conclusion about both from the pid."""


class RunLockTimeout(Exception):
    """Another run holds this task's lock, or a dead one left it behind."""


class LedgerError(Exception):
    """A ledger operation could not be completed."""


# ----- the per-task run lock --------------------------------------------------


def locks_root(home: Path) -> Path:
    """Directory holding one lock file per task with a live run."""
    return runs_root(home) / LOCKS_DIRNAME


PROJECT_SEPARATOR = "~"
"""Joins a project id to a task id in a lock name. Neither id may contain it: both are
validated slugs of ``[a-z0-9._-]``, so a lock name splits back into its two halves
unambiguously."""


def lock_name(project_id: Optional[str], task_id: str) -> str:
    """The lock name for one project's task, or the bare name for an unscoped lock.

    **The project is part of the key** (task-264, P2-5). Until then the name was the task
    id alone, and task ids are per-project -- so ``task-001`` in two projects shared one
    lock, and the second project's dispatch was refused for a run it did not have.
    ``None`` is for the one lock that is not a task's: the repository runway, whose name
    is already a digest of a path.
    """
    if project_id is None:
        return task_id
    return f"{project_id}{PROJECT_SEPARATOR}{task_id}"


def split_lock_name(name: str) -> Tuple[str, str]:
    """``(project_id, task_id)`` from a lock name; project is ``""`` for a legacy name."""
    project, separator, task = name.partition(PROJECT_SEPARATOR)
    if not separator:
        return "", name
    return project, task


def run_lock_path(home: Path, task_id: str, *, project_id: Optional[str]) -> Path:
    """The lock file for one project's task's run.

    Named rather than spelled out at each caller because five places had built this
    path from the same two pieces, and a reader of any one of them had no way to know
    the convention was shared. ``project_id`` is required by keyword so a caller cannot
    fall back to the unscoped name by forgetting it.
    """
    return locks_root(home) / f"{lock_name(project_id, task_id)}.lock"


def legacy_run_lock_path(home: Path, task_id: str) -> Path:
    """Where a build before task-264 put this task's lock. Read, never created."""
    return locks_root(home) / f"{task_id}.lock"


def read_task_lock_holder(home: Path, task_id: str, *, project_id: str) -> Optional["LockHolder"]:
    """Who holds this project's task lock: the scoped file, else a pre-task-264 one.

    The fallback is what keeps a run started by a server that has not restarted since
    the upgrade recognisable as the holder of its own task.
    """
    holder = read_lock_holder(run_lock_path(home, task_id, project_id=project_id))
    if holder is not None:
        return holder
    return read_lock_holder(legacy_run_lock_path(home, task_id))


@dataclass(frozen=True)
class LockHolder:
    """Who a lock file says is holding it, as far as the file can be read.

    Every field is optional because every one of them can be missing from a file written
    by an older version, and a lock nobody can attribute must still be *describable* --
    the refusal a human reads is built from this.

    ``kind`` is what stops the refusal guessing. The message must never claim more than
    the lock knows, so the holder writes down what it is at the moment it takes the lock
    rather than leaving a reader to infer it from a pid's command line.
    """

    pid: Optional[int] = None
    run_id: str = ""
    kind: str = ""
    finish_id: str = ""
    started_at: str = ""
    text: str = ""

    @classmethod
    def parse(cls, text: str) -> "LockHolder":
        """Read ``pid=<n> run=<id> kind=<k> finish=<id> started=<iso>``, tolerating rest.

        Whitespace-separated ``key=value``, so every value written here must be free of
        spaces -- which an ISO-8601 timestamp is.
        """
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
        return cls(
            pid=pid,
            run_id=fields.get("run", ""),
            kind=fields.get("kind", ""),
            finish_id=fields.get("finish", ""),
            started_at=fields.get("started", ""),
            text=text.strip(),
        )

    @property
    def is_runway(self) -> bool:
        """Whether this lock is the repo-scoped finish runway (task-223)."""
        return self.kind == KIND_RUNWAY

    @property
    def is_finish(self) -> bool:
        """Whether a scripted finish holds this, rather than a dispatch.

        Only an explicit ``kind=finish`` counts. A lock file that predates the field is
        a dispatch, because that is the only thing that wrote one.
        """
        return self.kind == KIND_FINISH

    def describe(self) -> str:
        """The holder as a sentence fragment, for a message a human reads."""
        parts = []
        if self.run_id:
            parts.append(f"run {self.run_id}")
        if self.finish_id:
            parts.append(self.finish_id)
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        return ", ".join(parts) or (self.text or "nothing recorded")

    def since_phrase(self) -> str:
        """ " started at 13:38:02, 3m ago", or "" when the lock recorded no clock.

        Leading space, so it appends to a sentence fragment or vanishes. The elapsed
        half is the part that answers the question actually being asked -- four seconds
        in and four hours in are different situations, and until now they read alike.
        """
        if not self.started_at:
            return ""
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:
            return ""
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        clock = started.astimezone().strftime("%H:%M:%S")
        elapsed = (dispatch_clock.utcnow() - started).total_seconds()
        return f" started at {clock}, {_elapsed_phrase(elapsed)} ago"


def _elapsed_phrase(seconds: float) -> str:
    """``4s``, ``3m``, ``1h 12m`` -- enough to tell "just now" from "stuck"."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


# ``process_alive`` and ``process_identity`` moved to ``dispatch.pids`` with task-505,
# which needed the same creation-time read that ``execution.store`` had grown its own
# copy of. Re-exported here because every caller in the subsystem imports them from
# this module and a pid rule wants one implementation, not three halves of one.


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
    2. **With no run record to consult, the pid decides.** Three ways to get here: the
       narrow window between taking the lock and the run existing to be named, a lock
       naming a run whose directory is no longer on disk, and a **scripted finish**,
       which never names a run at all. Falling back rather than refusing is deliberate
       -- a run with no directory cannot be followed, concluded or cancelled by
       anything, so refusing on it would be the permanent silent block this whole change
       exists to remove, merely rarer.

       For a finish the fallback is not a fallback but the right rule outright: the
       recorded pid *is* the finish, running in the foreground of its own process, so
       its absence is proof the attempt is over rather than an inference about one.

    A live pid, or a file too mangled to read a pid out of, is left alone. "I cannot
    tell" refuses, which is the direction that cannot lose work.

    Note what is deliberately absent: no clock. Nothing here expires, so there is no
    lease to renew and no window in which a slow run loses a lock it is still using.
    A lock is cleared only against a positive statement on disk -- a terminal run, or
    an absent process -- never against elapsed time.
    """
    if holder.run_id:
        # The journal first, when it knows the run (task-264). A run directory's meta is
        # writable by the agent working inside it, so a run that wrote `status: finished`
        # into its own meta would otherwise release its own lock while it kept working --
        # auditor 12's question, and the answer is now no.
        from agentjobs.dispatch.journal import journal_liveness  # local: journal imports runner

        liveness = journal_liveness(home, holder.run_id)
        if liveness is True:
            return None
        if liveness is False:
            return f"its run {holder.run_id} has concluded"
        directory = runs_root(home) / holder.run_id
        if directory.is_dir():
            record = read_run(directory)
            if record.is_live:
                return None
            return f"its run {holder.run_id} is {record.outcome or record.status}"
    if holder.pid is not None and not process_alive(holder.pid):
        named = f" {holder.finish_id}" if holder.finish_id else ""
        if holder.is_runway:
            return (
                f"the finish{named} holding the runway (pid {holder.pid}) is gone, so "
                "nothing is merging in that repository"
            )
        if holder.is_finish:
            return f"the scripted finish{named} that took it (pid {holder.pid}) is gone"
        missing = (
            f"and there is no record of run {holder.run_id}"
            if holder.run_id
            else "and it named no run"
        )
        return f"the process that took it (pid {holder.pid}) is gone, {missing}"
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
    kind: str = KIND_DISPATCH
    finish_id: str = ""
    started_at: str = ""
    project_id: str = ""

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
        self._rewrite()

    def adopt_finish(self, finish_id: str) -> None:
        """Name the attempt this lock is held for, once there is a finish id to name.

        The finish's counterpart to ``adopt``, and it exists for the same reason: the
        lock is taken *before* the finish directory is created, because taking it is
        what decides whether this attempt happens at all. Until this is called the
        holder is attributable only as "a scripted finish", which is already the fact
        the old refusal got wrong.
        """
        self.finish_id = finish_id
        self._rewrite()

    def _rewrite(self) -> None:
        try:
            self.path.write_text(
                _holder_text(
                    self.run_id,
                    kind=self.kind,
                    finish_id=self.finish_id,
                    started_at=self.started_at,
                ),
                encoding="ascii",
            )
        except OSError:  # pragma: no cover - the lock was cleared underneath us
            pass

    def held_by_another(self, holder: Optional["LockHolder"]) -> bool:
        """Whether the lock file now names somebody other than this handle.

        **The rule is the holder's, not the releaser's**, and that asymmetry is the whole
        of task-514. A run's terminal write and its lock release are two steps, and
        between them the lock can legitimately have been reclaimed -- by a second
        dispatch, or, the case that cost an hour, by the *scripted finish* the approval
        started. A finish's lock names a finish and no run at all, so the old test --
        "both sides name a run and the names differ" -- was never satisfied by it: the
        poller settling ``run_c39ec860`` deleted ``fin_9323da60``'s lock, and the
        duplicate spawned two seconds later walked in through a door nothing was holding.

        So each identifying field the *holder* carries has to match ours, and a field it
        carries that we do not is a mismatch rather than a licence. The direction that
        errs leaves a lock behind, which ``stale_lock_reason`` reclaims; the direction
        that errs the other way puts two finishes on one task.
        """
        if holder is None:
            return False
        if holder.run_id and holder.run_id != self.run_id:
            return True
        if holder.finish_id and holder.finish_id != self.finish_id:
            return True
        return False

    def release(self) -> None:
        """Delete the lock. Safe to call twice, and safe to call late.

        A lock file that has come to name a *different* holder is left alone -- see
        :meth:`held_by_another`. Releasing blind would delete the new holder's lock and
        let a third claimant in beside it.
        """
        if self.held_by_another(read_lock_holder(self.path)):
            return
        try:
            self.path.unlink()
        except FileNotFoundError:  # pragma: no cover - already cleaned up
            pass
        except OSError:  # pragma: no cover - a peer is mid-unlink
            pass


def _holder_text(
    run_id: str, *, kind: str = KIND_DISPATCH, finish_id: str = "", started_at: str = ""
) -> str:
    """What a lock file says about who holds it.

    Whitespace-separated ``key=value`` pairs, read back by ``LockHolder.parse``. Fields
    are appended rather than reordered: an older reader takes what it recognises and
    ignores the rest, which is what makes a lock written by a newer process readable by
    a server that has not restarted yet.
    """
    parts = [f"pid={os.getpid()}", f"run={run_id}", f"kind={kind or KIND_DISPATCH}"]
    if finish_id:
        parts.append(f"finish={finish_id}")
    if started_at:
        parts.append(f"started={started_at}")
    return " ".join(parts)


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
    home: Path,
    task_id: str,
    *,
    project_id: Optional[str],
    run_id: str = "",
    kind: str = KIND_DISPATCH,
    timeout: float = LOCK_TIMEOUT_SECONDS,
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
    locks_root(home).mkdir(parents=True, exist_ok=True)
    path = run_lock_path(home, task_id, project_id=project_id)
    legacy = legacy_run_lock_path(home, task_id) if project_id is not None else None
    deadline = dispatch_clock.monotonic() + timeout
    started_at = dispatch_clock.utcnow().isoformat(timespec="seconds")
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
                text = _holder_text(run_id, kind=kind, started_at=started_at)
                os.write(handle, text.encode("ascii"))
            finally:
                os.close(handle)
            acquired = RunLock(
                task_id=task_id,
                path=path,
                run_id=run_id,
                kind=kind,
                started_at=started_at,
                project_id=project_id or "",
            )
            blocker = _live_legacy_holder(home, legacy, project_id)
            if blocker is None:
                return acquired
            # A server that has not restarted since task-264 still writes the unscoped
            # name. Its lock is honoured, so an upgrade under a live run cannot put a
            # second run beside it; ours is given back and this counts as contention.
            acquired.release()
            if dispatch_clock.monotonic() >= deadline:
                raise RunLockTimeout(_lock_refusal(home, task_id, legacy or path, blocker))
            dispatch_clock.sleep(LOCK_POLL_SECONDS)
            continue

        holder = read_lock_holder(path)
        if holder is not None and not reclaimed:
            reason = stale_lock_reason(home, holder)
            if reason is not None:
                # Once. A lock that is retaken and judged stale a second time within one
                # acquisition means something is creating them faster than they can be
                # judged, and looping on that would be a busy-wait dressed as recovery.
                reclaimed = True
                # Both identities, because since task-514 a release only deletes a lock
                # whose holder it can name. A finish's lock carries a finish id and no
                # run id, so reclaiming one on the run id alone would now be refused.
                RunLock(
                    task_id=task_id,
                    path=path,
                    run_id=holder.run_id,
                    finish_id=holder.finish_id,
                ).release()
                continue

        if dispatch_clock.monotonic() >= deadline:
            raise RunLockTimeout(_lock_refusal(home, task_id, path, holder))
        dispatch_clock.sleep(LOCK_POLL_SECONDS)


# ----- the repo-scoped finish runway (task-223) --------------------------------


def runway_lock_name(root: Path) -> str:
    """The lock name for one repository's finish runway.

    Keyed on the **resolved checkout path**, not on the project id, because the resource
    being protected is a git repository: two registered projects pointing at one clone
    share a ``main`` and must share a runway, and one project reachable under two spellings
    of its path must not get two. ``os.path.normcase`` folds the case and the separators,
    which is what makes ``C:/projects/agentjobs`` and ``C:\\Projects\\AgentJobs`` the same
    runway on Windows.

    Hashed rather than sanitised so the name has a fixed length and no path separators in
    it; the digest is not a secret and truncation to 12 hex characters is ample for the
    handful of checkouts one machine has.
    """
    key = os.path.normcase(str(Path(root).resolve()))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{RUNWAY_PREFIX}{digest}"


def _live_legacy_holder(
    home: Path, legacy: Optional[Path], project_id: Optional[str]
) -> Optional[LockHolder]:
    """The holder of a pre-task-264 unscoped lock for this task, if it binds this project.

    It binds when it is held (not provably stale) and its run is this project's, or its
    run cannot be attributed to any project -- the direction that refuses.
    """
    if legacy is None or not legacy.exists():
        return None
    holder = read_lock_holder(legacy)
    if holder is None or stale_lock_reason(home, holder) is not None:
        return None
    if holder.run_id:
        directory = runs_root(home) / holder.run_id
        if directory.is_dir():
            owner = read_run(directory).project_id
            if owner and project_id and owner != project_id:
                return None
    return holder


def acquire_runway_lock(
    home: Path,
    root: Path,
    *,
    finish_id: str = "",
    timeout: float = RUNWAY_TIMEOUT_SECONDS,
    poll: float = 1.0,
    on_wait: Optional[Callable[[LockHolder], None]] = None,
    on_poll: Optional[Callable[[], None]] = None,
) -> RunLock:
    """Take the one runway this repository has, waiting for it rather than refusing.

    **The runway is the sequential half of a parallel epic** (task-223). Children fly in
    parallel and land one at a time, and the reason is not that git cannot merge two
    branches -- it can, and it says so loudly when it cannot. The reason is that a
    finish's guarantee is *the commit that landed is the commit the gate verified*, and
    that guarantee is only true if nothing moves the base between the gate and the merge.
    Held across rebase, gate and merge, this makes that true by construction. Without it,
    N concurrent finishers all pass their gates against a base the others are moving, and
    ``merge``'s ``base_moved`` check -- written for a rare race -- becomes the normal
    outcome, costing a dispatched run each time.

    **Taken at merge time only was the rejected alternative**, and it is cheaper: the
    runway would then be seconds rather than the whole gate. It was rejected because it
    needs an answer for "the base moved while I was gating", and the only correct answer
    is to rebase and re-gate -- which spends the gate twice and, under real contention,
    can spend it repeatedly. Holding the runway across the gate spends it once, and the
    honest cost is stated rather than hidden: with a four-minute finish and four
    children, roughly sixteen minutes of the epic is runway. The flights are what
    parallelise, and they are five-sixths of a run.

    **Waiting rather than refusing** is the other half of the difference from
    :func:`acquire_run_lock`. Contention on a task lock means somebody else is already
    doing this task, which is an error. Contention here means the queue is working.
    ``on_wait`` is called at most once, with the holder found the first time the runway
    was busy, so a caller can say on its own record that it is queued rather than hung.

    ``on_poll`` is called once per wait, and **may raise to abandon the queue** (task-514).
    The one-at-a-time rule is untouched by it: what it adds is the question nothing asked
    for an hour, which is whether the thing this finish is queued to do still needs doing.
    Waiting out the timeout to be told the branch was merged and the worktree removed is
    the worst available outcome, and it costs the *next* finish in line its place.
    """
    deadline = dispatch_clock.monotonic() + timeout
    announced = False
    while True:
        try:
            lock = acquire_run_lock(
                home,
                runway_lock_name(root),
                project_id=None,
                kind=KIND_RUNWAY,
                timeout=0.0,
            )
        except RunLockTimeout:
            pass
        else:
            if finish_id:
                lock.adopt_finish(finish_id)
            return lock

        if not announced and on_wait is not None:
            holder = read_lock_holder(locks_root(home) / f"{runway_lock_name(root)}.lock")
            announced = True
            if holder is not None:
                on_wait(holder)

        if on_poll is not None:
            on_poll()

        if dispatch_clock.monotonic() >= deadline:
            holder = read_lock_holder(locks_root(home) / f"{runway_lock_name(root)}.lock")
            described = holder.describe() if holder is not None else "an unreadable lock"
            raise RunLockTimeout(
                f"Waited {timeout / 60:.0f} minutes for the finish runway on {root} and "
                f"it is still held by {described}. One repository merges one branch at a "
                "time on purpose, so this is a queue rather than a fault -- but an hour "
                "of it means the holder is not progressing. Look at what is merging "
                "there; restarting AgentJobs clears a runway whose holder has ended."
            )
        dispatch_clock.sleep(poll)


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
    if holder.is_finish:
        return _finish_refusal(task_id, holder)
    if holder.run_id:
        return (
            f"{task_id} is held by run {holder.run_id}, which has not reported that it "
            "finished. Cancel that run and this task is dispatchable again; it appears "
            "under this task's runs. If it is genuinely stuck, restarting AgentJobs "
            "settles runs that were live when it last stopped and clears their locks."
        )
    if holder.pid is not None and process_alive(holder.pid):
        return (
            f"{task_id} is held by a dispatch that started ({holder.describe()}) and has "
            "not named its run yet -- that window is a moment wide, so try again shortly, "
            "and the run will be cancellable under this task once it appears."
        )
    return (
        f"{task_id} is held by a dispatch ({holder.describe()}) that cannot be shown to "
        "be over. If it is genuinely stuck, restarting AgentJobs clears locks whose runs "
        "have ended."
    )


def _finish_refusal(task_id: str, holder: LockHolder) -> str:
    """What to tell someone who clicked Dispatch while a scripted finish holds the task.

    Three things this has to do that the old single message could not (task-298).

    **Name the holder as what it is.** A finish is not a dispatch and never becomes a
    run, so "has not yet said which run it became" described a permanent, healthy state
    as an anomaly, for every finish, forever.

    **Say what it means for the reader**, which is that they do not want to dispatch:
    the merge they were about to ask for is already running. Turning on ``finish.enabled``
    quietly changes the workflow from approve-then-dispatch to approve-and-you-are-done,
    and this refusal is where most people will meet that change.

    **Withhold the restart remedy while the finish is alive.** It is sound advice for a
    lock outliving a dead run and it is the single most harmful thing on offer to
    somebody three minutes into a gate. It stays for the case it was written for, above
    and below, and it is not mentioned here at all -- naming it even to warn against it
    puts the idea in front of a reader who is already looking for a way out.
    """
    named = f" ({holder.finish_id})" if holder.finish_id else ""
    when = holder.since_phrase()
    # A finish holds its lock in the foreground of its own process, so an absent pid is
    # proof the attempt is over -- and an unreadable one is the "cannot tell" case, which
    # refuses without claiming the merge is under way.
    if holder.pid is None:
        return (
            f"{task_id} is held by a scripted finish{named}{when} which has not reported "
            "that it is over, and the lock does not say which process took it. Approving "
            "a task on this machine runs the merge itself, so there is nothing to "
            "dispatch: wait for the task to close or for the ball to come back."
        )
    if process_alive(holder.pid):
        return (
            f"{task_id} is held by a scripted finish{named}{when}, which is merging it "
            "now. Approving a task on this machine runs the merge itself -- rebase, the "
            "full gate, a --no-ff merge, and the rebuild that puts it in front of you -- "
            "so there is nothing to dispatch. When it lands the task closes; if it stops, "
            "the ball comes back with the step it stopped at written on the record."
        )
    return (
        f"{task_id} is held by a scripted finish{named}{when} whose process "
        f"(pid {holder.pid}) is gone, so nothing is merging. Restarting AgentJobs clears "
        "locks whose holders have ended."
    )


@dataclass(frozen=True)
class StaleLock:
    """One lock reclaimed by the startup sweep, for reporting."""

    task_id: str
    run_id: str
    reason: str
    project_id: str = ""


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
        project_id, task_id = split_lock_name(path.stem)
        RunLock(
            task_id=task_id,
            path=path,
            run_id=holder.run_id,
            finish_id=holder.finish_id,
            project_id=project_id,
        ).release()
        released.append(
            StaleLock(task_id=task_id, run_id=holder.run_id, reason=reason, project_id=project_id)
        )
    return released


def live_lock_holders(home: Path) -> List[Tuple[str, LockHolder]]:
    """Every lock on this machine that is still being held, as ``(lock name, holder)``.

    The read-only counterpart to ``release_stale_locks``: same evidence, same
    ``stale_lock_reason`` filter, nothing deleted. It exists because a run is not the
    only thing occupying this machine -- a scripted finish (task-241) and the merge
    runway (task-223) both take locks, are both real work a human wants to see, and
    neither has a run record to be found through ``live_runs``.

    The lock **name** rather than a task id, because it is not always one: a runway lock
    is named after a hash of the repository path (``runway_lock_name``). Callers that
    want a task read ``holder.is_runway`` first.

    Sorted so the answer is stable between two reads a second apart, which is what stops
    a polling surface reordering itself under the reader's eyes.
    """
    directory = locks_root(home)
    if not directory.is_dir():
        return []
    held: List[Tuple[str, LockHolder]] = []
    for path in sorted(directory.glob("*.lock")):
        holder = read_lock_holder(path)
        if holder is None:
            continue
        if stale_lock_reason(home, holder) is not None:
            continue
        held.append((path.stem, holder))
    return held


def _read_text(path: Path) -> str:
    """Best-effort read, for putting a lock's holder into an error message."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


#: How the session manager says the job this run named does not exist. Matching on the
#: message is the only signal there is -- ``claude rm`` exits 1 both for "no such job"
#: and for "I refuse to delete this one", and the second is the refusal worth keeping.
#: So the match is deliberately narrow and default-deny: anything else stays
#: ``reap_blocked`` and is retried, because the cost of retrying a transient refusal is
#: one process spawn and the cost of settling a real one is a lost conversation.
#: Observed verbatim on 2026-09-20: ``No job matching 'task-503-nonexistent-xyz'``.
_SESSION_ABSENT = re.compile(r"\bno job matching\b", re.IGNORECASE)


def _session_is_already_gone(output: str) -> bool:
    """Does this refusal mean the session no longer exists anywhere?

    True only for the one refusal that can never become a success. A session the
    manager has no record of will not reappear, so a reap aimed at it is settled --
    while a refusal that means "this session still owns something worth keeping", or a
    transient Windows file handle (observed 2026-08-19; the retry seconds later
    succeeded), is neither settled nor quiet.
    """
    return bool(_SESSION_ABSENT.search(output))


def _reap_is_settled(record: "RunRecord") -> bool:
    """Is this run's session state finished with, either removed or already absent?

    Substring rather than a YAML parse, because ``reap_finished`` asks this of every run
    directory the machine has ever had and parsing each one to read a boolean is the
    cost this function exists to avoid. ``write_status`` writes both keys through
    ``merge_yaml_atomically``, so the forms below are the only ones either key takes.

    ``reaped`` is deliberately *not* set by the absent case: ``wake._was_reaped`` reads
    it to mean the conversation was deleted, and a job the manager has forgotten is not
    the same claim.
    """
    meta_path = record.path / META_FILENAME
    if not meta_path.is_file():
        return False
    meta = _read_text(meta_path)
    return "reaped: true" in meta or "reap_settled: true" in meta


# ----- the ledger itself ------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """One run, as the ledger knows it."""

    run_id: str
    path: Path
    task_id: str = ""
    project_id: str = ""
    mode: str = ""
    agent: str = ""
    """The actor id this run was dispatched as, from the runner's ``actor_id``.

    Written into every ``meta.yaml`` since dispatch had a runner, and read back since
    task-332, which needs it to tell a run claiming to be *itself* from one claiming to
    be another agent. Empty on a meta written by hand or by a version that predates the
    field, and every reader treats empty as "unknown" rather than as a claim.
    """

    merge_mode: str = ""
    merge_mode_source: str = ""
    allow_automerge: Optional[bool] = None
    merge_mode_requested: str = ""
    """How this run's merge mode was arrived at, as ``ResolvedMergeMode.as_data`` wrote it.

    A meta written before task-602 names these ``posture*`` and stores the retired
    spellings; they are read through the legacy map, so ``merge_mode`` here is always a
    current value or empty.

    The dispatcher has recorded these since task-308 and nothing read them back until
    task-315, when the finisher needed to know what *this run* was authorised to do
    rather than what the project defaults to. Empty on a run started before that, and on
    anything that wrote a meta by hand -- which is why every reader of them treats an
    unparseable value as absent rather than as a claim.
    """

    status: str = "unknown"
    outcome: Optional[str] = None
    session_id: Optional[str] = None
    pid: Optional[int] = None
    pid_identity: str = ""
    """What :func:`agentjobs.dispatch.pids.process_identity` said about :attr:`pid` at spawn.

    The receipt that tells this run's worker from whoever inherits its number, which on
    this machine happens within seconds (task-505). Written for every batch run since
    task-416 and empty on a session, on a hand-written meta, and on anything older --
    empty means "cannot prove", never "it is".
    """
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    caused_by: Optional[int] = None
    argv: List[str] = field(default_factory=list)
    cwd: str = ""
    """Where the session runs, for an interactive run: the directory whose transcript
    store holds its JSONL. Empty on every other kind of run, which start in the
    project root."""
    origin: str = ""
    """``claimed`` or ``registered`` for a run AgentJobs did not start. Empty for a
    dispatched one."""
    over_ceiling: bool = False
    """Started above ``limits.max_concurrent_runs`` because a person chose to (task-461).

    Absent from every meta written before that and from every ordinary run since, and a
    missing key reads as ``False`` -- which is the only thing it can honestly mean."""
    handback_pending: Optional[int] = None
    """The log entry id of a human handback this run was told about but not given.

    Written by ``dispatch/handback.py`` when a click lands while this run is still going,
    which is the normal case rather than an edge one: the notification that brought the
    human to the page was this run's own handoff. Until task-384 that state was invisible
    -- the dashboard showed *Revising (claude)* beside a live run, and the only reasonable
    reading of that pair is that work is happening, which is why people waited.

    Never cleared. A run carrying one is settled before the handback is delivered, and a
    settled run's health is not read; leaving it is what keeps the ledger a record of what
    happened rather than of what is true this second.
    """

    slot_released_at: Optional[datetime] = None
    """When this run gave its machine slot back while still alive (task-482)."""
    slot_released_reason: str = ""
    """Why it did. ``task_closed`` is the only reason today."""

    @property
    def slot_released(self) -> bool:
        """Whether this run has given its slot back and is still going."""
        return self.slot_released_at is not None

    @property
    def is_live(self) -> bool:
        """True while nothing has declared this run over."""
        return self.status not in TERMINAL_STATUSES

    @property
    def is_session(self) -> bool:
        return self.mode == DispatchMode.SESSION.value

    @property
    def is_interactive(self) -> bool:
        """A session a person is sitting in (task-354). Followed by nothing; holds no slot."""
        return self.mode == DispatchMode.INTERACTIVE.value

    @property
    def is_walk(self) -> bool:
        """A dispatch that handed an epic's children to the server (task-458).

        It has no worker and is concluded in the call that created it, so one that is
        still readable as live is a conclusion that did not commit -- which must not be
        allowed to hold a slot no process is using.
        """
        return self.mode == DispatchMode.WALK.value

    @property
    def takes_slot(self) -> bool:
        """Whether this run counts against ``limits.max_concurrent_runs``.

        The third exemption is task-482's and is the only one that can arrive *after* the
        run started: a session whose task has closed has released its slot and is merely
        still open. See :attr:`slot_released`.
        """
        return not self.is_interactive and not self.is_walk and not self.slot_released

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
            moment = now or dispatch_clock.utcnow()
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

    ``load_yaml`` rather than ``yaml.safe_load``, and the difference is not cosmetic:
    every caller of ``list_runs`` parses **every** run directory the machine has ever
    had, and a dispatch meta carries the whole argv including a settings blob. Measured
    on this machine's ledger (135 runs, 224 KB) on 2026-09-04: ``yaml.safe_load`` 178 ms,
    ``load_yaml`` 16.7 ms -- the former is the pure-Python parser, while ``storage`` has
    always reached for libyaml. Nothing read the whole ledger on a clock until task-328's
    machine-wide surface, so nobody had a reason to notice; ``dispatch/guards.py`` pays
    it on every dispatch too.
    """
    meta: Dict[str, object] = {}
    meta_path = directory / META_FILENAME
    if meta_path.is_file():
        try:
            loaded = load_yaml(meta_path.read_text(encoding="utf-8"))
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
        agent=str(meta.get("agent") or ""),
        merge_mode=merge_mode_text(meta.get("merge_mode") or meta.get("posture")),
        merge_mode_source=str(meta.get("merge_mode_source") or meta.get("posture_source") or ""),
        allow_automerge=legacy_allow_automerge(
            meta["allow_automerge"] if "allow_automerge" in meta else meta.get("posture_ceiling")
        ),
        merge_mode_requested=merge_mode_text(
            meta.get("merge_mode_requested") or meta.get("posture_requested")
        ),
        status=str(meta.get("status") or "unknown"),
        outcome=str(meta["outcome"]) if meta.get("outcome") else None,
        session_id=str(meta["session_id"]) if meta.get("session_id") else None,
        pid=_as_optional_int(meta.get("pid")),
        pid_identity=str(meta.get("pid_identity") or ""),
        started_at=_as_moment(meta.get("started_at")),
        finished_at=_as_moment(meta.get("finished_at")),
        caused_by=_as_optional_int(meta.get("caused_by")),
        argv=[str(item) for item in argv] if isinstance(argv, list) else [],
        cwd=str(meta.get("cwd") or ""),
        origin=str(meta.get("origin") or ""),
        over_ceiling=bool(meta.get("over_ceiling")),
        handback_pending=_as_optional_int(meta.get("handback_pending")),
        slot_released_at=_as_moment(meta.get("slot_released_at")),
        slot_released_reason=str(meta.get("slot_released_reason") or ""),
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


def calling_run_id(environ: Optional[Dict[str, str]] = None) -> str:
    """The run this process was dispatched as, or ``""`` for a person at a shell.

    Deliberately the raw variable rather than ``finish.own_run_id``. That function
    repairs a *leaked* identity, and to do it needs a task id and this task's run lock --
    neither of which a whole-machine sweep has. What the sweep needs is weaker and this
    answers it exactly: a run id this process might be, which is enough to decline to
    conclude it.
    """
    source = os.environ if environ is None else environ
    return (source.get(RUN_ID_ENV) or "").strip()


def slot_runs(home: Path) -> List[RunRecord]:
    """The live runs that occupy a slot: everything ``live_runs`` answers, minus the
    interactive ones (task-354).

    This is the list the concurrency guard counts and the one ``GET /api/runs/live``
    reports as ``occupied``. Keeping the two on one function is what keeps a refused
    dispatch and the dashboard's "N of M slots busy" from ever disagreeing.
    """
    return [record for record in live_runs(home) if record.takes_slot]


def conclude_interactive(
    home: Path, record: RunRecord, outcome: DispatchOutcome, *, detail: str = ""
) -> None:
    """End an interactive run's record, and nothing else.

    Not ``Ledger._conclude``: that writes a ``dispatch_result`` onto the task and hands
    the ball to a human when the run ended without one, which is the right thing for a
    run AgentJobs started and the wrong thing for a session a person was sitting in --
    the person's own verbs are already on the record. The lock the claim took is
    released here because nothing else ever will; it names a run that is now terminal.

    Through the journal's terminal compare-and-set like every other ending (task-264), so
    a sweep and a verb settling the same interactive run cannot both write its ending. A
    journal that cannot be written does not stop the record closing: the session is a
    person's, and ``journal.attempt_evidence`` accepts its own record as proof it ended.
    """
    from agentjobs.dispatch import journal  # local: journal imports runner

    try:
        conclusion = journal.claim_conclusion(
            home, record, outcome, concluded_by=f"interactive: {detail}"[:300], status="finished"
        )
        if not conclusion.won:
            return
    except ExecutionStoreError:
        pass
    write_status(
        record,
        status="finished",
        outcome=outcome.value,
        finished_at=dispatch_clock.utcnow().isoformat(),
        ended=detail,
    )
    release_stale_locks(home)


HEALTH_WORKING = "working"
HEALTH_STARTING = "starting"
HEALTH_PARKED = "parked"
HEALTH_SILENT = "silent"
HEALTH_ORPHANED = "orphaned"
HEALTH_UNKNOWN = "unknown"
HEALTH_IDLE = "idle"
HEALTH_HANDBACK = "handback"
HEALTH_WORK_DONE = "work_done"
HEALTH_FINISHING = "finishing"

INTERACTIVE_IDLE_SECONDS = 600.0
"""How long an interactive session's transcript may go unwritten before it reads as
idle rather than working. Ten minutes: a person reading a long answer is not idle, and a
session left open over lunch is. Nothing acts on it; it is a word on a card."""


def _transcript_age_seconds(record: RunRecord) -> Optional[float]:
    """Seconds since the session's own transcript last changed, or ``None``."""
    if not record.session_id:
        return None
    from agentjobs.dispatch.transcript import find_session_transcript  # local: no cycle

    cwd = Path(record.cwd) if record.cwd else record.path
    path = find_session_transcript(record.session_id, cwd)
    if path is None:
        return None
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, dispatch_clock.utcnow().timestamp() - modified)


def run_health(record: RunRecord, *, finishing: bool = False) -> str:
    """What a *live* run is actually doing, which is not the same as being live.

    ``is_live`` answers only "nothing has declared this over", so every surface that
    renders a live run as working is asserting something the ledger never said. This is
    the missing half, and it invents no new evidence: every value below is read off
    something already written to disk by the code that owns it.

    - ``working`` -- the poller's ``running``. It has produced output recently.
    - ``starting`` -- written when the run directory is created, before a session id
      exists. Seconds, normally.
    - ``parked`` -- ``DispatchRunner`` writes this when a session stops on a permission
      prompt or an expired login. It is alive and will wait for ever; a human is the
      only thing that moves it.
    - ``silent`` -- the poller's ``stalled``: the session still claims to be working and
      has emitted nothing for ``limits.session_stall_seconds`` (task-296). Recoverable,
      so it stays live and keeps being polled. Called ``silent`` here rather than
      ``stalled`` because what the record can honestly claim is the absence of output,
      not that the agent is stuck.
    - ``orphaned`` -- a **batch** run whose recorded pid is gone. ``reconcile`` uses
      exactly this rule ("batch runs do not outlive their supervisor"), so this says the
      same thing a restart would, without waiting for one.
    - ``handback`` -- a human moved the ball back to the agent while this run was still
      going, so feedback is waiting for it (task-384). Ahead of ``working`` deliberately:
      both are true, and *feedback waiting* is the one that answers the question the
      reader actually has. It was reading *Revising (claude)* beside a run in its 50th
      minute that made a person wait rather than look.
    - ``work_done`` -- the task this run was dispatched for is closed and the session is
      still open (task-482). Ahead of everything but ``handback`` for the same reason
      that one is ahead of ``working``: a reader looking at a run beside a task marked
      Completed is asking which of the two to believe, and the answer is both. The run
      holds no slot from here on, so this word and the machine's capacity say the same
      thing.
    - ``unknown`` -- an unreadable or unrecognised meta. ``read_run`` deliberately keeps
      such a run live rather than calling it finished, and this is what stops that
      caution being rendered as confidence.
    - ``finishing`` -- a scripted finish is live for this run's task, which is still
      open (task-533). The ledger cannot see finishes, so the caller passes
      ``finishing``, and it must derive it from ``finish_status.live_finishes`` -- the
      same lookup the task read's ``live_finish`` comes from -- so this tile and the
      task's own "Finishing" chip are one fact, not two that happen to agree.

      **It outranks everything, ``handback`` and ``parked`` included**, which is
      ``derived_display_status``'s rule for the task and deliberately the same one. A
      finish starts only on an approval or on the run's own ``--merge-mode-release``, so
      feedback that was waiting has been superseded by the time one is live; and a
      finish holds the task's lock and does not need the session, so a parked prompt
      or a quiet one (a four-minute gate prints nothing the poller sees) is not what is
      happening to the task. Ranking ``parked`` first was the alternative, and rejected:
      it would put back exactly the disagreement this value exists to remove, one tile
      reading "Waiting on you" beside a page reading "Finishing".

      **A closed task never reads ``finishing``.** The caller passes ``False`` there,
      because the task page says "Completed" in the second or two a finish spends
      cleaning up after its ``close`` step, and ``work_done`` is this function's word
      for that.

    **The pid deliberately gets no vote on a session run**, for the reason
    ``stale_lock_reason`` spells out: a dispatched session outlives the process that
    launched it, so a gone pid is expected rather than diagnostic. Sessions record no
    pid at all today. The authoritative liveness check for one is ``session_ledger()``,
    which shells out to the session manager -- far too expensive for a surface on a
    two-second clock, and unnecessary, because the API server that serves that surface
    *is* the poller writing these statuses.
    """
    status = record.status
    if finishing and not record.slot_released:
        return HEALTH_FINISHING
    if record.handback_pending is not None:
        return HEALTH_HANDBACK
    if record.slot_released:
        return HEALTH_WORK_DONE
    if status == "parked":
        return HEALTH_PARKED
    if status == "stalled":
        return HEALTH_SILENT
    if status == "starting":
        return HEALTH_STARTING
    if status == "running":
        if record.is_interactive:
            # No poller writes a status for these, so the one thing on disk that says
            # whether the person is there is their transcript's modification time.
            age = _transcript_age_seconds(record)
            if age is not None and age > INTERACTIVE_IDLE_SECONDS:
                return HEALTH_IDLE
            return HEALTH_WORKING
        if not record.is_session and record.pid is not None and not process_alive(record.pid):
            return HEALTH_ORPHANED
        return HEALTH_WORKING
    return HEALTH_UNKNOWN


def find_run(home: Path, run_id: str) -> RunRecord:
    """One run by id, or an error naming what is there instead."""
    directory = runs_root(home) / run_id
    if not directory.is_dir():
        known = ", ".join(record.run_id for record in list_runs(home)[:5]) or "none"
        raise LedgerError(f"No run {run_id!r}. Recent runs: {known}.")
    return read_run(directory)


def write_status(record: RunRecord, **fields: object) -> None:
    """Merge fields into a run's meta.yaml, replacing it rather than rewriting it.

    The replacement is what makes ``cancel_requested`` mean anything (task-390). This is
    the write a cancellation puts the flag down with, and the batch supervisor is reading
    the same file a few milliseconds later to decide whether the kill it just woke from
    was a cancellation. Rewriting in place gave that reader a window in which the file was
    empty, and ``RunDirectory.read_meta`` reports an unreadable file as ``{}`` -- so the
    flag looked absent rather than unreadable and the supervisor wrote ``failed`` over the
    cancellation. See ``dispatch.atomic_yaml``.
    """
    meta_path = record.path / META_FILENAME
    merged = merge_yaml_atomically(meta_path, fields, merge=finish_stamped, loader=load_yaml)
    if str(merged.get("status") or "") in TERMINAL_STATUSES:
        # The write that ends a run destroys its credential digest -- see
        # `RunDirectory.update_meta`, which does the same for the other write path.
        revoke_run_credential(record.path)


# ----- stopping things --------------------------------------------------------


SESSION_TREE_READER: Callable[[], List["proctree.Proc"]] = proctree.fast_process_table
"""How a session stop walks the machine's process tree (task-548)."""

SESSION_FINDER: Callable[[], List["proctree.Proc"]] = proctree.claude_processes
"""How a session stop finds the session's own processes, by command line (task-548).

Both are module attributes so the suite can replace them: ``tests/conftest.py`` points
them at an empty table for every test, because a test cancelling a fake session must
never read -- or end -- the tree of a real one that happens to share a prefix.
"""


@dataclass(frozen=True)
class StopResult:
    """What happened when a run was asked to stop."""

    run_id: str
    stopped: bool
    detail: str
    kind: str = "run"
    """What ``run_id`` names. ``stop_everything`` also ends walks, pull armings and
    queued dispatches (task-573), and for those the id is a walk, arming or queue id."""


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

    def manager_for(self, record: RunRecord) -> Optional[TaskManagerLike]:
        """The TaskManager owning this run's task, or None when it cannot be resolved."""
        supplied = self.managers.get(record.project_id)
        if supplied is not None:
            return supplied
        try:
            project: Project = self.registry.get(record.project_id)
        except ProjectError:
            return None
        return dispatch_manager_for(project)

    def _manager_for_project(self, project_id: str) -> Optional[TaskManagerLike]:
        """The manager for a project id, or ``None`` -- the journal adapter's resolver."""
        supplied = self.managers.get(project_id)
        if supplied is not None:
            return supplied
        try:
            project: Project = self.registry.get(project_id)
        except ProjectError:
            return None
        try:
            return dispatch_manager_for(project)
        except Exception:  # noqa: BLE001 - an unopenable store resolves nothing
            return None

    def _session(self, *args: str) -> subprocess.CompletedProcess:
        """Run a session-manager subcommand. argv is a list; there is no shell."""
        argv = [resolve_executable(self.session_command[0]), *self.session_command[1:], *args]
        try:
            return program.run(
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

        if not (completed.stdout or "").strip():
            # Unreadable, not empty: a real empty listing prints `[]` (task-416).
            raise LedgerError("Session ledger printed nothing; no run is judged from it.")
        try:
            loaded = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Session ledger was not JSON: {exc}") from exc
        if isinstance(loaded, dict):
            loaded = loaded.get("agents") or loaded.get("sessions") or []
        return [row for row in loaded if isinstance(row, dict)]

    def active_sessions(self) -> List[Dict[str, object]]:
        """Every session the CLI currently lists, interactive ones included.

        Without ``--all``: that flag adds *completed* background sessions, and the whole
        point of this listing is that a session missing from it is over. Used by startup
        reconciliation to decide whether an interactive run's session survived the
        restart (task-354).
        """
        completed = self._session("agents", "--json")
        if completed.returncode != 0:
            raise LedgerError(
                f"Could not read the session ledger: {(completed.stderr or '').strip()[:300]}"
            )
        import json

        if not (completed.stdout or "").strip():
            # Unreadable, not empty: a real empty listing prints `[]` (task-416).
            raise LedgerError("Session ledger printed nothing; no run is judged from it.")
        try:
            loaded = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Session ledger was not JSON: {exc}") from exc
        if isinstance(loaded, dict):
            loaded = loaded.get("agents") or loaded.get("sessions") or []
        return [row for row in loaded if isinstance(row, dict)]

    # ----- cancellation ------------------------------------------------------

    def cancel(
        self,
        run_id: str,
        *,
        actor: str = "dispatcher",
        source: str = "ledger",
        requester: Optional[str] = None,
    ) -> StopResult:
        """Stop one run, by whichever means its mode calls for.

        The request is recorded in the journal -- who asked, from where -- **before**
        anything is signalled (task-264). That record is what makes a poll tick landing in
        the same second defer to this cancellation instead of concluding the run itself,
        and it survives the process: a restart finds the Stop, not a run to recover.
        """
        from agentjobs.dispatch import journal  # local: journal imports runner

        record = find_run(self.home, run_id)
        liveness = journal.journal_liveness(self.home, run_id)
        if liveness is False or (liveness is None and not record.is_live):
            waiting = journal.controlled_execution(self.home, run_id)
            if waiting is not None and not waiting.terminal:
                # The run is over but its execution is waiting to try again (task-416). A
                # Stop cancels that intent, so no retry follows it, now or after a restart.
                try:
                    journal.journal(self.home).stop_execution(
                        waiting.execution_id,
                        requester=requester or actor,
                        source=source,
                        reason="cancel",
                    )
                except ExecutionStoreError as exc:
                    raise LedgerError(
                        f"Could not record the Stop of {waiting.execution_id}: {exc}"
                    ) from exc
                return StopResult(
                    run_id, True, f"stopped execution {waiting.execution_id}; no retry follows"
                )
            return StopResult(run_id, False, f"already {record.outcome or record.status}")
        if record.is_interactive:
            # The session belongs to a person, and cancelling the *record* must not reach
            # into it. Nor does it write to the task: nothing about the work changed.
            conclude_interactive(
                self.home, record, DispatchOutcome.CANCELLED, detail=f"cancelled by {actor}"
            )
            return StopResult(
                run_id, True, "interactive run closed; the session itself was left running"
            )
        try:
            journal.request_cancel(
                self.home, record, requester=requester or actor, source=source, reason="cancel"
            )
        except ExecutionStoreError as exc:
            raise LedgerError(
                f"Could not record the cancellation of {run_id}, so nothing was stopped: {exc}"
            ) from exc
        result = self._stop(record)
        if not result.stopped:
            # from-264-stop (task-312). A stop that could not be confirmed is still
            # stopping: the process may be alive and writing, so the run is not concluded,
            # its task lock is not released, and the Stop stays on record. The poller
            # concludes it once the session is seen stopped or gone.
            self._record_unconfirmed_stop(
                record, result, requester=requester or actor, source=source
            )
            return result
        self._conclude(record, DispatchOutcome.CANCELLED, actor=actor, body=result.detail)
        return result

    def _record_unconfirmed_stop(
        self, record: RunRecord, result: StopResult, *, requester: str, source: str
    ) -> None:
        """Say on the run and on its task that a Stop is in force and not yet confirmed."""
        write_status(record, stop_unconfirmed=result.detail or "stop was not confirmed")
        manager = self.manager_for(record)
        if manager is None or not record.task_id:
            return
        try:
            manager.add_log_entry(
                record.task_id,
                actor="dispatcher",
                type=LogEntryType.NOTE,
                body=(
                    f"{requester} asked run `{record.run_id}` to stop ({source}), and the "
                    f"stop could not be confirmed: {result.detail}\n\n"
                    "The run is **stopping**, not cancelled. It keeps this task, so nothing "
                    "else can start on it, until its session is seen stopped or gone; the "
                    "poller concludes it then. The Stop stays on record, so it will not be "
                    "resumed or woken in the meantime."
                ),
                data={"stop_unconfirmed": record.run_id, "requester": requester, "source": source},
            )
        except Exception:  # noqa: BLE001 - the run's state is already on disk and in the journal
            return

    def conclude_confirmed_stop(self, record: RunRecord) -> None:
        """Conclude a run left ``stopping`` now that its session has quiesced (task-312)."""
        self._conclude(
            record,
            DispatchOutcome.CANCELLED,
            actor="dispatcher",
            body="The stop requested earlier is confirmed: the session is stopped or gone.",
        )

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
        """`claude stop <id>`, then end what the session leaves behind (task-548).

        The session itself is stopped by the session manager and not reimplemented with
        signals: going around it with a pid would leave its ledger claiming a session
        that no longer exists, and would lose the conversation that `stop` preserves.

        **But `stop` ends `claude.exe` and nothing it started.** On Windows a child
        outlives its parent, so a gate in flight -- `check.py`, `pytest -n 26`, its
        workers -- and both MCP servers kept running after every cancel, which is how
        the machine lost about 30 GB on 2026-09-23. So the session's tree is read
        *before* the stop, while every parent link is live, and whatever in it survives
        the stop is ended afterwards, each process checked by creation time through the
        handle that ends it. See :mod:`agentjobs.dispatch.proctree`.
        """
        if not record.session_id:
            return StopResult(record.run_id, False, "no session id recorded")
        roots, tree, tree_error = proctree.session_tree(
            record.session_id, table=SESSION_TREE_READER, commands=SESSION_FINDER
        )
        try:
            completed = self._session("stop", record.session_id)
        except LedgerError as exc:
            return StopResult(record.run_id, False, str(exc))
        if completed.returncode == 0:
            detail = f"stopped session {record.session_id}"
            if tree_error:
                return StopResult(
                    record.run_id,
                    True,
                    f"{detail}; its process tree could not be read, so nothing it left "
                    f"behind was ended: {tree_error}",
                )
            reaped = proctree.reap(
                tree, roots=roots, table=SESSION_TREE_READER, end=proctree.terminate
            )
            return StopResult(record.run_id, True, f"{detail}; {reaped.sentence()}")
        return StopResult(
            record.run_id,
            False,
            f"`stop {record.session_id}` exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:200]}",
        )

    def _stop_batch(self, record: RunRecord) -> StopResult:
        """Signal the process group, then kill the tree -- once the pid is proved to be it.

        The tree, not the process: an agent that shelled out to pytest must not leave the
        pytest behind. Windows is the platform this runs on, so it is the reference path
        rather than the port.

        **The proof is not optional** (task-505). This used to run
        ``taskkill /PID <recorded pid> /T /F`` on whatever number the meta held, however
        old. A recorded pid is only as good as the machine's recycling rate, and this one
        recycles a number in seconds; the losing side of that coin flip kills a stranger,
        which on a machine running the gate is another pytest worker's child. A worker
        that is already gone is the ordinary case here and reports as stopped, because
        it is -- what it must not do is take somebody else with it.
        """
        if record.pid is None:
            return StopResult(record.run_id, False, "no pid recorded")
        from agentjobs.dispatch.runner import _kill_tree  # local: same subsystem

        killed = _kill_tree(
            record.pid,
            identity=record.pid_identity or None,
            recorded_at=None if record.pid_identity else record.started_at,
        )
        if killed:
            return StopResult(record.run_id, True, f"killed process tree at pid {record.pid}")
        return StopResult(
            record.run_id,
            True,
            f"worker pid {record.pid} is not this run's process any more; nothing was killed",
        )

    def stop_everything(
        self,
        *,
        actor: str = "dispatcher",
        requester: Optional[str] = None,
        source: str = "the CLI ('agentjobs dispatch stop')",
    ) -> List[StopResult]:
        """The panic button: refuse all new runs, then stop everything AgentJobs started.

        The sentinel is written **first**. If stopping takes a while -- a batch run using
        its grace period -- nothing new may start in the meantime, which is the whole
        point of pressing it.

        Then the three things that would start runs again the moment the sentinel is
        lifted (task-573): pull armings are disarmed, queued dispatches are cancelled and
        open epic walks are closed. The sentinel alone only *pauses* them, and a Resume
        that restarted a pull the person had just hit the panic button on would make the
        button a pause key. Only then the live runs, so no walk sees its child die and
        spends a retry on it.

        ``actor`` is who task-record writes are attributed to; ``requester`` is the
        person named in the sentinel and in each note, when they differ.
        """
        who = requester or actor
        write_sentinel(self.home, actor=who, source=source)
        results: List[StopResult] = []
        results.extend(self._disarm_pulls(who))
        results.extend(self._drain_queue(who))
        results.extend(self._close_walks(who, actor=actor))
        for record in live_runs(self.home):
            if record.is_interactive:
                # The panic button stops what AgentJobs started. A person's own session
                # is not that; its record is closed so the board stops showing it.
                conclude_interactive(
                    self.home, record, DispatchOutcome.CANCELLED, detail="dispatch stop"
                )
                results.append(StopResult(record.run_id, True, "interactive run closed"))
                continue
            from agentjobs.dispatch import journal  # local: journal imports runner

            try:
                journal.request_cancel(
                    self.home, record, requester=actor, source="stop_everything", reason="panic"
                )
            except ExecutionStoreError:
                # The panic button still stops things. The sentinel is already down, and
                # a Stop that could not be journalled is still a Stop.
                pass
            result = self._stop(record)
            if result.stopped:
                self._conclude(record, DispatchOutcome.CANCELLED, actor=actor, body=result.detail)
            else:
                self._record_unconfirmed_stop(
                    record, result, requester=actor, source="stop_everything"
                )
            results.append(result)
        return results

    def _disarm_pulls(self, who: str) -> List[StopResult]:
        """Retire every pull arming, so Resume does not start pulling again."""
        from agentjobs.dispatch import pull  # local: pull imports the journal

        results: List[StopResult] = []
        for arming in pull.armings(self.home):
            try:
                retired = pull.disarm(self.home, arming.project_id, requester=who)
            except ExecutionStoreError as exc:
                results.append(
                    StopResult(arming.arming_id, False, f"pull mode not disarmed: {exc}", "pull")
                )
                continue
            if retired is not None:
                results.append(
                    StopResult(
                        arming.arming_id,
                        True,
                        f"pull mode for {arming.project_id} disarmed",
                        "pull",
                    )
                )
        return results

    def _drain_queue(self, who: str) -> List[StopResult]:
        """Cancel every dispatch still waiting for a slot, with a note on its task."""
        from agentjobs.dispatch import queue as dispatch_queue  # local: imports the journal

        results: List[StopResult] = []
        for entry in dispatch_queue.waiting(self.home):
            try:
                removed = dispatch_queue.cancel(
                    self.home,
                    entry.queue_id,
                    requester=f"{who} (emergency stop)",
                    manager=self._manager_for_project(entry.project_id),
                )
            except ExecutionStoreError as exc:
                results.append(
                    StopResult(entry.queue_id, False, f"not taken out of the queue: {exc}", "queue")
                )
                continue
            if removed is not None:
                results.append(
                    StopResult(
                        entry.queue_id,
                        True,
                        f"queued dispatch of {entry.task_id} cancelled before it started",
                        "queue",
                    )
                )
        return results

    def _close_walks(self, who: str, *, actor: str) -> List[StopResult]:
        """Close every open epic walk and put its parent in front of a person.

        A walk left open would, on its next tick, try to start a child, be refused by the
        sentinel and ground on ``could_not_start_child`` -- blaming a child for a stop
        nobody's child caused. Closing it here writes the true reason instead. A walk
        held by a live process of its own (an attached ``dispatch walk``) is closed on the
        record too; that process's next start is refused by the sentinel and it ends.
        """
        from agentjobs.dispatch.epic import WalkStop
        from agentjobs.dispatch.journal import journal

        results: List[StopResult] = []
        try:
            store = journal(self.home)
            walks = store.open_walks()
        except ExecutionStoreError as exc:
            return [StopResult("walks", False, f"open walks unreadable: {exc}", "walk")]
        for walk in walks:
            detail = f"Ended by the emergency stop, pressed by {who}."
            try:
                store.update_walk(
                    walk.walk_id,
                    epoch=walk.epoch,
                    state="stopped",
                    stop=WalkStop.EMERGENCY_STOP.value,
                    detail=detail,
                )
            except ExecutionStoreError as exc:
                results.append(StopResult(walk.walk_id, False, f"walk not closed: {exc}", "walk"))
                continue
            self._tell_parent(walk.project_id, walk.parent_task_id, who=who, actor=actor)
            results.append(
                StopResult(walk.walk_id, True, f"epic walk of {walk.parent_task_id} ended", "walk")
            )
        return results

    def _tell_parent(self, project_id: str, parent_id: str, *, who: str, actor: str) -> None:
        """Say on an epic's parent that its walk was ended, and why. Never raises."""
        manager = self._manager_for_project(project_id)
        if manager is None:
            return
        prompt = (
            f"The emergency stop, pressed by {who}, ended this epic's walk; no further "
            "child will start. Children already running were stopped with it. Once dispatch is "
            "resumed, dispatch this parent again to restart the walk -- it re-reads each "
            "child's record, so nothing already merged is redone."
        )
        try:
            manager.add_log_entry(parent_id, actor=actor, type=LogEntryType.PROGRESS, body=prompt)
            parent = manager.get_task(parent_id)
            if parent is not None and parent.is_open and parent.ball is not Ball.HUMAN:
                manager.handoff(
                    parent_id,
                    actor=actor,
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.DECISION,
                    ball_prompt=prompt,
                )
        except Exception:  # noqa: BLE001 - the walk is closed; the note is a courtesy
            return

    # ----- reconciliation ----------------------------------------------------

    def reconcile(self, *, actor: str = "dispatcher") -> List[StopResult]:
        """Settle every run left behind by a previous process.

        Called at startup. Batch and session are opposites here, on purpose -- see the
        module docstring.

        **Except the caller's own run, which it never settles** (task-394). This is a
        sweep for runs whose process is gone, and the one run in the list that provably
        still has a process is the one running this. See the refusal in the loop for what
        concluding it cost.
        """
        results: List[StopResult] = []
        sessions: Optional[Dict[str, Dict[str, object]]] = None
        active: Optional[List[Dict[str, object]]] = None
        own = calling_run_id()

        # Before the sweep (task-264): a terminal result a previous process concluded in
        # the journal and died before writing to its task is delivered now, under the same
        # operation id, so the task learns how its run ended and learns it once.
        from agentjobs.dispatch import journal  # local: journal imports runner

        try:
            for operation in journal.flush_owed_results(self.home, self._manager_for_project):
                results.append(StopResult(operation, True, "delivered an owed dispatch_result"))
        except ExecutionStoreError as exc:
            results.append(StopResult("journal", False, f"owed results not delivered: {exc}"))
        for attempt in journal.release_ended(self.home, self._manager_for_project):
            results.append(
                StopResult(
                    attempt.run_id, True, f"released its journal ownership: {attempt.concluded_by}"
                )
            )

        for record in live_runs(self.home):
            if own and record.run_id == own:
                # A process cannot be evidence that it is itself gone (task-394). On
                # 2026-09-07 a dispatched session ran this by hand to clear a run stuck
                # at `starting`, and the run it cleared was its own: `_conclude` wrote a
                # false `interrupted` dispatch_result onto the task, moved the ball off
                # `human`/`review` to `human`/`decision` -- a "Needs decision" card for a
                # decision nobody needed to make -- and, because a credential is verified
                # against its run's status, invalidated the very session's ability to
                # correct any of it. One refusal removes all three.
                #
                # Believing the caller's own claim about itself is the whole cost. A
                # forged or leaked `AGENTJOBS_RUN_ID` defers one conclusion by one sweep,
                # and the next process to reconcile makes it; concluding a live caller
                # cannot be undone by anything.
                results.append(
                    StopResult(
                        record.run_id,
                        False,
                        "this is the run calling reconcile, which is not evidence it ended",
                    )
                )
                continue

            concluded = _journal_conclusion(self.home, record.run_id)
            if concluded is not None:
                # The journal already decided how this run ended and the process that
                # won died before projecting it onto the meta. Project it; concluding again
                # would lose the compare-and-set and leave the meta reading live for ever.
                write_status(
                    record,
                    status=concluded.status or "finished",
                    outcome=concluded.outcome,
                    finished_at=concluded.concluded_at,
                )
                results.append(
                    StopResult(record.run_id, True, "projected its journal conclusion onto meta")
                )
                continue

            if journal.controller_driven(self.home, record.run_id):
                # Recovered by the durable controller, which reattaches a surviving session
                # and proves a batch worker gone before it concludes anything (task-416).
                # This sweep's verdicts -- a session missing from one listing, a batch run
                # whose supervisor died -- are exactly the guesses it replaces.
                results.append(
                    StopResult(record.run_id, False, "recovered by the durable controller")
                )
                continue

            if record.is_interactive:
                # Presence is the only question. The task-state half of the sweep runs
                # on the poller's first tick, seconds from now, and nothing here writes
                # to a task.
                if active is None:
                    try:
                        active = self.active_sessions()
                    except LedgerError as exc:
                        # Cannot tell whether the session survived, so leave it alone.
                        results.append(
                            StopResult(record.run_id, False, f"ledger unreadable: {exc}")
                        )
                        continue
                if record.session_id and any(
                    record.session_id in (str(row.get("sessionId") or ""), str(row.get("id") or ""))
                    or str(row.get("sessionId") or "").startswith(record.session_id)
                    for row in active
                ):
                    results.append(
                        StopResult(record.run_id, False, "interactive session still open")
                    )
                    continue
                conclude_interactive(
                    self.home,
                    record,
                    DispatchOutcome.SESSION_ENDED,
                    detail="not in the driver's ledger when AgentJobs restarted",
                )
                results.append(StopResult(record.run_id, True, "interactive session gone"))
                continue

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
                try:
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
                except LedgerError as exc:
                    results.append(StopResult(record.run_id, False, str(exc)))
                    continue
                results.append(StopResult(record.run_id, True, "session gone; marked interrupted"))
                continue

            # Batch: the supervisor died with the process that owned it.
            try:
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
            except LedgerError as exc:
                results.append(StopResult(record.run_id, False, str(exc)))
                continue
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
        (``../worktrees/<repo>-<nnn>``, per ALLAGENTS.md) is outside AgentJobs' knowledge entirely.
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
        if _session_is_already_gone(output):
            # Settled, not blocked: there is no job to remove and there never will be
            # again, so retrying this on every start is work that cannot succeed. See
            # `_session_is_already_gone` for why this one refusal is singled out.
            write_status(record, reap_settled=True, reap_blocked=detail)
            return StopResult(record.run_id, True, f"session already gone: {detail}")
        write_status(record, reap_blocked=detail)
        return StopResult(record.run_id, False, f"not removed: {detail}")

    def reap_finished(self) -> List[StopResult]:
        """Reap every finished session run whose conversation is no longer needed.

        **The one session a still-open task could resume is kept, and that is task-234.**
        Reaping calls ``claude rm``, which deletes the conversation -- and the
        conversation is exactly what the next dispatch of that task resumes instead of
        booting a cold agent that has to rediscover the branch and the worktree. So the
        question this asks is no longer "has this run finished" but "can anything still
        want this session back".

        Precisely one run per open task is kept: the newest session run, which is the
        only one ``wake.find_wake_target`` will ever offer. It is found by calling that
        module's own function rather than by a second rule written here, so the reaper
        and the waker cannot come to disagree about which conversation matters -- the
        failure that would produce is a wake that resumes a session the reaper deleted,
        and it would surface as an unexplained dispatch failure long after the cause.

        The cost of keeping one is a row in the session manager's list. It is not a held
        process and not a concurrency slot: a stopped session has no ``pid``, which is
        why ``_finish_session`` was already calling ``stop`` rather than ``rm``. Closing
        the task collects it on the next sweep.

        **A run whose session state is settled is skipped for ever**, which is the half
        that was missing (task-503). Until then the only skip condition was ``reaped:
        true``, so a run the session manager had refused because its job no longer
        exists was re-attempted on every start from then on -- 20 of this machine's 31
        attempts, at about 0.85s of process spawn each, the oldest doomed since
        2026-09-10. That set only grows, so the cost grew with every dispatched run.
        """
        records = list_runs(self.home)
        keep = self._wakeable_run_ids(records)
        results = []
        for record in records:
            if not record.is_session or record.is_live:
                continue
            if _reap_is_settled(record):
                continue
            if record.run_id in keep:
                continue
            results.append(self.reap(record))
        return results

    def _wakeable_run_ids(self, records: Optional[Sequence[RunRecord]] = None) -> Set[str]:
        """The run ids whose sessions a later dispatch could still resume.

        Empty on every uncertainty -- a run with no task id, a project the registry
        cannot resolve, a task that has been deleted, a storage error. Each of those
        means nothing is going to resume that conversation, so keeping it would be
        hoarding rather than caution, and an unbounded pile of sessions nobody can
        account for is a worse outcome than a cold start.
        """
        from agentjobs.dispatch.wake import newest_session_runs

        keep: Set[str] = set()
        # Keyed on the project as well as the task (task-264, P2-5). Task ids are
        # per-project, and keying on the id alone let one project's open task keep --
        # or fail to keep -- another project's conversation.
        #
        # One pass, over records a caller may already hold. Asking `newest_session_run`
        # per pair rescanned the runs directory once per pair: 185 pairs over 330 runs
        # cost 23.5s of a 34s call, and both numbers grow together (task-503).
        scanned = list_runs(self.home) if records is None else records
        for (_project_id, task_id), newest in newest_session_runs(scanned).items():
            if newest.is_live:
                continue
            try:
                manager = self.manager_for(newest)
                task = manager.get_task(task_id) if manager is not None else None
            except Exception:  # noqa: BLE001 - an unreadable task is not a reason to keep
                continue
            if task is not None and task.is_open:
                keep.add(newest.run_id)
        return keep

    # ----- writing the outcome back to the task ------------------------------

    def _conclusion_prompt(
        self, record: RunRecord, outcome: DispatchOutcome, task: object
    ) -> Optional[str]:
        """What a concluded run tells a human, or ``None`` when it must say nothing (task-312).

        **A standing approval is a human decision already made, and a run's ending never
        overwrites it with a request to make one.** That sentence -- *nobody was told what
        this task needs* -- was written over an approval on task-021 and task-022, beside a
        merge that was going ahead. So:

        - **A Stop** is a newer decision than any approval before it. The approval is
          marked superseded in the journal, and the prompt names who stopped the run and
          says the approval it withdrew merged nothing.
        - **Any other ending** while an approval stands leaves the ball alone. The approval
          still says what the task needs, and whoever acts on it -- the scripted finish,
          which this spawns when the machine offers one -- is not this run.
        """
        from agentjobs.dispatch.approval import approval_standing_on, project_config_for

        if outcome is DispatchOutcome.CANCELLED:
            requester, source = self._stop_provenance(record)
            from agentjobs.dispatch.approval import withdraw_approval_on_stop

            withdrawn = withdraw_approval_on_stop(
                self.home,
                record.project_id,
                task,  # type: ignore[arg-type]
                project_config_for(self.home, record.project_id),
                run_id=record.run_id,
                requester=requester,
                source=source,
            )
            stopped_by = f"{requester} stopped run {record.run_id} ({source})"
            if withdrawn is not None:
                return (
                    f"{stopped_by} after this task was approved by {withdrawn.approver} "
                    f"(entry {withdrawn.entry_id}). The stop withdrew that approval and "
                    "**nothing was merged**. Approve again to finish it, or say what should "
                    "happen instead."
                )
            return (
                f"{stopped_by}. Read the dispatch_result entry, then either dispatch again "
                "or take it on yourself."
            )
        standing = approval_standing_on(self.home, record.project_id, task)  # type: ignore[arg-type]
        dispatched_at = _dispatch_entry_id(task, record.run_id)
        if standing is not None and (dispatched_at is None or standing.entry_id > dispatched_at):
            # Only a run the approval *arrived during* is owed the finish on ending. A run
            # dispatched after it -- a repair the finish itself escalated to -- ending is
            # not a reason to run that finish again, and doing so would be a loop.
            from agentjobs.dispatch.finish import resume_approved_finish

            resume_approved_finish(
                project_id=record.project_id,
                task_id=record.task_id,
                approver=standing.approver,
                home=self.home,
            )
            return None
        from agentjobs.dispatch.finish_resume import resume_finish_of_settled_run

        manager = self.manager_for(record)
        if manager is not None and resume_finish_of_settled_run(
            self.home,
            project_id=record.project_id,
            task_id=record.task_id,
            run_id=record.run_id,
            manager=manager,
        ):
            # The run died inside its own merge mode finish, which has been started again
            # to carry on (task-443). That finish is what acts next.
            return None
        return (
            f"Run {record.run_id} ended `{outcome.value}` and nobody was told "
            "what this task needs. Read the dispatch_result entry, then either "
            "dispatch again or take it on yourself."
        )

    def _stop_provenance(self, record: RunRecord) -> Tuple[str, str]:
        """Who asked for this run to stop and from where, as the journal recorded it."""
        from agentjobs.dispatch.journal import journal

        try:
            attempt = journal(self.home).attempt(record.run_id)
        except ExecutionStoreError:
            attempt = None
        cancel = (attempt.cancel if attempt is not None else None) or {}
        return str(cancel.get("requester") or "somebody"), str(cancel.get("source") or "unknown")

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
        from agentjobs.dispatch import journal  # local: journal imports runner

        finished = dispatch_clock.utcnow()
        manager = self.manager_for(record)
        task = manager.get_task(record.task_id) if manager is not None and record.task_id else None
        # From the instant this conclusion is made, not from the record in hand: that
        # record was read before the status changed and still describes a live run.
        duration = (
            (finished - record.started_at).total_seconds()
            if record.started_at is not None
            else None
        )
        projection = (
            journal.result_projection(
                record,
                outcome,
                actor=actor,
                re=_dispatch_entry_id(task, record.run_id),
                duration_seconds=duration,
                body=body,
            )
            if task is not None
            else None
        )
        # The one terminal transition (task-264). A poll tick and this sweep -- or this
        # cancellation -- can both reach here for one run; only one of them wins, and the
        # other writes nothing, rather than a second, contradictory result.
        try:
            conclusion = journal.claim_conclusion(
                self.home,
                record,
                outcome,
                concluded_by=f"ledger: {actor}",
                projection=projection,
            )
        except ExecutionStoreError as exc:
            raise LedgerError(
                f"Could not record how run {record.run_id} ended, so nothing was written: {exc}"
            ) from exc
        if not conclusion.won:
            return
        # The status that says how it ended. It used to be `cancelled` for every outcome
        # this wrote, `interrupted` included, so a swept run read as cancelled by somebody.
        write_status(
            record,
            status=journal.status_for(outcome),
            outcome=outcome.value,
            finished_at=finished.isoformat(),
        )
        if manager is None or not record.task_id:
            write_status(record, unattributed=f"no task record for {record.task_id!r}")
            return
        if task is None or projection is None:
            write_status(record, unattributed=f"task {record.task_id!r} not found")
            return

        journal.deliver_projection(self.home, manager, projection)
        task = manager.get_task(record.task_id)
        if task is not None and task.is_open and task.ball is not Ball.HUMAN:
            prompt = self._conclusion_prompt(record, outcome, task)
            if prompt is not None:
                manager.handoff(
                    record.task_id,
                    actor=actor,
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.DECISION,
                    ball_prompt=prompt,
                )

        # This sweep runs precisely because no session is left to speak for the run, so
        # there is certainly none left to commit what the sweep wrote (task-203).
        write_status(
            record,
            record_commit=commit_task_record(
                manager,
                record.task_id,
                subject=f"record swept run {record.run_id} as {outcome.value}",
            ).detail,
        )


def _journal_conclusion(home: Path, run_id: str) -> Optional["Attempt"]:
    """The journal's terminal attempt for this run, or ``None`` if live, unknown or unreadable."""
    from agentjobs.dispatch.journal import journal  # local: journal imports runner

    try:
        attempt = journal(home).attempt(run_id)
    except ExecutionStoreError:
        return None
    if attempt is None or attempt.is_live:
        return None
    return attempt


def _dispatch_entry_id(task: object, run_id: str) -> Optional[int]:
    """The id of the dispatch entry this run belongs to, so the result threads to it."""
    for entry in reversed(getattr(task, "log", [])):
        if entry.type.value == "dispatch" and entry.data.get("run_id") == run_id:
            return int(entry.id)
    return None
