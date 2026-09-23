"""The arithmetic behind task-534's two instruments, checked on hand-built inputs.

``scripts/run_report.py --overlap`` says how many dispatched gates were live at once, and
``scripts/gate_shape.py`` says what a shape of concurrent gates costs. Both produce numbers
that a decision is then taken on, and the failure mode of a measurement tool is not a
crash but a figure that is quietly wrong and gets quoted for a month -- so every case here
is a timeline or a gate transcript whose answer can be counted on fingers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    """Import a file under ``scripts/``, which is a script rather than a package module."""
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_report = _load("run_report")
gate_shape = _load("gate_shape")

START = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def gate(offset_minutes: Optional[float], length_minutes: float) -> Any:
    return run_report.Gate(
        seconds=length_minutes * 60,
        passed=True,
        scope="full",
        stages_run=10,
        stages_total=10,
        failed_stage=None,
        started_at=None if offset_minutes is None else START + timedelta(minutes=offset_minutes),
    )


def run(gates: List[Any], length_minutes: float, run_id: str = "run_a") -> Any:
    return run_report.Run(
        run_id=run_id,
        task_id="task-1",
        outcome="completed",
        started_at=START,
        finished_at=START + timedelta(minutes=length_minutes),
        gates=gates,
    )


class TestGateOverlap:
    def test_two_runs_whose_gates_overlap_by_five_minutes(self) -> None:
        # run_a gates 0-10, run_b gates 5-15: one live 0-5, two live 5-10, one 10-15.
        overlap = run_report.gate_overlap(
            [run([gate(0, 10)], 60, "run_a"), run([gate(5, 10)], 60, "run_b")]
        )
        assert overlap.gates == 2
        assert overlap.live_seconds == {1: 600.0, 2: 300.0}
        assert overlap.gate_seconds == {1: 600.0, 2: 600.0}
        assert overlap.neighboured_share == 0.5
        assert overlap.most == 2

    def test_a_gate_starting_as_another_ends_is_not_a_neighbour(self) -> None:
        overlap = run_report.gate_overlap(
            [run([gate(0, 10)], 60, "run_a"), run([gate(10, 10)], 60, "run_b")]
        )
        assert overlap.live_seconds == {1: 1200.0}
        assert overlap.neighboured_share == 0.0

    def test_three_at_once_is_three_gates_worth_of_time(self) -> None:
        overlap = run_report.gate_overlap([run([gate(0, 10)], 30, f"run_{n}") for n in range(3)])
        assert overlap.live_seconds == {3: 600.0}
        assert overlap.gate_seconds == {3: 1800.0}
        assert overlap.mean_live == 3.0

    def test_a_gate_with_no_start_is_counted_as_undated_not_placed(self) -> None:
        overlap = run_report.gate_overlap([run([gate(None, 10), gate(20, 5)], 60)])
        assert overlap.gates == 1
        assert overlap.undated == 1
        assert overlap.live_seconds == {1: 300.0}

    def test_work_to_gate_is_run_time_outside_gates_over_time_inside(self) -> None:
        # 60 minutes of run, 10 of them gating: 50 : 10.
        overlap = run_report.gate_overlap([run([gate(0, 10)], 60)])
        assert overlap.work_to_gate == 5.0

    def test_a_run_that_never_gated_does_not_dilute_the_ratio(self) -> None:
        overlap = run_report.gate_overlap([run([gate(0, 10)], 60, "run_a"), run([], 600, "run_b")])
        assert overlap.runs_with_gates == 1
        assert overlap.work_to_gate == 5.0

    def test_the_report_names_the_ratio_and_the_share(self) -> None:
        text = run_report.overlap_report(
            run_report.gate_overlap(
                [run([gate(0, 10)], 60, "run_a"), run([gate(5, 10)], 60, "run_b")]
            )
        )
        assert "50.0% of gate time" in text
        assert "5.0 : 1 over 2 runs" in text


class TestReadGatesKeepsTheStart:
    def _write(self, directory: Path, records: List[dict]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "phases.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def test_a_finished_gate_carries_its_started_timestamp(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            [
                {"kind": "gate_started", "ts": "2026-09-22T12:00:00+00:00"},
                {
                    "kind": "gate_finished",
                    "ts": "2026-09-22T12:10:00+00:00",
                    "seconds": 600,
                    "passed": True,
                },
            ],
        )
        [found] = run_report.read_gates(tmp_path)
        assert found.started_at == START
        assert found.seconds == 600

    def test_an_abandoned_gate_carries_its_started_timestamp(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            [
                {"kind": "gate_started", "ts": "2026-09-22T12:00:00+00:00"},
                {"kind": "note", "ts": "2026-09-22T12:04:00+00:00"},
            ],
        )
        [found] = run_report.read_gates(tmp_path)
        assert found.abandoned
        assert found.started_at == START
        assert found.seconds == 240


GATE_TAIL = """
pytest runs at -n 13, sharing this machine with 2 gates: 32 cores less 6 reserved for the owner.
===== 4411 passed, 3 skipped in 402.11s (0:06:42) =====

  black       1.0s
  pytest    410.3s
  vitest      7.1s
  build       4.4s
  e2e       112.0s
  total     560.2s
"""


class TestGateShapeParsing:
    def test_the_stage_table_is_read_including_its_total(self) -> None:
        rows = dict(gate_shape.STAGE_ROW.findall(GATE_TAIL))
        assert rows["pytest"] == "410.3"
        assert rows["e2e"] == "112.0"
        assert rows["total"] == "560.2"

    def test_the_width_is_read_from_the_gates_own_announcement(self) -> None:
        match = gate_shape.WIDTH.search(GATE_TAIL)
        assert match is not None and match.group(1) == "13"

    def test_a_gate_row_reports_what_the_gate_said_about_itself(self, tmp_path: Path) -> None:
        run_ = gate_shape.GateRun(
            worktree=tmp_path / "agentjobs-534-a",
            interpreter=Path("python"),
            environment={},
            keep=tmp_path / "logs" / "a.log",
        )
        run_.output = GATE_TAIL
        run_.returncode = 0
        run_.seconds = 575.0
        row = run_.row()
        assert row["workers"] == "13"
        assert row["pytest"] == 410.3
        assert row["stage_total"] == 560.2
        assert row["passed"] == 4411
        assert row["verdict"] == "ok"
        assert (tmp_path / "logs" / "a.log").read_text(encoding="utf-8") == GATE_TAIL

    def test_tasklist_working_sets_are_summed_per_image(self) -> None:
        text = (
            '"python.exe","100","Console","1","1,024 K"\n'
            '"python.exe","101","Console","1","2,048 K"\n'
            '"node.exe","200","Console","1","512 K"\n'
            '"chrome.exe","300","Console","1","9,999,999 K"\n'
            '"python.exe","102","Console","1","N/A"\n'
            "garbage line\n"
        )
        totals = gate_shape.parse_tasklist(text, ("python.exe", "node.exe"))
        assert totals["python.exe"] == {"count": 2.0, "ws_mb": 3.0}
        assert totals["node.exe"] == {"count": 1.0, "ws_mb": 0.5}

    def test_the_sampler_summary_reports_the_worst_moment(self) -> None:
        sampler = gate_shape.Sampler()
        sampler.trace = [
            {
                "at": 0,
                "free_mb": 9000.0,
                "committed_mb": 70000.0,
                "python_count": 40,
                "python_ws_mb": 2000.0,
                "node_ws_mb": 100.0,
            },
            {
                "at": 5,
                "free_mb": 1200.0,
                "committed_mb": 90000.0,
                "python_count": 120,
                "python_ws_mb": 7000.0,
                "node_ws_mb": 900.0,
            },
            {
                "at": 10,
                "free_mb": 5000.0,
                "committed_mb": 80000.0,
                "python_count": 60,
                "python_ws_mb": 3000.0,
                "node_ws_mb": 100.0,
            },
        ]
        summary = sampler.summary()
        assert summary["lowest_free_mb"] == 1200.0
        assert summary["peak_committed_mb"] == 90000.0
        assert summary["peak_gate_ws_mb"] == 7900.0
        assert summary["peak_python_processes"] == 120

    def test_a_red_gate_and_a_neighbour_are_both_named_in_the_table(self) -> None:
        wave = {
            "label": "C",
            "rep": 1,
            "concurrency": 3,
            "wave_seconds": 900.0,
            "gates_per_hour": 12.0,
            "gates": [
                {"worktree": "a", "workers": "8", "gate_seconds": 900.0},
                {"worktree": "b", "workers": "8", "gate_seconds": 850.0},
                {"worktree": "c", "workers": "8", "gate_seconds": 800.0},
            ],
            "reds": ["b"],
            "memory": {"lowest_free_mb": 2048.0, "peak_gate_ws_mb": 10240.0},
            "neighbour_present": True,
            "slots_before": 0,
            "slots_after": 1,
        }
        line = gate_shape.render([wave]).splitlines()[-1]
        assert "12.00" in line
        assert "RED: b" in line
        assert "NEIGHBOUR 0->1" in line
        assert "10.0" in line  # peak gate working set, in GB

    def test_the_arm_widens_capacity_to_its_own_concurrency(self) -> None:
        # The whole arm C rests on this: at the default capacity of two, its third gate
        # would queue rather than be measured.
        seen: List[dict] = []

        class Recorder:
            def __init__(self, **kwargs: Any) -> None:
                seen.append(kwargs["environment"])
                self.worktree = kwargs["worktree"]

            def run(self, begun: float) -> None:
                return None

            def row(self) -> dict:
                return {"worktree": self.worktree.name, "gate_seconds": 1.0, "verdict": "ok"}

        original = (gate_shape.GateRun, gate_shape.interpreter_for, gate_shape.slots_seen)
        gate_shape.GateRun = Recorder
        gate_shape.interpreter_for = lambda tree: Path("python")
        gate_shape.slots_seen = lambda: 0
        try:
            wave = gate_shape.run_wave([Path("a"), Path("b"), Path("c")], None, "C", 1)
        finally:
            gate_shape.GateRun, gate_shape.interpreter_for, gate_shape.slots_seen = original
        assert [env["AGENTJOBS_GATE_CAPACITY"] for env in seen] == ["3", "3", "3"]
        assert all("VIRTUAL_ENV" not in env for env in seen)
        assert wave["concurrency"] == 3
