"""The read-only REST surface for playbooks, scoped and unscoped.

The routes are exercised through the real registry and the real dependency wiring, for
the reason ``test_api_multiproject`` states: the thing most likely to be wrong is the
wiring, and overriding it would test around that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.playbooks import install_references
from agentjobs.projects import ProjectRegistry

VALID = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates:
  - before: close
    what: A human approved the list.
---

# Groom

The brief a run reads.
"""


@pytest.fixture()
def project(tmp_path: Path) -> Iterator[Tuple[TestClient, Path]]:
    """A registered project with a playbooks directory, and a client for it."""
    root = tmp_path / "alpha"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Alpha", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    (root / "tasks").mkdir()
    ProjectRegistry().add(root, project_id="alpha", name="Alpha")
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client, root
    reset_dependency_cache()


class TestCollection:
    """``GET /api/projects/{id}/playbooks``."""

    def test_a_project_with_no_directory_reports_that_rather_than_failing(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        client, _ = project
        response = client.get("/api/projects/alpha/playbooks")
        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is False
        assert body["playbooks"] == []
        assert body["directory"].endswith("playbooks")

    def test_playbooks_are_listed_without_their_bodies(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        """The collection is discovery, not a way to fetch every brief at once."""
        client, root = project
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        body = client.get("/api/projects/alpha/playbooks").json()
        assert body["exists"] is True
        assert [item["name"] for item in body["playbooks"]] == ["groom"]
        entry = body["playbooks"][0]
        assert entry["target"] == "project"
        assert entry["difficulty"] == "hard"
        assert entry["gates"] == [{"before": "close", "what": "A human approved the list."}]
        assert entry["filename"] == "groom.md"
        assert "body" not in entry

    def test_invalid_files_are_reported_beside_valid_ones(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        (root / "playbooks" / "broken.md").write_text(
            VALID.replace("difficulty: hard", "difficulty: extreme"), encoding="utf-8"
        )
        body = client.get("/api/projects/alpha/playbooks").json()
        assert [item["name"] for item in body["playbooks"]] == ["groom"]
        assert {problem["filename"] for problem in body["problems"]} == {"broken.md"}
        assert any(problem["field"] == "difficulty" for problem in body["problems"])

    def test_the_configured_directory_is_honoured(self, project: Tuple[TestClient, Path]) -> None:
        client, root = project
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "project_name": "Alpha",
                    "tasks_directory": "tasks",
                    "playbooks_directory": "briefs",
                }
            ),
            encoding="utf-8",
        )
        (root / "briefs").mkdir()
        (root / "briefs" / "groom.md").write_text(VALID, encoding="utf-8")
        body = client.get("/api/projects/alpha/playbooks").json()
        assert body["directory"].endswith("briefs")
        assert [item["name"] for item in body["playbooks"]] == ["groom"]

    def test_the_unscoped_mount_serves_the_same_handler(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        assert (
            client.get("/api/playbooks").json()
            == client.get("/api/projects/alpha/playbooks").json()
        )


class TestSinglePlaybook:
    """``GET /api/projects/{id}/playbooks/{name}`` -- the only route that returns a brief."""

    def test_one_playbook_carries_its_brief(self, project: Tuple[TestClient, Path]) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        body = client.get("/api/projects/alpha/playbooks/groom").json()
        assert body["name"] == "groom"
        assert "The brief a run reads." in body["body"]

    def test_an_unknown_name_is_404(self, project: Tuple[TestClient, Path]) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        assert client.get("/api/projects/alpha/playbooks/absent").status_code == 404

    def test_a_file_that_does_not_validate_is_422_not_404(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        """It is there and repairable; 'not found' would send its author hunting."""
        client, root = project
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(
            VALID.replace("difficulty: hard", "difficulty: extreme"), encoding="utf-8"
        )
        response = client.get("/api/projects/alpha/playbooks/groom")
        assert response.status_code == 422
        assert "difficulty" in response.json()["detail"]

    @pytest.mark.parametrize("name", ["..", ".hidden", "with space"])
    def test_a_name_that_is_not_a_filename_stem_is_refused(
        self, project: Tuple[TestClient, Path], name: str
    ) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        assert client.get(f"/api/projects/alpha/playbooks/{name}").status_code in (400, 404)

    def test_a_traversal_attempt_cannot_read_outside_the_directory(
        self, project: Tuple[TestClient, Path]
    ) -> None:
        client, root = project
        (root / "playbooks").mkdir()
        (root / "secret.md").write_text(VALID, encoding="utf-8")
        response = client.get("/api/projects/alpha/playbooks/..%2Fsecret")
        assert response.status_code in (400, 404)


def test_running_is_the_only_thing_a_playbook_route_does_besides_read(
    project: Tuple[TestClient, Path],
) -> None:
    """The playbook surface is three reads and one run, on either mount.

    Asserted against the application's own route table rather than by probing verbs, so
    a route added later fails here even if nobody writes a test for it. Until task-215
    this asserted that *no* non-GET route existed at all, which was the correct claim
    while nothing could be run; the claim now is that exactly one can, and that it is
    the run route. Widening this is a design change (P10), not a test fix.
    """
    client, _ = project
    del client
    mutating = {
        getattr(route, "path", "")
        for route in app.routes
        if "playbook" in getattr(route, "path", "")
        and set(getattr(route, "methods", set()) or set()) - {"GET", "HEAD"}
    }
    assert mutating == {
        "/api/playbooks/{name}/run",
        "/api/projects/{project_id}/playbooks/{name}/run",
    }


def test_a_project_initialized_through_the_api_gets_a_playbooks_directory_setting(
    tmp_path: Path,
) -> None:
    """`playbooks_directory` follows `prompts_directory` into a new project's config."""
    root = tmp_path / "fresh"
    root.mkdir()
    reset_dependency_cache()
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/init",
            json={"path": str(root), "id": "fresh", "project_name": "Fresh"},
        )
    assert response.status_code == 201
    config = yaml.safe_load((root / ".agentjobs" / "config.yaml").read_text(encoding="utf-8"))
    assert config["playbooks_directory"] == "playbooks"
    reset_dependency_cache()


def test_installed_references_are_served_by_the_api(project: Tuple[TestClient, Path]) -> None:
    """End to end: what `playbook init` writes is what the API reports."""
    client, root = project
    install_references(root / "playbooks")
    body = client.get("/api/projects/alpha/playbooks").json()
    assert [item["name"] for item in body["playbooks"]] == ["flesh-out", "groom", "reorder"]
    assert body["problems"] == []


# ----- running ----------------------------------------------------------------

RUNNABLE = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates: []
run_task:
  title: Groom the {project} backlog
  category: meta
  priority: medium
  tags: [grooming]
  acceptance:
    - text: Nothing outside the approved list was closed.
---

# Groom

A distinctive brief sentence nothing else says.
"""

TASK_SHAPED = """---
name: flesh-out
description: Write a full spec onto a thin task.
target: task
difficulty: hard
verbs: [update, log]
gates: []
---

# Flesh out

A distinctive brief sentence nothing else says.
"""


@pytest.fixture()
def runnable(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path]]:
    """A project this machine is configured to dispatch, with two playbooks in it.

    Built separately from ``project`` above because running needs three things reading
    is indifferent to: an actor vocabulary with a human in it, a git repository, and a
    machine-local dispatch config naming a runner that exits immediately.
    """
    import subprocess
    import sys

    from agentjobs.api.dependencies import reset_dependency_cache
    from agentjobs.projects import HOME_ENV

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))

    root = tmp_path / "beta"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Beta",
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
    (root / "tasks").mkdir()
    (root / "playbooks").mkdir()
    (root / "playbooks" / "groom.md").write_text(RUNNABLE, encoding="utf-8")
    (root / "playbooks" / "flesh-out.md").write_text(TASK_SHAPED, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)

    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {
                        "argv": [sys.executable, str(runner), "{prompt}"],
                        "actor": "claude",
                    }
                },
                "projects": {
                    "beta": {"enabled": True, "runner": "fake", "require_clean_tree": False}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    ProjectRegistry().add(root, project_id="beta", name="Beta")
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client, root
    reset_dependency_cache()


class TestRun:
    """``POST /api/projects/{id}/playbooks/{name}/run`` -- the one route that spends."""

    def test_a_project_playbook_creates_a_run_task_and_starts_a_run(
        self, runnable: Tuple[TestClient, Path]
    ) -> None:
        client, _ = runnable
        response = client.post(
            "/api/projects/beta/playbooks/groom/run", json={"user": "Jeff Posey"}
        )
        assert response.status_code == 202, response.text
        body = response.json()

        assert body["playbook"] == "groom"
        assert body["playbook_path"] == "playbooks/groom.md"
        assert body["playbook_hash"].startswith("sha256:")
        assert body["created_run_task"] is True
        assert body["run_id"]

        task = client.get(f"/api/projects/beta/tasks/{body['task_id']}").json()
        assert task["title"] == "Groom the beta backlog"
        assert "A distinctive brief sentence" not in task["spec"]["description"]

    def test_a_task_playbook_dispatches_the_task_it_is_given(
        self, runnable: Tuple[TestClient, Path]
    ) -> None:
        client, _ = runnable
        created = client.post(
            "/api/projects/beta/tasks",
            json={
                "title": "Thin",
                "description": "Barely a sentence.",
                "actor": "Jeff Posey",
                "lifecycle": "ready",
            },
        )
        assert created.status_code in (200, 201), created.text
        task_id = created.json()["id"]

        response = client.post(
            "/api/projects/beta/playbooks/flesh-out/run",
            json={"user": "Jeff Posey", "task": task_id},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["task_id"] == task_id
        assert body["created_run_task"] is False

    def test_a_shape_mismatch_is_a_409_naming_the_reason(
        self, runnable: Tuple[TestClient, Path]
    ) -> None:
        client, _ = runnable
        response = client.post(
            "/api/projects/beta/playbooks/groom/run",
            json={"user": "Jeff Posey", "task": "task-001"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "target_mismatch"

    def test_an_unknown_playbook_is_a_404(self, runnable: Tuple[TestClient, Path]) -> None:
        client, _ = runnable
        response = client.post("/api/projects/beta/playbooks/nope/run", json={"user": "Jeff Posey"})
        assert response.status_code == 404
        assert response.json()["code"] == "unknown_playbook"

    def test_an_agent_cannot_be_the_user(self, runnable: Tuple[TestClient, Path]) -> None:
        client, _ = runnable
        response = client.post("/api/projects/beta/playbooks/groom/run", json={"user": "claude"})
        assert response.status_code == 403
        assert response.json()["code"] == "authorizer_not_human"

    def test_nobody_signed_in_is_refused_rather_than_defaulted(
        self, runnable: Tuple[TestClient, Path]
    ) -> None:
        """``default_user`` is configured here, and is deliberately not substituted."""
        client, _ = runnable
        response = client.post("/api/projects/beta/playbooks/groom/run", json={})
        assert response.status_code == 403
        assert response.json()["code"] == "no_authorizing_human"
