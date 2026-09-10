"""A claim that names its session, over real HTTP, and what the dashboard then says.

The unit tests in ``test_interactive_runs.py`` cover the record and its lifecycle. These
cover the seam task-354 chose -- the claim is what writes it -- and the answer the GUI
actually reads, because the defect was never in a function: it was that
``GET /api/runs/live`` said *nothing is running* while a task was being worked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from agentjobs.projects import ProjectRegistry
from support import task_store

SESSION = "0feedf93-6af3-40bf-832a-81f722fdb841"

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


@pytest.fixture()
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path, str]]:
    """A served project with one ready task, and a home nothing else writes to."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "sandbox"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    (root / "tasks").mkdir()
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    manager = TaskManager(task_store(root / "tasks"))
    task = manager.create_task(
        id="task-001",
        title="Worked in a chat window",
        summary="A task somebody claims from a session AgentJobs did not start.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="claude",
    )

    # Without the lifespan: it reconciles runs at startup, and these tests write the
    # very records it would settle. test_dispatch_lifecycle.py owns reconciliation.
    yield TestClient(app), home, task.id

    reset_dependency_cache()


def _claim(client: TestClient, task_id: str, **extra: Any) -> Dict[str, Any]:
    response = client.post(
        f"/api/projects/sandbox/tasks/{task_id}/claim",
        json={"agent": "claude", **extra},
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _live(client: TestClient) -> Dict[str, Any]:
    response = client.get("/api/runs/live")
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


class TestTheClaimMakesTheWorkVisible:
    def test_a_session_that_claims_is_running_on_the_machine_wide_surface(self, served) -> None:
        # The defect, stated as a test: this exact sequence used to leave the dashboard
        # saying nothing was running while the task read active/agent/work.
        client, _, task_id = served

        _claim(client, task_id, session_id=SESSION, session_cwd="C:/projects/agentjobs")

        body = _live(client)
        assert [run["task_id"] for run in body["runs"]] == [task_id]
        row = body["runs"][0]
        assert row["mode"] == "interactive"
        assert row["session"] is False, "not a dispatched session run"
        assert row["task_title"] == "Worked in a chat window"
        assert row["task_url"] == "/p/sandbox/tasks/task-001"

    def test_it_is_running_without_being_a_busy_slot(self, served) -> None:
        # Two numbers, two questions. `runs` is what is happening; `occupied` is what a
        # dispatch would have to wait for, and a session Jeff is typing into is not that.
        client, _, task_id = served

        _claim(client, task_id, session_id=SESSION)

        body = _live(client)
        assert len(body["runs"]) == 1
        assert body["occupied"] == 0

    def test_a_claim_with_no_session_writes_no_run(self, served) -> None:
        # Every caller that predates this, and every human at the keyboard.
        client, _, task_id = served

        _claim(client, task_id)

        assert _live(client)["runs"] == []

    def test_claiming_twice_leaves_one_run(self, served) -> None:
        client, _, task_id = served
        _claim(
            client, task_id, session_id=SESSION, operation_id="11111111-1111-4111-8111-111111111111"
        )

        # A replay of the same operation, which the API answers from its record.
        _claim(
            client, task_id, session_id=SESSION, operation_id="11111111-1111-4111-8111-111111111111"
        )

        assert len(_live(client)["runs"]) == 1


class TestItEndsWithTheWork:
    def test_handing_off_to_a_human_takes_it_off_the_board(self, served) -> None:
        client, _, task_id = served
        claimed = _claim(client, task_id, session_id=SESSION)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/handoff",
            json={
                "actor": "claude",
                "ball": "human",
                "ball_reason": "review",
                "ball_prompt": "Please look at this.",
                "expected_revision": claimed["updated"],
            },
        )
        assert response.status_code == 200, response.text

        assert _live(client)["runs"] == [], "the session said it was done with the task"

    def test_closing_the_task_takes_it_off_the_board(self, served) -> None:
        client, _, task_id = served
        _claim(client, task_id, session_id=SESSION)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/close",
            json={"actor": "claude", "outcome": "completed", "body": "Done."},
        )
        assert response.status_code == 200, response.text

        assert _live(client)["runs"] == []

    def test_releasing_the_task_takes_it_off_the_board(self, served) -> None:
        client, _, task_id = served
        _claim(client, task_id, session_id=SESSION)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/release",
            json={"actor": "claude", "body": "Somebody else can have it."},
        )
        assert response.status_code == 200, response.text

        assert _live(client)["runs"] == []
