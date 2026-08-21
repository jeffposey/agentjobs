"""Report where dispatched agent time goes, from the run ledger on this machine.

The table this prints is the one task-233 assembled by hand on 2026-08-21 -- 52 runs,
21.1 hours, 2.13 runs per task -- and assembling it by hand is the reason it had never
been assembled before. Everything here comes from ``~/.agentjobs/runs/*/``:

* ``meta.yaml`` for the outer shape of a run: task, outcome, start, finish.
* ``phases.jsonl`` for what happened inside one (``agentjobs.dispatch.phases``).

``transcript.log`` is deliberately not read. It is a raw TTY capture -- ANSI escapes and
screen redraws -- so a line appears in it as many times as the terminal repainted it,
and every count derived from it is an artefact of that. Phase records exist so this
script never has to guess.

    python scripts/run_report.py                 # the whole ledger
    python scripts/run_report.py --since 7       # runs started in the last 7 days
    python scripts/run_report.py --task task-233 # one task's runs, listed
    python scripts/run_report.py --per-task      # every task, worst first
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentjobs.dispatch.phases import read_phases  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - the package depends on it
    print("PyYAML is required. Run `python scripts/bootstrap.py`.", file=sys.stderr)
    raise SystemExit(1)


HOME_ENV = "AGENTJOBS_HOME"
GATE_FINISHED = "gate_finished"


def default_home() -> Path:
    return Path(os.environ.get(HOME_ENV) or (Path.home() / ".agentjobs"))


def as_moment(value: object) -> Optional[datetime]:
    """A timestamp out of a record, naive values read as UTC, junk read as absent.

    A ``datetime`` is accepted as well as a string. Dispatch writes these through
    ``yaml.safe_dump``, which quotes them, so they come back as strings -- but YAML
    parses an *unquoted* ISO timestamp into a ``datetime``, and a hand-written or
    hand-corrected ``meta.yaml`` is unquoted. Refusing that reads as "this run has no
    duration", which is the one answer a reporting tool must not give by accident.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Gate:
    """One invocation of ``scripts/check.py`` inside a run."""

    seconds: float
    passed: bool
    scope: str
    stages_run: int
    stages_total: int
    failed_stage: Optional[str]


@dataclass(frozen=True)
class Run:
    """One dispatched run, as the ledger and its phase records describe it."""

    run_id: str
    task_id: str
    outcome: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    gates: Sequence[Gate]

    @property
    def seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def gate_seconds(self) -> float:
        return sum(gate.seconds for gate in self.gates)

    @property
    def wasted_gate_seconds(self) -> float:
        """Time in gate runs that failed. Real work, but not progress you keep."""
        return sum(gate.seconds for gate in self.gates if not gate.passed)


def read_gates(directory: Path) -> List[Gate]:
    """Every completed gate run recorded in a run directory.

    A ``gate_started`` with no matching finish is a gate the run was killed in the
    middle of. It contributes nothing here rather than being measured to the run's end:
    reporting an unknown duration as a number is the failure mode
    ``RunRecord.elapsed_seconds`` already refuses, and this should not reintroduce it
    one directory over.
    """
    gates: List[Gate] = []
    for record in read_phases(directory):
        if record.get("kind") != GATE_FINISHED:
            continue
        seconds = record.get("seconds")
        if not isinstance(seconds, (int, float)):
            continue
        failed_stage = record.get("failed_stage")
        gates.append(
            Gate(
                seconds=float(seconds),
                passed=bool(record.get("passed")),
                scope=str(record.get("scope") or "unknown"),
                stages_run=int(record.get("stages_run") or 0),
                stages_total=int(record.get("stages_total") or 0),
                failed_stage=str(failed_stage) if failed_stage else None,
            )
        )
    return gates


def read_run(directory: Path) -> Optional[Run]:
    """One run directory, or None when it holds no readable metadata."""
    meta_path = directory / "meta.yaml"
    if not meta_path.is_file():
        return None
    try:
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    meta = loaded if isinstance(loaded, dict) else {}
    return Run(
        run_id=str(meta.get("run_id") or directory.name),
        task_id=str(meta.get("task_id") or "?"),
        outcome=str(meta.get("outcome") or meta.get("status") or "unknown"),
        started_at=as_moment(meta.get("started_at")),
        finished_at=as_moment(meta.get("finished_at")),
        gates=read_gates(directory),
    )


def load_runs(home: Path) -> List[Run]:
    """Every run this machine has a directory for, oldest first."""
    root = home / "runs"
    if not root.is_dir():
        return []
    runs = [
        run
        for directory in sorted(root.iterdir())
        if directory.is_dir() and directory.name != ".locks"
        for run in [read_run(directory)]
        if run is not None
    ]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(runs, key=lambda run: run.started_at or epoch)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. Samples are small, so interpolation is false precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f}m"


def hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def outcome_counts(runs: Iterable[Run]) -> str:
    counts: Dict[str, int] = {}
    for run in runs:
        counts[run.outcome] = counts.get(run.outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


def summary(runs: List[Run]) -> str:
    """The baseline table, reproduced from the ledger rather than from a transcript."""
    timed = [run for run in runs if run.seconds is not None]
    durations = [run.seconds or 0.0 for run in timed]
    tasks = {run.task_id for run in runs}
    total = sum(durations)
    gate_total = sum(run.gate_seconds for run in runs)
    wasted = sum(run.wasted_gate_seconds for run in runs)
    gate_runs = sum(len(run.gates) for run in runs)
    instrumented = [run for run in timed if run.gates]

    lines = [
        f"  runs                  {len(runs)} ({outcome_counts(runs)})",
        f"  runs with durations   {len(timed)}",
        f"  total run time        {hours(total)}",
        f"  distinct tasks        {len(tasks)}",
        f"  runs per task         {len(runs) / len(tasks):.2f}",
        f"  run length            p50 {minutes(percentile(durations, 0.50))}, "
        f"p75 {minutes(percentile(durations, 0.75))}, "
        f"p90 {minutes(percentile(durations, 0.90))}, "
        f"max {minutes(max(durations, default=0.0))}",
    ]

    if gate_runs:
        share = gate_total / total * 100 if total else 0.0
        instrumented_total = sum(run.seconds or 0.0 for run in instrumented)
        instrumented_share = (
            sum(run.gate_seconds for run in instrumented) / instrumented_total * 100
            if instrumented_total
            else 0.0
        )
        lines += [
            "",
            f"  gate runs             {gate_runs} across {len(instrumented)} instrumented runs",
            f"  gate time             {hours(gate_total)} "
            f"({share:.0f}% of all run time, {instrumented_share:.0f}% of instrumented)",
            f"  gate time thrown away {hours(wasted)} in gate runs that failed",
        ]
    else:
        lines += [
            "",
            "  No phase records yet. Runs dispatched before instrumentation landed carry",
            "  only meta.yaml, so the gate lines cannot be computed for them.",
        ]

    return "\n".join(lines)


def per_task(runs: List[Run]) -> str:
    """Every task, most dispatched time first. Finding 2 of task-233 is this table."""
    by_task: Dict[str, List[Run]] = {}
    for run in runs:
        by_task.setdefault(run.task_id, []).append(run)
    rows = sorted(
        (
            (task, sum(run.seconds or 0.0 for run in group), len(group))
            for task, group in by_task.items()
        ),
        key=lambda row: row[1],
        reverse=True,
    )
    total = sum(row[1] for row in rows) or 1.0
    width = max((len(row[0]) for row in rows), default=4)
    lines = [f"  {'task'.ljust(width)}  runs   total   share"]
    for task, seconds, count in rows:
        share = seconds / total * 100
        lines.append(f"  {task.ljust(width)}  {count:>4}  {minutes(seconds):>6}  {share:5.1f}%")
    return "\n".join(lines)


def listing(runs: List[Run]) -> str:
    """A row per run: the raw material every aggregate above is built from."""
    width = max((len(run.run_id) for run in runs), default=6)
    lines = [f"  {'run'.ljust(width)}  task       outcome      elapsed  gates"]
    for run in runs:
        elapsed = minutes(run.seconds) if run.seconds is not None else "-"
        if run.gates:
            failed = sum(1 for gate in run.gates if not gate.passed)
            gates = f"{len(run.gates)} ({minutes(run.gate_seconds)}"
            gates += f", {failed} failed)" if failed else ")"
        else:
            gates = "-"
        lines.append(
            f"  {run.run_id.ljust(width)}  {run.task_id:<10} {run.outcome:<11}  "
            f"{elapsed:>7}  {gates}"
        )
    return "\n".join(lines)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report where dispatched agent time goes.")
    parser.add_argument(
        "--home", type=Path, default=None, help="ledger root (default ~/.agentjobs)"
    )
    parser.add_argument(
        "--since", type=float, metavar="DAYS", help="only runs started in the last N days"
    )
    parser.add_argument(
        "--task", metavar="TASK_ID", help="only this task's runs, listed individually"
    )
    parser.add_argument("--per-task", action="store_true", help="a row per task, most time first")
    parser.add_argument("--list", action="store_true", help="a row per run")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    home = args.home or default_home()
    runs = load_runs(home)
    if args.since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        runs = [run for run in runs if run.started_at and run.started_at >= cutoff]
    if args.task:
        runs = [run for run in runs if run.task_id == args.task]

    if not runs:
        print(f"No runs in {home / 'runs'} matching that selection.")
        return 0

    print(f"\nDispatched runs in {home / 'runs'}\n")
    print(summary(runs))
    if args.per_task:
        print(f"\nBy task\n\n{per_task(runs)}")
    if args.list or args.task:
        print(f"\nRuns\n\n{listing(runs)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
