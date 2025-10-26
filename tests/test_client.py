"""Unit tests for the TaskClient convenience wrapper."""

from __future__ import annotations

from typing import Any, Dict

import json
import httpx
import pytest

from agentjobs.client import TaskClient, TaskClientError
from agentjobs.models import Priority, TaskStatus


def _sample_task(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "id": "task-001",
        "title": "Sample",
        "created": "2025-01-01T00:00:00+00:00",
        "updated": "2025-01-01T00:00:00+00:00",
        "status": TaskStatus.DRAFT.value,
        "priority": Priority.MEDIUM.value,
        "category": "ops",
        "description": "Sample description",
        "phases": [],
        "success_criteria": [],
        "prompts": {"starter": "Sample description", "followups": []},
        "status_updates": [],
        "deliverables": [],
        "dependencies": [],
        "external_links": [],
        "issues": [],
        "tags": [],
        "branches": [],
    }
    payload.update(overrides)
    return payload


def _client_with_handler(handler: httpx.MockTransport) -> TaskClient:
    return TaskClient(base_url="http://testserver", transport=handler)


def test_client_get_next_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tasks/next"
        return httpx.Response(200, json=_sample_task(id="task-123"))

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.get_next_task()
    assert task.id == "task-123"
    client.close()


def test_client_mark_in_progress() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/status"
        body = json.loads(request.content.decode())
        assert body["status"] == TaskStatus.IN_PROGRESS.value
        assert body["author"] == "codex"
        return httpx.Response(200, json=_sample_task(status=TaskStatus.IN_PROGRESS.value))

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.mark_in_progress("task-001", agent="codex", summary="Working")
    assert task.status == TaskStatus.IN_PROGRESS.value
    client.close()


def test_client_add_progress_update() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/progress"
        body = json.loads(request.content.decode())
        assert body["summary"] == "Updated"

        return httpx.Response(
            200,
            json=_sample_task(
                status_updates=[
                    {
                        "summary": "Updated",
                        "author": "codex",
                        "status": TaskStatus.READY.value,
                        "timestamp": "2025-01-01T01:00:00+00:00",
                    }
                ]
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.add_progress_update("task-001", summary="Updated", agent="codex")
    assert task.status_updates
    client.close()


def test_client_mark_completed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/status"
        return httpx.Response(200, json=_sample_task(status=TaskStatus.COMPLETED.value))

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.mark_completed("task-001", summary="Done")
    assert task.status == TaskStatus.COMPLETED.value
    client.close()


def test_client_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(httpx.MockTransport(handler))
    with pytest.raises(TaskClientError):
        client.get_task("task-001")
    client.close()


def test_client_404_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Task not found"})

    client = _client_with_handler(httpx.MockTransport(handler))
    with pytest.raises(TaskClientError) as excinfo:
        client.get_task("missing")
    assert "Task not found" in str(excinfo.value)
    client.close()
