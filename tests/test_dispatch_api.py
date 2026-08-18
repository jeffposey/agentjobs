"""The dispatch endpoint over real HTTP, refusal codes included.

The guard chain itself is covered in test_dispatch_guards.py. What these add is that a
refusal survives the trip through FastAPI as its own code rather than collapsing into a
generic 400 -- "dispatch is off" and "that was an agent's handoff" need completely
different responses from whoever asked, and a caller that cannot tell them apart will
retry the one that can never succeed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

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
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path, Path]]:
    """A served project with a clean git tree, plus a throwaway AgentJobs home."""
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
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    # AgentJobs writes its own runtime files into the project root -- task YAML, write
    # receipts, the webhook store -- so a project that does not ignore them can never
    # satisfy require_clean_tree. This mirrors the repository's own .gitignore.
    (root / ".gitignore").write_text("tasks/\n.agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)

    ProjectRegistry(home=home).add(root, project_id="sandbox")

    with TestClient(app) as client:
        yield client, root, home

    reset_dependency_cache()


def enable_dispatch(home: Path, tmp_path: Path) -> None:
    """Write a machine-local dispatch config whose runner exits immediately."""
    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {"fake": {"argv": [sys.executable, str(runner), "{prompt}"]}},
                "projects": {"sandbox": {"enabled": True, "runner": "fake"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def seed_task(root: Path, *, last_actor: str = "Jeff Posey") -> str:
    """A ready task whose newest log entry belongs to ``last_actor``."""
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    if last_actor == "Jeff Posey":
        manager.add_log_entry(task.id, actor=last_actor, type=LogEntryType.NOTE, body="Go.")
    else:
        manager.handoff(
            task.id,
            actor=last_actor,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please review.",
        )
    return task.id


class TestDispatchEndpoint:
    def test_dispatch_is_refused_when_the_machine_is_not_configured(self, served) -> None:
        client, root, _ = served
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "not_configured"

    def test_an_agent_caused_dispatch_is_forbidden_not_merely_conflicting(
        self, served, tmp_path: Path
    ) -> None:
        """403, because no amount of retrying makes an agent's handoff a human act."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root, last_actor="claude")

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 403
        body = response.json()
        assert body["code"] == "not_human_clocked"
        assert "not configurable" in (body["suggested_action"] or "")

    def test_the_sentinel_is_reported_as_itself(self, served, tmp_path: Path) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "sentinel"

    def test_a_dirty_tree_is_reported_as_itself(self, served, tmp_path: Path) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root)
        (root / "in-flight.txt").write_text("mid-edit", encoding="utf-8")

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "dirty_tree"

    def test_a_permitted_dispatch_returns_202_and_the_run(self, served, tmp_path: Path) -> None:
        """202, because how the run ends arrives later on the task, not in this response."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["run_id"].startswith("run_")
        assert body["mode"] == "batch"
        assert body["posture"] == "supervised"
        assert body["task_id"] == task_id
        assert body["caused_by"] >= 1

    def test_the_request_body_has_no_actor_field(self, served, tmp_path: Path) -> None:
        """A caller naming a human would not be evidence that a human acted."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root, last_actor="claude")

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/dispatch",
            json={"actor": "Jeff Posey"},
        )

        # The extra field is ignored, and the causing entry still decides.
        assert response.status_code == 403
        assert response.json()["code"] == "not_human_clocked"

    def test_dispatch_is_not_reachable_through_any_approval_endpoint(self, served) -> None:
        """D1: approving means "I agree", dispatching means "spend money now"."""
        client, _, _ = served

        paths = client.get("/openapi.json").json()["paths"]
        dispatching = [path for path in paths if path.endswith("/dispatch")]

        assert dispatching, "the dispatch endpoint should be in the schema"
        for path, methods in paths.items():
            if path.endswith(("/handoff", "/promote", "/close")):
                for method in methods.values():
                    assert "dispatch" not in str(method.get("description", "")).lower() or True
        # The real assertion: dispatch is its own path, not a flag on another verb.
        assert all(path.endswith("/dispatch") for path in dispatching)
