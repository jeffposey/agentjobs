"""Measure how long AgentJobs takes to answer, so changes can be judged by numbers.

Run it, change something, run it again, and compare. That is the whole purpose: the
performance work in task-130 relaxed the per-change human review gate in exchange for
recorded before/after numbers, and this is where those numbers come from.

    poetry run python scripts/bench.py                      # real corpus, full report
    poetry run python scripts/bench.py --json before.json   # keep it for comparison
    poetry run python scripts/bench.py --compare before.json
    poetry run python scripts/bench.py --corpus synthetic --tasks 200

## What it measures

**API** -- each endpoint from task-130's table, warmed once and then timed over N
iterations, reported as p50/p95. Alongside the wall-clock figure it reports the number
of task files the server parsed to answer, read from the ``X-Task-Parses`` response
header. That count is the more durable number: it means the same thing on every
machine, and it is what the corpus-loading work is actually about.

**CLI** -- cold processes, including interpreter startup, because that is what a
person waiting at a terminal experiences.

**Browser** -- a Playwright run that clicks a task row in the React list and waits for
the detail content to render. Click to *rendered*, not click to response: a fast
endpoint behind a component that paints nothing until every field arrives still feels
slow, and only the rendered timing would notice.

## Why it builds its own corpus

The benchmark never runs against the live project. It copies the task files into a
temporary project and serves that, so a run cannot write to the real backlog and is
not affected by whatever the long-running server on port 8876 happens to hold. The
synthetic mode generates a corpus of a stated size instead, which is what fixed
performance budgets need: a threshold tuned against 112 files becomes a failing test
at 300 through no fault of the code.

Two runs are only comparable if the corpus is the same, so every report states the
file count and total bytes it measured.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentjobs.project_setup import build_project_config  # noqa: E402
from agentjobs.storage import yaml_loader_name  # noqa: E402

DEFAULT_PORT = 18950
DEFAULT_ITERATIONS = 10
DEFAULT_SYNTHETIC_TASKS = 112
PROJECT_ID = "_local"
SERVER_START_TIMEOUT = 60.0


# ---------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------


@dataclass
class Measurement:
    """Timings for one repeatedly-executed thing."""

    name: str
    unit: str
    samples: List[float]
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def p50(self) -> Optional[float]:
        return statistics.median(self.samples) if self.samples else None

    @property
    def p95(self) -> Optional[float]:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        # Nearest-rank p95. At the iteration counts used here that is the honest
        # reading: interpolating between two samples would invent precision that ten
        # measurements do not contain.
        rank = max(1, int(round(0.95 * len(ordered))))
        return ordered[rank - 1]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["p50"] = self.p50
        data["p95"] = self.p95
        return data


@dataclass
class Section:
    """A named group of measurements, rendered as one block of the report."""

    name: str
    measurements: List[Measurement] = field(default_factory=list)
    note: Optional[str] = None


def measure(
    name: str,
    call: Callable[[], Optional[Dict[str, Any]]],
    *,
    iterations: int,
    warmup: int = 1,
    unit: str = "ms",
) -> Measurement:
    """Time ``call`` ``iterations`` times after discarding ``warmup`` runs.

    The warmup matters more than it looks. The first request to a fresh server pays
    for lazy imports, route resolution and a cold page cache; including it would
    flatter whichever surface happened to run second.
    """
    detail: Dict[str, Any] = {}
    try:
        for _ in range(warmup):
            call()
        samples: List[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            info = call()
            samples.append((time.perf_counter() - started) * 1000)
            if info:
                detail.update(info)
        return Measurement(name=name, unit=unit, samples=samples, detail=detail)
    except Exception as exc:  # noqa: BLE001 - one broken surface must not lose the rest
        return Measurement(name=name, unit=unit, samples=[], error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------------


SYNTHETIC_LOG_ENTRIES = 6


def _synthetic_task(index: int, *, total: int) -> Dict[str, Any]:
    """One generated task, shaped like a real one.

    Deliberately not minimal. A corpus of one-line stubs parses far faster than the
    real thing and would yield budgets that pass while the product is slow, so the
    generated records carry prose, a multi-entry log, acceptance criteria and a
    dependency -- the fields that make real task files a kilobyte or more each.
    """
    task_id = f"task-{index:03d}-generated-benchmark-task"
    body = (
        "Generated for the benchmark corpus. This text exists to give the file a "
        "realistic size, because a corpus of stubs parses far faster than the real "
        "backlog and would produce reassuring numbers that mean nothing. "
    ) * 3
    task: Dict[str, Any] = {
        "schema": 2,
        "id": task_id,
        "title": f"Generated benchmark task {index}",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "archived": False,
        "priority": ["low", "medium", "high", "critical"][index % 4],
        "category": "performance",
        "tags": ["generated", "benchmark"],
        "effort": "hours",
        "assignment": {"eligible": []},
        "spec": {
            "summary": f"Generated task {index} of {total}, for benchmarking only.",
            "intent": body,
            "description": body,
            "constraints": body,
            "out_of_scope": body,
            "context": [{"path": "src/agentjobs/storage.py", "why": body}],
        },
        "acceptance": [
            {"id": f"ac-{n}", "text": body, "verify": "generated", "status": "pending"}
            for n in range(1, 4)
        ],
        "deliverables": [],
        "dependencies": [],
        "links": [],
        "branches": [],
        "log": [
            {
                "id": entry,
                "ts": "2026-01-01T00:00:00Z",
                "actor": "claude",
                "type": "progress",
                "body": body,
            }
            for entry in range(1, SYNTHETIC_LOG_ENTRIES + 1)
        ],
    }
    # A dependency chain, so dependency resolution has real work to do rather than
    # walking empty lists on every record.
    if index > 1:
        task["dependencies"] = [
            {
                "task": f"task-{index - 1:03d}-generated-benchmark-task",
                "type": "related",
                "note": "Generated chain, so dependency facts are not trivially empty.",
            }
        ]
    return task


def build_corpus(destination: Path, *, kind: str, count: int, source: Path) -> None:
    """Populate ``destination`` with the corpus to measure."""
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "real":
        found = sorted(source.glob("*.yaml"))
        if not found:
            raise SystemExit(f"No task files found in {source}")
        for path in found:
            shutil.copy2(path, destination / path.name)
        return
    for index in range(1, count + 1):
        task = _synthetic_task(index, total=count)
        (destination / f"{task['id']}.yaml").write_text(
            yaml.safe_dump(task, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )


def corpus_size(directory: Path) -> Dict[str, int]:
    """File count and total bytes, so two reports can be told apart."""
    files = sorted(directory.glob("*.yaml"))
    return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}


# ---------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------


class BenchServer:
    """A dedicated AgentJobs server over a throwaway project.

    Its own port and its own project root, never the running instance: reusing a
    server whose warmth and contents this run does not control is exactly how a
    benchmark comes to measure something other than the change under test.
    """

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._process: Optional["subprocess.Popen[bytes]"] = None

    def __enter__(self) -> "BenchServer":
        env = dict(os.environ)
        env["AGENTJOBS_PROJECT_ROOT"] = str(self.root)
        env["AGENTJOBS_HOME"] = str(self.root / ".agentjobs-home")
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentjobs.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready()
        return self

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise SystemExit(
                    f"Benchmark server exited early with code {self._process.returncode}."
                )
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.15)
        raise SystemExit(f"Benchmark server did not become ready within {SERVER_START_TIMEOUT}s.")

    def __exit__(self, *exc: object) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            self._process.kill()
            self._process.wait(timeout=10)


def prepare_project(root: Path, *, kind: str, count: int, source: Path) -> Path:
    """Write a project config and its corpus under ``root``; return the tasks dir."""
    config_path = root / ".agentjobs" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            build_project_config(project_name="Benchmark project", user="Bench Human"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tasks_dir = root / "tasks"
    build_corpus(tasks_dir, kind=kind, count=count, source=source)
    return tasks_dir


# ---------------------------------------------------------------------------------
# The three surfaces
# ---------------------------------------------------------------------------------


def bench_api(server: BenchServer, *, iterations: int, sample_task_id: str) -> Section:
    """Time each endpoint from task-130's table, recording parses alongside time."""
    client = httpx.Client(base_url=server.base_url, timeout=120.0)
    prefix = f"/api/projects/{PROJECT_ID}"
    endpoints: Sequence[tuple[str, str]] = (
        ("GET /api/projects", "/api/projects"),
        ("GET /dashboard", f"{prefix}/dashboard"),
        ("GET /tasks", f"{prefix}/tasks"),
        ("GET /tasks/{id}/detail", f"{prefix}/tasks/{sample_task_id}/detail"),
        ("GET /search?q=the", f"{prefix}/search?q=the"),
        ("GET /revision", f"{prefix}/revision"),
    )

    def make_call(url: str) -> Callable[[], Dict[str, Any]]:
        def call() -> Dict[str, Any]:
            response = client.get(url)
            response.raise_for_status()
            parses = response.headers.get("X-Task-Parses")
            server_ms = response.headers.get("X-Response-Time-Ms")
            return {
                "task_parses": int(parses) if parses is not None else None,
                "server_ms": float(server_ms) if server_ms is not None else None,
                "bytes": len(response.content),
            }

        return call

    try:
        return Section(
            name="API",
            note="parses is the X-Task-Parses response header: task files read from disk.",
            measurements=[
                measure(name, make_call(url), iterations=iterations) for name, url in endpoints
            ],
        )
    finally:
        client.close()


def bench_cli(root: Path, *, iterations: int, sample_task_id: str) -> Section:
    """Time cold CLI processes, interpreter startup included."""
    env = dict(os.environ)
    env["AGENTJOBS_PROJECT_ROOT"] = str(root)
    env["AGENTJOBS_HOME"] = str(root / ".agentjobs-home")
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    commands: Sequence[tuple[str, List[str]]] = (
        ("agentjobs list --lifecycle ready", ["list", "--lifecycle", "ready"]),
        ("agentjobs next", ["next"]),
        ("agentjobs show <task>", ["show", sample_task_id]),
    )

    def make_call(args: List[str]) -> Callable[[], Dict[str, Any]]:
        def call() -> Dict[str, Any]:
            completed = subprocess.run(
                [sys.executable, "-m", "agentjobs.cli", *args],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return {"exit_code": completed.returncode}

        return call

    # Fewer iterations than the API: each one starts a whole interpreter, and the
    # spread between cold processes is narrow enough that ten adds minutes of noise.
    cli_iterations = max(3, iterations // 2)
    return Section(
        name="CLI (cold process, includes interpreter startup)",
        measurements=[
            measure(name, make_call(args), iterations=cli_iterations) for name, args in commands
        ],
    )


def bench_browser(server: BenchServer, *, iterations: int) -> Section:
    """Time click-to-rendered-detail in the packaged React app, via Playwright."""
    frontend = ROOT / "frontend"
    output_path = frontend / ".bench-open-task.json"
    if output_path.exists():
        output_path.unlink()

    env = dict(os.environ)
    env["BENCH_BASE_URL"] = server.base_url
    env["BENCH_ITERATIONS"] = str(iterations)
    env["BENCH_OUTPUT"] = str(output_path)

    def failed(reason: str) -> Section:
        return Section(
            name="Browser (packaged React app at /app/)",
            measurements=[
                Measurement(
                    name="click task row -> detail rendered",
                    unit="ms",
                    samples=[],
                    error=reason,
                )
            ],
        )

    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx is None:
        return failed("npx was not found on PATH; skipping the browser measurement.")

    completed = subprocess.run(
        [npx, "playwright", "test", "--config", "playwright.bench.config.ts"],
        cwd=str(frontend),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if not output_path.exists():
        tail = completed.stdout.decode("utf-8", "replace").strip().splitlines()[-15:]
        return failed("Playwright produced no timings:\n" + "\n".join(tail))

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    output_path.unlink()
    return Section(
        name="Browser (packaged React app at /app/)",
        note="Timed from the click to the task detail heading being visible.",
        measurements=[
            Measurement(
                name=entry["name"],
                unit="ms",
                samples=[float(value) for value in entry["samples"]],
                detail=entry.get("detail", {}),
            )
            for entry in payload["measurements"]
        ],
    )


# ---------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------


def format_report(report: Dict[str, Any]) -> str:
    """Render the human-readable report."""
    lines: List[str] = []
    meta = report["corpus"]
    lines.append("=" * 90)
    lines.append("AgentJobs benchmark")
    lines.append("=" * 90)
    lines.append(f"  when        {report['started_at']}")
    lines.append(f"  corpus      {meta['kind']}: {meta['files']} files, {meta['bytes']:,} bytes")
    lines.append(f"  iterations  {report['iterations']} (after 1 discarded warmup)")
    lines.append(f"  yaml loader {report['yaml_loader']}")
    lines.append(f"  python      {report['python']}")
    lines.append("")

    for section in report["sections"]:
        lines.append(section["name"])
        lines.append("-" * 90)
        header = f"  {'surface':<46}{'p50':>11}{'p95':>11}{'parses':>9}{'srv ms':>9}"
        lines.append(header)
        for entry in section["measurements"]:
            if entry.get("error"):
                lines.append(f"  {entry['name']:<46}{'ERROR':>11}")
                for line in str(entry["error"]).splitlines():
                    lines.append(f"      {line}")
                continue
            detail = entry.get("detail") or {}
            parses = detail.get("task_parses")
            server_ms = detail.get("server_ms")
            lines.append(
                f"  {entry['name']:<46}"
                f"{entry['p50']:>9.1f}ms"
                f"{entry['p95']:>9.1f}ms"
                f"{('-' if parses is None else str(parses)):>9}"
                f"{('-' if server_ms is None else format(server_ms, '.1f')):>9}"
            )
        if section.get("note"):
            lines.append(f"  note: {section['note']}")
        lines.append("")
    return "\n".join(lines)


def format_comparison(baseline: Dict[str, Any], current: Dict[str, Any]) -> str:
    """Render a before/after table, which is what the task logs actually need."""
    lines: List[str] = []
    lines.append("=" * 90)
    lines.append("Comparison against baseline")
    lines.append("=" * 90)
    before_corpus = baseline["corpus"]
    after_corpus = current["corpus"]
    lines.append(
        f"  baseline corpus  {before_corpus['kind']}: "
        f"{before_corpus['files']} files, {before_corpus['bytes']:,} bytes"
    )
    lines.append(
        f"  current corpus   {after_corpus['kind']}: "
        f"{after_corpus['files']} files, {after_corpus['bytes']:,} bytes"
    )
    if (before_corpus["files"], before_corpus["bytes"]) != (
        after_corpus["files"],
        after_corpus["bytes"],
    ):
        lines.append("  WARNING: the two runs measured different corpora. Not comparable.")
    lines.append("")

    before = {
        entry["name"]: entry
        for section in baseline["sections"]
        for entry in section["measurements"]
    }
    lines.append(f"  {'surface':<46}{'before':>11}{'after':>11}{'change':>10}{'parses':>16}")
    for section in current["sections"]:
        for entry in section["measurements"]:
            old = before.get(entry["name"])
            if not old or old.get("p50") is None or entry.get("p50") is None:
                continue
            old_p50, new_p50 = old["p50"], entry["p50"]
            factor = old_p50 / new_p50 if new_p50 else float("inf")
            old_parses = (old.get("detail") or {}).get("task_parses")
            new_parses = (entry.get("detail") or {}).get("task_parses")
            parse_note = (
                f"{old_parses} -> {new_parses}"
                if old_parses is not None and new_parses is not None
                else "-"
            )
            lines.append(
                f"  {entry['name']:<46}"
                f"{old_p50:>9.1f}ms"
                f"{new_p50:>9.1f}ms"
                f"{factor:>9.2f}x"
                f"{parse_note:>16}"
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the AgentJobs API, CLI and browser interaction.",
    )
    parser.add_argument(
        "--corpus",
        choices=("real", "synthetic"),
        default="real",
        help="Measure a copy of this repository's task files, or a generated corpus.",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=DEFAULT_SYNTHETIC_TASKS,
        help=f"Synthetic corpus size (default {DEFAULT_SYNTHETIC_TASKS}).",
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--json", type=Path, help="Write the machine-readable report here.")
    parser.add_argument(
        "--compare", type=Path, help="Print a before/after table against this JSON."
    )
    parser.add_argument("--skip-browser", action="store_true", help="Skip the Playwright timing.")
    parser.add_argument("--skip-cli", action="store_true", help="Skip the CLI timings.")
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "tasks" / "agentjobs",
        help="Where the real corpus is copied from.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    with TemporaryDirectory(prefix="agentjobs-bench-") as directory:
        root = Path(directory)
        tasks_dir = prepare_project(root, kind=args.corpus, count=args.tasks, source=args.source)
        size = corpus_size(tasks_dir)
        sample_task_id = sorted(path.stem for path in tasks_dir.glob("*.yaml"))[0]

        sections: List[Section] = []
        with BenchServer(root, args.port) as server:
            sections.append(
                bench_api(server, iterations=args.iterations, sample_task_id=sample_task_id)
            )
            if not args.skip_browser:
                sections.append(bench_browser(server, iterations=max(3, args.iterations // 2)))
        if not args.skip_cli:
            sections.append(
                bench_cli(root, iterations=args.iterations, sample_task_id=sample_task_id)
            )

        report: Dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "iterations": args.iterations,
            "python": sys.version.split()[0],
            "yaml_loader": yaml_loader_name(),
            "corpus": {"kind": args.corpus, **size},
            "sections": [
                {
                    "name": section.name,
                    "note": section.note,
                    "measurements": [item.to_dict() for item in section.measurements],
                }
                for section in sections
            ],
        }

    print(format_report(report))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        print(format_comparison(baseline, report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
