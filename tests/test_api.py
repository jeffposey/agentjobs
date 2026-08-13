"""API integration tests for AgentJobs REST interface (schema v2)."""

from __future__ import annotations

from typing import Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, Priority
from agentjobs.storage import TaskStorage


@pytest.fixture()
def api_client(tmp_path) -> Iterator[Tuple[TestClient, TaskManager]]:
    """Provide a TestClient bound to a temporary storage directory."""
    reset_dependency_cache()
    storage = TaskStorage(tmp_path)
    manager = TaskManager(storage)

    def _override_manager() -> TaskManager:
        return manager

    app.dependency_overrides[get_task_manager] = _override_manager
    with TestClient(app) as client:
        yield client, manager
    app.dependency_overrides.clear()
    reset_dependency_cache()


def test_health_check_endpoint(api_client) -> None:
    client, _ = api_client
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_tasks_empty(api_client) -> None:
    client, _ = api_client
    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_create_task_success(api_client) -> None:
    client, manager = api_client
    payload = {
        "title": "Write docs",
        "description": "Document the API",
        "priority": "high",
        "category": "documentation",
    }
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Write docs"
    assert body["priority"] == Priority.HIGH.value
    assert body["lifecycle"] == "draft"
    assert body["ball"] == "human"
    assert body["spec"]["description"] == "Document the API"
    assert manager.get_task(body["id"]) is not None


def test_create_task_validation_error(api_client) -> None:
    client, _ = api_client
    response = client.post(
        "/api/tasks",
        json={"title": "Bad", "description": "Test", "priority": "invalid"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "priority" in detail.lower()


def test_list_tasks_with_filters(api_client) -> None:
    client, manager = api_client
    manager.create_task(
        title="Ready task",
        description="Active",
        priority=Priority.MEDIUM,
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    in_progress = manager.create_task(
        title="In progress",
        description="Working",
        priority=Priority.CRITICAL,
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(in_progress.id, agent="codex")

    response = client.get("/api/tasks", params={"lifecycle": "ready"})
    assert response.status_code == 200
    bodies = response.json()
    assert len(bodies) == 1
    assert bodies[0]["lifecycle"] == "ready"

    # The human inbox query returns nothing here: both tasks sit with the agent.
    response = client.get("/api/tasks", params={"ball": "human"})
    assert response.status_code == 200
    assert response.json() == []


def test_task_list_exposes_plain_dependency_state(api_client) -> None:
    client, manager = api_client
    prerequisite = manager.create_task(
        id="task-prerequisite",
        title="Prerequisite",
        description="First.",
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    manager.create_task(
        id="task-dependent",
        title="Dependent",
        description="Second.",
        category="ops",
        lifecycle=Lifecycle.READY,
        dependencies=[{"task": prerequisite.id, "type": "needs"}],
    )

    rows = {task["id"]: task for task in client.get("/api/tasks").json()}

    assert rows[prerequisite.id]["actionable"] is True
    assert rows[prerequisite.id]["unblocks_count"] == 1
    assert rows["task-dependent"]["actionable"] is False
    assert rows["task-dependent"]["unmet_needs"] == ["task-prerequisite (still open)"]


def test_task_responses_carry_display_status(api_client) -> None:
    """The API serves the computed label so surfaces stop deriving their own."""
    client, manager = api_client
    task = manager.create_task(
        title="Labelled",
        description="x",
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    response = client.get(f"/api/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["display_status"] == "Ready"


def test_get_task_success(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Inspect",
        description="Look closely",
        priority=Priority.LOW,
        category="qa",
    )
    response = client.get(f"/api/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["id"] == task.id


def test_get_task_not_found(api_client) -> None:
    client, _ = api_client
    response = client.get("/api/tasks/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "Task missing not found"


def test_patch_task_updates_fields(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Patch",
        description="Initial",
        priority=Priority.MEDIUM,
        category="ops",
    )
    response = client.patch(
        f"/api/tasks/{task.id}",
        json={"title": "Patched", "effort": "2 days"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Patched"
    assert body["effort"] == "2 days"


def test_patch_cannot_move_state_axes(api_client) -> None:
    """The axes only move through the verbs; a PATCH with lifecycle is rejected."""
    client, manager = api_client
    task = manager.create_task(
        title="Guarded",
        description="x",
        category="ops",
    )
    response = client.patch(f"/api/tasks/{task.id}", json={"lifecycle": "closed"})
    assert response.status_code == 400


def test_claim_endpoint(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Claim me",
        description="x",
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    response = client.post(f"/api/tasks/{task.id}/claim", json={"agent": "codex"})
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "active"
    assert body["assignment"]["owner"] == "codex"

    # The loser gets a 409, not a 404: the task exists, the claim is refused.
    response = client.post(f"/api/tasks/{task.id}/claim", json={"agent": "claude"})
    assert response.status_code == 409


def test_handoff_endpoint(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Hand off",
        description="x",
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="codex")
    response = client.post(
        f"/api/tasks/{task.id}/handoff",
        json={
            "actor": "codex",
            "ball": "human",
            "ball_reason": "review",
            "ball_prompt": "Review the diff.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ball"] == "human"
    assert body["ball_reason"] == "review"
    assert body["log"][-1]["type"] == "handoff"

    # A handoff without its ask is refused at the schema level.
    response = client.post(
        f"/api/tasks/{task.id}/handoff",
        json={"actor": "human", "ball": "agent", "ball_reason": "revise"},
    )
    assert response.status_code == 409


def test_release_endpoint(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Release", description="x", category="ops", lifecycle=Lifecycle.READY
    )
    manager.claim_task(task.id, agent="codex")
    response = client.post(f"/api/tasks/{task.id}/release", json={"actor": "codex"})
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "ready"
    assert body.get("assignment", {}).get("owner") is None


def test_close_endpoint(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Close", description="x", category="ops", lifecycle=Lifecycle.READY
    )
    manager.claim_task(task.id, agent="codex")
    response = client.post(
        f"/api/tasks/{task.id}/close",
        json={"actor": "codex", "outcome": "completed", "body": "Done."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "closed"
    assert body["outcome"] == "completed"

    response = client.post(
        f"/api/tasks/{task.id}/close",
        json={"actor": "codex", "outcome": "cancelled"},
    )
    assert response.status_code == 409


def test_log_endpoint_appends_and_rejects_transition(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(title="Log", description="x", category="ops")
    response = client.post(
        f"/api/tasks/{task.id}/log",
        json={"actor": "claude", "type": "decision", "body": "Chose A over B."},
    )
    assert response.status_code == 200
    assert response.json()["log"][-1]["type"] == "decision"

    response = client.post(
        f"/api/tasks/{task.id}/log",
        json={"actor": "claude", "type": "transition", "body": "sneaky"},
    )
    assert response.status_code == 409


def test_get_next_task(api_client) -> None:
    client, manager = api_client
    manager.create_task(
        title="Low priority",
        description="Background",
        priority=Priority.LOW,
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    critical = manager.create_task(
        title="Critical",
        description="Urgent",
        priority=Priority.CRITICAL,
        category="ops",
        lifecycle=Lifecycle.READY,
    )
    response = client.get("/api/tasks/next")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == critical.id


def test_add_progress_update(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Progress",
        description="Track",
        priority=Priority.MEDIUM,
        category="ops",
    )
    response = client.post(
        f"/api/tasks/{task.id}/progress",
        json={"author": "codex", "summary": "Halfway", "details": "50%"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["log"]) == 1
    assert body["log"][0]["type"] == "progress"
    assert body["log"][0]["body"].startswith("Halfway")


def test_mark_deliverable_complete(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Deliver",
        description="Ship",
        priority=Priority.MEDIUM,
        category="ops",
        deliverables=[{"path": "docs/output.md", "status": "pending"}],
    )
    response = client.patch(f"/api/tasks/{task.id}/deliverables/docs%2Foutput.md")
    assert response.status_code == 200
    body = response.json()
    assert body["deliverables"][0]["status"] == "done"


def test_search_tasks(api_client) -> None:
    client, manager = api_client
    manager.create_task(
        title="Write API documentation",
        description="Detailed docs",
        priority=Priority.MEDIUM,
        category="docs",
        tags=["documentation"],
    )
    response = client.get("/api/search", params={"q": "docs"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert "documentation" in body[0]["tags"]


def test_get_next_task_none(api_client) -> None:
    client, _ = api_client
    response = client.get("/api/tasks/next")
    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
)
def test_cors_preflight_allows_configured_origins(api_client, origin: str) -> None:
    """Preflight from each allowed origin echoes that origin back."""
    client, _ = api_client
    response = client.options(
        "/api/tasks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_preflight_rejects_unknown_origin(api_client) -> None:
    """An origin outside the allowlist gets no allow-origin header."""
    client, _ = api_client
    response = client.options(
        "/api/tasks",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers
