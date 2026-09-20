"""How many model calls this machine has made in the last hour.

**A separate counter from the dispatch budgets, on purpose** (design §5). The per-task
and machine-hourly caps in :mod:`agentjobs.dispatch.budget` count *runs* -- unattended
processes in a working tree -- and exist to bound a runaway agent. A person tidying a
backlog on their phone would exhaust that budget in a few minutes of drafting, which
inverts what the counter is for. So a drafting call consumes no run slot and is counted
here instead, machine-wide, against ``calls_per_hour`` from ``model.yaml``.

The store is a JSON file of timestamps beside the rest of the machine's state. It is not
the run ledger and it is not the task database: a drafting call is not an event anybody
resumes from, so what is kept is the minimum a rolling window needs and nothing about
what was drafted, for whom, or into which project.

**Pruned on every write**, so the file is bounded by the cap rather than by uptime.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from agentjobs.projects import default_home

LEDGER_FILENAME = "model-calls.json"
WINDOW = timedelta(hours=1)


def _home(home: Optional[Path]) -> Path:
    return Path(home).expanduser().resolve() if home else default_home()


def ledger_path(home: Optional[Path] = None) -> Path:
    """Where the rolling window of call timestamps is kept."""
    return _home(home) / LEDGER_FILENAME


def _read(path: Path) -> List[datetime]:
    """Every recorded timestamp, with anything unreadable discarded rather than raised.

    A corrupt counter must not be able to stop a person filing a task. The cost of
    discarding is that a truncated file under-counts for at most one window; the cost of
    raising would be a feature that breaks because a JSON file was half-written during a
    power cut, which is the worse failure by a distance.
    """
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    stamps: List[datetime] = []
    for item in loaded:
        if not isinstance(item, str):
            continue
        try:
            parsed = datetime.fromisoformat(item)
        except ValueError:
            continue
        stamps.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
    return stamps


def _write(path: Path, stamps: List[datetime]) -> None:
    """Replace the file atomically, so a reader never sees a half-written list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([stamp.isoformat() for stamp in stamps])
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name, suffix=".tmp", delete=False
    )
    try:
        with handle as out:
            out.write(payload)
        os.replace(handle.name, path)
    except OSError:
        # The counter is a bound, not a record. Failing to persist one tick must not
        # fail the draft the person is waiting for; the window simply forgets it.
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def calls_in_last_hour(home: Optional[Path] = None, *, now: Optional[datetime] = None) -> int:
    """How many calls this machine has made inside the rolling window."""
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - WINDOW
    return sum(1 for stamp in _read(ledger_path(home)) if stamp > cutoff)


def within_cap(
    cap: int, home: Optional[Path] = None, *, now: Optional[datetime] = None
) -> bool:
    """Whether one more call is permitted under ``cap``."""
    return calls_in_last_hour(home, now=now) < cap


def record_call(home: Optional[Path] = None, *, now: Optional[datetime] = None) -> int:
    """Count one call and return the new total inside the window.

    **Called before the request goes out, not after it comes back.** A provider that
    refuses or times out has still been asked, so counting the answer rather than the
    question would let a failing loop run without limit -- which is the one thing a cap
    exists to stop.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - WINDOW
    path = ledger_path(home)
    kept = [stamp for stamp in _read(path) if stamp > cutoff]
    kept.append(moment)
    _write(path, kept)
    return len(kept)
