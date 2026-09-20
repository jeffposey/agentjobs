"""The benchmark measures a corpus the server actually holds, or it stops (task-408).

`scripts/bench.py` built its corpus by writing task YAML into a throwaway project root.
That stopped being a backlog when task-402 deleted the file backend, and for the four
days between the cutover and this file every run timed an empty database: the corpus
line said 112 files because it globbed the directory it had just written, and the detail
endpoint 404'd for an id the store had never heard of.

Nothing failed. That is the part worth a test. A benchmark whose numbers are wrong by
two orders of magnitude and whose table still renders is worse than no benchmark, so the
assertions here are about *refusing to report* rather than about any timing:

-   the seeding puts rows in the file the server will resolve for itself, and
-   a run whose server does not answer for them exits instead of printing a table.

There is no timing in here, deliberately. Wall-clock budgets belong in
`test_performance_budgets.py`, which asserts on work done for the same reason.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_script(name: str) -> ModuleType:
    """Load a repository script by path.

    By location rather than ``from scripts.bench import ...`` for the reason
    ``test_performance_budgets`` documents at length: something else in the suite can
    leave ``sys.modules['scripts']`` pointing at pywin32's namespace package, and the
    package-style import then fails at collection.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - a missing script
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = load_script("bench")

from agentjobs.store_factory import LOCAL_PROJECT_ID, local_database  # noqa: E402
from support import task_store  # noqa: E402

#: Small. These tests are about whether rows arrive, not about how fast they arrive,
#: and every extra generated record is a kilobyte of YAML through the importer.
CORPUS_SIZE = 4


# ----- a server that answers what the test tells it to -----------------------------


class _StubServer:
    """An HTTP server that answers the two questions the assertion asks.

    Real HTTP rather than a patched client, because what is being tested is that the
    check notices a *server* holding nothing -- and the shape of that failure is a
    status code and a JSON body, not a Python call. It takes about a millisecond to
    stand up.
    """

    def __init__(self, listed: List[Dict[str, Any]], detail_status: int) -> None:
        answers: Dict[str, Tuple[int, Any]] = {
            "tasks": (200, listed),
            "detail": (detail_status, {"detail": "Not Found"}),
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                key = "detail" if self.path.endswith("/detail") else "tasks"
                status, payload = answers[key]
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Silence the per-request line on stderr."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_StubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def seeded(tmp_path: Path) -> Iterator[Tuple[Path, Path, Any]]:
    """A benchmark project whose corpus has been imported. Yields (root, tasks, corpus)."""
    root = tmp_path / "bench-project"
    root.mkdir()
    tasks_dir = bench.prepare_project(root, kind="synthetic", count=CORPUS_SIZE, source=None)
    corpus = bench.seed_store(root, tasks_dir, kind="synthetic")
    yield root, tasks_dir, corpus


class TestTheCorpusReachesTheStore:
    """The defect itself: files written, nothing served."""

    def test_the_rows_land_in_the_file_the_server_will_open(self, seeded) -> None:
        """Seeded through ``local_database``, read back through ``local_database``.

        The two sides have to agree or the benchmark seeds one database and serves
        another -- which is a failure with no symptom, since an unseeded store answers
        every request successfully and fast.
        """
        _, tasks_dir, corpus = seeded
        assert corpus.database == local_database(tasks_dir)

        store = task_store(tasks_dir, project_id=LOCAL_PROJECT_ID)
        ids = sorted(task.id for task in store.list_tasks())
        assert len(ids) == CORPUS_SIZE
        assert corpus.tasks == CORPUS_SIZE
        assert corpus.sample_task_id in ids

    def test_the_sample_task_comes_from_the_store_not_a_glob(self, seeded) -> None:
        """The id the detail timing uses is one the store answered for.

        Taking it from ``tasks_dir.glob("*.yaml")`` is what made every run's detail
        measurement a 404: the name of a file is not evidence of a row.
        """
        _, tasks_dir, corpus = seeded
        store = task_store(tasks_dir, project_id=LOCAL_PROJECT_ID)
        assert store.load_task(corpus.sample_task_id) is not None

    def test_the_report_states_the_rows_and_not_only_the_files(self, seeded) -> None:
        """``corpus.files`` was the whole story, and it was the misleading half."""
        _, _, corpus = seeded
        block = corpus.to_dict()
        assert block["tasks"] == CORPUS_SIZE
        assert block["files"] == CORPUS_SIZE
        assert block["bytes"] > 0


class TestARunThatLoadedNothingStops:
    """Every one of these printed a table of plausible timings before task-408."""

    def test_seeding_an_empty_directory_exits(self, tmp_path: Path) -> None:
        root = tmp_path / "empty-project"
        (root / "tasks").mkdir(parents=True)
        (root / ".agentjobs").mkdir()
        with pytest.raises(SystemExit) as raised:
            bench.seed_store(root, root / "tasks", kind="synthetic")
        assert "nothing to measure" in str(raised.value)

    def test_a_server_listing_no_tasks_exits(self, seeded) -> None:
        _, _, corpus = seeded
        with _StubServer(listed=[], detail_status=200) as server:
            with pytest.raises(SystemExit) as raised:
                bench.assert_corpus_is_served(server, corpus)
        assert "serving a different store" in str(raised.value)

    def test_a_sample_task_the_server_does_not_hold_exits(self, seeded) -> None:
        _, _, corpus = seeded
        listed = [{"id": f"task-{n:03d}"} for n in range(CORPUS_SIZE)]
        with _StubServer(listed=listed, detail_status=404) as server:
            with pytest.raises(SystemExit) as raised:
                bench.assert_corpus_is_served(server, corpus)
        assert corpus.sample_task_id in str(raised.value)

    def test_a_server_holding_the_corpus_passes_and_reports_its_rows(self, seeded) -> None:
        _, _, corpus = seeded
        listed = [{"id": f"task-{n:03d}"} for n in range(CORPUS_SIZE)]
        with _StubServer(listed=listed, detail_status=200) as server:
            assert bench.assert_corpus_is_served(server, corpus) == CORPUS_SIZE


class TestComparingAgainstAnOlderRun:
    """A before/after pair is the reason this tool exists, so a bad pair must say so."""

    @staticmethod
    def _report(corpus: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "corpus": corpus,
            "sections": [
                {
                    "name": "API",
                    "note": None,
                    "measurements": [
                        {"name": "GET /tasks", "p50": 10.0, "p95": 11.0, "detail": {}}
                    ],
                }
            ],
        }

    def test_a_baseline_from_before_the_seeding_is_called_out(self) -> None:
        """No ``tasks`` key means the baseline measured an empty store, whatever it says.

        Such a JSON file is still sitting in people's working directories and still
        loads, so the refusal to compare quietly has to be in the code rather than in a
        sentence in `docs/performance.md`.
        """
        baseline = self._report({"kind": "synthetic", "files": 112, "bytes": 1_209_921})
        current = self._report(
            {"kind": "synthetic", "files": 112, "bytes": 1_209_921, "tasks": 112}
        )
        rendered = bench.format_comparison(baseline, current)
        assert "predates task-408" in rendered
        assert "Not comparable" in rendered

    def test_two_seeded_runs_of_the_same_corpus_compare_without_a_warning(self) -> None:
        corpus = {"kind": "synthetic", "files": 112, "bytes": 1_209_921, "tasks": 112}
        rendered = bench.format_comparison(self._report(corpus), self._report(dict(corpus)))
        assert "WARNING" not in rendered
        assert "112 tasks served" in rendered
