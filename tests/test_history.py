"""Finish and gate history as rows (task-472, analytics design section 20).

What is guarded here: the migration makes the four tables and the run index; the same
derivation produces a finish's row whether the finisher writes it live or the import
reads it from disk; the gate's phase events assemble into one row and its stages,
including the stage that failed and the gate that was killed; an imported write never
overwrites a row; a finish for a task the project does not have is refused; the rows
reach the store over the service from a finish and from a gate in a worktree; and the
import is re-runnable with the second run writing nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from agentjobs import history
from agentjobs.client import TaskClient
from agentjobs.history import (
    FINISHES_DIRNAME,
    GateAssembler,
    GateHistory,
    finish_record,
    gate_origin,
    gates_from_phases,
    import_finishes,
    project_for_checkout,
    stamp,
    task_from_branch,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.remote_manager import RemoteTaskManager
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.sqlstore.connection import Database
from agentjobs.sqlstore.history import EXISTS, UNKNOWN_TASK, history_counts
from agentjobs.sqlstore.migrations import available, current_version, latest_version, upgrade

from support import task_store

ROOT = Path(__file__).resolve().parents[1]
STAGES = ["black", "ruff", "mypy", "api", "icons", "oxlint", "pytest", "vitest", "build", "e2e"]


# ----- fixtures -------------------------------------------------------------------------------


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "agentjobs.db")
    upgrade(db, agentjobs_version="test", snapshot_before=False)
    yield db
    db.close()


@pytest.fixture()
def store(database: Database) -> SqlTaskStore:
    task_store_ = SqlTaskStore(database, "demo")
    task_store_.ensure_project(root="C:/demo")
    return task_store_


@pytest.fixture()
def manager(store: SqlTaskStore) -> TaskManager:
    return TaskManager(store)


def make_task(manager: TaskManager, task_id: str = "task-001") -> str:
    task = manager.create_task(
        id=task_id,
        title="A task",
        description="Do the thing.",
        summary="A task.",
        lifecycle=Lifecycle.READY,
    )
    return task.id


def ts(minute: int, second: int = 0) -> str:
    return f"2026-09-19T12:{minute:02d}:{second:02d}+00:00"


def meta_for(task_id: str = "task-001", **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "finish_id": "fin_aaaaaaaa",
        "task_id": task_id,
        "project_id": "demo",
        "outcome": "finished",
        "started_at": ts(0),
        "finished_at": ts(6),
        "seconds": 360.0,
        "merge_commit": "abc123",
        "authority": "posture",
        "run_id": "run_11111111",
    }
    base.update(overrides)
    return base


def green_gate_events(prefix: str = "fin_aaaaaaaa", *, at: int = 1) -> List[Dict[str, Any]]:
    """The phase lines a full green gate writes, in order, as `record_phase` writes them."""
    events: List[Dict[str, Any]] = [
        {
            "ts": ts(at),
            "kind": "gate_started",
            "scope": "full",
            "stages": STAGES,
            "stages_total": 10,
            "tree": "798c8fcc7e7ae6aa",
            "run_id": prefix,
        }
    ]
    for index, stage in enumerate(STAGES, start=1):
        events.append(
            {"ts": ts(at, index), "kind": "gate_stage_started", "stage": stage, "index": index}
        )
        events.append(
            {
                "ts": ts(at, index + 1),
                "kind": "gate_stage_finished",
                "stage": stage,
                "index": index,
                "total": 10,
                "seconds": float(index),
            }
        )
    events.append(
        {
            "ts": ts(at + 5),
            "kind": "gate_finished",
            "scope": "full",
            "passed": True,
            "seconds": 300.0,
            "stages_run": 10,
            "stages_total": 10,
            "tree": "798c8fcc7e7ae6aa",
        }
    )
    return events


def finish_step_lines(finish_id: str) -> List[Dict[str, Any]]:
    steps = [
        ("preflight", True, "feat/task-001-thing at 75e664fb in C:/projects/worktrees/x-001", 0.9),
        ("runway", True, "taken immediately", 0.0),
        ("rebase", True, "already on main", 1.2),
        ("gate", True, "green", 300.0),
        ("merge", True, "merged as abc123", 0.4),
    ]
    return [
        {
            "ts": ts(0, index),
            "kind": "finish_step",
            "finish_id": finish_id,
            "step": step,
            "ok": ok,
            "skipped": False,
            "detail": detail,
            "seconds": seconds,
        }
        for index, (step, ok, detail, seconds) in enumerate(steps, start=1)
    ]


def write_finish_dir(
    home: Path, finish_id: str, meta: Dict[str, Any], lines: List[Dict[str, Any]]
) -> Path:
    directory = home / FINISHES_DIRNAME / finish_id
    directory.mkdir(parents=True)
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (directory / "phases.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return directory


def rows(database: Database, sql: str, *params: Any) -> List[Dict[str, Any]]:
    return [dict(row) for row in database.reader().execute(sql, params).fetchall()]


# ----- the migration ----------------------------------------------------------------------------


class TestMigration:
    def test_the_store_carries_the_four_tables_and_their_indexes(self, database: Database) -> None:
        # Against `latest_version()` rather than the literal 5 this used to name. The
        # number moved the first time an unrelated migration landed (006, task-506), and
        # a literal here says nothing about this migration -- what it is checking is that
        # 005's tables and indexes survive whatever is applied after them.
        assert current_version(database.writer) == latest_version()
        names = {
            row["name"]
            for row in database.reader().execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        for table in ("finish", "finish_step", "gate_run", "gate_stage"):
            assert table in names
        for index in (
            "ix_finish_started",
            "ix_finish_task",
            "ix_gate_started",
            "ix_gate_task",
            "ix_gate_stage_name",
            "ix_run_started",
        ):
            assert index in names

    def test_the_constraints_refuse_a_row_outside_the_vocabulary(self, store: SqlTaskStore) -> None:
        import sqlite3

        make_task(TaskManager(store))
        with pytest.raises(sqlite3.IntegrityError):
            store.record_finish(
                "fin_bad", {"task_id": "task-001", "started_at": ts(0), "outcome": "exploded"}
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_gate_run(
                "man_bad", {"origin": "finish", "scope": "necessity", "started_at": ts(0)}
            )

    def test_imported_run_rows_are_restamped_from_their_dispatch_entries(
        self, tmp_path: Path
    ) -> None:
        """Section 20.5 A: the cutover stamped 171 runs with the import minute.

        Built at version 4, seeded the way the cutover left it -- a run row whose start
        is the import instant beside the dispatch entry that knows the real one -- and
        then upgraded. The row must carry the entry's instants afterwards.
        """
        db = Database(tmp_path / "old.db")
        try:
            steps = available()
            assert [m.version for m in steps[:5]] == [1, 2, 3, 4, 5]
            for migration in steps[:4]:
                db.writer.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration.sql()
                    + f"\nPRAGMA user_version = {migration.version};\nCOMMIT;"
                )
            with db.write() as connection:
                connection.execute(
                    "INSERT INTO project(project_id, root, created_at, reporting_tz) VALUES (?, ?, ?, ?)",
                    ("demo", "C:/demo", ts(0), "UTC"),
                )
                connection.execute(
                    "INSERT INTO task(project_id, task_id, title, created_at, updated_at, lifecycle, "
                    "outcome, closed_at, last_activity_at, priority, category, spec_summary, "
                    "spec_description) VALUES ('demo', 'task-009', 'T', ?, ?, 'closed', "
                    "'completed', ?, ?, 'medium', 'general', 's', 'd')",
                    (ts(0), ts(0), ts(0), ts(0)),
                )
                for entry_id, kind, when in (
                    (1, "dispatch", "2026-08-20T10:00:00Z"),
                    (2, "dispatch_result", "2026-08-20T11:30:00Z"),
                ):
                    connection.execute(
                        "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, data_json) "
                        "VALUES ('demo', 'task-009', ?, ?, 'x', ?, ?)",
                        (entry_id, when, kind, json.dumps({"run_id": "run_deadbeef"})),
                    )
                connection.execute(
                    "INSERT INTO task_run(project_id, run_id, task_id, agent, runner, mode, posture, "
                    "trigger, caused_by, git_head, cwd, argv_json, started_at, ended_at) "
                    "VALUES ('demo', 'run_deadbeef', 'task-009', 'claude', 'r', 'session', 'auto', "
                    "'manual', 1, 'abc', 'C:/demo', '[]', '2026-09-07T19:03:31Z', '2026-09-07T19:03:31Z')"
                )
            report = upgrade(db, agentjobs_version="test", snapshot_before=False)
            # 005 is the one being exercised; anything numbered after it comes along and
            # is not this test's business.
            assert report.applied[0] == "005_finish_and_gate_history"
            row = (
                db.reader()
                .execute("SELECT started_at, ended_at FROM task_run WHERE run_id = 'run_deadbeef'")
                .fetchone()
            )
            assert (row["started_at"], row["ended_at"]) == (
                "2026-08-20T10:00:00Z",
                "2026-08-20T11:30:00Z",
            )
        finally:
            db.close()

    def test_a_new_run_row_takes_its_start_from_the_entry_not_the_clock(
        self, manager: TaskManager, database: Database
    ) -> None:
        make_task(manager)
        manager.record_dispatch(
            "task-001",
            actor="Jeff Posey",
            run_id="run_abcdef01",
            agent="claude",
            runner="claude",
            mode="session",  # type: ignore[arg-type]
            posture="auto",  # type: ignore[arg-type]
            trigger="manual",  # type: ignore[arg-type]
            caused_by=1,
            argv=["x"],
            cwd="C:/demo",
            git_head="abc",
        )
        task = manager.get_task("task-001")
        assert task is not None
        entry = next(e for e in task.log if e.type.value == "dispatch")
        row = rows(database, "SELECT started_at FROM task_run WHERE run_id = 'run_abcdef01'")[0]
        assert row["started_at"] == stamp(entry.ts)


# ----- the finish row --------------------------------------------------------------------------


class TestFinishRecord:
    def test_merged_is_derived_where_the_file_predates_the_key(self) -> None:
        record, why = finish_record(meta_for())
        assert why is None and record is not None
        assert record["merged"] is True
        assert record["started_at"] == "2026-09-19T12:00:00Z"
        record, _ = finish_record(meta_for(outcome="escalated", merge_commit=None))
        assert record is not None and record["merged"] is False

    def test_an_explicit_merged_flag_is_believed(self) -> None:
        record, _ = finish_record(meta_for(outcome="escalated", merged=True, merge_commit="abc"))
        assert record is not None and record["merged"] is True

    def test_a_stale_running_import_is_interrupted_and_a_young_one_is_left_alone(self) -> None:
        stale = meta_for(outcome="running", finished_at=None, seconds=None, merge_commit=None)
        now = datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc)
        record, why = finish_record(stale, source="imported", now=now)
        assert record is not None and record["outcome"] == "interrupted"
        assert record["finished_at"] is None and record["merged"] is False
        record, why = finish_record(stale, source="imported", now=now - timedelta(hours=23))
        assert record is None and why == history.STILL_RUNNING

    def test_the_native_writer_keeps_running_as_running(self) -> None:
        live = meta_for(outcome="running", finished_at=None, seconds=None, merge_commit=None)
        record, why = finish_record(live)
        assert why is None and record is not None and record["outcome"] == "running"

    def test_a_record_naming_no_task_has_no_row(self) -> None:
        assert finish_record(meta_for(task_id=""))[1] == history.NO_TASK

    def test_the_two_spellings_of_the_finishes_directory_agree(self) -> None:
        from agentjobs.dispatch.finish import FINISHES_DIRNAME as finisher_name

        assert FINISHES_DIRNAME == finisher_name


# ----- the gate row ----------------------------------------------------------------------------


class TestGateAssembler:
    def test_a_green_gate_is_one_row_and_ten_stages(self) -> None:
        gate = GateAssembler(
            "fin_aaaaaaaa:g1", origin="finish", finish_id="fin_aaaaaaaa", task_id="task-001"
        )
        changed = [gate.feed(e["ts"], e["kind"], e) for e in green_gate_events()]
        # started, ten stage finishes, finished: the stage starts change nothing.
        assert sum(changed) == 12
        assert gate.record["scope"] == "full"
        assert gate.record["passed"] is True
        assert gate.record["seconds"] == 300.0
        assert gate.record["stages_run"] == 10 and gate.record["stages_total"] == 10
        assert gate.record["tree"] == "798c8fcc7e7ae6aa"
        assert [s["stage"] for s in gate.stages] == STAGES
        assert [s["seq"] for s in gate.stages] == list(range(1, 11))
        assert gate.stages[0]["started_at"] == "2026-09-19T12:01:01Z"
        assert gate.stages[0]["finished_at"] == "2026-09-19T12:01:02Z"
        assert all(s["passed"] is True for s in gate.stages)
        assert not gate.open

    def test_the_failed_stage_is_a_row_with_no_seconds(self) -> None:
        events = green_gate_events()[:1]
        events += [
            {"ts": ts(1, 1), "kind": "gate_stage_started", "stage": "black"},
            {"ts": ts(1, 2), "kind": "gate_stage_finished", "stage": "black", "seconds": 1.0},
            {"ts": ts(1, 3), "kind": "gate_stage_started", "stage": "ruff"},
            {
                "ts": ts(1, 9),
                "kind": "gate_finished",
                "scope": "full",
                "passed": False,
                "seconds": 8.0,
                "stages_run": 2,
                "stages_total": 10,
                "failed_stage": "ruff",
            },
        ]
        gate = GateAssembler("x:g1", origin="run", run_id="run_1")
        for e in events:
            gate.feed(e["ts"], e["kind"], e)
        assert gate.record["passed"] is False and gate.record["failed_stage"] == "ruff"
        assert [(s["stage"], s["passed"], s["seconds"]) for s in gate.stages] == [
            ("black", True, 1.0),
            ("ruff", False, None),
        ]
        assert gate.stages[1]["started_at"] == "2026-09-19T12:01:03Z"

    def test_a_killed_gate_stays_open(self) -> None:
        gate = GateAssembler("x:g1", origin="manual")
        for e in green_gate_events()[:5]:
            gate.feed(e["ts"], e["kind"], e)
        assert gate.open and gate.record["finished_at"] is None and gate.record["passed"] is None

    def test_the_gate_vocabulary_is_renamed_once(self) -> None:
        gate = GateAssembler("x", origin="manual")
        gate.feed(ts(1), "gate_started", {"scope": "necessity"})
        assert gate.record["scope"] == "since_gate"
        gate.feed(ts(1), "gate_started", {"scope": "partial"})
        assert gate.record["scope"] == "partial"

    def test_events_before_the_start_change_nothing(self) -> None:
        gate = GateAssembler("x", origin="manual")
        assert gate.feed(ts(1), "gate_stage_finished", {"stage": "black", "seconds": 1}) is False
        assert gate.stages == []

    def test_a_finish_with_two_gates_numbers_them_in_file_order(self) -> None:
        lines = (
            finish_step_lines("fin_aaaaaaaa")
            + green_gate_events(at=1)
            + green_gate_events(at=7)[:3]
        )
        lines.append({"ts": ts(8), "kind": "finish_gate_retry", "finish_id": "fin_aaaaaaaa"})
        gates = gates_from_phases(
            lines, finish_id="fin_aaaaaaaa", task_id="task-001", branch="b", checkout="c"
        )
        assert [g.gate_id for g in gates] == ["fin_aaaaaaaa:g1", "fin_aaaaaaaa:g2"]
        assert gates[0].record["origin"] == "finish" and gates[0].record["branch"] == "b"
        assert not gates[0].open and gates[1].open


# ----- the store --------------------------------------------------------------------------------


class TestStoreWrites:
    def test_a_native_write_upserts_and_replaces_steps_by_seq(
        self, manager: TaskManager, store: SqlTaskStore, database: Database
    ) -> None:
        make_task(manager)
        record, _ = finish_record(meta_for(outcome="running", finished_at=None, seconds=None))
        assert record is not None
        assert store.record_finish(
            "fin_aaaaaaaa",
            record,
            [
                {
                    "seq": 1,
                    "step": "preflight",
                    "ok": True,
                    "skipped": False,
                    "seconds": 0.9,
                    "detail": "d",
                    "ts": ts(0, 1),
                }
            ],
        ).written
        record, _ = finish_record(meta_for())
        assert record is not None
        assert store.record_finish(
            "fin_aaaaaaaa",
            record,
            [
                {
                    "seq": 1,
                    "step": "preflight",
                    "ok": True,
                    "skipped": False,
                    "seconds": 1.1,
                    "detail": "d",
                    "ts": ts(0, 1),
                },
                {
                    "seq": 2,
                    "step": "gate",
                    "ok": True,
                    "skipped": False,
                    "seconds": 300,
                    "detail": "g",
                    "ts": ts(0, 2),
                },
            ],
        ).written
        finish = rows(database, "SELECT * FROM finish WHERE finish_id = 'fin_aaaaaaaa'")
        assert len(finish) == 1
        assert finish[0]["outcome"] == "finished" and finish[0]["merged"] == 1
        assert finish[0]["source"] == "native"
        steps = rows(
            database,
            "SELECT seq, step, seconds FROM finish_step WHERE finish_id = 'fin_aaaaaaaa' ORDER BY seq",
        )
        assert steps == [
            {"seq": 1, "step": "preflight", "seconds": 1.1},
            {"seq": 2, "step": "gate", "seconds": 300.0},
        ]

    def test_an_imported_write_never_overwrites_a_row(
        self, manager: TaskManager, store: SqlTaskStore, database: Database
    ) -> None:
        make_task(manager)
        native, _ = finish_record(meta_for())
        imported, _ = finish_record(meta_for(outcome="escalated"), source="imported")
        assert native is not None and imported is not None
        assert store.record_finish("fin_aaaaaaaa", native).written
        outcome = store.record_finish("fin_aaaaaaaa", imported)
        assert (outcome.written, outcome.reason) == (False, EXISTS)
        assert rows(database, "SELECT outcome FROM finish")[0]["outcome"] == "finished"
        # And the other way: an import first, then the native writer, which does win.
        assert store.record_finish("fin_bbbbbbbb", imported).written
        assert store.record_finish("fin_bbbbbbbb", native).written
        assert rows(
            database, "SELECT outcome, source FROM finish WHERE finish_id = 'fin_bbbbbbbb'"
        )[0] == {
            "outcome": "finished",
            "source": "native",
        }

    def test_a_finish_for_an_unknown_task_is_refused(
        self, store: SqlTaskStore, database: Database
    ) -> None:
        record, _ = finish_record(meta_for(task_id="task-404"))
        assert record is not None
        outcome = store.record_finish("fin_aaaaaaaa", record)
        assert (outcome.written, outcome.reason) == (False, UNKNOWN_TASK)
        assert history_counts(database.reader(), "demo")["finish"] == 0

    def test_gate_rows_land_without_a_finish_or_a_task(
        self, store: SqlTaskStore, database: Database
    ) -> None:
        gate = GateAssembler(
            "run_1:g1",
            origin="run",
            run_id="run_1",
            task_id="task-777",
            checkout="C:/wt",
            branch="feat/task-777-x",
        )
        for e in green_gate_events(prefix="run_1"):
            gate.feed(e["ts"], e["kind"], e)
        assert store.record_gate_run(gate.gate_id, gate.record, gate.stages).written
        counts = history_counts(database.reader(), "demo")
        assert counts == {"finish": 0, "finish_step": 0, "gate_run": 1, "gate_stage": 10}
        row = rows(database, "SELECT origin, task_id, passed, checkout FROM gate_run")[0]
        assert row == {"origin": "run", "task_id": "task-777", "passed": 1, "checkout": "C:/wt"}

    def test_deleting_a_task_takes_its_finishes_and_steps_with_it(
        self, manager: TaskManager, store: SqlTaskStore, database: Database
    ) -> None:
        make_task(manager)
        record, _ = finish_record(meta_for())
        assert record is not None
        store.record_finish(
            "fin_aaaaaaaa",
            record,
            [
                {
                    "seq": 1,
                    "step": "gate",
                    "ok": True,
                    "skipped": False,
                    "seconds": 1,
                    "detail": None,
                    "ts": ts(0),
                }
            ],
        )
        store.delete_task("task-001")
        counts = history_counts(database.reader(), "demo")
        assert counts["finish"] == 0 and counts["finish_step"] == 0


# ----- the live writers ----------------------------------------------------------------------------


class TestFinishDirectoryIndexesItself:
    def test_meta_and_steps_reach_the_store_as_they_are_written(
        self, manager: TaskManager, database: Database, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.finish import FinishDirectory, StepLog, StepResult

        make_task(manager)
        directory = FinishDirectory.create(
            tmp_path, "task-001", "demo", manager=manager, authority="approval"
        )
        assert directory.history is not None
        row = rows(database, "SELECT outcome, authority FROM finish")[0]
        assert row == {"outcome": "running", "authority": "approval"}

        steps = StepLog(directory)
        steps.append(StepResult("preflight", True, "ok", 0.9))
        steps.extend([StepResult("runway", True, "taken immediately", 0.0)])
        directory.write_meta(
            outcome="finished", finished_at=ts(6), seconds=360.0, merge_commit="abc123"
        )

        finish = rows(database, "SELECT outcome, merged, merge_commit, seconds FROM finish")[0]
        assert finish == {
            "outcome": "finished",
            "merged": 1,
            "merge_commit": "abc123",
            "seconds": 360.0,
        }
        assert rows(database, "SELECT seq, step, seconds FROM finish_step ORDER BY seq") == [
            {"seq": 1, "step": "preflight", "seconds": 0.9},
            {"seq": 2, "step": "runway", "seconds": 0.0},
        ]
        # The file is the same shape as the row.
        meta = yaml.safe_load(directory.meta_path.read_text(encoding="utf-8"))
        assert meta["outcome"] == "finished" and meta["merge_commit"] == "abc123"

    def test_a_directory_without_a_manager_writes_files_only(
        self, tmp_path: Path, database: Database
    ) -> None:
        from agentjobs.dispatch.finish import FinishDirectory

        directory = FinishDirectory.create(tmp_path, "task-001", "demo")
        assert directory.history is None
        assert directory.meta_path.is_file()
        assert history_counts(database.reader(), "demo")["finish"] == 0

    def test_a_failing_store_never_fails_the_finish(self, tmp_path: Path) -> None:
        class Broken:
            def record_finish(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("service down")

        from agentjobs.dispatch.finish import FinishDirectory

        directory = FinishDirectory.create(tmp_path, "task-001", "demo", manager=Broken())  # type: ignore[arg-type]
        directory.record(
            "finish_step", step="preflight", ok=True, skipped=False, detail="d", seconds=1.0
        )
        assert directory.history is not None
        assert directory.history.failures == 2 and directory.history.writes == 0
        assert directory.meta_path.is_file()


class RecordingManager:
    """A manager that remembers every gate write, for the gate's own wiring."""

    def __init__(self) -> None:
        self.gates: List[Dict[str, Any]] = []

    def record_gate_run(
        self, gate_id: str, record: Dict[str, Any], stages: List[Dict[str, Any]]
    ) -> Any:
        self.gates.append(
            {"gate_id": gate_id, "record": dict(record), "stages": [dict(s) for s in stages]}
        )
        from agentjobs.sqlstore.history import HistoryWrite

        return HistoryWrite(True)


class TestGateHistory:
    def test_the_gate_script_indexes_a_full_run_and_a_partial_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`check.main` with its children stubbed writes one row per run, scoped honestly."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_for_history", ROOT / "scripts" / "check.py"
        )
        assert spec is not None and spec.loader is not None
        check = importlib.util.module_from_spec(spec)
        sys.modules["check_for_history"] = check
        spec.loader.exec_module(check)

        monkeypatch.setattr(
            check.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
        )
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")
        monkeypatch.setattr(check.gate_scope, "write_receipt", lambda *a, **k: Path("r"))
        monkeypatch.setattr(check.gate_scope, "head_commit", lambda root: "b" * 40)
        monkeypatch.setattr(check.gate_scope, "tree_is_clean", lambda root: True)
        monkeypatch.setattr(check.gate_scope, "tree_fingerprint", lambda root: "fingerprint")
        monkeypatch.setattr(check.gate_scope, "dirty_paths", lambda root: [])

        recorder = RecordingManager()

        def opener() -> GateHistory:
            return GateHistory(
                recorder,
                "demo",
                origin="run",
                finish_id=None,
                run_id="run_1",
                task_id="task-472",
                checkout="C:/projects/worktrees/agentjobs-472",
                branch="feat/task-472-x",
                gate_id="run_1:g1",
            )

        monkeypatch.setattr(check, "open_gate_history", opener)
        assert check.main([]) == 0
        full = recorder.gates[-1]
        assert full["gate_id"] == "run_1:g1"
        assert full["record"]["scope"] == "full" and full["record"]["passed"] is True
        assert full["record"]["origin"] == "run" and full["record"]["task_id"] == "task-472"
        assert full["record"]["checkout"] == "C:/projects/worktrees/agentjobs-472"
        assert full["record"]["tree"] == "fingerprint"
        assert [s["stage"] for s in full["stages"]] == STAGES
        # One write at the start, one per stage, one at the end.
        assert len(recorder.gates) == 12

        recorder.gates.clear()
        assert check.main(["--only", "black"]) == 0
        partial = recorder.gates[-1]
        assert partial["record"]["scope"] == "partial"
        assert partial["record"]["stages_run"] == 1 and partial["record"]["stages_total"] == 10
        assert [s["stage"] for s in partial["stages"]] == ["black"]

    def test_the_switch_and_an_unregistered_checkout_both_decline(self, tmp_path: Path) -> None:
        assert GateHistory.open(tmp_path, environ={"AGENTJOBS_GATE_HISTORY": "off"}) is None
        assert GateHistory.declined is not None and "off" in GateHistory.declined
        registry = ProjectRegistry(tmp_path / "home")
        assert GateHistory.open(tmp_path, environ={}, registry=registry) is None
        assert GateHistory.declined is not None and "registered" in GateHistory.declined

    def test_two_refusals_switch_the_writer_off(self) -> None:
        class Refusing:
            calls = 0

            def record_gate_run(self, *a: Any, **k: Any) -> Any:
                self.calls += 1
                raise httpx.ConnectError("refused")

        refusing = Refusing()
        gate = GateHistory(
            refusing,
            "demo",
            origin="manual",
            finish_id=None,
            run_id=None,
            task_id=None,
            checkout="C:/x",
            branch=None,
            gate_id="man_1",
        )
        for e in green_gate_events():
            gate.observe(e["kind"], **{k: v for k, v in e.items() if k not in ("kind", "ts")})
        assert refusing.calls == 2 and gate.enabled is False and gate.writes == 0
        assert "did not answer" in gate.summary()

    def test_gate_origin_reads_the_finish_or_run_it_inherited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "fin_12345678"
        run_dir.mkdir()
        (run_dir / "meta.yaml").write_text(
            yaml.safe_dump({"task_id": "task-042", "outcome": "running"}), encoding="utf-8"
        )
        (run_dir / "phases.jsonl").write_text(
            json.dumps({"kind": "gate_started"}) + "\n", encoding="utf-8"
        )
        monkeypatch.setenv("AGENTJOBS_RUN_ID", "fin_12345678")
        monkeypatch.setenv("AGENTJOBS_RUN_DIR", str(run_dir))
        import os

        assert gate_origin(os.environ) == ("finish", "fin_12345678", None, "task-042")
        assert history.gates_started_so_far() == 1
        monkeypatch.setenv("AGENTJOBS_RUN_ID", "run_12345678")
        assert gate_origin(os.environ) == ("run", None, "run_12345678", "task-042")
        # A run declared over is nobody's identity (task-249).
        (run_dir / "meta.yaml").write_text(
            yaml.safe_dump({"task_id": "task-042", "finished_at": ts(1)}), encoding="utf-8"
        )
        assert gate_origin(os.environ) == ("manual", None, None, None)
        monkeypatch.delenv("AGENTJOBS_RUN_ID")
        assert gate_origin(os.environ) == ("manual", None, None, None)

    def test_task_from_branch(self) -> None:
        assert task_from_branch("feat/task-472-finish-gate-history-store") == "task-472"
        assert task_from_branch("spike/task-171-voice-input-decision") == "task-171"
        assert task_from_branch("main") is None
        assert task_from_branch(None) is None


# ----- over the service, from a worktree --------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, Any]]:
    """A registered project `demo` whose clone has a worktree, served by the real app."""
    from agentjobs.api.dependencies import reset_dependency_cache
    from agentjobs.api.main import app
    from agentjobs.store_factory import close_databases, mark_server_process

    clone = tmp_path / "clone"
    clone.mkdir()
    git(tmp_path, "init", "--initial-branch=main", str(clone))
    git(clone, "config", "user.email", "t@t.t")
    git(clone, "config", "user.name", "t")
    (clone / "README.md").write_text("x\n", encoding="utf-8")
    git(clone, "add", "README.md")
    git(clone, "commit", "-m", "init")
    worktree = tmp_path / "worktrees" / "clone-001"
    git(clone, "worktree", "add", "-b", "feat/task-001-thing", str(worktree), "main")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    project = Project(id="demo", name="Demo", root=clone)
    ProjectRegistry(home).add(clone, project_id="demo")
    close_databases()
    reset_dependency_cache()
    local = TaskManager(task_store(clone / "tasks", project_id="demo"))
    make_task(local)
    mark_server_process()
    connection = TestClient(app)
    client = TaskClient("http://testserver", client=connection, project_id="demo")
    remote = RemoteTaskManager(client, project)
    yield {
        "clone": clone,
        "worktree": worktree,
        "home": home,
        "project": project,
        "local": local,
        "remote": remote,
        "registry": ProjectRegistry(home),
    }
    connection.close()
    close_databases()


def served_rows(served: Dict[str, Any], sql: str) -> List[Dict[str, Any]]:
    database = served["local"].storage.database
    return [dict(row) for row in database.reader().execute(sql).fetchall()]


class TestOverTheService:
    def test_a_finish_recorded_through_the_remote_manager_lands_in_the_store(
        self, served: Dict[str, Any]
    ) -> None:
        record, _ = finish_record(meta_for())
        assert record is not None
        steps = history.steps_from_phases(finish_step_lines("fin_aaaaaaaa"))
        outcome = served["remote"].record_finish("fin_aaaaaaaa", record, steps)
        assert outcome.written
        assert served_rows(served, "SELECT outcome, merged FROM finish") == [
            {"outcome": "finished", "merged": 1}
        ]
        assert len(served_rows(served, "SELECT * FROM finish_step")) == 5
        refused = served["remote"].record_finish("fin_cccccccc", {**record, "task_id": "task-404"})
        assert (refused.written, refused.reason) == (False, UNKNOWN_TASK)

    def test_a_gate_in_a_worktree_lands_in_the_served_projects_store(
        self, served: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The checkout is a worktree beside the clone, inside no registered root."""
        import agentjobs.store_factory as factory

        assert project_for_checkout(served["worktree"], registry=served["registry"]) is not None
        assert project_for_checkout(served["worktree"], registry=served["registry"]).id == "demo"  # type: ignore[union-attr]
        assert project_for_checkout(served["clone"], registry=served["registry"]).id == "demo"  # type: ignore[union-attr]

        built: List[Dict[str, Any]] = []

        def over_the_service(project: Project, **options: Any) -> RemoteTaskManager:
            built.append({"project": project.id, **options})
            remote: RemoteTaskManager = served["remote"]
            return remote

        monkeypatch.setattr(factory, "task_manager_for", over_the_service)
        gate = GateHistory.open(served["worktree"], environ={}, registry=served["registry"])
        assert gate is not None, GateHistory.declined
        assert built == [{"project": "demo", "client_timeout": 5.0, "patient": False}]
        assert gate.origin == "manual" and gate.task_id == "task-001"
        assert gate.branch == "feat/task-001-thing"
        for e in green_gate_events():
            gate.observe(e["kind"], **{k: v for k, v in e.items() if k not in ("kind", "ts")})
        assert gate.writes == 12 and gate.failures == 0
        row = served_rows(
            served,
            "SELECT gate_id, origin, task_id, branch, scope, passed, stages_run FROM gate_run",
        )[0]
        assert row["gate_id"].startswith("man_")
        assert row["origin"] == "manual" and row["task_id"] == "task-001"
        assert row["branch"] == "feat/task-001-thing"
        assert (row["scope"], row["passed"], row["stages_run"]) == ("full", 1, 10)
        assert (
            Path(
                row["gate_id"]
                and served_rows(served, "SELECT checkout FROM gate_run")[0]["checkout"]
            ).resolve()
            == served["worktree"].resolve()
        )
        assert len(served_rows(served, "SELECT * FROM gate_stage")) == 10
        assert "12 writes" in gate.summary()
        gate.close()

    def test_a_gate_with_no_service_costs_one_refused_attempt_per_write_and_stops(self) -> None:
        """`patient=False` is what keeps a dead service from slowing the gate."""
        attempts = 0

        def refuse(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("refused", request=request)

        client = TaskClient(
            "http://nowhere",
            transport=httpx.MockTransport(refuse),
            project_id="demo",
            patient=False,
        )
        with pytest.raises(Exception):
            client.put_gate_record(
                "man_1", record={"origin": "manual", "scope": "full", "started_at": ts(0)}
            )
        assert attempts == 1

    def test_the_import_indexes_every_directory_once_and_says_what_it_skipped(
        self, served: Dict[str, Any]
    ) -> None:
        home = served["home"]
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        # A finished finish with a full green gate and a second gate that was killed.
        write_finish_dir(
            home,
            "fin_aaaaaaaa",
            meta_for(),
            finish_step_lines("fin_aaaaaaaa")
            + green_gate_events(at=1)
            + green_gate_events(at=7)[:4],
        )
        # A stale running one: interrupted.
        write_finish_dir(
            home,
            "fin_bbbbbbbb",
            meta_for(
                finish_id="fin_bbbbbbbb",
                outcome="running",
                finished_at=None,
                seconds=None,
                merge_commit=None,
            ),
            finish_step_lines("fin_bbbbbbbb")[:2],
        )
        # A young running one: the writer's.
        write_finish_dir(
            home,
            "fin_cccccccc",
            meta_for(
                finish_id="fin_cccccccc",
                outcome="running",
                finished_at=None,
                seconds=None,
                merge_commit=None,
                started_at=(now - timedelta(hours=1)).isoformat(),
            ),
            [],
        )
        # A finish for a task this project never had.
        write_finish_dir(
            home, "fin_dddddddd", meta_for(finish_id="fin_dddddddd", task_id="task-404"), []
        )
        # Another project's, a directory with no meta, and a torn line.
        write_finish_dir(
            home, "fin_eeeeeeee", meta_for(finish_id="fin_eeeeeeee", project_id="other"), []
        )
        (home / FINISHES_DIRNAME / "fin_ffffffff").mkdir()
        torn = write_finish_dir(
            home,
            "fin_99999999",
            meta_for(finish_id="fin_99999999", outcome="declined", merge_commit=None),
            finish_step_lines("fin_99999999")[:1],
        )
        with (torn / "phases.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "finish_step", "step": "runway"\n')

        report = import_finishes(home, served["remote"], "demo", now=now)
        assert report.scanned == 7
        assert (report.finishes, report.steps, report.gates, report.stages) == (3, 8, 2, 11)
        assert report.torn_lines == 1
        assert sorted(report.skipped) == [
            ("fin_cccccccc", history.STILL_RUNNING),
            ("fin_dddddddd", UNKNOWN_TASK),
            ("fin_eeeeeeee", history.OTHER_PROJECT),
            ("fin_ffffffff", history.NO_META),
        ]
        finishes = {r["finish_id"]: r for r in served_rows(served, "SELECT * FROM finish")}
        assert finishes["fin_bbbbbbbb"]["outcome"] == "interrupted"
        assert (
            finishes["fin_aaaaaaaa"]["source"] == "imported"
            and finishes["fin_aaaaaaaa"]["merged"] == 1
        )
        assert (
            finishes["fin_99999999"]["outcome"] == "declined"
            and finishes["fin_99999999"]["merged"] == 0
        )
        gates = {r["gate_id"]: r for r in served_rows(served, "SELECT * FROM gate_run")}
        assert set(gates) == {"fin_aaaaaaaa:g1", "fin_aaaaaaaa:g2"}
        assert (
            gates["fin_aaaaaaaa:g1"]["passed"] == 1
            and gates["fin_aaaaaaaa:g1"]["origin"] == "finish"
        )
        assert gates["fin_aaaaaaaa:g1"]["branch"] == "feat/task-001-thing"
        assert gates["fin_aaaaaaaa:g1"]["checkout"] == "C:/projects/worktrees/x-001"
        assert (
            gates["fin_aaaaaaaa:g2"]["finished_at"] is None
            and gates["fin_aaaaaaaa:g2"]["passed"] is None
        )
        rendered = report.render()
        assert "finishes  3" in rendered and "Skipped 4" in rendered

        # Second run: everything already there is left alone, and nothing else changes.
        again = import_finishes(home, served["remote"], "demo", now=now)
        assert (again.finishes, again.gates) == (0, 0)
        assert sum(1 for _, reason in again.skipped if reason == EXISTS) == 3
        assert (
            served_rows(served, "SELECT * FROM finish") == list(finishes.values())
            or len(served_rows(served, "SELECT * FROM finish")) == 3
        )

    def test_a_run_credential_may_index_history(self) -> None:
        from agentjobs.capabilities import GRANTS, Capability, PrincipalKind

        assert Capability.HISTORY_RECORD in GRANTS[PrincipalKind.RUN]
