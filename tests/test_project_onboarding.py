"""API and page tests for project onboarding."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.projects import ProjectRegistry


@pytest.fixture()
def onboarding_client(tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    """Run onboarding against a disposable machine registry."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client
    reset_dependency_cache()


def configured_project(root: Path, name: str = "Existing Project") -> Path:
    """Create an existing AgentJobs project for registration tests."""
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": name, "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    return root


class TestRegisterExistingProject:
    def test_registers_and_returns_the_stored_project(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = configured_project(tmp_path / "existing")
        config_path = root / ".agentjobs" / "config.yaml"
        original = config_path.read_bytes()

        response = onboarding_client.post("/api/projects", json={"path": str(root)})

        assert response.status_code == 201, response.text
        assert response.json()["id"] == "existing-project"
        assert response.json()["name"] == "Existing Project"
        assert response.json()["root"] == str(root.resolve())
        assert config_path.read_bytes() == original

    @pytest.mark.parametrize(
        ("path_kind", "message"),
        [("missing", "Not a directory"), ("bare", "No AgentJobs config")],
    )
    def test_refuses_a_missing_directory_or_config(
        self,
        onboarding_client: TestClient,
        tmp_path: Path,
        path_kind: str,
        message: str,
    ) -> None:
        root = tmp_path / path_kind
        if path_kind == "bare":
            root.mkdir()

        response = onboarding_client.post("/api/projects", json={"path": str(root)})

        assert response.status_code == 400
        assert message in response.json()["detail"]

    def test_refuses_an_invalid_id(self, onboarding_client: TestClient, tmp_path: Path) -> None:
        root = configured_project(tmp_path / "existing")

        response = onboarding_client.post(
            "/api/projects", json={"path": str(root), "id": "Not/A/Slug"}
        )

        assert response.status_code == 400
        assert "Invalid project id" in response.json()["detail"]

    def test_refuses_duplicate_root_and_duplicate_id(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        first = configured_project(tmp_path / "first", "First")
        second = configured_project(tmp_path / "second", "Second")
        assert onboarding_client.post("/api/projects", json={"path": str(first)}).status_code == 201

        duplicate_root = onboarding_client.post(
            "/api/projects", json={"path": str(first), "id": "another"}
        )
        duplicate_id = onboarding_client.post(
            "/api/projects", json={"path": str(second), "id": "first"}
        )

        assert duplicate_root.status_code == 400
        assert "already registered" in duplicate_root.json()["detail"]
        assert duplicate_id.status_code == 400
        assert "already registered" in duplicate_id.json()["detail"]

    def test_malformed_config_is_a_usable_client_error(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = configured_project(tmp_path / "broken")
        (root / ".agentjobs" / "config.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")

        response = onboarding_client.post("/api/projects", json={"path": str(root)})

        assert response.status_code == 400
        assert "expected a mapping" in response.json()["detail"]


class TestInitializeProject:
    def test_initializes_and_registers_without_prompts(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = tmp_path / "fresh"
        root.mkdir()

        response = onboarding_client.post(
            "/api/projects/init",
            json={"path": str(root), "project_name": "Fresh Project", "user": "jeff"},
        )

        assert response.status_code == 201, response.text
        assert response.json()["id"] == "fresh-project"
        assert (root / "tasks").is_dir()
        config = yaml.safe_load((root / ".agentjobs" / "config.yaml").read_text())
        assert config["project_name"] == "Fresh Project"
        assert config["default_user"] == "jeff"
        assert ProjectRegistry().get("fresh-project").root == root.resolve()

    def test_requires_an_existing_directory(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = tmp_path / "missing" / "child"

        response = onboarding_client.post("/api/projects/init", json={"path": str(root)})

        assert response.status_code == 400
        assert "Not a directory" in response.json()["detail"]
        assert not root.exists()

    def test_invalid_id_does_not_initialize_the_directory(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = tmp_path / "fresh"
        root.mkdir()

        response = onboarding_client.post(
            "/api/projects/init", json={"path": str(root), "id": "Not/A/Slug"}
        )

        assert response.status_code == 400
        assert "Invalid project id" in response.json()["detail"]
        assert not (root / ".agentjobs" / "config.yaml").exists()

    def test_refuses_to_overwrite_a_config(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = configured_project(tmp_path / "existing")
        config_path = root / ".agentjobs" / "config.yaml"
        original = config_path.read_bytes()

        response = onboarding_client.post("/api/projects/init", json={"path": str(root)})

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]
        assert config_path.read_bytes() == original

    @pytest.mark.parametrize("field", ["tasks_directory", "prompts_directory"])
    def test_refuses_configured_directories_outside_the_root(
        self,
        onboarding_client: TestClient,
        tmp_path: Path,
        field: str,
    ) -> None:
        root = tmp_path / field
        root.mkdir()

        response = onboarding_client.post(
            "/api/projects/init", json={"path": str(root), field: "../outside"}
        )

        assert response.status_code == 400
        assert "inside the project directory" in response.json()["detail"]
        assert not (root / ".agentjobs" / "config.yaml").exists()


class TestInspectAndPage:
    def test_inspect_distinguishes_register_from_initialize(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        existing = configured_project(tmp_path / "existing", "Existing")
        fresh = tmp_path / "fresh"
        fresh.mkdir()

        register = onboarding_client.post(
            "/api/projects/inspect", json={"path": str(existing)}
        ).json()
        initialize = onboarding_client.post(
            "/api/projects/inspect", json={"path": str(fresh)}
        ).json()

        assert register["action"] == "register"
        assert initialize["action"] == "initialize"

    def test_onboarding_page_contains_the_two_api_flows(
        self, onboarding_client: TestClient
    ) -> None:
        body = onboarding_client.get("/projects/new").text

        assert "Add a project" in body
        assert "/api/projects/inspect" in body
        assert "'/api/projects'" in body
        assert "/api/projects/init" in body
