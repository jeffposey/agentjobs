"""Measure how long AgentJobs takes to answer, so changes can be judged by numbers.

Run it, change something, run it again, and compare. That is the whole purpose: the
performance work in task-130 relaxed the per-change human review gate in exchange for
recorded before/after numbers, and this is where those numbers come from.

    poetry run python scripts/bench.py --corpus synthetic --tasks 200
    poetry run python scripts/bench.py --json before.json   # keep it for comparison
    poetry run python scripts/bench.py --compare before.json
    poetry run python scripts/bench.py --corpus real --source <an exported directory>

**The corpus is seeded into the store, not written beside it** (task-408). Records are
rows since task-402, so a directory of task YAML is inert to the server: every run
between that cutover and this one timed an empty database and said nothing about it.
The YAML is now an import *source* -- written into a throwaway project, imported with
``CorpusImporter`` into the database the benchmark server will open, and then proved to
be served before a single timing is taken. A run that seeded nothing, or whose sample
task the running server does not hold, exits non-zero rather than printing a table.

The ``--corpus real`` default source went with this repository's tracked records in
task-380, which is why that mode asks for a directory; ``agentjobs storage export``
writes one.

## What it measures

**API** -- each endpoint from task-130's table, warmed once and then timed over N
iterations, reported as p50/p95. Alongside the wall-clock figure it reports the
``X-Task-Parses`` response header: task files the server read from disk to answer.

Since task-402 that number is **expected to be zero**, and it is reported for exactly
that reason -- it is the cheap standing assertion that no request has quietly started
reading a directory again. It is not evidence that a corpus loaded, because an empty
store reports zero too; that a corpus loaded is asserted against the store and the
running server instead, before any timing runs.

Beside it is ``X-Corpus-Loads``: times the request loaded **every task in the
project**. That is the number the slow endpoints were actually made of (task-485) --
``/dashboard`` asked the corpus nine separate questions and loaded it nine times, which
no timer here could have attributed, because it looked exactly like one slow endpoint.
One is the expected value for a request that needs the corpus at all, and zero for
``/revision``, which answers from a counter.

**CLI** -- cold processes, including interpreter startup, because that is what a
person waiting at a terminal experiences.

**Browser** -- a Playwright run that clicks a task row in the React list and waits for
the detail content to render. Click to *rendered*, not click to response: a fast
endpoint behind a component that paints nothing until every field arrives still feels
slow, and only the rendered timing would notice.

## Why it builds its own corpus

The benchmark never runs against the live project. It writes the task files into a
temporary project, imports them into a database named from that temporary directory,
and serves that -- so a run cannot write to the real backlog, and is not affected by
whatever the long-running server on port 8876 happens to hold. The synthetic mode
generates a corpus of a stated size instead, which is what fixed performance budgets
need: a threshold tuned against 112 files becomes a failing test at 300 through no
fault of the code.

Two runs are only comparable if the corpus is the same, so every report states the
number of task rows the server held, and the file count and total bytes they were
imported from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentjobs.__version__ import __version__  # noqa: E402
from agentjobs.project_setup import build_project_config  # noqa: E402
from agentjobs.sqlstore import SqlTaskStore  # noqa: E402
from agentjobs.sqlstore.connection import Database  # noqa: E402
from agentjobs.sqlstore.importer import CorpusImporter  # noqa: E402
from agentjobs.sqlstore.migrations import upgrade  # noqa: E402
from agentjobs.store_factory import LOCAL_PROJECT_ID, local_database  # noqa: E402
from agentjobs.taskfiles import yaml_loader_name  # noqa: E402

BENCH_PORT_ENV = "AGENTJOBS_BENCH_PORT"
BENCH_PORT_BASE = 30000
BENCH_PORT_SPAN = 10000


def checkout_port(root: Path) -> int:
    """Pick the port this checkout's benchmark server owns.

    Same reasoning as ``frontend/playwright.config.ts``: several worktrees of this
    repository are worked at once, and a module-level constant means the second run
    fails to bind. Deriving the port from the checkout's path keeps it stable for a
    given worktree, so a bind failure can be attributed from the path alone.

    A different base and a different span from the gate's, so a benchmark and a gate
    running in the same checkout cannot land on one socket either.
    """
    override = os.environ.get(BENCH_PORT_ENV, "").strip()
    if override:
        port = int(override)
        if not 1 <= port <= 65535:
            raise SystemExit(f"{BENCH_PORT_ENV} must be between 1 and 65535, got {port}.")
        return port
    digest = hashlib.sha256(f"{root}\nbench".encode()).digest()
    return BENCH_PORT_BASE + (int.from_bytes(digest[:4], "big") % BENCH_PORT_SPAN)


DEFAULT_PORT = checkout_port(ROOT)
DEFAULT_ITERATIONS = 10
DEFAULT_SYNTHETIC_TASKS = 112
PROJECT_ID = LOCAL_PROJECT_ID
"""The id the server gives a directory nobody registered.

Taken from ``store_factory`` rather than spelled again here: a store is keyed on (file,
project id), so a second spelling would seed rows under an id the server never asks
about and leave it serving an empty project -- the same silent-empty failure task-408
was, one layer down.
"""

HOME_DIRNAME = ".agentjobs-home"
"""The throwaway AgentJobs home, under the run's temporary root.

Three processes have to agree on it -- this one, which seeds the store, the server
subprocess, and the CLI subprocesses -- because it is what ``local_database`` names the
database from. A disagreement would not raise: each side would quietly open a different
empty file.
"""

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


def bench_home(root: Path) -> Path:
    """The throwaway AgentJobs home for a run rooted at ``root``."""
    return root / HOME_DIRNAME


SYNTHETIC_LOG_ENTRIES = 6

#: The instant the newest generated log entry carries. Every other entry is a whole
#: number of seconds before it.
SYNTHETIC_LOG_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _synthetic_entry_ts(index: int, entry: int, *, total: int) -> str:
    """A distinct timestamp for one generated log entry, newest at the end.

    **Every generated entry used to carry the same instant**, which made the corpus
    degenerate for any question about log order: a panel showing the ten newest entries
    in a project where all 2,880 of them are simultaneous has to consider all 2,880 to
    answer, so a bounded read and the whole-corpus read it replaced were
    indistinguishable (task-498). No real project looks like that, and the budget these
    records exist for is meant to catch a read that follows the corpus.

    One second apart, ascending with entry id within a task and with index across them,
    and always at or before :data:`SYNTHETIC_LOG_BASE` so nothing is stamped after the
    ``updated`` the records carry. The format is fixed-width, so the bytes a record puts
    on the wire are what they were.
    """
    behind = (total - index) * SYNTHETIC_LOG_ENTRIES + (SYNTHETIC_LOG_ENTRIES - entry)
    return (SYNTHETIC_LOG_BASE - timedelta(seconds=behind)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        # Every open task needs a place in its band, and the bands here are the four
        # above round-robin -- so index // 4 numbers each band 100, 200, 300, ...
        # without two generated tasks ever colliding.
        "queue_position": (index // 4 + 1) * 100,
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
                "ts": _synthetic_entry_ts(index, entry, total=total),
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


def synthetic_documents(count: int) -> List[Dict[str, Any]]:
    """The generated corpus as documents, before anything writes them anywhere.

    ``build_corpus`` dumps these to YAML; ``tests/test_performance_budgets.py`` validates
    them into records and saves them straight into a store. Both reach the corpus through
    this one function so a budget and a benchmark cannot come to measure different
    records -- which is the property the budget module's docstring claims, and it used to
    rest on the budgets going through the YAML round trip that the benchmark goes
    through. That round trip costs about 6ms a record, all of it in ``yaml.safe_dump``,
    and at a corpus near the real backlog's size it dominated the budget module.
    """
    return [_synthetic_task(index, total=count) for index in range(1, count + 1)]


def build_corpus(destination: Path, *, kind: str, count: int, source: Optional[Path]) -> None:
    """Populate ``destination`` with the corpus to measure."""
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "real":
        if source is None:
            raise SystemExit(
                "--corpus real needs --source: this repository's tracked records were "
                "retired in task-380, so there is no longer a directory to default to. "
                "`agentjobs storage export <dir>` writes one."
            )
        found = sorted(source.glob("*.yaml"))
        if not found:
            raise SystemExit(f"No task files found in {source}")
        for path in found:
            shutil.copy2(path, destination / path.name)
        # The sidecar too, or the corpus cannot be imported at all: a log entry naming
        # an attachment whose bytes are absent is refused, and the whole task with it.
        # Nine of this repository's own 488 records quarantined that way on 2026-09-19,
        # which is enough for the import check below to refuse the run -- so `--corpus
        # real` was unusable against any export that had ever had a screenshot on it.
        attachments = source / "attachments"
        if attachments.is_dir():
            shutil.copytree(attachments, destination / "attachments", dirs_exist_ok=True)
        return
    for task in synthetic_documents(count):
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
        # Named so a bind failure says which checkout wanted the socket.
        print(f"[bench] checkout {ROOT} serving {self.base_url}", flush=True)
        env = dict(os.environ)
        env["AGENTJOBS_PROJECT_ROOT"] = str(self.root)
        env["AGENTJOBS_HOME"] = str(bench_home(self.root))
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


def prepare_project(root: Path, *, kind: str, count: int, source: Optional[Path]) -> Path:
    """Write a project config and its corpus under ``root``; return the tasks dir.

    The directory it returns is an import source, not a backlog. Nothing serves it --
    see :func:`seed_store`, which is what makes the records reachable.
    """
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


@dataclass
class SeededCorpus:
    """The corpus the store holds, and the files it was built from."""

    kind: str
    files: int
    bytes: int
    tasks: int
    database: Path
    sample_task_id: str

    def to_dict(self) -> Dict[str, Any]:
        """The report's ``corpus`` block: what two runs are compared on."""
        return {"kind": self.kind, "files": self.files, "bytes": self.bytes, "tasks": self.tasks}


def seed_store(root: Path, tasks_dir: Path, *, kind: str) -> SeededCorpus:
    """Import the corpus into the database the benchmark server will open.

    **Importing rather than building records through the manager** (task-408). The two
    would produce comparable rows, and the import is the closer match to what this
    benchmark measured before the cutover -- the same documents through the same
    validation -- while exercising the import path as a bonus. It is also the only one
    of the two that gives ``--corpus real`` an answer, since what a real corpus is
    available as today is a directory from ``agentjobs storage export``.

    The file is the one the server will resolve for itself: ``local_database`` names it
    from the tasks directory, which is the only identity a project nobody registered
    has. Both sides computing it from the same directory is what stops this process
    seeding one database while the server serves another.

    Raises ``SystemExit`` if the import wrote no rows or quarantined anything. A
    benchmark that reports plausible numbers for a corpus it never loaded is worse than
    no benchmark.
    """
    database_path = local_database(tasks_dir)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    try:
        # No snapshot: the database was created three lines ago, and a VACUUM INTO of
        # an empty file is a backup of nothing.
        upgrade(database, agentjobs_version=__version__, snapshot_before=False)
        store = SqlTaskStore(database, PROJECT_ID)
        store.ensure_project(root=str(root))
        report = CorpusImporter(store, tasks_dir).run()
        task_ids = sorted(task.id for task in store.list_tasks())
    finally:
        # The server is a separate process opening this same file, and this process
        # holds a write connection until the database is closed.
        database.close()

    size = corpus_size(tasks_dir)
    if report.quarantined:
        raise SystemExit(
            "The benchmark corpus did not import cleanly, so the numbers would be "
            "measured over less than was asked for:\n" + report.render()
        )
    if not task_ids:
        raise SystemExit(
            f"Seeded no tasks into {database_path} from {size['files']} file(s) in "
            f"{tasks_dir}. There is nothing to measure."
        )
    return SeededCorpus(
        kind=kind,
        files=size["files"],
        bytes=size["bytes"],
        tasks=len(task_ids),
        database=database_path,
        sample_task_id=task_ids[0],
    )


def assert_corpus_is_served(server: BenchServer, corpus: SeededCorpus) -> int:
    """Prove the running server answers for the seeded corpus. Returns the rows it listed.

    Two questions, in the order that makes a failure legible. Does the list endpoint
    return anything -- a server that resolved a different database answers ``[]`` here,
    which is what every run between task-402 and task-408 was really reporting. And does
    the sample task's detail endpoint return 200 -- that surface 404'd on every one of
    those runs, because the id came from a glob over YAML the store had never read.

    Asked before any measurement, so a benchmark with nothing to measure exits instead
    of printing a table of timings for an empty store.
    """
    prefix = f"/api/projects/{PROJECT_ID}"
    with httpx.Client(base_url=server.base_url, timeout=120.0) as client:
        listed = client.get(f"{prefix}/tasks")
        if listed.status_code != 200:
            raise SystemExit(
                f"The benchmark server answered {listed.status_code} for {prefix}/tasks; "
                f"{corpus.tasks} task(s) were seeded into {corpus.database}."
            )
        rows = len(listed.json())
        if rows == 0:
            raise SystemExit(
                f"The benchmark server lists no tasks, though {corpus.tasks} were "
                f"imported into {corpus.database}. It is serving a different store, so "
                "every timing below would be of an empty one."
            )
        detail = client.get(f"{prefix}/tasks/{corpus.sample_task_id}/detail")
        if detail.status_code != 200:
            raise SystemExit(
                f"The benchmark server answered {detail.status_code} for the sample "
                f"task {corpus.sample_task_id}, which was imported into "
                f"{corpus.database}. Nothing measured against it would mean anything."
            )
    return rows


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
        ("GET /tasks/next", f"{prefix}/tasks/next"),
        ("GET /revision", f"{prefix}/revision"),
    )

    def make_call(url: str) -> Callable[[], Dict[str, Any]]:
        def call() -> Dict[str, Any]:
            response = client.get(url)
            response.raise_for_status()
            parses = response.headers.get("X-Task-Parses")
            loads = response.headers.get("X-Corpus-Loads")
            server_ms = response.headers.get("X-Response-Time-Ms")
            return {
                "task_parses": int(parses) if parses is not None else None,
                "corpus_loads": int(loads) if loads is not None else None,
                "server_ms": float(server_ms) if server_ms is not None else None,
                "bytes": len(response.content),
            }

        return call

    try:
        return Section(
            name="API",
            note=(
                "parses is the X-Task-Parses response header: task files read from "
                "disk. Zero is the expected value since task-402 -- it says no request "
                "read a directory, not that the corpus is empty. loads is "
                "X-Corpus-Loads: times a request loaded every task in the project. "
                "That is the number task-485's endpoints were made of -- the dashboard "
                "asked the corpus nine separate questions and loaded it nine times -- "
                "and one is the expected value for a request that needs the corpus at "
                "all."
            ),
            measurements=[
                measure(name, make_call(url), iterations=iterations) for name, url in endpoints
            ],
        )
    finally:
        client.close()


def bench_cli(root: Path, *, iterations: int, sample_task_id: str) -> Section:
    """Time cold CLI processes, interpreter startup included.

    **Run from the benchmark project, not from this checkout** (task-408).
    ``AGENTJOBS_PROJECT_ROOT`` is read by the API and by nothing else: the CLI resolves
    its project from the working directory. Started in this repository it answered for
    *this* checkout's project -- which, under the throwaway ``AGENTJOBS_HOME`` the rest
    of the run uses, is an unregistered directory with an empty database. `list` said
    "No tasks found" and exited 0, and the timing went into the table looking ordinary.
    """
    env = dict(os.environ)
    env["AGENTJOBS_PROJECT_ROOT"] = str(root)
    env["AGENTJOBS_HOME"] = str(bench_home(root))
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
                cwd=str(root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                # `measure` turns this into an ERROR row rather than ending the run:
                # a broken CLI should not cost the API numbers. But it must not be
                # timed silently either -- a command that fails fast is fast.
                detail = completed.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"`agentjobs {' '.join(args)}` exited {completed.returncode} against "
                    f"the benchmark store:\n{detail[-800:]}"
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
    if not payload["measurements"]:
        # The spec writes its output in `afterAll`, which runs when the test failed as
        # well -- so an empty file is a failure, and printing it as a section with no
        # rows reported a broken measurement as nothing at all (task-483).
        tail = completed.stdout.decode("utf-8", "replace").strip().splitlines()[-15:]
        return failed("Playwright ran but recorded no timings:\n" + "\n".join(tail))
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
    lines.append(
        f"  corpus      {meta['kind']}: {meta.get('tasks', '?')} tasks served"
        f" (imported from {meta['files']} files, {meta['bytes']:,} bytes)"
    )
    lines.append(f"  iterations  {report['iterations']} (after 1 discarded warmup)")
    lines.append(f"  yaml loader {report['yaml_loader']}")
    lines.append(f"  python      {report['python']}")
    lines.append("")

    for section in report["sections"]:
        lines.append(section["name"])
        lines.append("-" * 90)
        header = f"  {'surface':<42}{'p50':>11}{'p95':>11}{'parses':>8}{'loads':>7}{'srv ms':>9}"
        lines.append(header)
        for entry in section["measurements"]:
            if entry.get("error"):
                lines.append(f"  {entry['name']:<42}{'ERROR':>11}")
                for line in str(entry["error"]).splitlines():
                    lines.append(f"      {line}")
                continue
            detail = entry.get("detail") or {}
            parses = detail.get("task_parses")
            loads = detail.get("corpus_loads")
            server_ms = detail.get("server_ms")
            lines.append(
                f"  {entry['name']:<42}"
                f"{entry['p50']:>9.1f}ms"
                f"{entry['p95']:>9.1f}ms"
                f"{('-' if parses is None else str(parses)):>8}"
                f"{('-' if loads is None else str(loads)):>7}"
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

    def describe(corpus: Dict[str, Any]) -> str:
        return (
            f"{corpus['kind']}: {corpus.get('tasks', '?')} tasks served "
            f"(from {corpus['files']} files, {corpus['bytes']:,} bytes)"
        )

    lines.append(f"  baseline corpus  {describe(before_corpus)}")
    lines.append(f"  current corpus   {describe(after_corpus)}")
    if (before_corpus["files"], before_corpus["bytes"]) != (
        after_corpus["files"],
        after_corpus["bytes"],
    ):
        lines.append("  WARNING: the two runs measured different corpora. Not comparable.")
    # A baseline with no `tasks` key predates task-408, which means it was taken against
    # a store nothing had been seeded into: the corpus line said 112 files and the
    # server held none. Such a pair is not a before/after of anything, and saying so is
    # the whole reason the key is checked rather than defaulted quietly.
    if "tasks" not in before_corpus:
        lines.append(
            "  WARNING: the baseline predates task-408 and was measured against an "
            "empty store, whatever its corpus line says. Not comparable."
        )
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
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            f"Port for the benchmark server (default {DEFAULT_PORT}, derived from this "
            f"checkout's path; {BENCH_PORT_ENV} overrides)."
        ),
    )
    parser.add_argument("--json", type=Path, help="Write the machine-readable report here.")
    parser.add_argument(
        "--compare", type=Path, help="Print a before/after table against this JSON."
    )
    parser.add_argument("--skip-browser", action="store_true", help="Skip the Playwright timing.")
    parser.add_argument("--skip-cli", action="store_true", help="Skip the CLI timings.")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Where the real corpus is copied from. Required by --corpus real, which no "
            "longer has a default: task-380 retired this repository's tracked records. "
            "`agentjobs storage export <dir>` writes a directory to point at."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    # ``ignore_cleanup_errors`` because the run now leaves a SQLite database in here,
    # and on Windows the handle a just-terminated server held is not always released by
    # the time the directory is removed. Losing the whole report to a failed rmtree of a
    # directory the OS will reap anyway is the wrong trade.
    with TemporaryDirectory(prefix="agentjobs-bench-", ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        # Set in this process, not only in the subprocess environments below: it is
        # what `local_database` reads to place the file, so the seeding here and the
        # server's own resolution have to be answering the same question. It also
        # guarantees nothing in this run can reach the real home.
        os.environ["AGENTJOBS_HOME"] = str(bench_home(root))

        tasks_dir = prepare_project(root, kind=args.corpus, count=args.tasks, source=args.source)
        corpus = seed_store(root, tasks_dir, kind=args.corpus)
        sample_task_id = corpus.sample_task_id

        sections: List[Section] = []
        with BenchServer(root, args.port) as server:
            served = assert_corpus_is_served(server, corpus)
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
            "corpus": {**corpus.to_dict(), "served": served},
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
