"""Count the processes each test starts, over the whole suite (task-525).

**Why a count and not a time.** Task-518 found the pytest stage bounded by process
creation, and found process creation on this machine ranging from 0.064s to 16.7s for
identical argv depending on what else was running. A wall-clock figure through that noise
is an anecdote; a spawn count is the same on any machine and any evening, so it is the
before/after figure this file exists to produce.

    poetry run python scripts/spawn_census.py                  # whole suite, -n auto
    poetry run python scripts/spawn_census.py tests/dispatch   # any pytest selection
    poetry run python scripts/spawn_census.py --report DIR     # re-rank an earlier run

**What it counts.** Every ``subprocess.Popen`` constructed *in a pytest worker*, which is
what ``subprocess.run``, ``check_output`` and asyncio's subprocess transport all go
through. Each is attributed to the test running at the time and classed by its argv. It
does not see what a spawned process spawns in turn -- a fake runner starting its own
child, a served app's own subprocesses -- because those happen in another interpreter.
That is a floor, stated rather than hidden.

**How.** This file is also a pytest plugin, loaded with ``-p`` into every xdist worker.
It wraps ``Popen.__init__`` at configure time and writes one JSON per process into the
directory named by ``AGENTJOBS_SPAWN_CENSUS_DIR``. The driver then merges them and ranks.
Nothing on disk is patched.

Not a gate stage: it is a measurement, run before and after a change that claims to
reduce what the suite starts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CENSUS_ENV = "AGENTJOBS_SPAWN_CENSUS_DIR"

_current_test: Optional[str] = None
_records: List[Dict[str, Any]] = []


def classify(argv: Any) -> str:
    """Name a spawn by what it runs, coarsely enough that a table has a few rows.

    The class is the program plus, for an interpreter, the module or script it runs and
    the first subcommand -- ``python fake_runner.py agents`` rather than every flag.
    """
    if isinstance(argv, (str, bytes)):
        parts = str(argv).split()
    else:
        parts = [str(a) for a in argv]
    if not parts:
        return "<empty>"
    program = Path(parts[0]).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if program.endswith(suffix):
            program = program[: -len(suffix)]
    rest = parts[1:]
    if program.startswith("python"):
        if rest[:1] == ["-c"]:
            return "python -c"
        if rest[:1] == ["-m"] and len(rest) > 1:
            head = f"python -m {rest[1]}"
            rest = rest[2:]
        elif rest:
            head = f"python {Path(rest[0]).name}"
            rest = rest[1:]
        else:
            return "python"
        words = [w for w in rest if not w.startswith("-")][:1]
        return " ".join([head, *words])
    if program == "git":
        while rest[:1] in (["-C"], ["-c"]):
            rest = rest[2:]
        words = [w for w in rest if not w.startswith("-")][:1]
        return " ".join(["git", *words])
    words = [w for w in rest if not w.startswith("-") and len(w) < 40][:1]
    return " ".join([program, *words])


def pytest_configure(config: Any) -> None:
    """Wrap ``Popen.__init__`` in this process, if the census asked for it."""
    if not os.environ.get(CENSUS_ENV):
        return
    original = subprocess.Popen.__init__

    def counting_init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> None:
        _records.append({"test": _current_test or "<collection or fixture>", "argv": args})
        original(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = counting_init  # type: ignore[method-assign]


def pytest_runtest_protocol(item: Any, nextitem: Any) -> None:
    global _current_test
    _current_test = item.nodeid


def pytest_unconfigure(config: Any) -> None:
    directory = os.environ.get(CENSUS_ENV)
    if not directory:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    per_test: Dict[str, Counter] = defaultdict(Counter)
    for record in _records:
        per_test[record["test"]][classify(record["argv"])] += 1
    out = Path(directory) / f"{worker}-{os.getpid()}.json"
    out.write_text(
        json.dumps({test: dict(counts) for test, counts in per_test.items()}, indent=1),
        encoding="utf-8",
    )


def merge(directory: Path) -> Dict[str, Counter]:
    merged: Dict[str, Counter] = defaultdict(Counter)
    for path in sorted(directory.glob("*.json")):
        for test, counts in json.loads(path.read_text(encoding="utf-8")).items():
            merged[test].update(counts)
    return merged


def report(directory: Path, top: int) -> None:
    merged = merge(directory)
    total = sum(sum(c.values()) for c in merged.values())
    by_class: Counter = Counter()
    by_file: Counter = Counter()
    for test, counts in merged.items():
        by_class.update(counts)
        by_file[test.split("::")[0]] += sum(counts.values())
    print(f"TOTAL SPAWNS {total} across {len(merged)} tests")
    print("\nBy argv class:")
    for name, count in by_class.most_common(top):
        print(f"  {count:6d}  {name}")
    print("\nBy file:")
    for name, count in by_file.most_common(top):
        print(f"  {count:6d}  {name}")
    print("\nBy test:")
    ranked = sorted(merged.items(), key=lambda kv: -sum(kv[1].values()))
    for test, counts in ranked[:top]:
        detail = ", ".join(f"{n} x{c}" for n, c in counts.most_common(3))
        print(f"  {sum(counts.values()):6d}  {test}  ({detail})")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("selection", nargs="*", help="pytest arguments (default: tests)")
    parser.add_argument("--report", type=Path, help="re-rank an earlier census directory")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--serial", action="store_true", help="no -n auto")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the checkout to measure, run with its own interpreter (a before/after pair)",
    )
    parser.add_argument("--python", default=sys.executable, help="that checkout's interpreter")
    args = parser.parse_args(argv)
    if args.report:
        report(args.report, args.top)
        return 0
    directory = Path(tempfile.mkdtemp(prefix="spawn-census-"))
    env = dict(os.environ)
    env[CENSUS_ENV] = str(directory)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "scripts"), env.get("PYTHONPATH", "")])
    command = [args.python, "-m", "pytest", "-q", "-p", "spawn_census"]
    if not args.serial:
        command += ["-n", "auto"]
    command += args.selection or ["tests"]
    started = time.monotonic()
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(command, cwd=args.root, env=env)
    print(f"\npytest exit {result.returncode} in {time.monotonic() - started:.1f}s")
    print(f"census directory: {directory}\n")
    report(directory, args.top)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
