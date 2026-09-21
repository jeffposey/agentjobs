"""Provoke the dispatch-subsystem flake on demand, and measure what causes it (task-505).

Two subcommands, because the task needed two different kinds of evidence and the next
one like it will too.

``pids`` measures how fast this machine recycles a process id. That is the mechanism
behind task-505: a pid recorded and acted on later is only as trustworthy as the
recycling rate, and on this machine it is not trustworthy at all::

    poetry run python scripts/flake_probe.py pids --spawns 2000
    2000 spawns in 198.8s (10/s), 988 distinct pids
    pid reuses observed: 1012
      spawns between reuse: min 3 median 234 max 1874
      seconds between reuse: min 0.35 median 23.73 max 191.09

``runs`` re-runs the files that flaked, as many times as asked, at the worker count a
contended gate would use, and reports the failure rate with every failing test named::

    poetry run python scripts/flake_probe.py runs --times 20 --slots 2

**Why a rate and not just a green run.** A flake is a rate, so a fix for one is a pair of
rates over the same files on the same machine. ``--slots`` is how the second half of that
pair is made comparable: it holds the gate slots ``scripts/gate_slots.py`` counts, so
pytest is handed the same ``-n`` it would get beside two other gates without needing two
other gates.

This is a probe, not a gate stage. It is minutes of wall clock by design and nothing runs
it automatically.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

AFFECTED = [
    "tests/test_execution_controller.py",
    "tests/test_dispatch_api.py",
    "tests/test_dispatch_finish.py",
    "tests/dispatch/test_durable_replay.py",
    "tests/test_auth_recovery.py",
    "tests/test_dispatch_posture_ceiling.py",
]
"""The files task-505 was filed about: each drives a real child process or shells out to
git, and across six gate runs of one tree nothing else in 5384 tests failed once."""

CSI = re.compile(r"\x1b\[[0-9;]*m")
"""Colour, which pytest emits even into a pipe and which hid every failing test id the
first time this script was used. ``finish.failing_tests`` had the identical defect and
the identical fix (task-419); stripping first is not optional here."""

FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
"""A failing test *or* one that errored in setup. Only ``FAILED`` was matched at first,
so a fixture error read as "red, no test named" -- which is exactly the sort of red this
script exists to explain."""


# ----- pid recycling ----------------------------------------------------------


def _spawn_once(_: int) -> Tuple[int, Optional[str], float]:
    from agentjobs.dispatch.pids import process_identity

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = process_identity(process.pid)
    process.wait()
    return process.pid, identity, time.monotonic()


def measure_pids(spawns: int, width: int) -> int:
    seen: dict[int, Tuple[Optional[str], int, float]] = {}
    reuses: List[Tuple[int, int, float]] = []
    started = time.monotonic()
    index = 0
    with ThreadPoolExecutor(max_workers=width) as pool:
        for pid, identity, when in pool.map(_spawn_once, range(spawns)):
            index += 1
            previous = seen.get(pid)
            if previous is not None and previous[0] != identity:
                reuses.append((pid, index - previous[1], when - previous[2]))
            seen[pid] = (identity, index, when)
    elapsed = time.monotonic() - started
    print(
        f"{spawns} spawns in {elapsed:.1f}s ({spawns / elapsed:.0f}/s), "
        f"{len(seen)} distinct pids"
    )
    print(f"pid reuses observed: {len(reuses)}")
    if reuses:
        gaps = sorted(item[1] for item in reuses)
        seconds = sorted(item[2] for item in reuses)
        print(
            f"  spawns between reuse: min {gaps[0]} median {gaps[len(gaps) // 2]} "
            f"max {gaps[-1]}"
        )
        print(
            f"  seconds between reuse: min {seconds[0]:.2f} "
            f"median {seconds[len(seconds) // 2]:.2f} max {seconds[-1]:.2f}"
        )
    return 0


# ----- the failure rate -------------------------------------------------------


@contextlib.contextmanager
def _slots_held(count: int):
    """Hold ``count`` gate slots, so pytest is handed a contended gate's worker count."""
    import gate_slots  # type: ignore[import-not-found]

    with contextlib.ExitStack() as stack:
        for _ in range(count):
            stack.enter_context(gate_slots.hold(ROOT))
        yield


def measure_runs(times: int, slots: int, files: Sequence[str], keep: Path) -> int:
    import gate_slots

    victims: collections.Counter[str] = collections.Counter()
    red = 0
    keep.mkdir(parents=True, exist_ok=True)
    with _slots_held(slots):
        workers = gate_slots.workers()
        gates = gate_slots.active()
        print(f"{times} runs of {len(files)} files at -n {workers} ({gates} gates hold slots)")
        print(f"red runs are kept under {keep}")
        for attempt in range(1, times + 1):
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", *files, "-n", workers, "-q"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # No colour from the child. A red run's output is read by this script
                # and by whoever opens the kept file, and CSI sequences broke both --
                # the same defect `finish.failing_tests` had (task-419).
                env={**os.environ, "NO_COLOR": "1", "PY_COLORS": "0"},
            )
            elapsed = time.monotonic() - started
            output = CSI.sub("", (completed.stdout or "") + (completed.stderr or ""))
            failing = FAILED.findall(output)
            victims.update(failing)
            verdict = "green"
            if completed.returncode != 0:
                red += 1
                verdict = "red"
                # **Kept, always.** A red whose cause nobody can look at afterwards is
                # worth almost nothing, and re-running to capture it is the waste this
                # script exists to stop.
                (keep / f"run-{attempt:02d}.log").write_text(output, encoding="utf-8")
            named = (" " + ", ".join(failing)) if failing else (" (no test named)" if red else "")
            print(f"  run {attempt:>3} {verdict} {elapsed:6.1f}s{named}", flush=True)
    print(f"{red}/{times} red")
    if victims:
        print("failing tests, by how often:")
        for name, count in victims.most_common():
            print(f"  {count} {name}")
    return 1 if red else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    pids = sub.add_parser("pids", help="how fast this machine recycles a process id")
    pids.add_argument("--spawns", type=int, default=2000)
    pids.add_argument("--width", type=int, default=8)

    runs = sub.add_parser("runs", help="re-run the affected files and report the failure rate")
    runs.add_argument("--times", type=int, default=20)
    runs.add_argument("--slots", type=int, default=0, help="extra gate slots to hold")
    runs.add_argument("--files", nargs="*", default=AFFECTED)
    runs.add_argument(
        "--keep",
        type=Path,
        default=ROOT / "flake-probe-reds",
        help="where a red run's output is written (gitignored)",
    )

    args = parser.parse_args(argv)
    if args.command == "pids":
        return measure_pids(args.spawns, args.width)
    return measure_runs(args.times, args.slots, args.files, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
