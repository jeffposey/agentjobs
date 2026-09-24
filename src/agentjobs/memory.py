"""How much memory the machine has left, and a census of where it went (task-548).

On 2026-09-23 the dispatch machine lost about 30 GB over an afternoon of dispatched work.
pytest went from about 180s a gate to 1,000-1,900s, children were killed for low memory,
and the owner rebooted -- which cleared whatever held it before anybody had looked. So
the cause stayed a guess, and nothing on any gate's output had pointed at memory.

Two tools, for the two halves of that:

- :func:`read_memory` is cheap -- two Win32 calls, milliseconds -- so the gate samples it
  at every stage boundary and says in words when a stage ran below :func:`floor_mb`.
- :func:`capture` is the census: the system's commit and pool counters, and every agent
  process with its parent, whether that parent is alive, its memory, and the run or
  worktree it belongs to. It is written under ``~/.agentjobs/memory/<UTC stamp>/`` by
  ``agentjobs memory census``, and by itself -- from the gate and from the poller --
  whenever free memory falls below the floor, at most once per
  :data:`AUTO_CAPTURE_INTERVAL_SECONDS`.

**RAMMap is not scripted.** It splits physical memory more finely than any counter here
(mapped file, driver-locked, per-list standby), and is the right tool when the census
says the memory is not in processes. It is a manual step: Sysinternals RAMMap, File >
Save, into the same capture directory. The census's pool and standby counters are what
say whether that step is needed -- a large nonpaged pool with small process totals is a
driver, not us.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

FLOOR_ENV = "AGENTJOBS_MEMORY_FLOOR_MB"
DEFAULT_FLOOR_MB = 8192
"""8 GB available (standby included), or 8 GB of commit headroom.

A fresh boot of the dispatch machine leaves about 33 GB available with runs going, and a
whole gate peaks at 5.7 GB of its own, so neither trips it. It is the point below which
a gate of that size is competing with the machine rather than with its neighbours.
"""

WATCH_ENV = "AGENTJOBS_MEMORY_WATCH"
"""``off`` disables the automatic census. The suite sets it; nothing else should."""

AUTO_CAPTURE_INTERVAL_SECONDS = 30 * 60
MB = 1024 * 1024


@dataclass(frozen=True)
class MemoryState:
    """The machine's memory, in MB. ``None`` where the platform does not say."""

    available_mb: int
    total_mb: int
    commit_mb: Optional[int] = None
    commit_limit_mb: Optional[int] = None
    paged_pool_mb: Optional[int] = None
    nonpaged_pool_mb: Optional[int] = None
    system_cache_mb: Optional[int] = None

    @property
    def commit_headroom_mb(self) -> Optional[int]:
        if self.commit_mb is None or self.commit_limit_mb is None:
            return None
        return self.commit_limit_mb - self.commit_mb

    def low(self, floor: int) -> bool:
        """Below the floor on physical memory or on commit headroom."""
        headroom = self.commit_headroom_mb
        return self.available_mb < floor or (headroom is not None and headroom < floor)

    def describe(self) -> str:
        text = f"{gb(self.available_mb)} available of {gb(self.total_mb)}"
        if self.commit_mb is not None and self.commit_limit_mb is not None:
            text += f"; commit {gb(self.commit_mb)} of {gb(self.commit_limit_mb)}"
        return text


def gb(mb: Optional[int]) -> str:
    return "?" if mb is None else f"{mb / 1024:.1f} GB"


def floor_mb(environ: Optional[Mapping[str, str]] = None) -> int:
    """The floor, from :data:`FLOOR_ENV` when it holds a positive number of MB."""
    raw = (environ if environ is not None else os.environ).get(FLOOR_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FLOOR_MB
    return value if value > 0 else DEFAULT_FLOOR_MB


def read_memory() -> Optional[MemoryState]:
    """The machine's memory now, or ``None`` when it cannot be read. Never raises."""
    try:
        if os.name == "nt":
            return _read_windows()
        return _read_proc()
    except Exception:  # noqa: BLE001 - a reading is instrumentation, never a failure
        return None


def _read_windows() -> Optional[MemoryState]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    windll = getattr(ctypes, "windll")
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    state = MemoryState(
        available_mb=int(status.ullAvailPhys // MB),
        total_mb=int(status.ullTotalPhys // MB),
    )
    info = PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
    if not windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return state
    page = int(info.PageSize)

    def pages(value: int) -> int:
        return int(value) * page // MB

    return MemoryState(
        available_mb=state.available_mb,
        total_mb=state.total_mb,
        commit_mb=pages(info.CommitTotal),
        commit_limit_mb=pages(info.CommitLimit),
        paged_pool_mb=pages(info.KernelPaged),
        nonpaged_pool_mb=pages(info.KernelNonpaged),
        system_cache_mb=pages(info.SystemCache),
    )


def _read_proc() -> Optional[MemoryState]:  # pragma: no cover - Windows is the reference
    values: Dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            values[name] = int(parts[0]) // 1024
    return MemoryState(
        available_mb=values.get("MemAvailable", values.get("MemFree", 0)),
        total_mb=values.get("MemTotal", 0),
        commit_mb=values.get("Committed_AS"),
        commit_limit_mb=values.get("CommitLimit"),
        system_cache_mb=values.get("Cached"),
    )


# ----- sampling across a stretch of work -----------------------------------------------


class Sampler:
    """The lowest reading inside each named window, sampled every ``interval`` seconds.

    A stage boundary alone would miss a dip in the middle of pytest, which is where the
    memory goes. Windows are named so stages that overlap -- ``--concurrent`` -- each get
    their own lowest. The thread is a daemon and every reading is swallowed, so a sampler
    can never keep the gate alive or fail it.
    """

    def __init__(
        self,
        *,
        interval: float = 5.0,
        reader: Callable[[], Optional[MemoryState]] = read_memory,
    ) -> None:
        self.interval = interval
        self.reader = reader
        self._lock = threading.Lock()
        self._lowest: Dict[str, Optional[MemoryState]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Sampler":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="memory-sampler", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def read(self) -> Optional[MemoryState]:
        """Read now, folding the reading into every open window's lowest."""
        try:
            state = self.reader()
        except Exception:  # noqa: BLE001
            return None
        if state is not None:
            with self._lock:
                for name, lowest in self._lowest.items():
                    if lowest is None or state.available_mb < lowest.available_mb:
                        self._lowest[name] = state
        return state

    def open(self, name: str) -> Optional[MemoryState]:
        """Start a window and return the reading it starts on."""
        with self._lock:
            self._lowest[name] = None
        return self.read()

    def close(self, name: str) -> tuple[Optional[MemoryState], Optional[MemoryState]]:
        """End a window: ``(reading now, lowest inside it)``."""
        state = self.read()
        with self._lock:
            lowest = self._lowest.pop(name, None)
        return state, lowest

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.read()


# ----- the census ----------------------------------------------------------------------

WORKTREE = re.compile(r"[\\/]worktrees[\\/]([A-Za-z0-9._-]+)")
VIRTUALENV = re.compile(r"virtualenvs[\\/]([A-Za-z0-9._-]+)")


@dataclass
class CensusRow:
    pid: int
    ppid: int
    name: str
    parent_alive: bool
    working_set_mb: float
    private_mb: float
    session: Optional[str] = None
    run_id: Optional[str] = None
    worktree: Optional[str] = None
    cmdline: str = ""


@dataclass
class Census:
    taken_at: str
    memory: Optional[Dict[str, Any]]
    counters: Dict[str, Any] = field(default_factory=dict)
    rows: List[CensusRow] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        """What a reader needs from the census, in a dozen lines."""
        lines = [f"Memory census {self.taken_at}"]
        if self.memory:
            state = MemoryState(**self.memory)
            lines.append(f"  {state.describe()}")
            lines.append(
                f"  kernel pool: paged {gb(state.paged_pool_mb)}, "
                f"nonpaged {gb(state.nonpaged_pool_mb)}; system cache {gb(state.system_cache_mb)}"
            )
        for name, value in sorted(self.counters.items()):
            lines.append(f"  {name}: {value}")
        if self.error:
            lines.append(f"  processes not read: {self.error}")
            return "\n".join(lines)
        orphans = [row for row in self.rows if not row.parent_alive]
        total = sum(row.private_mb for row in self.rows)
        lines.append(
            f"  {len(self.rows)} agent processes, {total / 1024:.1f} GB private; "
            f"{len(orphans)} with a dead parent "
            f"({sum(row.private_mb for row in orphans) / 1024:.1f} GB)"
        )
        by_owner: Dict[str, List[CensusRow]] = {}
        for row in self.rows:
            owner = (
                row.run_id
                or (f"session {row.session}" if row.session else None)
                or (f"worktree {row.worktree}" if row.worktree else None)
                or ("no live parent" if not row.parent_alive else "not a session's")
            )
            by_owner.setdefault(owner, []).append(row)
        for owner, rows in sorted(
            by_owner.items(), key=lambda item: -sum(r.private_mb for r in item[1])
        )[:12]:
            size = sum(r.private_mb for r in rows) / 1024
            dead = sum(1 for r in rows if not r.parent_alive)
            lines.append(f"    {owner}: {len(rows)} processes, {size:.2f} GB, {dead} orphaned")
        return "\n".join(lines)


def _counters() -> Dict[str, Any]:
    """Standby, modified and pool figures RAMMap would show, from the perf counters."""
    if os.name != "nt":
        return {}
    script = (
        "Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | Select-Object "
        "AvailableMBytes, CommittedBytes, CommitLimit, PoolNonpagedBytes, PoolPagedBytes, "
        "StandbyCacheNormalPriorityBytes, StandbyCacheReserveBytes, "
        "StandbyCacheCoreBytes, ModifiedPageListBytes, FreeAndZeroPageListBytes, "
        "CacheBytes | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        loaded = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {key: _counter_text(key, value) for key, value in loaded.items() if value is not None}


def _counter_text(key: str, value: Any) -> Any:
    if key == "AvailableMBytes":
        return f"{int(value) / 1024:.2f} GB"
    if key.endswith("Bytes") or key == "CommitLimit":
        return f"{int(value) / MB / 1024:.2f} GB"
    return value


def take_census(
    home: Path,
    *,
    table: Optional[Callable[[], Sequence[Any]]] = None,
    reader: Callable[[], Optional[MemoryState]] = read_memory,
    counters: Callable[[], Dict[str, Any]] = _counters,
    sessions: Optional[Mapping[str, str]] = None,
) -> Census:
    """Read the machine: counters, and every agent process with its owner."""
    from agentjobs.dispatch import proctree

    state = reader()
    census = Census(
        taken_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        memory=asdict(state) if state is not None else None,
        counters=counters(),
    )
    try:
        rows = list((table or proctree.process_table)())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        census.error = str(exc)[:300]
        return census

    by_pid = {row.pid: row for row in rows}
    runs = dict(sessions) if sessions is not None else _run_sessions(home)

    def parent_alive(row: Any) -> bool:
        parent = by_pid.get(row.ppid)
        return parent is not None and (
            not parent.created or not row.created or parent.created <= row.created
        )

    def session_of(row: Any) -> Optional[str]:
        seen = 0
        current = row
        while current is not None and seen < 64:
            match = proctree.SESSION_FLAG.search(current.cmdline or "")
            if match:
                return match.group(1)[:8].lower()
            if not parent_alive(current):
                return None
            current = by_pid.get(current.ppid)
            seen += 1
        return None

    for row in rows:
        if (row.name or "").lower() not in proctree.AGENT_IMAGES:
            continue
        session = session_of(row)
        worktree = WORKTREE.search(row.cmdline or "")
        census.rows.append(
            CensusRow(
                pid=row.pid,
                ppid=row.ppid,
                name=row.name,
                parent_alive=parent_alive(row),
                working_set_mb=round(row.working_set / MB, 1),
                private_mb=round(row.private_bytes / MB, 1),
                session=session,
                run_id=runs.get(session) if session else None,
                worktree=worktree.group(1) if worktree else None,
                cmdline=(row.cmdline or "")[:400],
            )
        )
    census.rows.sort(key=lambda row: -row.private_mb)
    return census


def _run_sessions(home: Path) -> Dict[str, str]:
    """Short session id -> run id, for every run this home has recorded."""
    try:
        from agentjobs.dispatch.ledger import list_runs

        return {record.session_id: record.run_id for record in list_runs(home) if record.session_id}
    except Exception:  # noqa: BLE001 - attribution is a nicety; the census still stands
        return {}


def captures_root(home: Path) -> Path:
    return Path(home) / "memory"


def capture(home: Path, *, reason: str = "by command", **census_kwargs: Any) -> Path:
    """Take a census and write it: ``census.json`` and ``summary.txt``. Returns the dir."""
    census = take_census(home, **census_kwargs)
    directory = captures_root(home) / census.taken_at.replace(":", "")
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "taken_at": census.taken_at,
        "reason": reason,
        "memory": census.memory,
        "counters": census.counters,
        "error": census.error,
        "processes": [asdict(row) for row in census.rows],
        "rammap": (
            "not scripted: open Sysinternals RAMMap and File > Save into this directory "
            "when the processes above do not account for the memory"
        ),
    }
    (directory / "census.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (directory / "summary.txt").write_text(
        f"{census.summary()}\n  reason: {reason}\n", encoding="utf-8"
    )
    return directory


def latest_capture(home: Path) -> Optional[Path]:
    root = captures_root(home)
    if not root.is_dir():
        return None
    found = sorted(path for path in root.iterdir() if (path / "census.json").is_file())
    return found[-1] if found else None


def capture_if_low(
    home: Path,
    *,
    state: Optional[MemoryState] = None,
    reason: str,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
    **census_kwargs: Any,
) -> Optional[Path]:
    """A census when memory is below the floor and none was taken recently. Never raises.

    "Recently" is read from the captures on disk rather than held in memory, so the gate,
    the poller and a second server all share one throttle.
    """
    env = environ if environ is not None else os.environ
    if env.get(WATCH_ENV, "").strip().lower() in {"off", "0", "false", "no"}:
        return None
    try:
        state = state or read_memory()
        if state is None or not state.low(floor_mb(env)):
            return None
        last = latest_capture(home)
        moment = time.time() if now is None else now
        if last is not None:
            age = moment - (last / "census.json").stat().st_mtime
            if age < AUTO_CAPTURE_INTERVAL_SECONDS:
                return None
        return capture(home, reason=f"{reason}: {state.describe()}", **census_kwargs)
    except Exception:  # noqa: BLE001 - a census must never take down its caller
        return None


__all__ = [
    "AUTO_CAPTURE_INTERVAL_SECONDS",
    "Census",
    "CensusRow",
    "DEFAULT_FLOOR_MB",
    "FLOOR_ENV",
    "MemoryState",
    "Sampler",
    "WATCH_ENV",
    "capture",
    "capture_if_low",
    "captures_root",
    "floor_mb",
    "gb",
    "latest_capture",
    "read_memory",
    "take_census",
]
