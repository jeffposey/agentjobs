"""Budgets that stop the task-130 performance work from being given back quietly.

**These assert on work done, not on elapsed time.** A wall-clock threshold means
something different on every machine, drifts with hardware, fails on a loaded laptop,
and eventually gets loosened until it catches nothing -- or deleted, which is worse
than never having had it. "This request read each task file once" means the same thing
on every machine forever, and it is precisely the property the original defect
violated: a single `GET /tasks` used to walk a 119-file corpus 476 times.

Measured on the real corpus before the fixes (task-131's baseline) and after:

    GET /dashboard          7344ms, 952 parses  ->  174ms, 119 parses
    GET /tasks              3659ms, 476 parses  ->  181ms, 119 parses
    GET /tasks/{id}/detail  3954ms, 478 parses  ->  162ms, 119 parses

The parse counts are what these tests pin. The milliseconds are recorded in the task
log, where they belong, because they describe one afternoon on one laptop.

**The corpus is generated, at a fixed size.** Running against `tasks/` would tie the
thresholds to a backlog that grows, so the suite would start failing because the
project succeeded. The generator is the one `scripts/bench.py` uses, so a budget and a
benchmark run cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bench import build_corpus  # noqa: E402  - the shared synthetic-corpus generator

from agentjobs.api.dependencies import reset_dependency_cache  # noqa: E402
from agentjobs.api.main import PARSE_COUNT_HEADER, app  # noqa: E402
from agentjobs.instrumentation import count_task_parses, reset_task_parses  # noqa: E402
from agentjobs.project_setup import build_project_config  # noqa: E402
from agentjobs.storage import TaskStorage  # noqa: E402

#: Fixed, and stated. Big enough that a repeated corpus walk is unmissable in the
#: counts, small enough that generating it costs a fraction of a second.
CORPUS_SIZE = 60

#: Wall-clock budgets exist only to catch an order-of-magnitude collapse -- losing the
#: libyaml loader, or reintroducing a corpus walk in a loop. They are deliberately far
#: looser than the measured numbers (a whole-corpus load measures around 60ms for this
#: size; the budget is 5 seconds).
#:
#: **Do not tighten these.** A performance test that fails on a busy laptop gets
#: disabled, and a disabled test catches nothing at all. The parse-count assertions
#: below are the real gate; these only catch catastrophe.
CATASTROPHE_SECONDS = 5.0


@pytest.fixture(scope="module")
def budget_project(tmp_path_factory) -> Iterator[Path]:
    """A generated project of stated size, built once for the whole module."""
    root = tmp_path_factory.mktemp("budget-project")
    config = root / ".agentjobs" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            build_project_config(project_name="Budget project", user="Budget Human"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    build_corpus(root / "tasks", kind="synthetic", count=CORPUS_SIZE, source=root)
    yield root


@pytest.fixture
def budget_client(budget_project: Path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(budget_project))
    reset_dependency_cache()
    yield TestClient(app)
    reset_dependency_cache()


def _parses(response) -> int:
    assert response.status_code == 200, response.text
    return int(response.headers[PARSE_COUNT_HEADER])


class TestOneParsePerFilePerRequest:
    """The durable form of task-132, and the budget that would actually catch it.

    Each of these endpoints computes dependency facts, which used to mean four
    independent passes over the corpus. If someone adds a fifth caller that goes back
    to storage on its own, or moves a `list_tasks()` inside a loop, the count moves and
    this fails -- on any machine, at any speed.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/projects/_local/tasks",
            "/api/projects/_local/dashboard",
            "/api/projects/_local/tasks/task-001-generated-benchmark-task/detail",
            "/api/projects/_local/search?q=generated",
            "/api/projects/_local/tasks/next",
            "/api/projects/_local/tasks/broken",
        ],
    )
    def test_a_request_never_reads_a_task_file_twice(self, budget_client, path: str) -> None:
        parses = _parses(budget_client.get(path))
        assert parses <= CORPUS_SIZE, (
            f"{path} parsed {parses} task files for a {CORPUS_SIZE}-file corpus. "
            f"A request must walk the corpus at most once; {parses / CORPUS_SIZE:.1f} "
            "passes means something is reading storage independently again. "
            "See task-132: this was 4x, and the dashboard was 8x."
        )


class TestTheRevisionPollStaysCheap:
    """The 15-second poll runs forever, per connected client, on every device.

    It answers "has anything changed", and it must do that without parsing anything.
    Parsing in the poll would put the whole corpus through pydantic every 15 seconds
    for a question that never needed the answer.
    """

    def test_the_poll_parses_nothing(self, budget_client) -> None:
        parses = _parses(budget_client.get("/api/projects/_local/revision"))
        assert parses == 0, (
            f"GET /revision parsed {parses} task files. The revision signal is a hash "
            "over file bytes precisely so it can answer without parsing or validating; "
            "if it is parsing, something has started loading tasks to compute it."
        )


class TestTheStorageLayerItself:
    """Below the API, so a regression is attributed to storage rather than to a route."""

    def test_a_scope_parses_each_file_once_however_often_it_is_asked(
        self, budget_project: Path
    ) -> None:
        from agentjobs.storage import corpus_snapshot

        storage = TaskStorage(budget_project / "tasks")
        reset_task_parses()
        with corpus_snapshot():
            with count_task_parses() as tally:
                for _ in range(5):
                    storage.list_tasks()
                    storage.load_all()
        assert tally.parses == CORPUS_SIZE, (
            f"Ten whole-corpus reads inside one scope parsed {tally.parses} files "
            f"instead of {CORPUS_SIZE}. The request-scoped snapshot is not holding."
        )

    def test_without_a_scope_nothing_is_cached_across_calls(self, budget_project: Path) -> None:
        """The other half of the contract, and the one that keeps it honest.

        The snapshot must not leak into a process-wide cache. AgentJobs has several
        writers -- the CLI, other agents, git checkouts, a person editing YAML -- and a
        cache that outlived its request would serve a task record that had already
        changed on disk. task-134 was cancelled rather than take that risk; this test
        is what notices if it arrives by accident.
        """
        storage = TaskStorage(budget_project / "tasks")
        reset_task_parses()
        with count_task_parses() as tally:
            storage.list_tasks()
            storage.list_tasks()
        assert tally.parses == CORPUS_SIZE * 2, (
            f"Two unscoped corpus reads parsed {tally.parses} files instead of "
            f"{CORPUS_SIZE * 2}. Something is caching across scopes, which is only safe "
            "with an invalidation story -- see task-134's decision log."
        )

    def test_a_whole_corpus_load_has_not_collapsed(self, budget_project: Path) -> None:
        """The catastrophe check: order of magnitude only, never a percentage."""
        import time

        storage = TaskStorage(budget_project / "tasks")
        started = time.perf_counter()
        assert len(storage.list_tasks()) == CORPUS_SIZE
        elapsed = time.perf_counter() - started
        assert elapsed < CATASTROPHE_SECONDS, (
            f"Loading {CORPUS_SIZE} task files took {elapsed:.2f}s against a "
            f"{CATASTROPHE_SECONDS}s catastrophe budget. This budget is loose on "
            "purpose, so failing it means something structural: the libyaml loader is "
            "gone (13x), or the read path grew a per-file cost. Do not fix this by "
            "raising the number."
        )
