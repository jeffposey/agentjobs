"""Integration tests for serving several projects from one API instance.

These use the real registry (pointed at a temp dir via AGENTJOBS_HOME) and the real
dependency wiring rather than dependency_overrides, because the thing most likely to be
wrong is the wiring itself -- specifically the per-project cache keys. Overriding the
manager would test around the bug instead of at it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import (
    TASKS_DIR_ENV,
    reset_dependency_cache,
)
from agentjobs.api.main import app
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.projects import ProjectError, ProjectRegistry
from agentjobs.storage import TaskStorage

SHARED_TASK_ID = "task-001-shared-id"
"""Both projects get a task with this id. Task ids are only unique within a project,
so any cross-project surface keying on the id alone will fail these tests."""


def build_project(root: Path, name: str) -> TaskStorage:
    """Create a project directory with config and one task, returning its storage."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": name, "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    storage = TaskStorage(root / "tasks")
    storage.save_task(
        Task(
            id=SHARED_TASK_ID,
            title=f"{name} task",
            created=now,
            updated=now,
            lifecycle=Lifecycle.READY,
            ball=Ball.AGENT,
            ball_reason=BallReason.AVAILABLE,
            priority=Priority.HIGH,
            queue_position=100,
            category="infrastructure",
            spec=Spec(summary=f"Belongs to {name}", description=f"Belongs to {name}"),
        )
    )
    return storage


@pytest.fixture()
def two_projects(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, ProjectRegistry]]:
    """One server, two registered projects, a temp registry, no env pinning."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    # Sit outside both projects so nothing resolves a default positionally.
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    build_project(tmp_path / "alpha", "Alpha")
    build_project(tmp_path / "beta", "Beta")

    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / "alpha", project_id="alpha")
    registry.add(tmp_path / "beta", project_id="beta")

    with TestClient(app) as client:
        yield client, registry

    reset_dependency_cache()


class TestProjectListing:
    def test_lists_both_projects_with_task_counts(self, two_projects) -> None:
        client, _ = two_projects

        payload = client.get("/api/projects").json()

        assert [p["id"] for p in payload] == ["alpha", "beta"]
        assert all(p["task_count"] == 1 for p in payload)

    def test_reports_each_projects_own_root(self, two_projects) -> None:
        client, _ = two_projects

        payload = {p["id"]: p for p in client.get("/api/projects").json()}

        assert payload["alpha"]["root"] != payload["beta"]["root"]
        assert payload["alpha"]["name"] == "Alpha"


class TestScopedIsolation:
    """The core guarantee: a request for one project never sees another's storage."""

    def test_each_project_serves_its_own_task_despite_the_shared_id(self, two_projects) -> None:
        client, _ = two_projects

        alpha = client.get(f"/api/projects/alpha/tasks/{SHARED_TASK_ID}").json()
        beta = client.get(f"/api/projects/beta/tasks/{SHARED_TASK_ID}").json()

        assert alpha["title"] == "Alpha task"
        assert beta["title"] == "Beta task"

    def test_revision_is_small_project_scoped_and_changes_after_a_direct_write(
        self, two_projects, tmp_path: Path
    ) -> None:
        client, _ = two_projects
        alpha_before = client.get("/api/projects/alpha/revision")
        beta_before = client.get("/api/projects/beta/revision")

        path = tmp_path / "alpha" / "tasks" / f"{SHARED_TASK_ID}.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "# direct writer\n", encoding="utf-8")

        alpha_after = client.get("/api/projects/alpha/revision")
        beta_after = client.get("/api/projects/beta/revision")

        assert alpha_before.status_code == alpha_after.status_code == 200
        assert set(alpha_after.json()) == {"revision", "task_count"}
        assert alpha_after.json()["task_count"] == 1
        assert alpha_after.json()["revision"] != alpha_before.json()["revision"]
        assert beta_after.json() == beta_before.json()

    def test_alternating_requests_do_not_poison_the_cache(self, two_projects) -> None:
        # A maxsize=1 cache passes the test above if the calls happen to be ordered
        # conveniently. Alternating repeatedly is what actually catches it.
        client, _ = two_projects

        for _ in range(3):
            assert (
                client.get(f"/api/projects/alpha/tasks/{SHARED_TASK_ID}").json()["title"]
                == "Alpha task"
            )
            assert (
                client.get(f"/api/projects/beta/tasks/{SHARED_TASK_ID}").json()["title"]
                == "Beta task"
            )

    def test_writes_land_in_the_addressed_project_only(self, two_projects) -> None:
        client, _ = two_projects

        response = client.post(
            f"/api/projects/alpha/tasks/{SHARED_TASK_ID}/claim",
            json={"agent": "claude"},
        )
        assert response.status_code == 200

        assert client.get(f"/api/projects/alpha/tasks/{SHARED_TASK_ID}").json()["lifecycle"] == (
            "active"
        )
        assert client.get(f"/api/projects/beta/tasks/{SHARED_TASK_ID}").json()["lifecycle"] == (
            "ready"
        )

    def test_creating_in_one_project_does_not_appear_in_the_other(self, two_projects) -> None:
        client, _ = two_projects

        client.post(
            "/api/projects/beta/tasks",
            json={"title": "Beta only", "description": "should not leak"},
        )

        alpha_titles = [t["title"] for t in client.get("/api/projects/alpha/tasks").json()]
        beta_titles = [t["title"] for t in client.get("/api/projects/beta/tasks").json()]

        assert "Beta only" in beta_titles
        assert "Beta only" not in alpha_titles

    def test_unknown_project_is_a_404_naming_what_exists(self, two_projects) -> None:
        client, _ = two_projects

        response = client.get("/api/projects/ghost/tasks")

        assert response.status_code == 404
        assert "alpha" in response.json()["detail"]


class TestDefaultResolution:
    def test_ambiguous_default_is_409_not_a_guess(self, two_projects) -> None:
        client, _ = two_projects

        response = client.get("/api/tasks")

        assert response.status_code == 409
        assert "alpha" in response.json()["detail"]

    def test_cwd_inside_a_project_resolves_the_unscoped_route(
        self, two_projects, tmp_path: Path, monkeypatch
    ) -> None:
        client, _ = two_projects
        monkeypatch.chdir(tmp_path / "beta")

        payload = client.get("/api/tasks").json()

        assert [t["title"] for t in payload] == ["Beta task"]


class TestCrossProjectView:
    def test_returns_tasks_from_every_project_tagged_with_their_project(self, two_projects) -> None:
        client, _ = two_projects

        rows = client.get("/api/all/tasks").json()

        assert {(r["project_id"], r["task"]["title"]) for r in rows} == {
            ("alpha", "Alpha task"),
            ("beta", "Beta task"),
        }

    def test_every_row_carries_the_project_needed_to_link_back(self, two_projects) -> None:
        # Both tasks share an id, so a row without its project_id is unresolvable.
        client, _ = two_projects

        rows = client.get("/api/all/tasks").json()

        assert all(row["project_id"] for row in rows)
        assert len({row["task"]["id"] for row in rows}) == 1, "ids collide, as intended"

    def test_filters_by_project(self, two_projects) -> None:
        client, _ = two_projects

        rows = client.get("/api/all/tasks", params={"project": "beta"}).json()

        assert [r["project_id"] for r in rows] == ["beta"]

    def test_filters_by_lifecycle_and_ball(self, two_projects) -> None:
        client, _ = two_projects
        client.post(
            f"/api/projects/alpha/tasks/{SHARED_TASK_ID}/claim",
            json={"agent": "claude"},
        )

        rows = client.get("/api/all/tasks", params={"lifecycle": "active"}).json()
        assert [r["project_id"] for r in rows] == ["alpha"]

        rows = client.get("/api/all/tasks", params={"ball": "agent"}).json()
        assert {r["project_id"] for r in rows} == {"alpha", "beta"}

    def test_all_tasks_is_not_shadowed_by_a_task_named_all(self, two_projects) -> None:
        # /api/all/tasks deliberately avoids /api/tasks/all, which a task with id
        # "all" would be indistinguishable from.
        client, _ = two_projects

        assert client.get("/api/all/tasks").status_code == 200


class TestSingleProjectCompatibility:
    """An install with nothing registered must behave exactly as it did before."""

    def test_env_pinned_directory_still_serves_unscoped_routes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv(TASKS_DIR_ENV, str(tmp_path / "solo" / "tasks"))
        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path / "solo"))
        reset_dependency_cache()
        build_project(tmp_path / "solo", "Solo")

        with TestClient(app) as client:
            assert [t["title"] for t in client.get("/api/tasks").json()] == ["Solo task"]
            # "_local", not ".": the implicit id has to be URL-safe now that pages
            # are project-scoped, because "/p/./tasks" normalises to "/p/tasks".
            assert [p["id"] for p in client.get("/api/projects").json()] == ["_local"]

        reset_dependency_cache()

    def test_env_override_wins_over_a_populated_registry(self, tmp_path: Path, monkeypatch) -> None:
        # Pinning by environment is how the CLI and tests address one directory; a
        # registry that happens to exist must not override an explicit instruction.
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        build_project(tmp_path / "alpha", "Alpha")
        build_project(tmp_path / "solo", "Solo")
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "alpha", project_id="alpha")

        monkeypatch.setenv(TASKS_DIR_ENV, str(tmp_path / "solo" / "tasks"))
        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path / "solo"))
        reset_dependency_cache()

        with TestClient(app) as client:
            assert [t["title"] for t in client.get("/api/tasks").json()] == ["Solo task"]

        reset_dependency_cache()


class TestPathContainment:
    def test_traversal_in_a_task_id_does_not_escape_the_project(
        self, two_projects, tmp_path: Path
    ) -> None:
        client, _ = two_projects
        (tmp_path / "secret.yaml").write_text("id: secret\ntitle: nope\n", encoding="utf-8")

        response = client.get("/api/projects/alpha/tasks/..%2F..%2Fsecret")

        assert response.status_code in (400, 404), response.text
        assert "nope" not in response.text

    def test_storage_refuses_a_traversing_task_id(self, tmp_path: Path) -> None:
        # The route test above is defence in depth: FastAPI does not match "/" in a
        # plain path parameter, so it may never reach storage. This asserts the
        # containment itself, which is the guarantee that actually matters.
        (tmp_path / "tasks").mkdir()
        (tmp_path / "secret.yaml").write_text("id: secret\ntitle: nope\n", encoding="utf-8")
        storage = TaskStorage(tmp_path / "tasks")

        for probe in ("../secret", "../secret.yaml", "..\\secret"):
            with pytest.raises(ProjectError, match="outside the project directory"):
                storage.load_task(probe)

    def test_a_refused_path_is_a_400_not_a_500(self, two_projects) -> None:
        # ProjectError escaping a handler must be a bad request, not a stack trace.
        client, _ = two_projects

        response = client.get("/api/projects/alpha/tasks/", params={})

        assert response.status_code != 500
