"""Unit tests for the TaskClient convenience wrapper (schema v2)."""

from __future__ import annotations

from typing import Any, Dict

import json
import httpx
import pytest

from agentjobs.client import TaskClient, TaskClientError


def _sample_task(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema": 2,
        "id": "task-001",
        "title": "Sample",
        "created": "2025-01-01T00:00:00+00:00",
        "updated": "2025-01-01T00:00:00+00:00",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": "medium",
        "category": "ops",
        "spec": {"summary": "Sample summary", "description": "Sample description"},
        "log": [],
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
    assert task is not None
    assert task.id == "task-123"
    client.close()


def test_client_tolerates_served_display_status() -> None:
    """The API serves the computed display_status; the client must not choke on it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sample_task(display_status="Ready"))

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.get_task("task-001")
    assert task.display_status == "Ready"
    client.close()


def test_client_claim_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/claim"
        body = json.loads(request.content.decode())
        assert body == {"agent": "codex"}
        return httpx.Response(
            200,
            json=_sample_task(
                lifecycle="active",
                ball="agent",
                ball_reason="work",
                ball_prompt="Execute the spec.",
                assignment={"owner": "codex", "eligible": []},
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.claim_task("task-001", agent="codex")
    assert task.assignment.owner == "codex"
    client.close()


def test_client_handoff_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/handoff"
        body = json.loads(request.content.decode())
        assert body["ball"] == "human"
        assert body["ball_reason"] == "review"
        assert body["ball_prompt"] == "Review the diff."
        return httpx.Response(
            200,
            json=_sample_task(
                lifecycle="active",
                ball="human",
                ball_reason="review",
                ball_prompt="Review the diff.",
                assignment={"owner": "codex", "eligible": []},
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.handoff_task(
        "task-001",
        actor="codex",
        ball="human",
        ball_reason="review",
        ball_prompt="Review the diff.",
    )
    assert task.ball == "human"
    client.close()


def test_client_close_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/close"
        body = json.loads(request.content.decode())
        assert body["outcome"] == "completed"
        return httpx.Response(
            200,
            json=_sample_task(
                lifecycle="closed",
                ball=None,
                ball_reason=None,
                outcome="completed",
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.close_task("task-001", actor="codex", outcome="completed")
    assert task.outcome == "completed"
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
                log=[
                    {
                        "id": 1,
                        "ts": "2025-01-01T01:00:00+00:00",
                        "actor": "codex",
                        "type": "progress",
                        "body": "Updated",
                    }
                ]
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.add_progress_update("task-001", summary="Updated", agent="codex")
    assert task.log
    client.close()


def test_client_add_log_entry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks/task-001/log"
        body = json.loads(request.content.decode())
        assert body["type"] == "decision"
        return httpx.Response(
            200,
            json=_sample_task(
                log=[
                    {
                        "id": 1,
                        "ts": "2025-01-01T01:00:00+00:00",
                        "actor": "claude",
                        "type": "decision",
                        "body": "Chose A.",
                    }
                ]
            ),
        )

    client = _client_with_handler(httpx.MockTransport(handler))
    task = client.add_log_entry("task-001", actor="claude", type="decision", body="Chose A.")
    assert task.log[0].type == "decision"
    client.close()


def test_client_list_tasks_sends_axis_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ball"] == "human"
        assert request.url.params["lifecycle"] == "active"
        return httpx.Response(200, json=[])

    client = _client_with_handler(httpx.MockTransport(handler))
    assert client.list_tasks(lifecycle="active", ball="human") == []
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
