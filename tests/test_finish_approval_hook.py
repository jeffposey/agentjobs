"""What the Approve button does, and what it must never do to a different button.

The scripted finish (task-241) is started by exactly one route. That is a design
decision with a sharp failure mode behind it: **requesting changes and approving both
hand the ball to the agent**, so anything that decided "should this merge?" by looking at
the resulting task would eventually merge a branch somebody had just asked for changes
on. The route that received the click is the only thing that knows the difference, so it
is the route that passes ``finishable``.

The human approval gate itself (f6) is unchanged by all of this and is checked here too:
no merge happens on any path that a person did not click Approve on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [{"name": "jeff", "kind": "human"}, {"name": "claude", "kind": "agent"}],
                "default_user": "jeff",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Record every finish this test would have started, instead of starting one.

    The subprocess itself is covered against real repositories in
    ``test_dispatch_finish.py``. What is being checked here is only *which click starts
    one*, and spawning a real detached process from an API test would be measuring the
    wrong thing at considerable cost.
    """
    calls: List[Dict[str, Any]] = []

    def fake_spawn(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "spawned"

    monkeypatch.setattr("agentjobs.api.routes.tasks.spawn_finish", fake_spawn)
    monkeypatch.setattr("agentjobs.api.routes.tasks.finish_is_offered", lambda project_id: True)
    return calls


@pytest.fixture
def offered_off(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr("agentjobs.api.routes.tasks.spawn_finish", lambda **kw: calls.append(kw))
    monkeypatch.setattr("agentjobs.api.routes.tasks.finish_is_offered", lambda project_id: False)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def task_awaiting_review(client: TestClient) -> str:
    created = client.post(
        "/api/tasks",
        json={
            "title": "Waiting on a human",
            "description": "Built and handed off.",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    client.post(f"/api/tasks/{task_id}/claim", json={"agent": "claude"})
    client.post(
        f"/api/tasks/{task_id}/handoff",
        json={
            "actor": "claude",
            "ball": "human",
            "ball_reason": "review",
            "ball_prompt": "Please review the branch.",
        },
    )
    return str(task_id)


class TestWhichClickFinishes:
    def test_approve_starts_a_finish(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        task_id = task_awaiting_review(client)
        response = client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        assert response.status_code == 200, response.text
        assert len(spawned) == 1
        assert spawned[0]["task_id"] == task_id
        assert spawned[0]["approver"] == "jeff"

    def test_requesting_changes_never_starts_a_finish(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """The whole reason ``finishable`` is a parameter and not a derived value."""
        task_id = task_awaiting_review(client)
        response = client.post(
            f"/api/tasks/{task_id}/request-changes",
            json={"user": "jeff", "feedback": "Rename the thing before you merge."},
        )
        assert response.status_code == 200, response.text
        assert spawned == []

    def test_a_machine_that_does_not_offer_finishing_falls_through(
        self, client: TestClient, offered_off: List[Dict[str, Any]]
    ) -> None:
        """Off is the default everywhere, and Approve then behaves exactly as before."""
        task_id = task_awaiting_review(client)
        response = client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        assert response.status_code == 200
        assert offered_off == []

    def test_approve_still_records_the_approval_and_moves_the_ball(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """f6: the human gate is untouched. The click is still what authorises a merge."""
        task_id = task_awaiting_review(client)
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        task = client.get(f"/api/tasks/{task_id}").json()
        assert task["ball"] == "agent"
        assert task["lifecycle"] == "active"
        approvals = [entry for entry in task["log"] if entry["actor"] == "jeff"]
        assert any("Approved by jeff" in (entry["body"] or "") for entry in approvals)

    def test_nothing_merges_without_a_click(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """A task sitting in review starts no finish on its own, however long it sits."""
        task_awaiting_review(client)
        assert spawned == []
