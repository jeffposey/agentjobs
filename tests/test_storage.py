"""Tests for YAML-backed storage implementation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentjobs.models import Priority, Task, TaskStatus
from agentjobs.storage import TaskStorage


def _build_task(task_id: str, title: str = "Sample") -> Task:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        title=title,
        created=now,
        updated=now,
        status=TaskStatus.PLANNED,
        priority=Priority.MEDIUM,
        category="testing",
        description="Task description",
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """Saving and loading a task should preserve data."""
    storage = TaskStorage(tmp_path)
    task = _build_task("task-001")
    stored = storage.save_task(task)

    assert stored.updated >= task.created
    reloaded = storage.load_task("task-001.yaml")
    assert reloaded is not None
    assert reloaded.id == "task-001"
    assert reloaded.title == task.title


def test_load_task_with_invalid_yaml(tmp_path: Path) -> None:
    """Malformed YAML should be handled gracefully."""
    storage = TaskStorage(tmp_path)
    bad_file = tmp_path / "task-bad.yaml"
    bad_file.write_text("foo: [unterminated", encoding="utf-8")
    assert storage.load_task("task-bad") is None

    empty_file = tmp_path / "task-empty.yaml"
    empty_file.write_text("", encoding="utf-8")
    assert storage.load_task("task-empty") is None

    invalid_file = tmp_path / "task-invalid.yaml"
    invalid_file.write_text("id: missing-fields\n", encoding="utf-8")
    assert storage.load_task("task-invalid") is None


def test_list_and_search_tasks(tmp_path: Path) -> None:
    """Listing and searching tasks returns expected results."""
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-001", title="Implement feature"))
    storage.save_task(_build_task("task-002", title="Write docs"))

    tasks = storage.list_tasks()
    assert len(tasks) == 2

    matches = storage.search_tasks("docs")
    assert len(matches) == 1
    assert matches[0].id == "task-002"


def test_delete_task(tmp_path: Path) -> None:
    """Deleting a task removes its YAML file."""
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-050"))

    assert storage.delete_task("task-050") is True
    assert storage.load_task("task-050") is None
    assert storage.delete_task("task-050") is False
