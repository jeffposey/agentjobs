"""Tests for human action API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.main import app
from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.manager import TaskManager
from agentjobs.models import TaskStatus
from agentjobs.storage import TaskStorage, WebhookStorage
from agentjobs.webhooks import WebhookManager


@pytest.fixture(autouse=True)
def setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up test environment with temporary directories."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    agentjobs_dir = tmp_path / ".agentjobs"
    agentjobs_dir.mkdir()

    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))

    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def sample_task_waiting(client: TestClient) -> str:
    """Create a sample task in waiting_for_human status."""
    response = client.post(
        "/api/tasks",
        json={
            "title": "Sample Task",
            "description": "Test task",
            "category": "test",
            "status": "waiting_for_human",
            "assigned_to": "test-agent",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_approve_task(client: TestClient, sample_task_waiting: str) -> None:
    """Test approving a task."""
    response = client.post(
        f"/api/tasks/{sample_task_waiting}/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task"]["status"] == "ready"
    assert any("Approved by jeff" in update["summary"] for update in data["task"]["status_updates"])


def test_request_changes(client: TestClient, sample_task_waiting: str) -> None:
    """Test requesting changes on a task."""
    feedback = "Please add more error handling"
    response = client.post(
        f"/api/tasks/{sample_task_waiting}/request-changes",
        json={"user": "jeff", "feedback": feedback},
    )
    assert response.status_code == 200
    data = response.json()

    # Check that comment was added
    assert len(data["task"]["comments"]) > 0
    comment = data["task"]["comments"][0]
    assert comment["kind"] == "feedback"
    assert comment["content"] == feedback
    assert comment["author"] == "jeff"


def test_reject_task(client: TestClient, sample_task_waiting: str) -> None:
    """Test rejecting a task."""
    response = client.post(
        f"/api/tasks/{sample_task_waiting}/reject",
        json={"user": "jeff", "reason": "Out of scope"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task"]["status"] == "archived"
    assert any("Rejected by jeff" in update["summary"] for update in data["task"]["status_updates"])


def test_approve_nonexistent_task(client: TestClient) -> None:
    """Test approving a nonexistent task returns 404."""
    response = client.post(
        "/api/tasks/nonexistent-task/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 404


def test_request_changes_without_feedback(client: TestClient, sample_task_waiting: str) -> None:
    """Test that request-changes requires feedback."""
    response = client.post(
        f"/api/tasks/{sample_task_waiting}/request-changes",
        json={"user": "jeff", "feedback": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 422


def test_reject_without_reason(client: TestClient, sample_task_waiting: str) -> None:
    """Test that reject requires a reason."""
    response = client.post(
        f"/api/tasks/{sample_task_waiting}/reject",
        json={"user": "jeff", "reason": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 422
