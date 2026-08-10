"""Tests for TaskManager business logic (schema v2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    Lifecycle,
    LogEntryType,
    Outcome,
    Priority,
)
from agentjobs.storage import TaskStorage


def _manager(tmp_path: Path) -> TaskManager:
    storage = TaskStorage(tmp_path)
    return TaskManager(storage)


def test_create_task_persists_yaml(tmp_path: Path) -> None:
    """Creating a task writes YAML to disk, stamped and reloadable."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-001",
        title="Initial setup",
        description="Bootstrap project",
        priority=Priority.HIGH,
        category="infra",
    )

    assert (tmp_path / "task-001.yaml").exists()
    assert (tmp_path / "task-001.yaml").read_text(encoding="utf-8").startswith("schema: 2")
    assert task.priority == Priority.HIGH
    assert manager.get_task("task-001") is not None

    with pytest.raises(ValueError):
        manager.create_task(
            id="task-001",
            title="Duplicate",
            description="Should fail",
            category="infra",
        )


def test_create_task_defaults_to_draft_with_human_ball(tmp_path: Path) -> None:
    """A new task is draft, ball human/spec, with a prompt."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-002",
        title="New idea",
        description="Something",
        category="misc",
    )
    assert task.lifecycle is Lifecycle.DRAFT
    assert task.ball is Ball.HUMAN
    assert task.ball_reason is BallReason.SPEC
    assert task.ball_prompt


def test_create_ready_task_is_available(tmp_path: Path) -> None:
    """A ready task carries agent/available and needs no prompt."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-003",
        title="Ready work",
        description="Go",
        category="misc",
        lifecycle=Lifecycle.READY,
    )
    assert task.lifecycle is Lifecycle.READY
    assert task.ball is Ball.AGENT
    assert task.ball_reason is BallReason.AVAILABLE


def test_create_task_rejects_active_start(tmp_path: Path) -> None:
    """Tasks are born draft or ready; anything else skips the logged transitions."""
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="draft or ready"):
        manager.create_task(
            id="task-004",
            title="Cheater",
            description="",
            category="misc",
            lifecycle=Lifecycle.ACTIVE,
        )


def test_claim_task_sets_owner_and_logs_transition(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-010",
        title="Implement feature",
        description="Implement business logic",
        category="feature",
        lifecycle=Lifecycle.READY,
    )

    task = manager.claim_task("task-010", agent="codex")

    assert task.lifecycle is Lifecycle.ACTIVE
    assert task.assignment.owner == "codex"
    assert task.ball is Ball.AGENT
    assert task.ball_reason is BallReason.WORK
    assert task.log[-1].type is LogEntryType.TRANSITION
    assert task.log[-1].actor == "codex"


def test_claim_refuses_unready_task(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(id="task-011", title="Draft", description="", category="misc")
    with pytest.raises(ValueError, match="not available to claim"):
        manager.claim_task("task-011", agent="codex")


def test_claim_respects_eligibility(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-012",
        title="Restricted",
        description="",
        category="misc",
        lifecycle=Lifecycle.READY,
        assignment={"eligible": ["claude"]},
    )
    with pytest.raises(ValueError, match="claimable only by"):
        manager.claim_task("task-012", agent="codex")
    task = manager.claim_task("task-012", agent="claude")
    assert task.assignment.owner == "claude"


def test_claim_refuses_unmet_needs(tmp_path: Path) -> None:
    """A ready task with an open `needs` dependency refuses to be claimed."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-020", title="Dep", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.create_task(
        id="task-021",
        title="Dependent",
        description="",
        category="misc",
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": "task-020", "type": "needs"}],
    )
    with pytest.raises(ValueError, match="unmet dependencies"):
        manager.claim_task("task-021", agent="codex")

    manager.claim_task("task-020", agent="codex")
    manager.close_task("task-020", actor="codex", outcome=Outcome.COMPLETED)
    task = manager.claim_task("task-021", agent="codex")
    assert task.assignment.owner == "codex"


def test_claim_refuses_a_dependency_that_does_not_exist(tmp_path: Path) -> None:
    """A dangling `needs` reference blocks and says so, rather than being ignored.

    Requested in review on task-052. It previously passed: the lookup returned None for
    an unknown id, which is not False, so a typo'd or renamed dependency silently
    disabled the claimability gate altogether -- strict about a misspelled field,
    permissive about a misspelled task id.
    """
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-022-typo",
        title="Depends on a ghost",
        description="",
        category="misc",
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": "task-999-does-not-exist", "type": "needs"}],
    )

    with pytest.raises(ValueError, match="not a task in this project"):
        manager.claim_task("task-022-typo", agent="codex")

    assert manager.get_next_task() is None, "it must not be offered by /next either"


def test_claim_refuses_a_dependency_whose_file_is_unreadable(tmp_path: Path) -> None:
    """A dependency that cannot be loaded is not evidence that it is finished."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-023-dependent",
        title="Dependent",
        description="",
        category="misc",
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": "task-024-broken", "type": "needs"}],
    )
    (tmp_path / "task-024-broken.yaml").write_text("schema: 2\nid: x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="task-024-broken"):
        manager.claim_task("task-023-dependent", agent="codex")


def test_a_non_needs_dependency_never_blocks(tmp_path: Path) -> None:
    """Only `needs` gates a claim; `blocks` and `related` are descriptive."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-025-related",
        title="Merely related",
        description="",
        category="misc",
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": "task-999-absent", "type": "related"}],
    )

    assert manager.claim_task("task-025-related", agent="codex").assignment.owner == "codex"


def test_handoff_moves_ball_and_logs(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-030", title="Work", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.claim_task("task-030", agent="codex")

    task = manager.handoff(
        "task-030",
        actor="codex",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Review the diff and approve or request changes.",
    )
    assert task.ball is Ball.HUMAN
    assert task.ball_reason is BallReason.REVIEW
    assert task.log[-1].type is LogEntryType.HANDOFF
    assert task.log[-1].data == {"ball": "human", "ball_reason": "review"}


def test_handoff_requires_prompt_for_non_available(tmp_path: Path) -> None:
    """Rule 4 travels through the manager: a handoff without its ask is rejected."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-031", title="Work", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.claim_task("task-031", agent="codex")
    with pytest.raises(ValueError, match="ball_prompt"):
        manager.handoff(
            "task-031",
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
        )


def test_release_returns_task_to_pool(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-040", title="Work", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.claim_task("task-040", agent="codex")

    task = manager.release_task("task-040", actor="codex")
    assert task.lifecycle is Lifecycle.READY
    assert task.assignment.owner is None
    assert task.ball_reason is BallReason.AVAILABLE


def test_close_task_sets_outcome_and_clears_ball(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-050", title="Work", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.claim_task("task-050", agent="codex")

    task = manager.close_task("task-050", actor="codex", outcome=Outcome.COMPLETED)
    assert task.lifecycle is Lifecycle.CLOSED
    assert task.outcome is Outcome.COMPLETED
    assert task.ball is None
    assert task.assignment.owner is None

    with pytest.raises(ValueError, match="already closed"):
        manager.close_task("task-050", actor="codex", outcome=Outcome.CANCELLED)


def test_get_next_task_honours_priority(tmp_path: Path) -> None:
    """Next task selection prioritises highest urgency among ready tasks."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-101",
        title="Low priority task",
        description="Backlog item",
        priority=Priority.LOW,
        category="misc",
        lifecycle=Lifecycle.READY,
    )
    manager.create_task(
        id="task-102",
        title="Critical task",
        description="Urgent work",
        priority=Priority.CRITICAL,
        category="urgent",
        lifecycle=Lifecycle.READY,
    )

    next_task = manager.get_next_task()
    assert next_task is not None
    assert next_task.id == "task-102"

    priority_only = manager.get_next_task(priority=Priority.CRITICAL)
    assert priority_only is not None
    assert priority_only.id == "task-102"


def test_get_next_task_skips_unmet_needs_and_ineligible(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-110", title="Dep", description="", category="misc", lifecycle=Lifecycle.READY
    )
    manager.create_task(
        id="task-111",
        title="Blocked by dep",
        description="",
        category="misc",
        priority=Priority.CRITICAL,
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": "task-110", "type": "needs"}],
    )
    manager.create_task(
        id="task-112",
        title="Someone else's",
        description="",
        category="misc",
        priority=Priority.CRITICAL,
        lifecycle=Lifecycle.READY,
        assignment={"eligible": ["claude"]},
    )

    next_task = manager.get_next_task(agent="codex")
    assert next_task is not None
    assert next_task.id == "task-110"


def test_add_log_entry_appends_and_rejects_transition(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(id="task-202", title="Docs", description="", category="documentation")

    task = manager.add_log_entry(
        "task-202", actor="claude", type=LogEntryType.NOTE, body="A remark."
    )
    assert task.log[-1].type is LogEntryType.NOTE

    with pytest.raises(ValueError, match="transition"):
        manager.add_log_entry("task-202", actor="claude", type=LogEntryType.TRANSITION)


def test_add_progress_update_appends_entry(tmp_path: Path) -> None:
    """Progress updates land in the unified log without touching the axes."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-203",
        title="Documentation",
        description="Write docs",
        category="documentation",
    )
    task = manager.add_progress_update(
        task_id="task-203",
        author="claude",
        summary="Docs in progress",
        details="Added first section",
    )
    assert task.log[-1].type is LogEntryType.PROGRESS
    assert "Added first section" in (task.log[-1].body or "")
    assert task.lifecycle is Lifecycle.DRAFT


def test_mark_deliverable_complete_updates_status(tmp_path: Path) -> None:
    """Marking a deliverable updates its status to done."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-301",
        title="Deliverable test",
        description="Ensure deliverable updates",
        category="qa",
        deliverables=[{"path": "docs/output.md", "status": "pending"}],
    )

    task = manager.mark_deliverable_complete("task-301", "docs/output.md")
    assert task.deliverables[0].status == "done"

    with pytest.raises(ValueError):
        manager.mark_deliverable_complete("task-301", "missing.md")


def test_get_next_task_returns_none_when_empty(tmp_path: Path) -> None:
    """Requesting next task from empty storage returns None."""
    manager = _manager(tmp_path)
    assert manager.get_next_task() is None


def test_verbs_raise_not_found_for_missing_task(tmp_path: Path) -> None:
    """A missing task raises TaskNotFoundError, not a generic state error."""
    manager = _manager(tmp_path)
    with pytest.raises(TaskNotFoundError):
        manager.claim_task("task-999", agent="codex")
    with pytest.raises(TaskNotFoundError):
        manager.handoff(
            "task-999",
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="x",
        )


def test_archive_open_task_closes_it_cancelled(tmp_path: Path) -> None:
    """Archiving an open task closes it as cancelled first; archived is a flag."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-400",
        title="Archive me",
        description="",
        category="ops",
    )
    archived = manager.archive_task(task.id, author="system")
    assert archived.lifecycle is Lifecycle.CLOSED
    assert archived.outcome is Outcome.CANCELLED
    assert archived.archived is True
    assert archived.log[-1].body == "Task archived."


def test_archive_closed_task_only_flags_it(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-401", title="Done", description="", category="ops", lifecycle=Lifecycle.READY
    )
    manager.claim_task("task-401", agent="codex")
    manager.close_task("task-401", actor="codex", outcome=Outcome.COMPLETED)

    archived = manager.archive_task("task-401", author="system")
    assert archived.outcome is Outcome.COMPLETED
    assert archived.archived is True


def test_archive_task_rejects_unknown_id(tmp_path: Path) -> None:
    """Archiving a task that does not exist raises rather than silently succeeding."""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="task-does-not-exist"):
        manager.archive_task("task-does-not-exist", author="system")


def test_update_task_edits_fields_but_keeps_log(tmp_path: Path) -> None:
    """Partial updates edit content fields; identifiers and history survive."""
    manager = _manager(tmp_path)
    task = manager.create_task(
        id="task-402",
        title="Original",
        description="before",
        category="ops",
    )
    manager.add_log_entry("task-402", actor="claude", type=LogEntryType.NOTE, body="kept")
    updated = manager.update_task(task.id, title="Updated", effort="1 day")
    assert updated.id == task.id
    assert updated.title == "Updated"
    assert updated.effort == "1 day"
    assert updated.created == task.created
    assert [entry.body for entry in updated.log] == ["kept"]


def test_list_tasks_filters_by_axes_and_priority(tmp_path: Path) -> None:
    """List helper filters by lifecycle, ball and priority."""
    manager = _manager(tmp_path)
    manager.create_task(
        id="task-500",
        title="Ready",
        description="",
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    manager.create_task(
        id="task-501",
        title="Active",
        description="",
        priority=Priority.HIGH,
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task("task-501", agent="codex")

    ready = manager.list_tasks(lifecycle=Lifecycle.READY)
    assert [task.id for task in ready] == ["task-500"]
    with_agent = manager.list_tasks(ball=Ball.AGENT)
    assert len(with_agent) == 2
    high_priority = manager.list_tasks(priority=Priority.HIGH)
    assert [task.id for task in high_priority] == ["task-501"]
