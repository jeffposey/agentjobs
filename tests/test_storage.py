"""Tests for YAML-backed storage implementation (schema v2)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

import pytest


from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.storage import TaskLoadError, TaskStorage


def _build_task(task_id: str, title: str = "Sample") -> Task:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        title=title,
        created=now,
        updated=now,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        priority=Priority.MEDIUM,
        category="testing",
        spec=Spec(summary=f"{title} summary", description="Task description"),
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


def test_written_file_carries_the_schema_stamp_by_alias(tmp_path: Path) -> None:
    """The stamp is written as `schema:`, not the Python-side `schema_version`.

    `schema` shadows a BaseModel attribute, so the field is aliased in Python; a dump
    without by_alias writes the wrong key and produces a file the loader rejects as v1.
    """
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-002"))

    text = (tmp_path / "task-002.yaml").read_text(encoding="utf-8")
    assert text.startswith("schema: 2")
    assert "schema_version" not in text
    # display_status is computed for API responses and must never be stored.
    assert "display_status" not in text


def test_load_task_with_invalid_yaml(tmp_path: Path) -> None:
    """A broken file raises instead of vanishing (task-049)."""
    storage = TaskStorage(tmp_path)

    bad_file = tmp_path / "task-bad.yaml"
    bad_file.write_text("foo: [unterminated", encoding="utf-8")
    with pytest.raises(TaskLoadError, match="invalid YAML"):
        storage.load_task("task-bad")

    empty_file = tmp_path / "task-empty.yaml"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(TaskLoadError, match="empty"):
        storage.load_task("task-empty")

    invalid_file = tmp_path / "task-invalid.yaml"
    invalid_file.write_text("schema: 2\nid: missing-fields\n", encoding="utf-8")
    with pytest.raises(TaskLoadError) as caught:
        storage.load_task("task-invalid")
    # The point of the change: the message names the file and the fields.
    assert "task-invalid.yaml" in str(caught.value)
    assert "title" in str(caught.value)


def test_unmigrated_v1_file_is_reported_by_filename(tmp_path: Path) -> None:
    """A file with no `schema: 2` stamp raises TaskLoadError naming the migrator.

    This is the failure mode of exactly this migration: a stray v1 file must show up in
    the broken-files listing rather than crashing the whole listing or vanishing.
    """
    storage = TaskStorage(tmp_path)
    v1_file = tmp_path / "task-old.yaml"
    v1_file.write_text(
        "id: task-old\ntitle: Old\nstatus: ready\ncategory: misc\ndescription: x\n",
        encoding="utf-8",
    )
    with pytest.raises(TaskLoadError, match="migrate-schema") as caught:
        storage.load_task("task-old")
    assert "task-old.yaml" in str(caught.value)

    result = storage.load_all()
    assert result.tasks == []
    assert len(result.errors) == 1
    assert result.errors[0].task_id == "task-old"


def test_a_missing_file_still_returns_none(tmp_path: Path) -> None:
    """Absent is not the same as broken, and must stay a plain None."""
    assert TaskStorage(tmp_path).load_task("task-does-not-exist") is None


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


def test_project_revision_tracks_direct_same_count_rewrites(tmp_path: Path) -> None:
    """A direct writer is visible even when count, size, and mtime are unchanged."""
    storage = TaskStorage(tmp_path)
    path = tmp_path / "task-001.yaml"
    path.write_text("title: alpha\n", encoding="utf-8")
    original_stat = path.stat()
    before = storage.project_revision()

    path.write_text("title: bravo\n", encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    after = storage.project_revision()
    assert before[1] == after[1] == 1
    assert before[0] != after[0]


def test_project_revision_tracks_bulk_add_change_and_delete(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-001"))
    initial = storage.project_revision()

    storage.save_task(_build_task("task-001", title="Changed"))
    storage.save_task(_build_task("task-002"))
    changed = storage.project_revision()

    (tmp_path / "task-002.yaml").unlink()
    deleted = storage.project_revision()

    assert initial[1] == 1
    assert changed[1] == 2
    assert deleted[1] == 1
    assert len({initial[0], changed[0], deleted[0]}) == 3


def test_lock_files_are_not_globbed_as_tasks(tmp_path: Path) -> None:
    """*.lock artifacts beside task files never appear in listings."""
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-001"))
    (tmp_path / "task-001.lock").write_text("1234", encoding="utf-8")

    result = storage.load_all()
    assert [task.id for task in result.tasks] == ["task-001"]
    assert result.errors == []


def test_delete_task(tmp_path: Path) -> None:
    """Deleting a task removes its YAML file."""
    storage = TaskStorage(tmp_path)
    storage.save_task(_build_task("task-050"))

    assert storage.delete_task("task-050") is True
    assert storage.load_task("task-050") is None
    assert storage.delete_task("task-050") is False
