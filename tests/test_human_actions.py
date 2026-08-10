"""Tests for human action API endpoints (schema v2)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.main import app
from agentjobs.api.dependencies import reset_dependency_cache


@pytest.fixture(autouse=True)
def setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
def sample_task_in_review(client: TestClient) -> str:
    """Create a task sitting in the human inbox: active, ball human/review."""
    response = client.post(
        "/api/tasks",
        json={
            "title": "Sample Task",
            "description": "Test task",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert response.status_code == 201
    task_id = str(response.json()["id"])

    response = client.post(f"/api/tasks/{task_id}/claim", json={"agent": "test-agent"})
    assert response.status_code == 200
    response = client.post(
        f"/api/tasks/{task_id}/handoff",
        json={
            "actor": "test-agent",
            "ball": "human",
            "ball_reason": "review",
            "ball_prompt": "Review the work and approve or request changes.",
        },
    )
    assert response.status_code == 200
    return task_id


def test_approve_task(client: TestClient, sample_task_in_review: str) -> None:
    """Approving hands the ball back to the owning agent (agent/work), not the pool."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 200
    data = response.json()
    task = data["task"]
    assert task["lifecycle"] == "active"
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "work"
    # The owner survives: approval is not a release back into the pool.
    assert task["assignment"]["owner"] == "test-agent"
    assert any("Approved by jeff" in (entry["body"] or "") for entry in task["log"])


def test_approve_does_not_claim_to_have_merged(
    client: TestClient, sample_task_in_review: str
) -> None:
    """The recorded approval must say a merge is still owed, not that one happened."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "jeff"},
    )

    task = response.json()["task"]
    assert "does not run git" in task["ball_prompt"]


def test_request_changes(client: TestClient, sample_task_in_review: str) -> None:
    """Requesting changes moves the ball to agent/revise with the feedback as the ask."""
    feedback = "Please add more error handling"
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/request-changes",
        json={"user": "jeff", "feedback": feedback},
    )
    assert response.status_code == 200
    task = response.json()["task"]

    assert task["ball"] == "agent"
    assert task["ball_reason"] == "revise"
    # The feedback is the payload of the handoff: it rides in the ball_prompt and log.
    assert task["ball_prompt"] == feedback
    latest = task["log"][-1]
    assert latest["type"] == "handoff"
    assert feedback in (latest["body"] or "")
    assert latest["actor"] == "jeff"


def test_reject_task(client: TestClient, sample_task_in_review: str) -> None:
    """Rejecting closes the task as cancelled and archives it, reason on the record."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/reject",
        json={"user": "jeff", "reason": "Out of scope"},
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["lifecycle"] == "closed"
    assert task["outcome"] == "cancelled"
    assert task["archived"] is True
    assert any("Out of scope" in (entry["body"] or "") for entry in task["log"])


def test_approve_nonexistent_task(client: TestClient) -> None:
    """Test approving a nonexistent task returns 404."""
    response = client.post(
        "/api/tasks/nonexistent-task/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 404


def test_request_changes_without_feedback(client: TestClient, sample_task_in_review: str) -> None:
    """Test that request-changes requires feedback."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/request-changes",
        json={"user": "jeff", "feedback": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 400


def test_reject_without_reason(client: TestClient, sample_task_in_review: str) -> None:
    """Test that reject requires a reason."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/reject",
        json={"user": "jeff", "reason": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 400
