"""Stop what is running out of a task's worktree before the finish removes it (task-566).

**Why this exists.** A review sandbox is started from the task's worktree and lives for the
review, and the review ends at approval. Nothing owned that lifetime: on 2026-09-24
task-557's two sandboxes kept serving for fifteen minutes after the finish had merged,
closed and removed the worktree out from under them, the empty worktree root survived
because a sandbox's cwd pinned it, and the dispatched session could not exit because the
sandboxes were its live background jobs. Stopping them here ends all three at once: the
sandboxes go, the directory can go, and the session is told its jobs ended and can finish.

**What belongs to a worktree.** A process whose current directory is inside it, or whose
command line names a path inside it. Both are read from the process itself -- its PEB on
Windows, ``/proc`` elsewhere -- and every descendant of such a process goes with it, which
is how ``scripts/epic_walk_sandbox.py``'s sleeper children are reached even when their own
cwd is elsewhere.

**What is never stopped**, whatever it matches:

* this process and every one of its ancestors -- a posture finish is started by the very
  session whose shell may have ``cd``-ed into the worktree to run it;
* a Claude Code session process (``claude.exe``, or anything carrying ``--session-id``):
  a session is ended with ``claude stop``, which keeps the conversation, and never by a
  kill -- that is ``dispatch/closed_sessions.py``'s business, under its own conditions;
* a process whose identity cannot be read, because nothing can then be proved about it.

**The pid-reuse guard** is the idle sweep's (task-419/task-444): the creation time and the
command line are read again immediately before each stop, and the stop itself goes through
:func:`proctree.terminate`, which checks the creation time through the handle it ends.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from agentjobs.dispatch import proctree
from agentjobs.dispatch.proctree import Proc

SETTLE_SECONDS = 5.0
"""How long to wait for stopped processes to release their handles before removal.

``TerminateProcess`` is asynchronous: the handle a dead process held on its cwd is let go
a moment later, and a ``git worktree remove`` in that moment fails exactly as it did with
the process alive."""

SESSION_IMAGES = frozenset({"claude.exe", "claude"})


@dataclass(frozen=True)
class Facts:
    """What one process says about itself: when it started, where it is, how it was run."""

    created: int
    cwd: str
    cmdline: str


@dataclass(frozen=True)
class Resident:
    """A process that belongs to the worktree, and why it was judged to."""

    proc: Proc
    why: str


@dataclass
class Teardown:
    """What stopping a worktree's residents did."""

    ended: List[Resident] = field(default_factory=list)
    declined: List[Tuple[Resident, str]] = field(default_factory=list)
    gone: List[Resident] = field(default_factory=list)
    """Matched, then exited before it was stopped: usually a launcher whose child was
    stopped first -- a virtualenv's ``python.exe`` exits when the interpreter it started
    does."""
    protected: List[Tuple[Proc, str]] = field(default_factory=list)
    error: str = ""

    def sentence(self) -> str:
        if self.error:
            return f"could not look for processes in the worktree: {self.error}"
        if not self.ended and not self.declined and not self.gone:
            text = "nothing to stop"
        else:
            parts = []
            if self.ended:
                named = ", ".join(_describe(item) for item in self.ended[:6])
                more = f" and {len(self.ended) - 6} more" if len(self.ended) > 6 else ""
                parts.append(f"stopped {len(self.ended)} process(es): {named}{more}")
            if self.declined:
                named = "; ".join(f"pid {item.proc.pid}: {why}" for item, why in self.declined[:4])
                parts.append(f"did not stop {len(self.declined)}: {named}")
            if self.gone:
                pids = ", ".join(str(item.proc.pid) for item in self.gone[:6])
                parts.append(f"{len(self.gone)} exited by themselves (pid {pids})")
            text = "; ".join(parts)
        if self.protected:
            kept = ", ".join(f"pid {proc.pid} ({why})" for proc, why in self.protected[:4])
            text += f"; left running: {kept}"
        return text


def _describe(item: Resident) -> str:
    name = item.proc.name or "process"
    return f"{name} {item.proc.pid} ({item.why})"


# ----- matching a path -------------------------------------------------------------------


def path_forms(worktree: Path) -> List[str]:
    """Every spelling of ``worktree`` a command line on this machine might use, lower-cased.

    ``C:\\x\\y``, ``C:/x/y`` and Git Bash's ``/c/x/y``. Lower-cased because Windows paths
    compare without case, and a command line typed by an agent uses whichever case it liked.
    """
    text = str(worktree).replace(chr(92), "/").rstrip("/")
    forms = {text.lower()}
    if len(text) > 2 and text[1] == ":":
        forms.add(("/" + text[0] + text[2:]).lower())
    return sorted(forms)


def _normal(text: str) -> str:
    return (text or "").replace(chr(92), "/").lower()


def _within(text: str, form: str) -> bool:
    """Whether ``form`` occurs in ``text`` as a whole path, not as the prefix of a longer name.

    ``agentjobs-56`` must not match ``agentjobs-566``: the character after the match has to
    end the path or start a deeper one.
    """
    start = 0
    while True:
        index = text.find(form, start)
        if index < 0:
            return False
        after = text[index + len(form) : index + len(form) + 1]
        if after in ("", "/", '"', "'", " ", ";", "&", "|", ")", "\t", "\n"):
            return True
        start = index + 1


def cwd_inside(cwd: str, worktree: Path) -> bool:
    normal = _normal(cwd).rstrip("/")
    return any(normal == form or normal.startswith(form + "/") for form in path_forms(worktree))


def names_worktree(cmdline: str, worktree: Path) -> bool:
    normal = _normal(cmdline)
    return any(_within(normal, form) for form in path_forms(worktree))


# ----- reading the machine ---------------------------------------------------------------


def read_facts(pid: int) -> Optional[Facts]:
    """``pid``'s creation time, current directory and command line, or None if unreadable."""
    if pid <= 4:
        return None
    if os.name == "nt":
        return _read_facts_windows(pid)
    return _read_facts_posix(pid)  # pragma: no cover - Windows is the reference platform


def _read_facts_windows(pid: int) -> Optional[Facts]:
    """Read the facts out of the process's own PEB, with one handle for all three.

    ``NtQueryInformationProcess`` gives the PEB's address; ``PEB+0x20`` holds the address of
    ``RTL_USER_PROCESS_PARAMETERS``, whose ``CurrentDirectory.DosPath`` is at ``+0x38`` and
    ``CommandLine`` at ``+0x70`` (both ``UNICODE_STRING``, x64 layout). A 32-bit Python
    cannot read a 64-bit process this way, so it reads nothing rather than guessing.
    """
    import ctypes
    from ctypes import wintypes

    if sys.maxsize <= 2**32:  # pragma: no cover - this machine's Python is 64-bit
        return None
    kernel32 = getattr(ctypes, "windll").kernel32
    ntdll = getattr(ctypes, "windll").ntdll
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return None
    try:
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = int(times[0].value)

        info = (ctypes.c_byte * 48)()
        returned = ctypes.c_ulong()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        )
        if status != 0:
            return None
        peb = ctypes.c_uint64.from_buffer(info, 8).value
        if not peb:
            return None

        def read(address: int, size: int) -> Optional[bytes]:
            buffer = (ctypes.c_char * size)()
            got = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(
                handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)
            ):
                return None
            return buffer.raw[: got.value]

        def pointer(address: int) -> Optional[int]:
            raw = read(address, 8)
            return int.from_bytes(raw, "little") if raw and len(raw) == 8 else None

        def unicode_string(address: int) -> str:
            header = read(address, 16)
            if not header or len(header) < 16:
                return ""
            length = int.from_bytes(header[0:2], "little")
            buffer = int.from_bytes(header[8:16], "little")
            if not length or not buffer:
                return ""
            raw = read(buffer, min(length, 65534))
            return raw.decode("utf-16-le", "replace") if raw else ""

        parameters = pointer(peb + 0x20)
        if not parameters:
            return None
        return Facts(
            created=created,
            cwd=unicode_string(parameters + 0x38),
            cmdline=unicode_string(parameters + 0x70),
        )
    finally:
        kernel32.CloseHandle(handle)


def _read_facts_posix(pid: int) -> Optional[Facts]:  # pragma: no cover
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        cwd = os.readlink(f"/proc/{pid}/cwd")
        return Facts(created=int(fields[19]), cwd=cwd, cmdline=cmdline)
    except (OSError, ValueError, IndexError):
        return None


def machine_rows() -> List[Tuple[Proc, Optional[Facts]]]:
    """Every process, with the facts it would give up. Raises ``OSError`` if none."""
    rows = []
    for proc in proctree.fast_process_table():
        facts = read_facts(proc.pid)
        if (
            facts is not None
            and proc.created
            and not proctree.same_creation(facts.created, proc.created)
        ):
            facts = None  # the pid changed hands between the two reads
        rows.append(
            (
                Proc(proc.pid, proc.ppid, proc.created, proc.name, facts.cmdline if facts else ""),
                facts,
            )
        )
    return rows


RESIDENT_READER: Callable[[], List[Tuple[Proc, Optional[Facts]]]] = machine_rows
"""The machine, replaceable. The test suite installs an empty table (see ``conftest``)."""

FACTS_READER: Callable[[int], Optional[Facts]] = read_facts
TERMINATE: Callable[[Proc], bool] = proctree.terminate


# ----- judging ---------------------------------------------------------------------------


def ancestors(rows: Sequence[Proc], pid: int) -> Set[int]:
    """``pid`` and every live ancestor of it, each linked only to a parent older than it."""
    by_pid: Dict[int, Proc] = {row.pid: row for row in rows}
    found: Set[int] = {pid}
    current = by_pid.get(pid)
    while current is not None:
        parent = by_pid.get(current.ppid)
        if parent is None or parent.pid in found:
            break
        if (
            parent.created
            and current.created
            and parent.created > current.created + proctree.TICK_SLACK
        ):
            break  # the ppid has been handed on to a younger process
        found.add(parent.pid)
        current = parent
    return found


def is_session(proc: Proc) -> bool:
    return proc.name.lower() in SESSION_IMAGES or "--session-id" in (proc.cmdline or "")


def residents(
    rows: Sequence[Tuple[Proc, Optional[Facts]]],
    worktree: Path,
    *,
    self_pid: int,
) -> Tuple[List[Resident], List[Tuple[Proc, str]]]:
    """``(to stop, left running with the reason)`` for ``worktree``.

    Matched processes and all their descendants, minus the protected. A protected process
    is reported only when it matched on its own, so the report names what could still pin
    the directory rather than every ancestor of this process.
    """
    procs = [proc for proc, _ in rows]
    protected_pids = ancestors(procs, self_pid)
    matched: List[Resident] = []
    kept: List[Tuple[Proc, str]] = []
    for proc, facts in rows:
        if facts is None:
            continue
        if cwd_inside(facts.cwd, worktree):
            why = "working directory"
        elif names_worktree(facts.cmdline, worktree):
            why = "command line"
        else:
            continue
        if proc.pid in protected_pids:
            kept.append((proc, "runs this finish"))
        elif is_session(proc):
            kept.append((proc, "a Claude session, ended only by `claude stop`"))
        else:
            matched.append(Resident(proc, why))

    seen = {(item.proc.pid, item.proc.created) for item in matched}
    for child in proctree.descendants(procs, [item.proc for item in matched]):
        key = (child.pid, child.created)
        if key in seen or child.pid in protected_pids or is_session(child):
            continue
        seen.add(key)
        matched.append(Resident(child, "started by one of them"))
    return matched, kept


def stop_residents(
    worktree: Path,
    *,
    rows: Optional[Callable[[], List[Tuple[Proc, Optional[Facts]]]]] = None,
    facts: Optional[Callable[[int], Optional[Facts]]] = None,
    end: Optional[Callable[[Proc], bool]] = None,
    self_pid: Optional[int] = None,
    settle_seconds: float = SETTLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Teardown:
    """Stop every process that belongs to ``worktree``, leaves first. Never raises."""
    rows = rows or RESIDENT_READER
    facts = facts or FACTS_READER
    end = end or TERMINATE
    result = Teardown()
    try:
        table = rows()
        targets, result.protected = residents(
            table, worktree, self_pid=os.getpid() if self_pid is None else self_pid
        )
    except (OSError, ValueError) as exc:
        result.error = str(exc)[:200]
        return result
    before = {proc.pid: proc for proc, _ in table}
    for item in sorted(targets, key=lambda resident: resident.proc.created, reverse=True):
        now = facts(item.proc.pid)
        if now is None:
            if item.why == "started by one of them":
                # A child read without facts may simply be unreadable; terminate() still
                # proves its identity through the handle before ending it.
                (result.ended if end(item.proc) else result.gone).append(item)
            else:
                result.gone.append(item)
            continue
        if not proctree.same_creation(now.created, item.proc.created):
            result.declined.append((item, "the pid now belongs to a different process"))
            continue
        seen_cmdline = before[item.proc.pid].cmdline if item.proc.pid in before else ""
        if seen_cmdline and now.cmdline != seen_cmdline:
            result.declined.append((item, "its command line changed"))
            continue
        if end(item.proc):
            result.ended.append(item)
            continue
        after = facts(item.proc.pid)
        if after is None or not proctree.same_creation(after.created, item.proc.created):
            result.gone.append(item)
        else:
            result.declined.append((item, "could not be ended"))
    if result.ended and settle_seconds > 0:
        sleep(settle_seconds)
    return result


__all__ = [
    "Facts",
    "Resident",
    "Teardown",
    "ancestors",
    "cwd_inside",
    "machine_rows",
    "names_worktree",
    "path_forms",
    "read_facts",
    "residents",
    "stop_residents",
]
