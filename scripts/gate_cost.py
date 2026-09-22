"""What one gate's pytest stage costs when it believes N gates share the machine.

Task-513 was filed on a table that looked like contention and was not. Five gate runs
over one evening as worktrees accumulated -- 225.4s, 268.9s, 314.2s, 358.7s, 663.5s --
a 2.9x spread on identical work, monotonic with the number of live worktrees, read at
the time as five agents oversubscribing the machine.

The reading nobody had checked is that `scripts/gate_slots.py` **hands the Nth
concurrent gate 32/N workers on purpose** (task-339). Both readings predict that exact
table, and one experiment tells them apart: throttle a gate's worker count *without*
giving it any neighbours.

**The experiment.** Hold `k` synthetic gate slots, so `gate_slots.active()` answers
`k+1` without `k` real gates existing, then run this checkout's own pytest stage and
time it. A held slot is a file and nothing more, so this buys a contended machine's
worker count without any of its contention.

**The answer, measured 2026-09-21 on the 32-core dispatch machine**, with no other gate
running for any of these three arms:

    gates believed    -n        pytest stage
    1                 auto(32)  507.5s
    2                 16        546.1s   (+8%)
    5                 6         1520.6s  (3.0x)

So a **lone** gate throttled to what a fifth gate would be handed reproduces the whole
spread the task was filed about, and beats it. The neighbours were never the cause; the
division is. Two further notes a reader needs before quoting this: the suite is nearly
flat from 32 workers to 16, so the machine was over-provisioned at `-n auto` and the
steep part is below about 10; and run-to-run noise at `-n 6` is large -- a second arm
that evening, with a real neighbour, came in at 1112.0s against this 1520.6s.

    poetry run python scripts/gate_cost.py curve
    poetry run python scripts/gate_cost.py curve --gates 1,5 --reps 2

**It drives `scripts/check.py --only pytest` rather than pytest.** That is deliberate and
costs a little startup: the number this script exists to compare is the one in the gate's
own per-stage table, and reimplementing the stage's arguments here would measure a
slightly different command every time one of them changed. `check.py` holds a slot of its
own, so `--gates 5` holds four and lets the gate be the fifth.

**A partial run earns no receipt**, so nothing here can be mistaken for having gated the
branch -- `--only` prints `PARTIAL RUN` at both ends and this script inherits that.

**Neighbours are recorded, not assumed away.** This machine dispatches real agents while
a probe runs, and a gate that starts beside one is a polluted sample that must be visible
rather than averaged in. Every row carries the slot count seen at the start and end of
the run and is flagged when they disagree, which is the only honest thing to do on a
machine whose other users cannot be paused.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CSI = re.compile(r"\x1b\[[0-9;]*m")
"""Colour, stripped before anything is matched.

The same defect twice already: `finish.failing_tests` (task-419) and task-505's own
probe both matched against coloured output and reported "red, no test named"."""

STAGE = re.compile(r"^\s+pytest\s+([0-9.]+)s\s*$", re.MULTILINE)
"""The `pytest` row of `check.py`'s own printed timing table -- the figure to report.

Not the `=== pytest ok in 12.3s ===` banner, which a **concurrent** run prints and the
serial one does not: `run_serially` streams each stage's output as it goes and says what
it cost only in the table at the end. The gate this script drives is the serial one, so
matching the banner matched nothing and reported every arm as `None` (caught on the first
arm of the first curve, 2026-09-21, which is why every arm's output is kept)."""

SUITE = re.compile(r"in ([0-9.]+)s \(0:")
"""pytest's own total, which excludes the stage's process startup. Worth keeping beside
the stage figure: the gap between them is what `check.py` costs to get to pytest."""

COUNTS = re.compile(r"(\d+) passed")
SELECTED = re.compile(r"-n[ =](\w+)")


@contextlib.contextmanager
def slots_held(count: int) -> Iterator[None]:
    """Hold `count` gate slots for the length of this block.

    A held slot is a file `gate_slots` counts, nothing more, so this buys the worker
    count of a contended machine without the contention -- which is the whole experiment.

    **Above the capacity this has to say so, or it deadlocks** (task-536). Since a third
    gate queues rather than taking a third share, the third synthetic slot would wait
    forty minutes for itself and the `check.py` below would then wait behind it. The
    capacity is therefore lifted for this process and its subprocess, through
    `gate_slots.CAPACITY_ENV`, which is the one sanctioned use of that variable: the
    experiment's whole purpose is to run a gate at a width it would not otherwise be
    handed, and that is a different question from how many gates the machine allows.
    """
    import gate_slots  # type: ignore[import-not-found]

    previous = os.environ.get(gate_slots.CAPACITY_ENV)
    os.environ[gate_slots.CAPACITY_ENV] = str(count + 1)
    try:
        with contextlib.ExitStack() as stack:
            for _ in range(count):
                stack.enter_context(gate_slots.hold(ROOT))
            yield
    finally:
        if previous is None:
            os.environ.pop(gate_slots.CAPACITY_ENV, None)
        else:
            os.environ[gate_slots.CAPACITY_ENV] = previous


def _active() -> int:
    import gate_slots

    return int(gate_slots.active())


def measure_once(gates: int, keep: Optional[Path]) -> dict:
    """One pytest stage with `gates` gates believed live, timed by `check.py` itself."""
    import gate_slots

    synthetic = max(0, gates - 1)
    with slots_held(synthetic):
        before = _active()
        # What the stage will be handed, computed the way `commands_for` computes it:
        # +1 because `check.py` takes a slot of its own for the length of its run.
        expected = gate_slots.workers(gates=before + 1)
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check.py"), "--only", "pytest"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NO_COLOR": "1", "PY_COLORS": "0"},
        )
        wall = time.monotonic() - started
        after = _active()

    output = CSI.sub("", (completed.stdout or "") + (completed.stderr or ""))
    stage = STAGE.search(output)
    suite = SUITE.search(output)
    passed = COUNTS.search(output)
    chosen = SELECTED.search(output)
    row = {
        "gates": gates,
        "synthetic_slots": synthetic,
        "workers_expected": expected,
        "workers_used": chosen.group(1) if chosen else None,
        "stage_seconds": float(stage.group(1)) if stage else None,
        "suite_seconds": float(suite.group(1)) if suite else None,
        "stage_verdict": "ok" if completed.returncode == 0 else "red",
        "wall_seconds": round(wall, 1),
        "passed": int(passed.group(1)) if passed else None,
        "slots_before": before,
        "slots_after": after,
        # A neighbour that came or went mid-run makes this sample describe a machine
        # other than the one asked for. Flagged rather than dropped: the reader decides.
        "neighbour_moved": before != after,
        "returncode": completed.returncode,
    }
    if keep is not None:
        # Every arm's output, not only a red one. Task-505 restarted a twenty-run arm
        # because the one red in it had been summarised and thrown away; re-running to
        # capture what you already had is the waste this costs a few megabytes to avoid.
        keep.mkdir(parents=True, exist_ok=True)
        path = keep / f"gates-{gates}-{int(time.time())}.log"
        path.write_text(output, encoding="utf-8")
        row["kept"] = str(path)
    return row


def render(rows: Sequence[dict]) -> str:
    head = (
        f"{'gates':>5}  {'-n':>5}  {'stage':>9}  {'suite':>9}  {'wall':>9}  "
        f"{'passed':>7}  {'slots':>9}  note"
    )
    lines = [head, "-" * len(head)]
    for row in rows:
        stage = f"{row['stage_seconds']:.1f}s" if row["stage_seconds"] else "-"
        suite = f"{row['suite_seconds']:.1f}s" if row.get("suite_seconds") else "-"
        note = []
        if row["neighbour_moved"]:
            note.append(f"NEIGHBOUR MOVED {row['slots_before']}->{row['slots_after']}")
        if row["stage_verdict"] != "ok":
            note.append("stage RED")
        lines.append(
            f"{row['gates']:>5}  {str(row['workers_used'] or '?'):>5}  {stage:>9}  "
            f"{suite:>9}  {row['wall_seconds']:>8.1f}s  {str(row['passed'] or '-'):>7}  "
            f"{row['slots_before']:>4}->{row['slots_after']:<4} {' '.join(note)}"
        )
    return "\n".join(lines)


def curve(gates: Sequence[int], reps: int, keep: Optional[Path]) -> int:
    rows: List[dict] = []
    total = len(gates) * reps
    done = 0
    for rep in range(1, reps + 1):
        for count in gates:
            done += 1
            print(f"[{done}/{total}] rep {rep}, {count} gate(s)...", flush=True)
            row = measure_once(count, keep)
            row["rep"] = rep
            rows.append(row)
            print(f"    {row['workers_used']} workers, {row['stage_seconds']}s", flush=True)
    print()
    print(render(rows))
    print()
    print(json.dumps(rows, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("curve", help="time the pytest stage against believed gate count")
    run.add_argument(
        "--gates",
        default="1,2,3,4,5",
        help="comma-separated gate counts to simulate (default 1,2,3,4,5)",
    )
    run.add_argument("--reps", type=int, default=1, help="repetitions of the whole curve")
    run.add_argument(
        "--keep",
        type=Path,
        default=ROOT / "out" / "gate-cost",
        help="where every arm's full output is kept (`out/` is gitignored)",
    )
    args = parser.parse_args(argv)
    counts = [int(piece) for piece in str(args.gates).split(",") if piece.strip()]
    return curve(counts, args.reps, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
