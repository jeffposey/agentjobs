"""What a *shape* of concurrent gates costs this machine, in gates per hour.

`scripts/gate_cost.py` answers a different question and answers it well: what one gate's
pytest stage costs when it believes N gates are sharing the machine. It buys that number
by holding synthetic slots, so the gate is throttled without any neighbours -- which is
exactly how task-513 separated "the budget is throttling us" from "the machine is
contended", and exactly why it cannot answer this one. A held slot consumes no memory,
starts no Playwright browser and runs no second copy of the other 11% of the gate.

**The question here is throughput, not per-gate time**, and the two disagree whenever
scaling is sublinear. A gate at 8 workers costing 1.6x a gate at 26 is a *loss* per gate
and a *win* per machine, if three of them run at once. So this script runs real gates,
concurrently, and reports the arm by what it actually produced:

    gates per hour = K / wave_seconds * 3600

Each arm is a wave of `K` whole `scripts/check.py` runs, one per worktree, started
together and watched to the last one finishing. The wave, not the mean gate, is the
arm's cost: an arm is done when its slowest member is done, because the next wave of
work cannot start on a machine that is still gating.

    python scripts/gate_shape.py arm --label A --worktree ../worktrees/agentjobs-534-a
    python scripts/gate_shape.py arm --label C --worktree ..a --worktree ..b --worktree ..c
    python scripts/gate_shape.py report out/gate-shape

**The capacity is lifted to `K` for the arm and for nothing else.** `gate_slots.CAPACITY`
is two, so a three-gate arm would queue its third rather than measure it, which is the
deadlock `gate_cost.slots_held` documents. `AGENTJOBS_GATE_CAPACITY` is set to `K` in the
gates' own environment, which both lifts the queue and makes the division `26 // K`
rather than `26 // min(K, 2)`. That is the one sanctioned use of the variable: measuring
a shape the default forbids is the whole point, and the arm records the width every gate
actually reported so a lifted capacity can never be mistaken for the default one.

**Every gate is unqualified.** The confounder task-534 names is that pytest is most of
the gate and the rest is fixed cost every concurrent gate pays in full, so an arm of
three pays it three times. Measuring `--only pytest` would measure the confounder away.

**Memory is sampled for the whole machine, not for the gates.** Two gates at `-n auto`
drove this machine to 6 MB free on 2026-09-05 with CPU at a third, so the binding
constraint is not obviously cores and an arm that reports only seconds cannot say which
it was. Every arm carries its own lowest free memory and peak committed bytes beside its
wall clock, sampled from `GlobalMemoryStatusEx` and `tasklist`.

**Neighbours are recorded, not assumed away.** This machine dispatches real agents while
a probe runs. Each arm records the gate slots visible before and after it, and says so
when they disagree -- the same honesty `gate_cost.py` settled on, for the same reason.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CSI = re.compile(r"\x1b\[[0-9;]*m")
"""Colour, stripped before anything is matched. The same defect twice already -- see
`gate_cost.CSI`, `finish.failing_tests` (task-419) and task-505's probe."""

STAGE_ROW = re.compile(r"^ {2}(\w+) {2,}([0-9.]+)s\s*$", re.MULTILINE)
"""One row of `check.format_timings`, including its `total`. Two leading spaces and at
least two before the figure, which is what distinguishes the table from pytest's own
output and from the `=== stage ok in 12.3s ===` banners a concurrent run prints."""

WIDTH = re.compile(r"pytest runs at -n (\w+), ")
"""The width from `gate_slots.note`, printed at the top of every pytest stage.

Read from the gate's own announcement rather than computed here. A shape comparison whose
widths are asserted rather than observed is measuring what it assumed."""

PASSED = re.compile(r"(\d+) passed")

ACTIVATION_VARS = ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV")
"""Stripped from every subprocess environment, as `scripts/bootstrap.py` strips them.

A dispatched session inherits `VIRTUAL_ENV` pointing at the main clone, Poetry prefers an
activated environment over the one it keys on the project path, and a gate launched
through it would refuse to run at all. Naming each worktree's own interpreter is the
other half; this is the half that makes `poetry -P ... env info` answer about the
worktree rather than about whatever the shell activated."""


# --------------------------------------------------------------------------- sampling


class _MemoryStatus(ctypes.Structure):
    """`MEMORYSTATUSEX`, which is the cheapest honest answer to "is this machine full".

    Sampling with `Get-Counter` costs about a second per sample and sampling with WMI
    spawns a process; this is a syscall, so the interval can be short enough that a
    minute-long spike is not missed between two samples.
    """

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def machine_memory() -> Optional[Dict[str, float]]:
    """Free physical memory and committed bytes, in MB, or None off Windows."""
    if os.name != "nt":
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:  # noqa: BLE001 - a sample is an optimisation; the arm is not
        return None
    committed = status.ullTotalPageFile - status.ullAvailPageFile
    return {
        "free_mb": status.ullAvailPhys / 1024 / 1024,
        "committed_mb": committed / 1024 / 1024,
        "load_percent": float(status.dwMemoryLoad),
    }


def parse_tasklist(text: str, images: Sequence[str]) -> Dict[str, Dict[str, float]]:
    """Working-set totals and counts per image name, from `tasklist /fo csv /nh`.

    Split out from the call so the parsing has a test that does not need a machine in a
    particular state. `tasklist` reports the working set with thousands separators and a
    trailing ` K`, and reports `N/A` for a process it could not read.
    """
    wanted = {name.lower() for name in images}
    totals: Dict[str, Dict[str, float]] = {name: {"count": 0.0, "ws_mb": 0.0} for name in wanted}
    for row in csv.reader(text.splitlines()):
        if len(row) < 5:
            continue
        image = row[0].strip().lower()
        if image not in wanted:
            continue
        digits = re.sub(r"[^\d]", "", row[4])
        if not digits:
            continue
        totals[image]["count"] += 1
        totals[image]["ws_mb"] += int(digits) / 1024
    return totals


def process_memory(images: Sequence[str]) -> Optional[Dict[str, Dict[str, float]]]:
    """:func:`parse_tasklist` over a live `tasklist`, or None where it cannot be run."""
    if os.name != "nt":
        return None
    try:
        done = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - see `machine_memory`
        return None
    if done.returncode != 0:
        return None
    return parse_tasklist(done.stdout or "", images)


IMAGES = ("python.exe", "node.exe")
"""What a gate is made of on this machine: xdist workers and the frontend lane.

Reported separately from the machine-wide free figure because the two answer different
halves of the question. The machine figure says whether anything was left for the owner;
these say whether it was the gates that took it."""


@dataclass
class Sampler:
    """A thread that watches the machine while an arm runs.

    Peaks and troughs only: a full trace is written too, but the summary is what an arm
    is judged on and a mean over a wave that starts and ends idle understates both.
    """

    interval: float = 5.0
    trace: List[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    def _sample(self) -> None:
        memory = machine_memory()
        processes = process_memory(IMAGES)
        row: dict = {"at": time.time()}
        if memory:
            row.update(memory)
        if processes:
            for image, figures in processes.items():
                key = image.split(".")[0]
                row[f"{key}_count"] = figures["count"]
                row[f"{key}_ws_mb"] = figures["ws_mb"]
        self.trace.append(row)

    def _loop(self) -> None:
        self._sample()
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception:  # noqa: BLE001 - a missing sample is not a failed arm
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="gate-shape-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2)

    def summary(self) -> dict:
        """The figures an arm is compared on: the worst moment of each series."""
        if not self.trace:
            return {"samples": 0}
        gate_ws = [
            row.get("python_ws_mb", 0.0) + row.get("node_ws_mb", 0.0)
            for row in self.trace
            if "python_ws_mb" in row or "node_ws_mb" in row
        ]
        free = [row["free_mb"] for row in self.trace if "free_mb" in row]
        committed = [row["committed_mb"] for row in self.trace if "committed_mb" in row]
        counts = [row.get("python_count", 0.0) for row in self.trace if "python_count" in row]
        return {
            "samples": len(self.trace),
            "lowest_free_mb": round(min(free), 1) if free else None,
            "peak_committed_mb": round(max(committed), 1) if committed else None,
            "peak_gate_ws_mb": round(max(gate_ws), 1) if gate_ws else None,
            "peak_python_processes": int(max(counts)) if counts else None,
        }


# ------------------------------------------------------------------------------ gates


def detached_environment() -> Dict[str, str]:
    """This process's environment with any activated virtualenv removed."""
    return {key: value for key, value in os.environ.items() if key not in ACTIVATION_VARS}


def interpreter_for(worktree: Path) -> Path:
    """The worktree's *own* virtualenv interpreter, which is the only correct one.

    Borrowing another checkout's environment runs the gate against that checkout's source
    and reports a result that says nothing about this tree -- which `check.py` refuses,
    so getting this wrong costs an arm rather than corrupting one. Asked of Poetry rather
    than guessed from a hash, because the hash is Poetry's business.
    """
    done = subprocess.run(
        ["poetry", "-P", str(worktree), "env", "info", "--path"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=detached_environment(),
        shell=os.name == "nt",
    )
    path = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not path:
        raise SystemExit(
            f"No virtualenv for {worktree}. Run `python scripts/bootstrap.py` in it first.\n"
            f"{(done.stderr or '').strip()}"
        )
    venv = Path(path[-1])
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@dataclass
class GateRun:
    """One whole `check.py` in one worktree, and what it reported about itself."""

    worktree: Path
    interpreter: Path
    environment: Dict[str, str]
    keep: Optional[Path] = None
    started: float = 0.0
    seconds: float = 0.0
    returncode: Optional[int] = None
    output: str = ""

    def run(self, begun: float) -> None:
        self.started = time.monotonic() - begun
        at = time.monotonic()
        done = subprocess.run(
            [str(self.interpreter), "scripts/check.py"],
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
        )
        self.seconds = time.monotonic() - at
        self.returncode = done.returncode
        self.output = CSI.sub("", (done.stdout or "") + (done.stderr or ""))

    def row(self) -> dict:
        stages = {name: float(value) for name, value in STAGE_ROW.findall(self.output)}
        width = WIDTH.search(self.output)
        passed = PASSED.search(self.output)
        kept = None
        if self.keep is not None:
            self.keep.parent.mkdir(parents=True, exist_ok=True)
            self.keep.write_text(self.output, encoding="utf-8")
            kept = str(self.keep)
        return {
            "worktree": self.worktree.name,
            "workers": width.group(1) if width else None,
            "gate_seconds": round(self.seconds, 1),
            "started_offset": round(self.started, 1),
            "stage_total": stages.get("total"),
            "pytest": stages.get("pytest"),
            "e2e": stages.get("e2e"),
            "vitest": stages.get("vitest"),
            "build": stages.get("build"),
            "verdict": "ok" if self.returncode == 0 else "red",
            "returncode": self.returncode,
            "passed": int(passed.group(1)) if passed else None,
            "kept": kept,
        }


def slots_seen() -> int:
    import gate_slots  # type: ignore[import-not-found]

    return int(gate_slots.active())


def run_wave(worktrees: Sequence[Path], keep: Optional[Path], label: str, rep: int) -> dict:
    """Start one gate per worktree at the same moment and watch every one of them down.

    The wave is the measurement, so nothing is reported until the last gate returns. A
    mean over gates that finished at different times would flatter a shape whose slowest
    member is what actually holds the machine.
    """
    width = len(worktrees)
    environment = detached_environment()
    environment.update(
        {
            "AGENTJOBS_GATE_CAPACITY": str(width),
            "NO_COLOR": "1",
            "PY_COLORS": "0",
        }
    )
    stamp = int(time.time())
    gates = [
        GateRun(
            worktree=tree,
            interpreter=interpreter_for(tree),
            environment=dict(environment),
            keep=(keep / f"{label}-rep{rep}-{tree.name}-{stamp}.log") if keep else None,
        )
        for tree in worktrees
    ]

    sampler = Sampler()
    before = slots_seen()
    sampler.start()
    begun = time.monotonic()
    threads = [
        threading.Thread(target=gate.run, args=(begun,), name=gate.worktree.name) for gate in gates
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wave = time.monotonic() - begun
    sampler.stop()
    after = slots_seen()

    rows = [gate.row() for gate in gates]
    return {
        "label": label,
        "rep": rep,
        "concurrency": width,
        "wave_seconds": round(wave, 1),
        "gates_per_hour": round(width / wave * 3600, 2),
        "gates": rows,
        "reds": [row["worktree"] for row in rows if row["verdict"] != "ok"],
        "memory": sampler.summary(),
        "slots_before": before,
        "slots_after": after,
        # `before` counts nothing (this process holds no slot) and `after` should too. A
        # non-zero either end is a real gate belonging to somebody else, which makes this
        # sample describe a machine other than the one asked for.
        "neighbour_present": bool(before or after),
        "trace": sampler.trace,
        "at": stamp,
    }


# ----------------------------------------------------------------------------- output


def render(waves: Sequence[dict]) -> str:
    head = (
        f"{'arm':>4}  {'rep':>3}  {'K':>2}  {'-n':>4}  {'wave':>8}  {'gates/h':>8}  "
        f"{'slowest':>8}  {'fastest':>8}  {'free MB':>8}  {'gate GB':>8}  note"
    )
    lines = [head, "-" * len(head)]
    for wave in waves:
        seconds = [gate["gate_seconds"] for gate in wave["gates"]]
        widths = {gate["workers"] for gate in wave["gates"]}
        memory = wave.get("memory") or {}
        free = memory.get("lowest_free_mb")
        peak = memory.get("peak_gate_ws_mb")
        note = []
        if wave["reds"]:
            note.append("RED: " + ",".join(wave["reds"]))
        if wave.get("neighbour_present"):
            note.append(f"NEIGHBOUR {wave['slots_before']}->{wave['slots_after']}")
        lines.append(
            f"{wave['label']:>4}  {wave['rep']:>3}  {wave['concurrency']:>2}  "
            f"{'/'.join(sorted(str(w) for w in widths)):>4}  {wave['wave_seconds']:>7.1f}s  "
            f"{wave['gates_per_hour']:>8.2f}  {max(seconds):>7.1f}s  {min(seconds):>7.1f}s  "
            f"{(f'{free:.0f}' if free else '-'):>8}  "
            f"{(f'{peak / 1024:.1f}' if peak else '-'):>8}  {' '.join(note)}"
        )
    return "\n".join(lines)


def load(directory: Path) -> List[dict]:
    waves: List[dict] = []
    for path in sorted(directory.glob("wave-*.json")):
        try:
            waves.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(waves, key=lambda wave: (wave.get("label", ""), wave.get("rep", 0)))


def arm(
    worktrees: Sequence[Path],
    label: str,
    reps: int,
    out: Path,
    keep: Optional[Path],
    first: int = 1,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    waves: List[dict] = []
    for rep in range(first, first + reps):
        print(f"[{label} rep {rep}/{reps}] {len(worktrees)} concurrent gate(s)...", flush=True)
        wave = run_wave(worktrees, keep, label, rep)
        path = out / f"wave-{label}-{rep}-{wave['at']}.json"
        path.write_text(json.dumps(wave, indent=2) + "\n", encoding="utf-8")
        waves.append(wave)
        print(
            f"    {wave['wave_seconds']:.1f}s wave, {wave['gates_per_hour']:.2f} gates/h, "
            f"lowest free {wave['memory'].get('lowest_free_mb')} MB -> {path.name}",
            flush=True,
        )
    print()
    print(render(waves))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("arm", help="run one shape of concurrent gates and time the wave")
    one.add_argument("--label", required=True, help="the arm's name in the table, e.g. A")
    one.add_argument(
        "--worktree",
        action="append",
        required=True,
        type=Path,
        help="a bootstrapped worktree to gate in; repeat once per concurrent gate",
    )
    one.add_argument("--reps", type=int, default=1, help="repetitions of the wave")
    one.add_argument(
        "--rep",
        type=int,
        default=1,
        help=(
            "the number the first repetition is recorded under, so arms interleaved "
            "by a driver script do not all record themselves as rep 1"
        ),
    )
    one.add_argument("--out", type=Path, default=ROOT / "out" / "gate-shape")
    one.add_argument(
        "--keep",
        type=Path,
        default=ROOT / "out" / "gate-shape" / "logs",
        help="where every gate's full output is kept (`out/` is gitignored)",
    )

    table = sub.add_parser("report", help="render every wave recorded under a directory")
    table.add_argument("directory", type=Path, nargs="?", default=ROOT / "out" / "gate-shape")

    args = parser.parse_args(argv)
    if args.command == "report":
        waves = load(args.directory)
        if not waves:
            print(f"No waves recorded under {args.directory}.")
            return 1
        print(render(waves))
        return 0
    trees = [tree.resolve() for tree in _unique(args.worktree)]
    return arm(trees, args.label, args.reps, args.out.resolve(), args.keep.resolve(), args.rep)


def _unique(paths: Iterable[Path]) -> List[Path]:
    """Each worktree once. Two gates in one checkout share a build directory and ports."""
    seen: List[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


if __name__ == "__main__":
    raise SystemExit(main())
