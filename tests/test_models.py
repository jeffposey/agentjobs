"""Unit tests for AgentJobs data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentjobs.models import (
    Branch,
    Deliverable,
    Dependency,
    Issue,
    Priority,
    Prompts,
    SuccessCriterion,
    Task,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def test_task_model_helper_methods() -> None:
    """Task helper methods reflect workflow state."""
    task = Task(
        id="task-001",
        title="Test Task",
        created=_now(),
        updated=_now(),
        status=TaskStatus.COMPLETED,
        priority=Priority.HIGH,
        category="testing",
        description="Ensure helper methods behave as expected.",
    )

    assert task.is_completed() is True
    assert task.is_blocked() is False
    assert task.is_active() is False
    assert task.priority_rank() == 1

    task.status = TaskStatus.BLOCKED
    assert task.is_blocked() is True
    assert task.is_active() is True

    task.status = TaskStatus.WAITING_FOR_HUMAN
    task.human_summary = "Needs product approval."
    assert task.is_active() is True
    assert task.human_summary == "Needs product approval."


def test_success_criterion_validation() -> None:
    """Success criterion enforces supported states."""
    criterion = SuccessCriterion(
        id="sc-1",
        description="All tests green",
        status="completed",
    )
    assert criterion.status == "completed"

    with pytest.raises(ValueError):
        SuccessCriterion(id="sc-2", description="Invalid", status="unknown")


def test_deliverable_validation() -> None:
    """Deliverable status is constrained."""
    deliverable = Deliverable(path="docs/report.md", status="pending")
    assert deliverable.status == "pending"

    with pytest.raises(ValueError):
        Deliverable(path="docs/report.md", status="blocked")


def test_dependency_and_branch_validation() -> None:
    """Derived models validate their enumerated fields."""
    dependency = Dependency(task_id="task-1")
    assert dependency.type == "depends_on"

    with pytest.raises(ValueError):
        Dependency(task_id="task-1", type="invalid")

    branch = Branch(name="feature/test")
    assert branch.status == "active"

    with pytest.raises(ValueError):
        Branch(name="feature/test", status="stale")


def test_issue_status_validation() -> None:
    """Issue status validator rejects unsupported values."""
    issue = Issue(id="issue-1", title="Bug", status="resolved")
    assert issue.status == "resolved"

    with pytest.raises(ValueError):
        Issue(id="issue-2", title="Bug", status="invalid")


def test_task_serialization_handles_prompts() -> None:
    """Prompts field serializes with nested data."""
    starter = "Initial prompt"
    prompts = Prompts(starter=starter)

    task = Task(
        id="task-010",
        title="Serialization",
        created=_now(),
        updated=_now(),
        category="infra",
        description="Serialize me",
        prompts=prompts,
    )

    payload = task.model_dump(mode="json")
    assert payload["prompts"]["starter"] == starter
    assert payload["status"] == TaskStatus.PLANNED.value
    assert payload["priority"] == Priority.MEDIUM.value
