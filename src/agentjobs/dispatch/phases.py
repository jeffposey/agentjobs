"""Phase records: what a dispatched run spent its time on, written down as it happens.

``meta.yaml`` says when a run started and when it stopped. Nothing said what happened
in between, and the only other artefact -- ``transcript.log`` -- is a raw TTY capture
full of ANSI escapes and screen redraws, so the same line appears many times and no
phase attribution survives it. Grepping it for gate invocations returns counts that are
artefacts of terminal repainting. Measuring an hour-long run therefore meant a person
reading a transcript, which is why nobody did it (task-233).

This is the cheapest thing that fixes that: one JSON object per line, appended to
``phases.jsonl`` in the run's own directory, beside ``meta.yaml``. No service, no
database, no daemon. ``scripts/run_report.py`` reads it.

Two properties are deliberate.

**Writing is never allowed to break the thing being measured.** Every failure mode --
no run directory, an unwritable disk, a value that will not serialise -- is swallowed.
A run that loses a phase line is a gap in a report; a run that dies because it could not
write one is a lost hour of work.

**The producer does not have to know it is being measured.** ``record_phase_from_env``
returns ``None`` and writes nothing when the environment says this is not a dispatched
run, so ``scripts/check.py`` calls it unconditionally and a developer running the gate
by hand pays a dictionary lookup. Dispatch sets the two variables when it spawns
(``runner.RunLaunch._environment``), and a child process of the agent -- the gate, the
CLI, the MCP server -- inherits them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RUN_ID_ENV = "AGENTJOBS_RUN_ID"
"""The run a process belongs to, set by dispatch on the session it spawns."""

RUN_DIR_ENV = "AGENTJOBS_RUN_DIR"
"""That run's directory. Passed explicitly rather than re-derived from the home.

A child does not necessarily resolve ``AGENTJOBS_HOME`` the way the dispatcher did --
the gate scrubs parts of the environment, tests point the home at a temp directory --
and a phase line written into the wrong run is worse than one not written at all.
"""

PHASES_FILENAME = "phases.jsonl"


def phases_path(directory: Path) -> Path:
    """Where a run's phase records live."""
    return directory / PHASES_FILENAME


def record_phase(directory: Path, kind: str, **fields: Any) -> Optional[Path]:
    """Append one phase record to a run directory. Returns the file, or None on failure.

    ``kind`` names what happened -- ``gate_started``, ``gate_finished``. Anything else
    passed becomes a field on the record. ``ts`` is stamped here, in UTC, so records from
    different processes in the same run are comparable.
    """
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **fields,
    }
    try:
        line = json.dumps(record, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str takes almost all
        return None
    path = phases_path(directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One append of one short line. Appends of a few hundred bytes do not interleave
        # in practice, and the reader tolerates a torn line regardless.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return None
    return path


def current_run() -> Optional[Path]:
    """The run directory this process belongs to, if it belongs to one."""
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_dir:
        return None
    directory = Path(run_dir)
    return directory if directory.is_dir() else None


def record_phase_from_env(kind: str, **fields: Any) -> Optional[Path]:
    """Record a phase if this process is part of a dispatched run; otherwise do nothing.

    This is the entry point every producer should use. It makes instrumentation free to
    add: a caller does not branch on whether it is dispatched, and a developer running
    the same code by hand is unaffected.
    """
    directory = current_run()
    if directory is None:
        return None
    run_id = os.environ.get(RUN_ID_ENV)
    if run_id:
        fields.setdefault("run_id", run_id)
    return record_phase(directory, kind, **fields)


def read_phases(directory: Path) -> List[Dict[str, Any]]:
    """Every phase record in a run directory, in the order it was written.

    A line that will not parse is skipped rather than raising. These files are appended
    to by several processes over the life of a run, and a report that refuses to render
    because one line is torn is less useful than one that renders the rest.
    """
    path = phases_path(directory)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records
