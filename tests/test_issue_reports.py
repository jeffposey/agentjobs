"""Creating a task that says who filed it.

The Report Issue action in the React shell is a shortcut to creating an ordinary task,
so there is no issue endpoint to test. What is new at this boundary is attribution: a
create may now name the actor who made it, and that id has to reach the log or be
refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app


@pytest.fixture(autouse=True)
def project_with_actors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A temporary project whose config names one human and one agent."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    config_dir = tmp_path / ".agentjobs"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "claude", "kind": "agent"},
                ],
                "default_user": "Jeff Posey",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def report_payload(**overrides: object) -> dict:
    """The body the Report Issue form sends, matching buildIssueTaskRequest."""
    payload: dict = {
        "title": "Task list filters match nothing",
        "description": "Every filter returns zero rows.\n\n---\nReported from the AgentJobs UI.",
        "lifecycle": "draft",
        "tags": ["reported-issue"],
        "actor": "Jeff Posey",
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "dependencies": [
            {"task": "task-052", "type": "related", "note": "Reported while viewing this task."}
        ],
    }
    payload.update(overrides)
    return payload


def test_reported_issue_is_a_normal_task_naming_its_reporter(client: TestClient) -> None:
    response = client.post("/api/tasks", json=report_payload())
    assert response.status_code == 201
    task = response.json()

    # A normal task, not a second kind of record.
    assert task["lifecycle"] == "draft"
    assert task["ball"] == "human"
    assert task["ball_reason"] == "spec"
    assert task["tags"] == ["reported-issue"]
    assert task["dependencies"] == [
        {"task": "task-052", "type": "related", "note": "Reported while viewing this task."}
    ]

    # And it says who filed it, which is the whole point of the actor field.
    creation = task["log"][0]
    assert creation["actor"] == "Jeff Posey"
    assert creation["type"] == "transition"
    assert "Jeff Posey" in creation["body"]


def test_creation_records_the_actor_without_an_operation_id(client: TestClient) -> None:
    """Attribution must not depend on also asking for retry safety.

    A caller that names an actor and receives no attribution has been silently
    ignored, which is exactly the failure actors.py exists to prevent.
    """
    payload = report_payload()
    payload.pop("operation_id")
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    assert response.json()["log"][0]["actor"] == "Jeff Posey"


def test_creation_naming_nobody_still_starts_with_an_empty_log(client: TestClient) -> None:
    """The old shape is untouched: no actor and no operation means no entry to write."""
    response = client.post(
        "/api/tasks",
        json={"title": "Anonymous", "description": "No actor supplied."},
    )
    assert response.status_code == 201
    assert response.json()["log"] == []


def test_unknown_reporter_is_refused_and_nothing_is_written(client: TestClient) -> None:
    response = client.post("/api/tasks", json=report_payload(actor="human"))
    assert response.status_code == 400
    assert "not an actor in this project" in response.text
    assert client.get("/api/tasks").json() == []


def test_retrying_a_report_resolves_to_the_task_the_first_attempt_filed(
    client: TestClient,
) -> None:
    """A double submit files one issue, not two."""
    first = client.post("/api/tasks", json=report_payload())
    second = client.post("/api/tasks", json=report_payload())
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/tasks").json()) == 1


def test_an_actionable_report_is_created_ready_for_an_agent(client: TestClient) -> None:
    response = client.post("/api/tasks", json=report_payload(lifecycle="ready"))
    assert response.status_code == 201
    task = response.json()
    assert task["lifecycle"] == "ready"
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "available"


def test_reported_issues_are_filterable_by_their_tag(client: TestClient) -> None:
    client.post("/api/tasks", json=report_payload())
    client.post(
        "/api/tasks",
        json={"title": "Ordinary work", "description": "Not a report."},
    )
    tasks = client.get("/api/tasks").json()
    reported = [task for task in tasks if "reported-issue" in (task.get("tags") or [])]
    assert len(reported) == 1
    assert reported[0]["title"] == "Task list filters match nothing"
