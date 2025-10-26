"""API integration tests for AgentJobs REST interface."""

from __future__ import annotations

from typing import Tuple

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager
from agentjobs.models import Priority, TaskStatus
from agentjobs.storage import TaskStorage


@pytest.fixture()
def api_client(tmp_path) -> Tuple[TestClient, TaskManager]:
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
        status=TaskStatus.READY,
    )
    manager.create_task(
        title="In progress",
        description="Working",
        priority=Priority.CRITICAL,
        category="ops",
        status=TaskStatus.READY,
    )
    critical = manager.get_next_task()
    assert critical is not None
    manager.update_status(
        task_id=critical.id,
        status=TaskStatus.IN_PROGRESS,
        author="codex",
        summary="Started",
    )

    response = client.get("/api/tasks", params={"status_filter": "ready"})
    assert response.status_code == 200
    bodies = response.json()
    assert len(bodies) == 1
    assert bodies[0]["status"] == TaskStatus.READY.value


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
        json={"status": "in_progress", "assigned_to": "codex"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TaskStatus.IN_PROGRESS.value
    assert body["assigned_to"] == "codex"


def test_status_update_endpoint(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Status",
        description="Track",
        priority=Priority.MEDIUM,
        category="ops",
    )
    response = client.post(
        f"/api/tasks/{task.id}/status",
        json={
            "status": "blocked",
            "author": "codex",
            "summary": "Waiting",
            "details": "Need input",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TaskStatus.BLOCKED.value
    assert body["status_updates"]


def test_get_next_task(api_client) -> None:
    client, manager = api_client
    manager.create_task(
        title="Low priority",
        description="Background",
        priority=Priority.LOW,
        category="ops",
        status=TaskStatus.READY,
    )
    critical = manager.create_task(
        title="Critical",
        description="Urgent",
        priority=Priority.CRITICAL,
        category="ops",
        status=TaskStatus.READY,
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
    assert len(body["status_updates"]) == 1
    assert body["status_updates"][0]["summary"] == "Halfway"


def test_mark_deliverable_complete(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Deliver",
        description="Ship",
        priority=Priority.MEDIUM,
        category="ops",
        deliverables=[{"path": "docs/output.md", "status": "in_progress"}],
    )
    response = client.patch(
        f"/api/tasks/{task.id}/deliverables/docs%2Foutput.md"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deliverables"][0]["status"] == "completed"


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


def test_get_starter_prompt(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Prompt",
        description="Starter text",
        priority=Priority.MEDIUM,
        category="ops",
    )
    response = client.get(f"/api/tasks/{task.id}/prompts/starter")
    assert response.status_code == 200
    payload = response.json()
    assert payload["starter"] == "Starter text"


def test_add_followup_prompt(api_client) -> None:
    client, manager = api_client
    task = manager.create_task(
        title="Prompt",
        description="Starter text",
        priority=Priority.MEDIUM,
        category="ops",
    )
    response = client.post(
        f"/api/tasks/{task.id}/prompts",
        json={
            "author": "codex",
            "content": "Need clarification",
            "context": "Followup",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["prompts"]["followups"]) == 1
    assert body["prompts"]["followups"][0]["author"] == "codex"
