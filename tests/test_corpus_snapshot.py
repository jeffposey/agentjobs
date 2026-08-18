"""One request, one parse per task file.

These are the assertions that outlive the hardware. A wall-clock threshold drifts with
the machine and eventually gets loosened until it catches nothing; "this request read
each file once" means the same thing forever, and it is the property the repeated
corpus walks actually violated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.main import PARSE_COUNT_HEADER, app
from agentjobs.instrumentation import count_task_parses, reset_task_parses
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.storage import TaskStorage, corpus_snapshot

CORPUS_SIZE = 12


def _build_task(index: int, *, parent: str | None = None) -> Task:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Task(
        id=f"task-{index:03d}",
        title=f"Task {index}",
        created=now,
        updated=now,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        priority=Priority.MEDIUM,
        category="testing",
        parent=parent,
        spec=Spec(summary=f"Task {index} summary", description="Description"),
    )


@pytest.fixture
def corpus(tmp_path: Path) -> TaskStorage:
    """A small corpus with a parent/child relationship, so the gates do real work."""
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task(1))
    for index in range(2, CORPUS_SIZE + 1):
        storage.save_task(_build_task(index, parent="task-001" if index % 3 == 0 else None))
    return storage


def test_without_a_scope_each_call_reparses(corpus: TaskStorage) -> None:
    """The baseline behaviour, kept explicit so the fix is visibly a change."""
    reset_task_parses()
    with count_task_parses() as tally:
        corpus.list_tasks()
        corpus.list_tasks()
    assert tally.parses == CORPUS_SIZE * 2


def test_a_scope_parses_each_file_once(corpus: TaskStorage) -> None:
    reset_task_parses()
    with corpus_snapshot():
        with count_task_parses() as tally:
            corpus.list_tasks()
            corpus.list_tasks()
            corpus.load_all()
    assert tally.parses == CORPUS_SIZE


def test_dependency_facts_no_longer_walks_the_corpus_four_times(corpus: TaskStorage) -> None:
    """The specific defect: four independent passes for one answer."""
    manager = TaskManager(corpus)
    reset_task_parses()
    with corpus_snapshot():
        with count_task_parses() as tally:
            tasks = manager.list_tasks()
            manager.dependency_facts(tasks)
    assert tally.parses == CORPUS_SIZE


def test_a_write_inside_the_scope_is_visible_to_the_next_read(corpus: TaskStorage) -> None:
    """A stale snapshot serving a record that has already changed is the failure mode.

    Cheaper to make the write drop the snapshot than to patch the written task into
    it, and much easier to be sure is correct.
    """
    with corpus_snapshot():
        assert corpus.load_task("task-001") is not None
        before = {task.id: task.title for task in corpus.list_tasks()}
        assert before["task-001"] == "Task 1"

        changed = _build_task(1)
        changed.title = "Renamed inside the scope"
        corpus.save_task(changed)

        after = {task.id: task.title for task in corpus.list_tasks()}
        assert after["task-001"] == "Renamed inside the scope"


def test_a_delete_inside_the_scope_is_visible_to_the_next_read(corpus: TaskStorage) -> None:
    with corpus_snapshot():
        assert len(corpus.list_tasks()) == CORPUS_SIZE
        assert corpus.delete_task("task-002") is True
        assert len(corpus.list_tasks()) == CORPUS_SIZE - 1


def test_a_scope_does_not_outlive_itself(corpus: TaskStorage) -> None:
    """The multi-writer guarantee: the next scope re-reads from disk.

    AgentJobs is written for several writers -- the CLI, agents, git checkouts, a
    person editing YAML. A snapshot that survived its scope would hide all of them.
    """
    with corpus_snapshot():
        assert len(corpus.list_tasks()) == CORPUS_SIZE

    # Written from outside, exactly as another process would.
    outside = TaskStorage(corpus.tasks_dir)
    outside.save_task(_build_task(CORPUS_SIZE + 1))

    with corpus_snapshot():
        assert len(corpus.list_tasks()) == CORPUS_SIZE + 1


def test_two_projects_each_get_their_own_snapshot(tmp_path: Path) -> None:
    """Keyed by tasks directory, so one project's parse is not served for another's."""
    first = TaskStorage(tmp_path / "one")
    second = TaskStorage(tmp_path / "two")
    first.save_task(_build_task(1))
    second.save_task(_build_task(2))
    second.save_task(_build_task(3))

    with corpus_snapshot():
        assert [task.id for task in first.list_tasks()] == ["task-001"]
        assert [task.id for task in second.list_tasks()] == ["task-002", "task-003"]


def test_broken_files_are_still_reported_by_filename_inside_a_scope(
    corpus: TaskStorage,
) -> None:
    """Speed must not cost the broken-file reporting; that is a load-bearing feature."""
    (corpus.tasks_dir / "task-999-broken.yaml").write_text(
        "this: [is: not: valid", encoding="utf-8"
    )
    with corpus_snapshot():
        result = corpus.load_all()
        assert [error.path.name for error in result.errors] == ["task-999-broken.yaml"]
        # And again from the snapshot: the errors must survive being cached, not just
        # the successful loads.
        assert [error.path.name for error in corpus.load_all().errors] == ["task-999-broken.yaml"]


class TestOverTheApi:
    """The property stated at the level the acceptance criterion is written against."""

    def test_one_request_parses_each_file_at_most_once(self, tmp_path: Path, monkeypatch) -> None:
        from agentjobs.api.dependencies import reset_dependency_cache
        from agentjobs.project_setup import build_project_config
        import yaml as _yaml

        root = tmp_path / "project"
        (root / ".agentjobs").mkdir(parents=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            _yaml.safe_dump(
                build_project_config(project_name="Snapshot project", user="Someone"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        storage = TaskStorage(root / "tasks")
        for index in range(1, CORPUS_SIZE + 1):
            storage.save_task(_build_task(index))

        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(root))
        reset_dependency_cache()
        client = TestClient(app)

        for path in (
            "/api/projects/_local/tasks",
            "/api/projects/_local/dashboard",
            "/api/projects/_local/tasks/task-001/detail",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            parses = int(response.headers[PARSE_COUNT_HEADER])
            assert parses <= CORPUS_SIZE, (
                f"{path} parsed {parses} files for a {CORPUS_SIZE}-file corpus; "
                "a request must not walk the corpus more than once"
            )
        reset_dependency_cache()
