"""A machine-level journal of every process AgentJobs kills (task-561).

Flake register row 13 is a sibling dispatch that exits 1 with both streams empty: the
signature of ``taskkill /F``. Task-554 instrumented every kill site and ran nineteen
loaded gates without one sighting, so the row fires too rarely to hunt on demand. Its
evidence has to be written the moment it happens, in whichever gate it happens in.

So every kill AgentJobs makes appends to this file, and a victim can ask it who killed
it. **An empty answer is also evidence**: if nothing here names the victim, the killer
was not AgentJobs.

Each kill writes two lines. ``"phase": "kill"`` goes down *before* the act, naming the
site, the caller and the target -- before, because a caller can be inside the tree it
kills, and a line written afterwards would never be written. ``"phase": "ended"`` follows
the act where there is one to follow, carrying what the OS reported, which for
``taskkill /T`` is every pid it ended rather than only the one it was aimed at.

Three properties are the contract, and :func:`record` keeps all of them:

-   **It never fails a kill.** Every error is swallowed. A journal that could stop a kill
    would turn a diagnosis into a new failure.
-   **It is cheap.** One append per line, no lock, no read.
-   **It is bounded.** Past :data:`CAP_BYTES` the file is moved to ``.1``, replacing
    whatever was there, so at most two files' worth is kept.

``AGENTJOBS_KILL_JOURNAL`` overrides the location. The suite sets it once per run to a
file outside every test's home, so a kill in one test's process can be found by a victim
in another's -- which is the case row 13 is.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

JOURNAL_ENV = "AGENTJOBS_KILL_JOURNAL"
FILENAME = "kills.jsonl"
CAP_BYTES = 1_000_000
"""Rotation threshold. A kill line is a few hundred bytes, so this holds thousands."""

_ENDED = re.compile(r"process with PID (\d+)", re.IGNORECASE)
"""What ``taskkill`` says for each process it ended: ``SUCCESS: The process with PID 123
(child process of PID 45) has been terminated.`` The parent it names is not ended, so
only the first number counts. English output only; the raw text is kept regardless."""


def journal_path() -> Path:
    """Where kills are journalled: the override, or ``kills.jsonl`` in the home."""
    override = os.environ.get(JOURNAL_ENV)
    if override:
        return Path(override)
    from agentjobs.projects import default_home

    return default_home() / FILENAME


def ended_pids(output: str) -> List[int]:
    """Every pid ``taskkill`` reported ending, in the order it reported them."""
    return [int(match) for match in _ENDED.findall(output or "")]


def _caller() -> Dict[str, Any]:
    from agentjobs.dispatch.pids import process_identity

    pid = os.getpid()
    return {
        "pid": pid,
        "ppid": os.getppid(),
        "identity": process_identity(pid),
        "command": " ".join(sys.argv)[:400],
    }


def record(
    site: str,
    target_pid: int,
    *,
    phase: str = "kill",
    identity: Optional[str] = None,
    output: Optional[str] = None,
    returncode: Optional[int] = None,
    ended: Optional[Iterable[int]] = None,
) -> None:
    """Append one line. Never raises, whatever goes wrong."""
    try:
        line: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "site": site,
            "caller": _caller(),
            "target": {"pid": int(target_pid), "identity": identity},
        }
        if output is not None:
            line["output"] = output[-2000:]
            line["ended"] = ended_pids(output)
        if ended is not None:
            line["ended"] = sorted({*line.get("ended", []), *(int(pid) for pid in ended)})
        if returncode is not None:
            line["returncode"] = returncode
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > CAP_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        data = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - the journal must never fail a kill
        pass


def read(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every readable line, oldest file first. A torn or foreign line is skipped."""
    path = path or journal_path()
    lines: List[Dict[str, Any]] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                lines.append(parsed)
    return lines


def naming(
    pids: Iterable[int],
    *,
    since: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Lines that aimed at, or reported ending, any of ``pids`` at or after ``since``.

    ``since`` is what keeps a reused number from matching: a kill of the same pid before
    the victim existed was a kill of somebody else. A second of slack covers clock
    granularity, in the direction of showing a line rather than hiding one.
    """
    wanted = {int(pid) for pid in pids}
    floor = since - timedelta(seconds=1) if since is not None else None
    found = []
    for line in read(path):
        target = line.get("target") or {}
        named = {target.get("pid"), *(line.get("ended") or [])}
        if not wanted & named:
            continue
        if floor is not None:
            try:
                if datetime.fromisoformat(str(line.get("ts"))) < floor:
                    continue
            except ValueError:
                pass
        found.append(line)
    return found


def describe(
    victims: Dict[str, int],
    *,
    since: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> str:
    """A paragraph for a failed assertion: which AgentJobs kill, if any, named a victim.

    ``victims`` maps a role (``"interpreter"``, ``"launcher"``) to its pid.
    """
    path = path or journal_path()
    who = ", ".join(f"{role} pid {pid}" for role, pid in victims.items())
    lines = naming(victims.values(), since=since, path=path)
    if not lines:
        return (
            f"Kill journal {path}: no AgentJobs kill named {who}. If this process was "
            "killed, the killer was not AgentJobs (flake register row 13)."
        )
    body = "\n".join(json.dumps(line, sort_keys=True) for line in lines)
    return f"Kill journal {path}: {len(lines)} line(s) name {who}:\n{body}"
