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


def test_the_playbook_routes_are_read_only(project: Tuple[TestClient, Path]) -> None:
    """Design P10: no run surface exists, on any method, on either mount.

    Asserted against the application's own route table rather than by probing verbs,
    so a POST added later fails here even if nobody writes a test for it.
    """
    client, _ = project
    del client
    offending = [
        (getattr(route, "path", ""), sorted(getattr(route, "methods", set()) or set()))
        for route in app.routes
        if "playbook" in getattr(route, "path", "")
        and set(getattr(route, "methods", set()) or set()) - {"GET", "HEAD"}
    ]
    assert offending == []


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
