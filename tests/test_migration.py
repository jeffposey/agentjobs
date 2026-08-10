"""Tests for migration functionality."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentjobs.migration.converter import TaskConverter
from agentjobs.migration.parser import MarkdownTaskParser
from agentjobs.models_v2 import Lifecycle, Outcome, Priority


@pytest.fixture
def sample_markdown(tmp_path: Path) -> Path:
    """Create sample markdown task file."""
    content = dedent(
        """
        # Task 016: Feature Implementation

        **Status**: Complete
        **Priority**: High
        **Category**: Feature Development
        **Estimated Effort**: 2-3 weeks
        **Assigned**: Codex
        **Branch**: feature/task-016-feature-name

        ## Objective

        Build a new feature that does XYZ.

        ## Deliverables

        - [x] Core implementation
        - [ ] Tests
        - [x] Documentation

        ### ✅ Phase 1: Planning (COMPLETE)
        Design and specification work.

        ### 🔄 Phase 2: Implementation (IN PROGRESS)
        Core development work.
        """
    ).strip()

    task_file = tmp_path / "task-016-feature.md"
    task_file.write_text(content, encoding="utf-8")
    return task_file


def test_parse_markdown_task(sample_markdown: Path) -> None:
    """Test parsing markdown task file."""
    parser = MarkdownTaskParser()
    parsed = parser.parse_file(sample_markdown)

    assert parsed.title == "Task 016: Feature Implementation"
    assert parsed.status == "Complete"
    assert parsed.priority == "High"
    assert parsed.category == "Feature Development"
    assert parsed.assigned_to == "Codex"
    assert len(parsed.phases) == 2
    assert parsed.phases[0]["status"] == "completed"
    assert parsed.deliverables[0]["status"] == "completed"
    assert parsed.deliverables[1]["status"] == "pending"
    assert "Build a new feature" in parsed.description
    assert parsed.human_summary.startswith("Build a new feature")


def test_convert_to_yaml_task(sample_markdown: Path) -> None:
    """Test converting parsed task to a v2 model."""
    parser = MarkdownTaskParser()
    converter = TaskConverter()

    parsed = parser.parse_file(sample_markdown)
    task = converter.convert(parsed)

    assert task.id == "task-016-feature"
    assert task.lifecycle is Lifecycle.CLOSED
    assert task.outcome is Outcome.COMPLETED
    assert task.priority == Priority.HIGH
    assert task.category == "Feature Development"
    # A closed import keeps the assignee as eligibility, not live ownership.
    assert task.assignment.eligible == ["codex"]
    assert len(task.deliverables) == 3
    assert task.deliverables[0].status == "done"
    # Phases have no v2 field; they are folded into the description.
    assert "## Phases (imported)" in task.spec.description
    assert "**Planning** (completed)" in task.spec.description
    assert task.spec.summary
    assert "Build a new feature" in task.spec.summary


def test_convert_in_progress_task_gets_an_owner(tmp_path: Path) -> None:
    """An active import must satisfy rule 5: owner present while active."""
    content = dedent(
        """
        # Task 017: Active work

        **Status**: In Progress
        **Priority**: Medium

        ## Objective

        Something underway.
        """
    ).strip()
    task_file = tmp_path / "task-017-active.md"
    task_file.write_text(content, encoding="utf-8")

    task = TaskConverter().convert(MarkdownTaskParser().parse_file(task_file))

    assert task.lifecycle is Lifecycle.ACTIVE
    assert task.assignment.owner == "unknown"
    assert task.ball_prompt
