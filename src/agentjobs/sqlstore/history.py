"""Finish and gate history rows: the SQL behind ``finish``, ``finish_step``, ``gate_run``
and ``gate_stage`` (task-472, analytics design section 20).

The rows are an index over files. A finish writes ``meta.yaml`` and ``phases.jsonl``
under the home directory's ``finishes/``; a gate appends to whichever run's
``phases.jsonl`` it is inside. Those stay, and remain what ``finish_status.py`` reads.
What is here is what a page can query without parsing a directory per request.

Two writers reach these functions and they are told apart by ``source``. The finisher
and the gate write ``native`` rows as they run, and a native write is an upsert: the
finish row is rewritten with every ``meta.yaml`` write, and steps and stages are replaced
by sequence number. The one-time import of the directories on disk writes ``imported``
rows, and **an imported write never overwrites a row that exists**, whatever wrote it.
That is what makes the import re-runnable, and what stops a re-run of it clobbering a
finish the finisher has since recorded itself.

A finish whose task is not in this project is refused rather than inserted with the
foreign key off (section 20.4). A gate belongs to nothing -- ``gate_run`` has no foreign
key by design -- so nothing refuses it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .connection import Database

NATIVE = "native"
IMPORTED = "imported"

EXISTS = "exists"
UNKNOWN_TASK = "unknown_task"


@dataclass(frozen=True)
class HistoryWrite:
    """What one write did: whether a row landed, and if not, why."""

    written: bool
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "reason": self.reason}


_FINISH_COLUMNS = (
    "task_id",
    "started_at",
    "finished_at",
    "seconds",
    "outcome",
    "reason",
    "stopped_at",
    "merged",
    "merge_commit",
    "run_id",
    "dispatched_run_id",
    "authority",
    "source",
)

_GATE_COLUMNS = (
    "origin",
    "finish_id",
    "run_id",
    "task_id",
    "scope",
    "tree",
    "checkout",
    "branch",
    "started_at",
    "finished_at",
    "seconds",
    "passed",
    "failed_stage",
    "stages_run",
    "stages_total",
    "source",
)


def _flag(value: Any) -> Optional[int]:
    """A boolean-ish value as the 0/1 the CHECK constraints accept; None stays None."""
    if value is None:
        return None
    return 1 if value else 0


def _exists(
    connection: sqlite3.Connection, table: str, key: str, project_id: str, value: str
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE project_id = ? AND {key} = ?", (project_id, value)
    ).fetchone()
    return row is not None


def _task_exists(connection: sqlite3.Connection, project_id: str, task_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM task WHERE project_id = ? AND task_id = ?", (project_id, task_id)
    ).fetchone()
    return row is not None


def upsert_finish(
    database: Database,
    project_id: str,
    finish_id: str,
    record: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]] = (),
) -> HistoryWrite:
    """Write one finish and its steps, replacing what a native writer wrote before.

    ``record`` carries the ``finish`` columns by name (``project_id`` and ``finish_id``
    come from the arguments). ``steps`` carry ``seq``, ``step``, ``ok``, ``skipped``,
    ``seconds``, ``detail`` and ``ts``; a step already stored under the same ``seq`` is
    replaced. Steps and the finish row commit together.
    """
    source = record.get("source") or NATIVE
    with database.write() as connection:
        if source == IMPORTED and _exists(connection, "finish", "finish_id", project_id, finish_id):
            return HistoryWrite(False, EXISTS)
        task_id = str(record.get("task_id") or "")
        if not _task_exists(connection, project_id, task_id):
            return HistoryWrite(False, UNKNOWN_TASK)
        values = {column: record.get(column) for column in _FINISH_COLUMNS}
        values["merged"] = _flag(values["merged"]) or 0
        values["source"] = source
        assignments = ", ".join(f"{column} = excluded.{column}" for column in _FINISH_COLUMNS)
        placeholders = ", ".join("?" for _ in _FINISH_COLUMNS)
        connection.execute(
            f"INSERT INTO finish(project_id, finish_id, {', '.join(_FINISH_COLUMNS)}) "
            f"VALUES (?, ?, {placeholders}) "
            f"ON CONFLICT(project_id, finish_id) DO UPDATE SET {assignments}",
            (project_id, finish_id, *(values[column] for column in _FINISH_COLUMNS)),
        )
        for step in steps:
            connection.execute(
                "INSERT OR REPLACE INTO finish_step(project_id, finish_id, seq, step, ok, "
                "skipped, seconds, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    finish_id,
                    int(step["seq"]),
                    str(step["step"]),
                    _flag(step.get("ok")) or 0,
                    _flag(step.get("skipped")) or 0,
                    float(step.get("seconds") or 0.0),
                    step.get("detail"),
                    str(step["ts"]),
                ),
            )
    return HistoryWrite(True)


def upsert_gate_run(
    database: Database,
    project_id: str,
    gate_id: str,
    record: Mapping[str, Any],
    stages: Sequence[Mapping[str, Any]] = (),
) -> HistoryWrite:
    """Write one gate run and its stages, replacing what a native writer wrote before.

    Same contract as :func:`upsert_finish`, for ``gate_run`` and ``gate_stage``. Stages
    carry ``seq``, ``stage``, ``seconds``, ``passed``, ``started_at`` and
    ``finished_at``.
    """
    source = record.get("source") or NATIVE
    with database.write() as connection:
        if source == IMPORTED and _exists(connection, "gate_run", "gate_id", project_id, gate_id):
            return HistoryWrite(False, EXISTS)
        values = {column: record.get(column) for column in _GATE_COLUMNS}
        values["passed"] = _flag(values["passed"])
        values["source"] = source
        assignments = ", ".join(f"{column} = excluded.{column}" for column in _GATE_COLUMNS)
        placeholders = ", ".join("?" for _ in _GATE_COLUMNS)
        connection.execute(
            f"INSERT INTO gate_run(project_id, gate_id, {', '.join(_GATE_COLUMNS)}) "
            f"VALUES (?, ?, {placeholders}) "
            f"ON CONFLICT(project_id, gate_id) DO UPDATE SET {assignments}",
            (project_id, gate_id, *(values[column] for column in _GATE_COLUMNS)),
        )
        for stage in stages:
            connection.execute(
                "INSERT OR REPLACE INTO gate_stage(project_id, gate_id, seq, stage, seconds, "
                "passed, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    gate_id,
                    int(stage["seq"]),
                    str(stage["stage"]),
                    stage.get("seconds"),
                    _flag(stage.get("passed")),
                    str(stage["started_at"]),
                    stage.get("finished_at"),
                ),
            )
    return HistoryWrite(True)


def history_counts(connection: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """How many rows each history table holds for a project. For the operator and tests."""
    counts: dict[str, int] = {}
    for table in ("finish", "finish_step", "gate_run", "gate_stage"):
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
        ).fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


__all__ = [
    "EXISTS",
    "IMPORTED",
    "NATIVE",
    "UNKNOWN_TASK",
    "HistoryWrite",
    "history_counts",
    "upsert_finish",
    "upsert_gate_run",
]
