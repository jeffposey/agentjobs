"""Tests for TaskManager business logic."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentjobs.manager import TaskManager
from agentjobs.models import Priority, TaskStatus
from agentjobs.storage import TaskStorage


def _manager(tmp_path: Path) -> TaskManager:
    storage = TaskStorage(tmp_path)
    return TaskManager(storage)


def _now() -> datetime:
    return datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_create_task_persists_yaml(tmp_path: Path) -> None:
    """Creating a task writes YAML to disk."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-001",
        title="Initial setup",
        description="Bootstrap project",
        priority=Priority.HIGH,
        category="infra",
    )

    assert (tmp_path / "task-001.yaml").exists()
    assert task.priority == Priority.HIGH
    assert manager.get_task("task-001") is not None

    with pytest.raises(ValueError):
        manager.create_task(
            id="task-001",
            title="Duplicate",
            description="Should fail",
            category="infra",
        )


def test_update_status_adds_status_update(tmp_path: Path) -> None:
    """Status updates are captured when changing workflow state."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-010",
        title="Implement feature",
        description="Implement business logic",
        category="feature",
    )

    task = manager.update_status(
        task_id="task-010",
        status=TaskStatus.IN_PROGRESS,
        author="codex",
        summary="Started work",
    )

    assert task.status == TaskStatus.IN_PROGRESS
    assert len(task.status_updates) == 1
    assert task.status_updates[0].author == "codex"


def test_get_next_task_honours_priority(tmp_path: Path) -> None:
    """Next task selection prioritises highest urgency."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-101",
        title="Low priority task",
        description="Backlog item",
        priority=Priority.LOW,
        category="misc",
        status=TaskStatus.PLANNED,
    )
    manager.create_task(
        id="task-102",
        title="Critical task",
        description="Urgent work",
        priority=Priority.CRITICAL,
        category="urgent",
        status=TaskStatus.PLANNED,
    )
    manager.update_status(
        task_id="task-101",
        status=TaskStatus.IN_PROGRESS,
        author="codex",
        summary="Working on low priority task",
    )

    next_task = manager.get_next_task()
    assert next_task is not None
    assert next_task.id == "task-102"

    priority_only = manager.get_next_task(priority=Priority.CRITICAL)
    assert priority_only is not None
    assert priority_only.id == "task-102"


def test_add_progress_update_appends_entry(tmp_path: Path) -> None:
    """Progress updates are appended without status change."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-202",
        title="Documentation",
        description="Write docs",
        category="documentation",
    )
    task = manager.add_progress_update(
        task_id="task-202",
        author="claude",
        summary="Docs in progress",
        details="Added first section",
    )
    assert len(task.status_updates) == 1
    assert task.status_updates[0].details == "Added first section"


def test_mark_deliverable_complete_updates_status(tmp_path: Path) -> None:
    """Marking a deliverable updates its status to completed."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-301",
        title="Deliverable test",
        description="Ensure deliverable updates",
        category="qa",
        deliverables=[{"path": "docs/output.md", "status": "in_progress"}],
    )

    task = manager.mark_deliverable_complete("task-301", "docs/output.md")
    assert task.deliverables[0].status == "completed"

    with pytest.raises(ValueError):
        manager.mark_deliverable_complete("task-301", "missing.md")


def test_get_next_task_returns_none_when_empty(tmp_path: Path) -> None:
    """Requesting next task from empty storage returns None."""
    manager = _manager(tmp_path)
    assert manager.get_next_task() is None


def test_update_status_raises_when_missing(tmp_path: Path) -> None:
    """Updating a nonexistent task raises an informative error."""
    manager = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.update_status(
            task_id="task-999",
            status=TaskStatus.IN_PROGRESS,
            author="codex",
            summary="No-op",
        )


def test_create_task_accepts_prompt_payload(tmp_path: Path) -> None:
    """Providing prompt payload exercises validation branch."""
    manager = _manager(tmp_path)
    prompts = {"starter": "Start here", "followups": []}
    task = manager.create_task(
        id="task-777",
        title="Prompted task",
        description="Has prompt data",
        category="infra",
        prompts=prompts,
    )
    assert task.prompts.starter == "Start here"


def test_archive_task_sets_status_and_update(tmp_path: Path) -> None:
    """Archiving a task sets status and records an update."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-400",
        title="Archive me",
        description="",
        category="ops",
    )
    archived = manager.archive_task(task.id, author="system")
    assert archived.status == TaskStatus.ARCHIVED
    assert archived.status_updates
    assert archived.status_updates[-1].summary == "Task archived."


def test_replace_task_updates_fields(tmp_path: Path) -> None:
    """Replacing a task overwrites fields while keeping identifiers."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-401",
        title="Original",
        description="before",
        category="ops",
    )
    replaced = manager.replace_task(
        task.id,
        title="Updated",
        description="after",
        category="ops",
    )
    assert replaced.id == task.id
    assert replaced.title == "Updated"
    assert replaced.description == "after"
    assert replaced.created == task.created


def test_list_tasks_filters_by_status_and_priority(tmp_path: Path) -> None:
    """List helper filters by status and priority."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-500",
        title="Planned",
        description="",
        category="ops",
        status=TaskStatus.PLANNED,
    )
    in_progress = manager.create_task(
        id="task-501",
        title="Active",
        description="",
        priority=Priority.HIGH,
        category="ops",
    )
    manager.update_status(
        task_id=in_progress.id,
        status=TaskStatus.IN_PROGRESS,
        author="codex",
        summary="Started",
    )
    planned = manager.list_tasks(status=TaskStatus.PLANNED)
    assert len(planned) == 1
    high_priority = manager.list_tasks(priority=Priority.HIGH)
    assert len(high_priority) == 1
