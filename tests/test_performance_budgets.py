"""Budgets that stop the task-130 performance work from being given back quietly.

**These assert on work done, not on elapsed time.** A wall-clock threshold means
something different on every machine, drifts with hardware, fails on a loaded laptop,
and eventually gets loosened until it catches nothing -- or deleted, which is worse
than never having had it. "This request read no task files" means the same thing on
every machine forever, and it is precisely the property the original defect violated:
a single `GET /tasks` used to walk a 119-file corpus 476 times.

Measured on the real corpus before the task-131 fixes and after:

    GET /dashboard          7344ms, 952 parses  ->  174ms, 119 parses
    GET /tasks              3659ms, 476 parses  ->  181ms, 119 parses
    GET /tasks/{id}/detail  3954ms, 478 parses  ->  162ms, 119 parses

**The budget is now zero, and that is a stronger assertion than the one it replaces.**
Records are rows (task-402), so a request that parses a task file is a request that has
started reading a directory again -- the exact coupling the migration removed, and the
kind of regression that would otherwise be invisible because it would still return the
right answer. The counter is kept for this and for nothing else.

**The corpus is generated, at a fixed size.** Running against the repository's own
backlog would tie the thresholds to something that grows, so the suite would start
failing because the project succeeded. The generator is the one `scripts/bench.py` uses,
so a budget and a benchmark run cannot drift apart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_script(name: str) -> ModuleType:
    """Load a repository script by path, the way the other script-reading tests do.

    ``from scripts.bench import ...`` looks equivalent and is not, because it depends on
    nothing else having claimed the name ``scripts`` first. Something does:
    ``scripts/build_release.py`` opens with ``from scripts.build_frontend import ...``
    under a ``try``, and ``test_frontend_packaging`` execs that file with the repository
    root off ``sys.path``. On Windows the import then succeeds against **pywin32's**
    ``site-packages/win32/scripts`` namespace package, the fallback branch quietly
    works, and ``sys.modules['scripts']`` is left pointing at pywin32 for the rest of
    the session. This module is collected after that one, so the package-style import
    resolved to a directory with no ``bench`` in it and the whole suite stopped at
    collection with ``No module named 'scripts.bench'``.

    Loading by file location cannot be poisoned that way, and it is what
    ``test_frontend_packaging`` and ``test_worktree_bootstrap`` already do.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_corpus = load_script("bench").build_corpus  # the shared synthetic-corpus generator

from agentjobs.api.dependencies import reset_dependency_cache  # noqa: E402
from agentjobs.api.main import PARSE_COUNT_HEADER, app  # noqa: E402
from agentjobs.project_setup import build_project_config  # noqa: E402
from agentjobs.taskfiles import TaskFileCorpus  # noqa: E402
from support import task_store  # noqa: E402


#: Fixed, and stated. Big enough that a repeated corpus walk is unmissable in the
#: counts, small enough that generating it costs a fraction of a second.
CORPUS_SIZE = 60

#: Wall-clock budgets exist only to catch an order-of-magnitude collapse -- a
#: per-record query in a loop, or a lost index. They are deliberately far looser than
#: the measured numbers (a whole-corpus read measures well under 60ms for this size;
#: the budget is 5 seconds).
#:
#: **Do not tighten these.** A performance test that fails on a busy laptop gets
#: disabled, and a disabled test catches nothing at all. The parse-count assertions
#: below are the real gate; these only catch catastrophe.
CATASTROPHE_SECONDS = 5.0


@pytest.fixture()
def budget_project(tmp_path_factory) -> Iterator[Path]:
    """A generated project of stated size.

    Per test rather than per module, and the reason is the machine home: the suite
    re-points ``AGENTJOBS_HOME`` for every test, and a project's database is resolved
    against it. A corpus imported once for the module would be sitting in a home the
    next test no longer looks in.
    """
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
    # Generated as files and then imported, which is the only road in now. The
    # directory is left in place: it is what the implicit project's database is named
    # from, and nothing reads the YAML after this line.
    build_corpus(root / "tasks", kind="synthetic", count=CORPUS_SIZE, source=root)
    store = task_store(root / "tasks", root=root)
    for task in TaskFileCorpus(root / "tasks", create=False).list_tasks():
        store.save_task(task)
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


class TestARequestParsesNothing:
    """The durable form of task-132, tightened to zero by task-402.

    Each of these endpoints computes dependency facts, which used to mean four
    independent passes over a directory. None of them may touch a file now, on any
    machine, at any speed.
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
    def test_a_request_reads_no_task_files_at_all(self, budget_client, path: str) -> None:
        parses = _parses(budget_client.get(path))
        assert parses == 0, (
            f"{path} parsed {parses} task files. A project's records are rows, so a "
            "request that parses YAML has started reading a directory again -- which "
            "would still return the right answer today and be wrong the moment the "
            "directory is retired. See task-132 for the 4x walk this replaces."
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
            f"GET /revision parsed {parses} task files. The revision signal exists to "
            "answer without loading or validating anything; if it is parsing, something "
            "has started loading tasks to compute it."
        )


class TestTheStorageLayerItself:
    """Below the API, so a regression is attributed to storage rather than to a route.

    The two tests that used to live here pinned the per-request parse snapshot -- that
    each file was parsed once inside a scope, and that nothing was cached across scopes.
    Both went with the parser they memoised (task-402); the assertion that survives them
    is the zero above, which is stronger than either.
    """

    def test_a_whole_corpus_load_has_not_collapsed(self, budget_project: Path) -> None:
        """The catastrophe check: order of magnitude only, never a percentage."""
        import time

        storage = task_store(budget_project / "tasks")
        started = time.perf_counter()
        assert len(storage.list_tasks()) == CORPUS_SIZE
        elapsed = time.perf_counter() - started
        assert elapsed < CATASTROPHE_SECONDS, (
            f"Reading {CORPUS_SIZE} records took {elapsed:.2f}s against a "
            f"{CATASTROPHE_SECONDS}s catastrophe budget. This budget is loose on "
            "purpose, so failing it means something structural: a per-record query in "
            "a loop, or an index gone. Do not fix this by raising the number."
        )
