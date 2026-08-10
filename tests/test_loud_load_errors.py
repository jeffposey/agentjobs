"""A broken task file must be loud, not invisible.

Storage used to return None for a file that failed validation, which made it
indistinguishable from a file that does not exist. The task dropped out of every
listing and the only evidence was a log line. These tests cover both halves of the
fix: the error carries file and field, and every listing surface shows it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.cli import app as cli_app
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.storage import TaskLoadError, TaskStorage

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)
BROKEN_FILENAME = "task-666-corrupt.yaml"
UNMIGRATED_FILENAME = "task-777-unmigrated.yaml"


def write_good_task(tasks_dir: Path, task_id: str = "task-100-good") -> Task:
    """A task that loads cleanly."""
    storage = TaskStorage(tasks_dir)
    return storage.save_task(
        Task(
            id=task_id,
            title="A perfectly good task",
            created=NOW,
            updated=NOW,
            lifecycle=Lifecycle.READY,
            ball=Ball.AGENT,
            ball_reason=BallReason.AVAILABLE,
            priority=Priority.HIGH,
            category="infrastructure",
            spec=Spec(summary="Nothing wrong here.", description="Nothing wrong here."),
        )
    )


def write_broken_task(tasks_dir: Path) -> Path:
    """A file that parses as YAML but is not a valid task: priority is not a Priority."""
    path = tasks_dir / BROKEN_FILENAME
    path.write_text(
        yaml.safe_dump(
            {
                "schema": 2,
                "id": "task-666-corrupt",
                "title": "Corrupt",
                "created": "2026-08-10T00:00:00Z",
                "updated": "2026-08-10T00:00:00Z",
                "lifecycle": "ready",
                "ball": "agent",
                "ball_reason": "available",
                "category": "infrastructure",
                "spec": {"summary": "x", "description": "x"},
                "priority": "extremely-urgent",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def corpus(tmp_path: Path) -> Tuple[Path, Task]:
    """A tasks directory holding one good task and one broken file."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    good = write_good_task(tasks_dir)
    write_broken_task(tasks_dir)
    return tasks_dir, good


@pytest.fixture()
def client(corpus, tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    """An API bound to that directory."""
    tasks_dir, _ = corpus
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(TASKS_DIR_ENV, str(tasks_dir))
    reset_dependency_cache()
    with TestClient(app) as test_client:
        yield test_client
    reset_dependency_cache()


class TestStorageIsLoud:
    def test_the_error_names_the_file_and_the_field(self, corpus) -> None:
        tasks_dir, _ = corpus
        storage = TaskStorage(tasks_dir)

        with pytest.raises(TaskLoadError) as caught:
            storage.load_task("task-666-corrupt")

        message = str(caught.value)
        assert BROKEN_FILENAME in message
        assert "priority" in message

    def test_the_error_is_serialisable_for_uis(self, corpus) -> None:
        tasks_dir, _ = corpus
        try:
            TaskStorage(tasks_dir).load_task("task-666-corrupt")
        except TaskLoadError as exc:
            payload = exc.as_dict()
        assert payload["filename"] == BROKEN_FILENAME
        assert "priority" in payload["reason"]

    def test_an_unmigrated_v1_file_is_reported_with_the_fix(self, corpus) -> None:
        """A file with no `schema: 2` stamp is broken-with-a-reason, not invisible."""
        tasks_dir, _ = corpus
        (tasks_dir / UNMIGRATED_FILENAME).write_text(
            "id: task-777-unmigrated\ntitle: Old\ncategory: x\ndescription: y\n",
            encoding="utf-8",
        )

        result = TaskStorage(tasks_dir).load_all()

        reasons = {error.path.name: error.reason for error in result.errors}
        assert UNMIGRATED_FILENAME in reasons
        assert "migrate-schema" in reasons[UNMIGRATED_FILENAME]

    def test_a_missing_file_is_still_a_plain_none(self, corpus) -> None:
        # Absent and broken must stay distinguishable; conflating them is the bug.
        tasks_dir, _ = corpus

        assert TaskStorage(tasks_dir).load_task("task-999-nonexistent") is None

    def test_one_broken_file_does_not_take_down_the_listing(self, corpus) -> None:
        tasks_dir, good = corpus

        result = TaskStorage(tasks_dir).load_all()

        assert [task.id for task in result.tasks] == [good.id]
        assert [error.path.name for error in result.errors] == [BROKEN_FILENAME]
        assert result.has_errors

    def test_list_tasks_keeps_its_old_signature(self, corpus) -> None:
        tasks_dir, good = corpus

        assert [t.id for t in TaskStorage(tasks_dir).list_tasks()] == [good.id]


class TestApiSurfacesIt:
    def test_the_listing_still_returns_the_valid_tasks(self, client: TestClient) -> None:
        response = client.get("/api/tasks")

        assert response.status_code == 200
        assert [t["id"] for t in response.json()] == ["task-100-good"]

    def test_broken_files_are_reachable_and_described(self, client: TestClient) -> None:
        payload = client.get("/api/tasks/broken").json()

        assert len(payload) == 1
        assert payload[0]["filename"] == BROKEN_FILENAME
        assert "priority" in payload[0]["reason"]

    def test_broken_is_not_captured_as_a_task_id(self, client: TestClient) -> None:
        # /broken is declared before /{task_id}; if that ordering regresses this 404s.
        assert client.get("/api/tasks/broken").status_code == 200

    def test_reading_the_broken_task_is_a_422_naming_it(self, client: TestClient) -> None:
        response = client.get("/api/tasks/task-666-corrupt")

        assert response.status_code == 422
        body = response.json()
        assert BROKEN_FILENAME in body["detail"]
        assert body["broken"]["filename"] == BROKEN_FILENAME

    def test_a_genuinely_missing_task_is_still_404(self, client: TestClient) -> None:
        assert client.get("/api/tasks/task-999-nonexistent").status_code == 404


class TestGuiSurfacesIt:
    def test_the_task_list_warns_and_names_the_file(self, client: TestClient) -> None:
        body = client.get("/api/../tasks", follow_redirects=True).text

        assert "could not be loaded" in body
        assert BROKEN_FILENAME in body

    def test_the_dashboard_warns_too(self, client: TestClient) -> None:
        body = client.get("/", follow_redirects=True).text

        assert "could not be loaded" in body

    def test_valid_tasks_still_render(self, client: TestClient) -> None:
        body = client.get("/api/../tasks", follow_redirects=True).text

        assert "A perfectly good task" in body


class TestCliSurfacesIt:
    def test_list_reports_the_broken_file_and_still_lists_the_rest(
        self, corpus, tmp_path: Path, monkeypatch
    ) -> None:
        tasks_dir, _ = corpus
        (tmp_path / ".agentjobs").mkdir(exist_ok=True)
        (tmp_path / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "T", "tasks_directory": "tasks"}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(cli_app, ["list"])

        # Click 8.3 captures the two streams separately. The warnings go to stderr on
        # purpose, so piping the list somewhere does not swallow them.
        combined = result.output + (result.stderr or "")
        assert result.exit_code == 0, combined
        assert BROKEN_FILENAME in combined
        assert "could not be loaded" in combined
        assert "A perfectly good task" in result.output
