"""Phase records: what a dispatched run spent its time on, and the report built on them.

Task-233 measured the corpus of dispatched runs and found the fourth finding was that
nothing could be measured at all: ``meta.yaml`` holds a start and a finish, and
``transcript.log`` is a raw TTY capture in which a line appears as many times as the
terminal repainted it. Every count derived from it is an artefact of that repainting.

So the properties guarded here are the ones that make the next measurement cheap:
records survive being written by several processes, a missing or torn file degrades to a
gap rather than an exception, and writing one can never break the run it is measuring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agentjobs.dispatch.phases import (
    PHASES_FILENAME,
    RUN_DIR_ENV,
    RUN_ID_ENV,
    current_run,
    read_phases,
    record_phase,
    record_phase_from_env,
)


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_report = load_script("run_report")


# --- writing ------------------------------------------------------------------------


class TestRecording:
    def test_a_record_round_trips(self, tmp_path: Path) -> None:
        record_phase(tmp_path, "gate_finished", passed=True, seconds=42.5)

        (record,) = read_phases(tmp_path)
        assert record["kind"] == "gate_finished"
        assert record["passed"] is True
        assert record["seconds"] == 42.5

    def test_every_record_is_stamped_so_processes_can_be_compared(self, tmp_path: Path) -> None:
        record_phase(tmp_path, "gate_started")

        (record,) = read_phases(tmp_path)
        assert record["ts"].endswith("+00:00")

    def test_records_accumulate_in_the_order_they_were_written(self, tmp_path: Path) -> None:
        record_phase(tmp_path, "gate_started")
        record_phase(tmp_path, "gate_finished", passed=False)

        assert [record["kind"] for record in read_phases(tmp_path)] == [
            "gate_started",
            "gate_finished",
        ]

    def test_a_value_that_will_not_serialise_is_stringified_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """Instrumentation must not be able to fail the thing it measures."""
        record_phase(tmp_path, "gate_finished", stage=object())

        assert len(read_phases(tmp_path)) == 1

    def test_an_unwritable_directory_is_swallowed(self, tmp_path: Path) -> None:
        blocked = tmp_path / "file-not-a-directory"
        blocked.write_text("", encoding="utf-8")

        assert record_phase(blocked, "gate_started") is None


# --- reading ------------------------------------------------------------------------


class TestReading:
    def test_a_run_with_no_records_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_phases(tmp_path) == []

    def test_a_torn_line_is_skipped_and_the_rest_survives(self, tmp_path: Path) -> None:
        """Several processes append to one file. One bad line must not lose the others."""
        record_phase(tmp_path, "gate_started")
        path = tmp_path / PHASES_FILENAME
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "torn"\n')
        record_phase(tmp_path, "gate_finished")

        assert [record["kind"] for record in read_phases(tmp_path)] == [
            "gate_started",
            "gate_finished",
        ]

    def test_a_line_that_is_not_an_object_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / PHASES_FILENAME).write_text("[1, 2, 3]\n", encoding="utf-8")

        assert read_phases(tmp_path) == []


# --- the environment gate -----------------------------------------------------------


class TestOutsideARun:
    """The property that makes instrumentation free to add: outside a run it is nothing."""

    def test_nothing_is_written_when_the_environment_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUN_DIR_ENV, raising=False)

        assert record_phase_from_env("gate_started") is None
        assert current_run() is None

    def test_a_run_directory_that_does_not_exist_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale variable inherited from somewhere must not conjure a run directory."""
        monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path / "gone"))

        assert record_phase_from_env("gate_started") is None

    def test_inside_a_run_the_record_carries_the_run_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(RUN_ID_ENV, "run_abc123")

        record_phase_from_env("gate_finished", passed=True, seconds=1.0)

        (record,) = read_phases(tmp_path)
        assert record["run_id"] == "run_abc123"


# --- the report ---------------------------------------------------------------------


def write_run(
    home: Path,
    run_id: str,
    *,
    task_id: str,
    outcome: str = "completed",
    started: str = "2026-08-21T10:00:00+00:00",
    finished: str | None = "2026-08-21T10:30:00+00:00",
) -> Path:
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    lines = [
        f"run_id: {run_id}",
        f"task_id: {task_id}",
        f"outcome: {outcome}",
        f"started_at: '{started}'",
    ]
    if finished:
        lines.append(f"finished_at: '{finished}'")
    (directory / "meta.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


class TestRunReport:
    """The baseline table task-233 built by hand, rebuilt from the ledger instead."""

    def test_it_counts_runs_tasks_and_time(self, tmp_path: Path) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")
        write_run(tmp_path, "run_b", task_id="task-001")
        write_run(tmp_path, "run_c", task_id="task-002")

        text = run_report.summary(run_report.load_runs(tmp_path))

        assert "runs                  3" in text
        assert "distinct tasks        2" in text
        assert "runs per task         1.50" in text
        assert "total run time        1.5h" in text

    def test_a_run_with_no_finish_time_is_not_measured_from_now(self, tmp_path: Path) -> None:
        """Inventing a plausible duration is worse than admitting the record is silent."""
        write_run(tmp_path, "run_a", task_id="task-001")
        write_run(tmp_path, "run_b", task_id="task-001", outcome="running", finished=None)

        text = run_report.summary(run_report.load_runs(tmp_path))

        assert "runs with durations   1" in text
        assert "total run time        0.5h" in text

    def test_without_phase_records_it_says_so_rather_than_reporting_zero(
        self, tmp_path: Path
    ) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")

        assert "No phase records yet" in run_report.summary(run_report.load_runs(tmp_path))

    def test_gate_time_comes_from_phase_records(self, tmp_path: Path) -> None:
        directory = write_run(tmp_path, "run_a", task_id="task-001")
        record_phase(directory, "gate_finished", passed=True, seconds=600, scope="full")
        record_phase(directory, "gate_finished", passed=False, seconds=300, scope="full")

        text = run_report.summary(run_report.load_runs(tmp_path))

        assert "gate runs             2" in text
        assert "0.1h in gate runs that failed" in text

    def test_a_gate_that_never_finished_contributes_nothing(self, tmp_path: Path) -> None:
        """A killed gate has an unknown duration, and unknown is not a number."""
        directory = write_run(tmp_path, "run_a", task_id="task-001")
        record_phase(directory, "gate_started", scope="full")

        assert "No phase records yet" in run_report.summary(run_report.load_runs(tmp_path))

    def test_the_per_task_table_ranks_by_time_spent(self, tmp_path: Path) -> None:
        """Finding 2 of task-233 -- one epic taking a third of everything -- is this view."""
        write_run(tmp_path, "run_a", task_id="task-001")
        write_run(
            tmp_path,
            "run_b",
            task_id="task-002",
            finished="2026-08-21T13:00:00+00:00",
        )

        rows = run_report.per_task(run_report.load_runs(tmp_path)).splitlines()

        assert "task-002" in rows[1]
        assert "task-001" in rows[2]

    def test_a_ledger_with_no_runs_is_not_an_error(self, tmp_path: Path) -> None:
        assert run_report.main(["--home", str(tmp_path)]) == 0

    def test_an_unquoted_timestamp_is_still_a_timestamp(self, tmp_path: Path) -> None:
        """YAML parses an unquoted ISO stamp into a datetime, and a hand-edited
        meta.yaml is unquoted. Refusing it reads as 'this run has no duration'."""
        directory = tmp_path / "runs" / "run_a"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text(
            "run_id: run_a\ntask_id: task-001\n"
            "started_at: 2026-08-21T10:00:00+00:00\n"
            "finished_at: 2026-08-21T10:30:00+00:00\n",
            encoding="utf-8",
        )

        text = run_report.summary(run_report.load_runs(tmp_path))

        assert "runs with durations   1" in text
        assert "total run time        0.5h" in text

    def test_an_unreadable_meta_file_does_not_sink_the_report(self, tmp_path: Path) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")
        broken = tmp_path / "runs" / "run_b"
        broken.mkdir()
        (broken / "meta.yaml").write_text("::: not yaml :::\n", encoding="utf-8")

        assert len(run_report.load_runs(tmp_path)) == 1


def write_finish(
    home: Path,
    finish_id: str,
    *,
    task_id: str,
    outcome: str = "finished",
    reason: str = "",
    stopped_at: str = "",
    merge_commit: str | None = "abc123",
    started: str = "2026-08-22T10:00:00+00:00",
    finished: str | None = "2026-08-22T10:01:30+00:00",
) -> Path:
    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True)
    lines = [
        f"finish_id: {finish_id}",
        f"task_id: {task_id}",
        f"outcome: {outcome}",
        f"started_at: '{started}'",
    ]
    if reason:
        lines.append(f"reason: {reason}")
    if stopped_at:
        lines.append(f"stopped_at: {stopped_at}")
    if merge_commit:
        lines.append(f"merge_commit: {merge_commit}")
    if finished:
        lines.append(f"finished_at: '{finished}'")
    (directory / "meta.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


class TestScriptedFinishesInTheReport:
    """task-241: a finish is the *absence* of a follow-on run, and has to read as one.

    The failure this guards is specific and quiet. A finish leaves no run behind, so a
    report that only reads ``runs/`` shows the runs-per-task figure falling with nothing
    to attribute it to -- the saving reads as missing data, which is exactly what the
    task's f2 says must not happen.
    """

    def test_a_finish_is_not_counted_as_a_run(self, tmp_path: Path) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")
        write_finish(tmp_path, "fin_a", task_id="task-001")

        runs = run_report.load_runs(tmp_path)

        assert len(runs) == 1
        assert "runs                  1" in run_report.summary(
            runs, run_report.load_finishes(tmp_path)
        )

    def test_finished_ones_are_reported_as_runs_that_never_happened(self, tmp_path: Path) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")
        write_finish(tmp_path, "fin_a", task_id="task-001")
        write_finish(tmp_path, "fin_b", task_id="task-002")

        text = run_report.summary(
            run_report.load_runs(tmp_path), run_report.load_finishes(tmp_path)
        )

        assert "scripted finishes     2" in text
        assert "finished            2, mean 1.5m" in text
        assert "follow-on runs that never happened" in text
        assert "tasks finished with no dispatched run: 2" in text

    def test_an_escalated_finish_says_where_it_stopped(self, tmp_path: Path) -> None:
        """An escalation did not save a run, and must not be counted as if it had."""
        write_finish(
            tmp_path,
            "fin_a",
            task_id="task-001",
            outcome="escalated",
            reason="gate_failed",
            stopped_at="gate",
            merge_commit=None,
        )
        write_run(tmp_path, "run_a", task_id="task-001")

        text = run_report.summary(
            run_report.load_runs(tmp_path), run_report.load_finishes(tmp_path)
        )

        assert "escalated           1" in text
        assert "stopped at: gate" in text
        assert "0 had already merged" in text

    def test_an_escalation_after_the_merge_is_distinguished(self, tmp_path: Path) -> None:
        write_finish(
            tmp_path,
            "fin_a",
            task_id="task-001",
            outcome="escalated",
            reason="no_restart_command",
            stopped_at="restart",
        )
        write_run(tmp_path, "run_a", task_id="task-001")

        text = run_report.summary(
            run_report.load_runs(tmp_path), run_report.load_finishes(tmp_path)
        )

        assert "1 had already merged" in text

    def test_the_block_is_silent_when_nothing_has_been_finished(self, tmp_path: Path) -> None:
        write_run(tmp_path, "run_a", task_id="task-001")

        text = run_report.summary(run_report.load_runs(tmp_path), [])

        assert "scripted finish" not in text

    def test_the_spawn_log_directory_is_not_read_as_a_finish(self, tmp_path: Path) -> None:
        (tmp_path / "finishes" / "spawn").mkdir(parents=True)
        (tmp_path / "finishes" / "spawn" / "task-001.log").write_text("noise\n", encoding="utf-8")
        write_finish(tmp_path, "fin_a", task_id="task-001")

        assert [finish.finish_id for finish in run_report.load_finishes(tmp_path)] == ["fin_a"]

    def test_a_machine_with_only_finishes_still_reports_them(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The one case the report used to answer with "no runs" and stop."""
        write_finish(tmp_path, "fin_a", task_id="task-001")

        assert run_report.main(["--home", str(tmp_path)]) == 0

        printed = capsys.readouterr().out
        assert "No dispatched runs matched" in printed
        assert "scripted finishes     1" in printed
