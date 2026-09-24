"""A session's process tree: find it while it is alive, end what it leaves behind (task-548).

**Why this exists.** A session run is stopped with ``claude stop <id>``, which ends
``claude.exe``. On Windows a child does not die with its parent, so everything the session
started -- its MCP servers, and the ``bash -> scripts/check.py -> pytest -n 26`` subtree of
a gate in flight -- carries on under a dead parent. On 2026-09-23 one session found about
seventy of them in minutes, all surviving ``dispatch cancel``, and the machine had lost
about 30 GB by evening.

**Why a snapshot, not a job object.** AgentJobs does not create the session process; the
``--bg`` daemon does, so there is no spawn to put in a job. Assigning one afterwards needs
a handle held for the run's whole life, and every holder is wrong: a kill-on-close job
held by the server kills every run when the server restarts, and a job without it cannot
be reopened by name once its holder is gone. So :func:`descendants` is taken *before* the
stop, while every parent link is still live, and :func:`reap` ends what outlives it.

**Why a pid alone is never enough** (task-505): this machine hands a dead pid to a new
process in seconds. Every row therefore carries its creation time, a child is linked to a
parent only when it was created after that parent, and :func:`terminate` opens the
process, checks the creation time *through that handle*, and terminates the same handle --
so the process checked is the process ended, with no window between.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

TICK_SLACK = 10_000
"""How far apart two readings of one creation time may be, in 100ns ticks (1 ms).

WMI reports ``CreationDate`` to the microsecond and ``GetProcessTimes`` to 100ns, so the
same process reads a few ticks apart through the two. A recycled pid is created seconds
later at the least, so a millisecond never hides one.
"""

AGENT_IMAGES = frozenset(
    {
        "python.exe",
        "node.exe",
        "claude.exe",
        "git.exe",
        "bash.exe",
        "sh.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "conhost.exe",
        "npm.exe",
        "uv.exe",
        "python",
        "node",
        "claude",
        "git",
        "bash",
        "sh",
    }
)
"""The images a run is made of. Command lines are read for these and nothing else."""


@dataclass(frozen=True)
class Proc:
    """One process as the OS reported it. ``created`` is FILETIME ticks (0 when unknown)."""

    pid: int
    ppid: int
    created: int
    name: str = ""
    cmdline: str = ""
    working_set: int = 0
    private_bytes: int = 0


# ----- reading the machine -------------------------------------------------------------

_WINDOWS_TABLE_SCRIPT = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$w = @(" + ", ".join(f"'{name}'" for name in sorted(AGENT_IMAGES)) + "); "
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$k = $w -contains $_.Name.ToLower(); "
    "[pscustomobject]@{ p = $_.ProcessId; q = $_.ParentProcessId; n = $_.Name; "
    "t = $(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }); "
    "w = $_.WorkingSetSize; v = $_.PrivatePageCount; "
    "c = $(if ($k) { $_.CommandLine } else { '' }) } } | ConvertTo-Json -Compress"
)


def process_table() -> List[Proc]:
    """Every process on the machine, with creation time, memory and (for agent images)
    command line. One CIM query on Windows, ``/proc`` elsewhere. Raises ``OSError`` when
    the machine will not say, so a caller can tell "nothing there" from "could not look".
    """
    if os.name == "nt":
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TABLE_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise OSError(f"Win32_Process query failed: {(completed.stderr or '').strip()[:300]}")
        loaded = json.loads(completed.stdout)
        if isinstance(loaded, dict):
            loaded = [loaded]
        return [
            Proc(
                pid=int(item.get("p") or 0),
                ppid=int(item.get("q") or 0),
                created=int(item.get("t") or 0),
                name=str(item.get("n") or ""),
                cmdline=str(item.get("c") or ""),
                working_set=int(item.get("w") or 0),
                private_bytes=int(item.get("v") or 0),
            )
            for item in loaded
            if isinstance(item, dict)
        ]
    return _proc_table()


def _proc_table() -> List[Proc]:  # pragma: no cover - Windows is the reference platform
    rows: List[Proc] = []
    page = getattr(os, "sysconf")("SC_PAGE_SIZE")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as handle:
                stat = handle.read()
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            with open(f"/proc/{entry}/statm", encoding="ascii") as handle:
                statm = handle.read().split()
        except OSError:
            continue
        name = stat[stat.find("(") + 1 : stat.rfind(")")]
        fields = stat.rsplit(")", 1)[-1].split()
        rows.append(
            Proc(
                pid=int(entry),
                ppid=int(fields[1]),
                created=int(fields[19]),
                name=name,
                cmdline=cmdline if name in AGENT_IMAGES else "",
                working_set=int(statm[1]) * page,
                private_bytes=(int(statm[1]) - int(statm[2])) * page,
            )
        )
    return rows


# ----- the tree ------------------------------------------------------------------------

SESSION_FLAG = re.compile(r"--session-id\s+\"?([0-9a-fA-F-]{8,})")


def session_roots(table: Sequence[Proc], short_id: str) -> List[Proc]:
    """The processes that *are* this session: the pty host and ``claude.exe``.

    Both carry ``--session-id <uuid>`` on their command line, and the short id a run
    record holds is the first eight characters of that uuid (observed 2026-09-23).
    """
    wanted = (short_id or "").strip().lower()
    if not wanted:
        return []
    found = []
    for row in table:
        match = SESSION_FLAG.search(row.cmdline or "")
        if match and match.group(1).lower().startswith(wanted):
            found.append(row)
    return found


def descendants(table: Sequence[Proc], roots: Iterable[Proc]) -> List[Proc]:
    """Everything below ``roots`` in ``table``, roots excluded, linked safely.

    A row is a child of ``parent`` when its ``ppid`` is the parent's pid **and** it was
    created no earlier than the parent -- a process cannot predate its own parent, so an
    older row with that ppid belongs to an earlier holder of the number. And when a live
    row holds the parent's pid with a *different* creation time, the number has been
    handed on, so only rows created before that new holder can be the old parent's.
    """
    by_pid: Dict[int, Proc] = {row.pid: row for row in table}
    children: Dict[int, List[Proc]] = {}
    for row in table:
        children.setdefault(row.ppid, []).append(row)

    seen: Set[tuple[int, int]] = set()
    out: List[Proc] = []
    frontier = list(roots)
    for root in frontier:
        seen.add((root.pid, root.created))
    while frontier:
        parent = frontier.pop()
        holder = by_pid.get(parent.pid)
        reused_at = (
            holder.created
            if holder is not None and not same_creation(holder.created, parent.created)
            else None
        )
        for child in children.get(parent.pid, []):
            key = (child.pid, child.created)
            if key in seen or child.pid == parent.pid:
                continue
            if parent.created and child.created and child.created + TICK_SLACK < parent.created:
                continue
            if reused_at is not None and child.created >= reused_at:
                continue
            seen.add(key)
            out.append(child)
            frontier.append(child)
    return out


def same_creation(a: int, b: int) -> bool:
    return abs(int(a) - int(b)) <= TICK_SLACK


# ----- ending a process ----------------------------------------------------------------


def terminate(proc: Proc) -> bool:
    """End ``proc`` if, and only if, the process holding its pid is still that process.

    Returns whether it was ended. ``False`` is the ordinary answer for a process that has
    already gone, and for one whose pid now belongs to a stranger.
    """
    if proc.pid <= 0 or not proc.created:
        return False
    if os.name != "nt":  # pragma: no cover - Windows is the reference platform
        return _terminate_posix(proc)
    import ctypes

    kernel32 = getattr(ctypes, "windll").kernel32
    access = 0x0001 | 0x1000  # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, proc.pid)
    if not handle:
        return False
    try:
        created = ctypes.c_ulonglong()
        ignored = [ctypes.c_ulonglong() for _ in range(3)]
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(ignored[0]),
            ctypes.byref(ignored[1]),
            ctypes.byref(ignored[2]),
        ):
            return False
        if not same_creation(created.value, proc.created):
            return False
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _terminate_posix(proc: Proc) -> bool:  # pragma: no cover
    import signal

    try:
        with open(f"/proc/{proc.pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
        if int(fields[19]) != proc.created:
            return False
        os.kill(proc.pid, getattr(signal, "SIGKILL"))
        return True
    except (OSError, ValueError, IndexError):
        return False


@dataclass
class ReapResult:
    """What :func:`reap` ended, and what it could not."""

    ended: List[Proc]
    survived: List[Proc]
    error: str = ""

    def sentence(self) -> str:
        if self.error:
            return f"its leftover processes were not checked: {self.error}"
        if not self.ended and not self.survived:
            return "it left no process behind"
        counts: Dict[str, int] = {}
        for row in self.ended:
            counts[row.name] = counts.get(row.name, 0) + 1
        named = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        text = f"ended {len(self.ended)} process(es) it left behind ({named})" if named else ""
        if self.survived:
            pids = ", ".join(str(row.pid) for row in self.survived[:10])
            gap = f"{len(self.survived)} could not be ended (pid {pids})"
            text = f"{text}; {gap}" if text else gap
        return text


def reap(
    snapshot: Sequence[Proc],
    *,
    roots: Sequence[Proc] = (),
    table: Callable[[], List[Proc]] = process_table,
    end: Callable[[Proc], bool] = terminate,
    wait_seconds: float = 10.0,
    poll_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> ReapResult:
    """End what ``snapshot`` names that is still alive, plus anything it started since.

    ``roots`` are the session's own processes. They are **not** ended here -- ``claude
    stop`` owns them and preserves the conversation doing it -- but they are waited for,
    up to ``wait_seconds``, so a ``git commit`` a graceful stop let finish is not cut off
    by a kill that arrived first.
    """
    if not snapshot and not roots:
        return ReapResult([], [])
    try:
        current = table()
        deadline = wait_seconds
        while roots and deadline > 0 and _any_alive(current, roots):
            sleep(poll_seconds)
            deadline -= poll_seconds
            current = table()
        alive = [row for row in snapshot if _alive_in(current, row)]
        # Anything started since the snapshot, by a snapshotted process or by a root.
        later = descendants(current, [*session_workers(roots), *snapshot])
        known = {(row.pid, row.created) for row in alive}
        targets = alive + [row for row in later if (row.pid, row.created) not in known]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ReapResult([], [], error=str(exc)[:200])

    ended: List[Proc] = []
    survived: List[Proc] = []
    # Leaves first, so a parent that respawns a dying child is gone before the child is.
    for row in sorted(targets, key=lambda proc: proc.created, reverse=True):
        (ended if end(row) else survived).append(row)
    if survived:
        try:
            after = table()
            survived = [row for row in survived if _alive_in(after, row)]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return ReapResult(ended, survived)


def _alive_in(table: Sequence[Proc], proc: Proc) -> bool:
    return any(row.pid == proc.pid and same_creation(row.created, proc.created) for row in table)


def _any_alive(table: Sequence[Proc], rows: Sequence[Proc]) -> bool:
    return any(_alive_in(table, row) for row in rows)


def session_tree(
    short_id: Optional[str], *, table: Callable[[], List[Proc]] = process_table
) -> tuple[List[Proc], List[Proc], str]:
    """``(roots, descendants, error)`` for a session, read now. Never raises."""
    if not short_id:
        return [], [], "no session id recorded"
    try:
        rows = table()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [], [], str(exc)[:200]
    roots = session_roots(rows, short_id)
    return roots, descendants(rows, session_workers(roots)), ""


def session_workers(roots: Sequence[Proc]) -> List[Proc]:
    """The roots whose children are the run's work: ``claude.exe``, not its pty host.

    The host's only other child is the headless ``conhost`` that serves it, which goes
    when the host does and is not the host's to lose early.
    """
    return [row for row in roots if "--bg-pty-host" not in (row.cmdline or "")]


__all__ = [
    "AGENT_IMAGES",
    "Proc",
    "ReapResult",
    "descendants",
    "process_table",
    "reap",
    "same_creation",
    "session_roots",
    "session_tree",
    "session_workers",
    "terminate",
]
