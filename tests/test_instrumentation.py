"""The per-request work counter, and the headers that report it.

The counter exists so a slow request can be attributed without a profiler, and so later
performance work can assert on *work done* rather than on wall-clock time. A millisecond
threshold means something different on every machine; "this request parsed the corpus
four times" means the same thing everywhere.

**What it counts is a task file being parsed, and a request no longer parses any**
(task-402). That makes it a different and stronger instrument than it was: zero is the
expected reading, so a non-zero one means a request has started reading a directory
again -- the exact coupling the migration removed, and a regression that would otherwise
be invisible because the answer would still be right. The counter's own mechanics are
pinned below over the file reader, which is the one thing that still parses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.main import MEASUREMENT_HEADER, PARSE_COUNT_HEADER, app
from agentjobs.instrumentation import (
    count_task_parses,
    record_task_parse,
    reset_task_parses,
    task_parse_count,
)
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.taskfiles import TaskFileCorpus, TaskLoadError, yaml_loader_name
from support import task_store


def _build_task(task_id: str, position: int = 100) -> Task:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        title="Sample",
        created=now,
        updated=now,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        priority=Priority.MEDIUM,
        queue_position=position,
        category="testing",
        spec=Spec(summary="Sample summary", description="Task description"),
    )


def test_counter_starts_at_zero_and_counts_up() -> None:
    reset_task_parses()
    assert task_parse_count() == 0
    record_task_parse()
    record_task_parse()
    assert task_parse_count() == 2


def test_reset_clears_the_counter() -> None:
    reset_task_parses()
    record_task_parse()
    reset_task_parses()
    assert task_parse_count() == 0


def test_block_counter_reports_only_its_own_parses() -> None:
    reset_task_parses()
    record_task_parse()
    with count_task_parses() as tally:
        record_task_parse()
        record_task_parse()
    assert tally.parses == 2


def test_tally_freezes_when_the_block_exits() -> None:
    """A tally read later must report the block, not everything since."""
    reset_task_parses()
    with count_task_parses() as tally:
        record_task_parse()
    record_task_parse()
    assert tally.parses == 1


def test_reading_a_file_counts_one_parse(tmp_path: Path) -> None:
    corpus = TaskFileCorpus(tmp_path)
    corpus.save_task(_build_task("task-001"))
    reset_task_parses()
    with count_task_parses() as tally:
        assert corpus.load_task("task-001") is not None
    assert tally.parses == 1


def test_reading_a_directory_counts_one_parse_per_file(tmp_path: Path) -> None:
    """An import is the one operation that still walks a corpus, and it is the big one."""
    corpus = TaskFileCorpus(tmp_path)
    for index in range(1, 6):
        corpus.save_task(_build_task(f"task-{index:03d}", position=index * 100))
    reset_task_parses()
    with count_task_parses() as tally:
        assert len(corpus.list_tasks()) == 5
    assert tally.parses == 5


def test_reading_a_record_from_the_store_parses_nothing(tmp_path: Path) -> None:
    """The reading that matters: zero, because a row is not a document to parse."""
    storage = task_store(tmp_path)
    storage.save_task(_build_task("task-001"))
    reset_task_parses()
    with count_task_parses() as tally:
        assert storage.load_task("task-001") is not None
        assert len(storage.list_tasks()) == 1
    assert tally.parses == 0


def test_a_missing_file_is_not_counted_as_a_parse(tmp_path: Path) -> None:
    """Nothing was read, so nothing should be counted."""
    corpus = TaskFileCorpus(tmp_path)
    reset_task_parses()
    with count_task_parses() as tally:
        assert corpus.load_task("task-999-absent") is None
    assert tally.parses == 0


def test_a_broken_file_still_counts_as_work_done(tmp_path: Path) -> None:
    """An unreadable file cost a read and a parse attempt, and must not vanish.

    Counting only successful loads would let a corpus of broken files report zero
    work while taking just as long, which is precisely the kind of blind spot the
    counter exists to remove.
    """
    corpus = TaskFileCorpus(tmp_path)
    (tmp_path / "task-002-broken.yaml").write_text("this: [is: not: valid", encoding="utf-8")
    reset_task_parses()
    with count_task_parses() as tally:
        with pytest.raises(TaskLoadError):
            corpus.load_task("task-002-broken")
    assert tally.parses == 1


def test_responses_carry_the_measurement_headers() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert float(response.headers[MEASUREMENT_HEADER]) >= 0
    assert int(response.headers[PARSE_COUNT_HEADER]) == 0


@pytest.fixture()
def a_served_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project of this test's own, served through the implicit single-project mode.

    Its own, and not the working directory's, for the reason task-378 found: with no
    registered project the implicit mode resolves from the cwd, and the cwd is this
    repository -- so these tests were reading *this repository's* records and depended
    on state they do not own.
    """
    from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    storage = task_store(tasks)
    storage.save_task(_build_task("task-001", position=100))
    storage.save_task(_build_task("task-002", position=200))
    monkeypatch.setenv(TASKS_DIR_ENV, str(tasks))
    reset_dependency_cache()
    yield tasks
    reset_dependency_cache()


def test_a_request_that_reads_the_backlog_parses_nothing(a_served_project: Path) -> None:
    """The reading this counter is for now: zero, on a route that reads every record.

    It used to assert a non-zero count, as the proof that the shared mutable counter
    crossed FastAPI's threadpool hop -- a synchronous route runs in a worker thread with
    a copied context, and the first version of the counter reported zero for every
    request that had genuinely read the corpus. That mechanism is asserted directly
    above, over the reader that still parses; what this asserts is the property that
    replaced it, and the one worth a standing check: no route reads a directory.
    """
    client = TestClient(app)
    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert int(response.headers[PARSE_COUNT_HEADER]) == 0


def test_each_request_reports_its_own_work_not_a_running_total(a_served_project: Path) -> None:
    """Two identical requests must report the same count, not an accumulating one."""
    client = TestClient(app)
    first = client.get("/api/tasks")
    second = client.get("/api/tasks")
    assert int(first.headers[PARSE_COUNT_HEADER]) == int(second.headers[PARSE_COUNT_HEADER])


def test_the_yaml_loader_is_named_for_the_benchmark_report() -> None:
    """The benchmark prints this so a before/after pair is not compared across loaders."""
    assert yaml_loader_name()
