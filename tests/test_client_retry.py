"""Riding through a restart instead of failing on it.

Under the SQLite store the server is the only process that may write, so "the server is
restarting" stopped being something a client could ignore. The owner's decision of
2026-09-05 (task-273, entry 6, item 1) is that clients retry on a bounded backoff keyed
on the same ``operation_id``, and that a spent budget reports *service unavailable,
nothing written* rather than a stack trace.

Every test here monkeypatches the backoff to zero. The delays are the point in
production and pure cost in a suite, and asserting on the *number* of attempts rather
than on elapsed time is what makes these mean the same thing on every machine.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from agentjobs import client as client_module
from agentjobs.client import ServiceUnavailable, TaskClient, TaskClientError


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same number of attempts, with none of the waiting."""
    monkeypatch.setattr(client_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _task_payload() -> Dict[str, Any]:
    return {
        "schema": 2,
        "id": "task-001",
        "title": "Sample",
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": "medium",
        "category": "ops",
        "spec": {"summary": "s", "description": "d"},
        "queue_position": 100,
        "log": [],
    }


def _client(handler: Any) -> TaskClient:
    return TaskClient(base_url="http://testserver", transport=httpx.MockTransport(handler))


class TestRidingThroughARestart:
    """The window between the old process exiting and the new one listening."""

    def test_a_refused_connection_is_retried_and_then_succeeds(self) -> None:
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json=_task_payload())

        task = _client(handler).get_task("task-001")
        assert task.id == "task-001"
        assert len(attempts) == 3

    def test_a_503_from_the_front_door_is_retried(self) -> None:
        # The proxy answers 503 while nothing is listening rather than resetting the
        # connection, precisely so a client can tell a restart from a real failure.
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                return httpx.Response(503, text="AgentJobs is not listening")
            return httpx.Response(200, json=_task_payload())

        assert _client(handler).get_task("task-001").id == "task-001"
        assert len(attempts) == 2

    def test_a_spent_budget_says_nothing_was_written(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(ServiceUnavailable) as raised:
            _client(handler).get_task("task-001")
        message = str(raised.value)
        assert "service unavailable, nothing written" in message
        assert "agentjobs serve" in message
        # The whole point of the wording: there is no local store to fall back to.
        assert "no local task store" in message

    def test_it_is_still_a_TaskClientError(self) -> None:
        # Every existing caller catches TaskClientError. A new exception type escaping
        # through them would turn a restart into a crash, which is what the retry is
        # for in the first place.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(TaskClientError):
            _client(handler).get_task("task-001")


class TestWhatIsNotRetried:
    """Retrying the wrong thing writes twice, which is worse than failing."""

    def test_a_mutation_with_an_operation_id_is_retried(self) -> None:
        # Safe because the operation ledger replays the original result rather than
        # writing again -- which is exactly why the id is required on this surface.
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ReadError("reset", request=request)
            return httpx.Response(
                200,
                json={
                    "project_id": "demo",
                    "operation_id": "op-1",
                    "replayed": True,
                    "task": _task_payload(),
                },
            )

        result = _client(handler).operations.claim("task-001", actor="claude", operation_id="op-1")
        assert result.replayed
        assert len(attempts) == 2

    def test_a_mutation_without_one_is_not_retried_past_the_connection(self) -> None:
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadError("reset after the request was sent", request=request)

        with pytest.raises(TaskClientError):
            _client(handler)._request("POST", "/api/tasks", json={"title": "x"})
        # Once. The server may have processed it, and re-sending would be a second
        # write with no ledger row to make it a replay.
        assert len(attempts) == 1

    def test_a_refused_connection_is_retried_even_without_an_operation_id(self) -> None:
        # A connect failure is different in kind: nothing was accepted, so there is
        # nothing that could have been written.
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json=_task_payload())

        _client(handler)._request("POST", "/api/tasks", json={"title": "x"})
        assert len(attempts) == 2

    def test_an_ordinary_error_is_reported_immediately(self) -> None:
        attempts: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(404, json={"detail": "Task not found"})

        with pytest.raises(TaskClientError) as raised:
            _client(handler).get_task("task-999")
        assert raised.value.status_code == 404
        assert len(attempts) == 1
