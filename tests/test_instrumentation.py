"""The per-request work counter, and the headers that report it.

The counter exists so a slow request can be attributed without a profiler, and so
later performance work can assert on *work done* rather than on wall-clock time.
A millisecond threshold means something different on every machine; "this request
parsed the corpus four times" means the same thing everywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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
from agentjobs.storage import TaskLoadError, TaskStorage, yaml_loader_name


def _build_task(task_id: str) -> Task:
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


def test_loading_a_task_counts_one_parse(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-001"))
    reset_task_parses()
    with count_task_parses() as tally:
        assert storage.load_task("task-001") is not None
    assert tally.parses == 1


def test_listing_counts_one_parse_per_file(tmp_path: Path) -> None:
    """The count must track the corpus, since that is what the epic is measuring."""
    storage = TaskStorage(tmp_path)
    for index in range(1, 6):
        storage.save_task(_build_task(f"task-{index:03d}"))
    reset_task_parses()
    with count_task_parses() as tally:
        assert len(storage.list_tasks()) == 5
    assert tally.parses == 5


def test_a_missing_file_is_not_counted_as_a_parse(tmp_path: Path) -> None:
    """Nothing was read, so nothing should be counted."""
    storage = TaskStorage(tmp_path)
    reset_task_parses()
    with count_task_parses() as tally:
        assert storage.load_task("task-999-absent") is None
    assert tally.parses == 0


def test_a_broken_file_still_counts_as_work_done(tmp_path: Path) -> None:
    """An unreadable file cost a read and a parse attempt, and must not vanish.

    Counting only successful loads would let a corpus of broken files report zero
    work while taking just as long, which is precisely the kind of blind spot the
    counter exists to remove.
    """
    storage = TaskStorage(tmp_path)
    (tmp_path / "task-002-broken.yaml").write_text("this: [is: not: valid", encoding="utf-8")
    reset_task_parses()
    with count_task_parses() as tally:
        with pytest.raises(TaskLoadError):
            storage.load_task("task-002-broken")
    assert tally.parses == 1


def test_responses_carry_the_measurement_headers() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert float(response.headers[MEASUREMENT_HEADER]) >= 0
    assert int(response.headers[PARSE_COUNT_HEADER]) == 0


def test_the_parse_header_survives_the_threadpool_hop() -> None:
    """A synchronous route runs in a worker thread, and the count must still arrive.

    This is the regression this test exists for. FastAPI copies the context into the
    worker, so an integer ContextVar set inside the route updates a copy the
    middleware never sees -- the first version of the counter reported zero for every
    request that actually read the corpus. A non-zero count here is the proof that
    the shared mutable counter crosses that boundary.
    """
    client = TestClient(app)
    response = client.get("/api/projects")
    assert response.status_code == 200
    assert int(response.headers[PARSE_COUNT_HEADER]) > 0


def test_each_request_reports_its_own_work_not_a_running_total() -> None:
    """Two identical requests must report the same count, not an accumulating one."""
    client = TestClient(app)
    first = client.get("/api/projects")
    second = client.get("/api/projects")
    assert int(first.headers[PARSE_COUNT_HEADER]) == int(second.headers[PARSE_COUNT_HEADER])


def test_the_yaml_loader_is_named_for_the_benchmark_report() -> None:
    """The benchmark prints this so a before/after pair is not compared across loaders."""
    assert yaml_loader_name()
