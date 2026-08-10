"""CLI tests for the project registry commands.

The registry these exercise is redirected to a temp directory by the autouse
``isolate_project_registry`` fixture in conftest.py -- without it these would write the
developer's real ~/.agentjobs.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.projects import ProjectRegistry

runner = CliRunner()


def make_project(root: Path, name: str) -> Path:
    """Create a directory with an AgentJobs config."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": name, "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    return root


def registry() -> ProjectRegistry:
    """The registry the CLI will use, honouring the test's AGENTJOBS_HOME."""
    return ProjectRegistry(home=Path(os.environ["AGENTJOBS_HOME"]))


class TestProjectAdd:
    def test_registers_a_directory(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "alpha", "Alpha")

        result = runner.invoke(app, ["project", "add", str(root)])

        assert result.exit_code == 0, result.output
        assert [p.id for p in registry().list_projects()] == ["alpha"]

    def test_explicit_id_and_name(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "alpha", "Alpha")

        result = runner.invoke(
            app, ["project", "add", str(root), "--id", "a", "--name", "Alpha Project"]
        )

        assert result.exit_code == 0, result.output
        project = registry().get("a")
        assert project.name == "Alpha Project"

    def test_missing_directory_exits_nonzero_with_a_message(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["project", "add", str(tmp_path / "nope")])

        assert result.exit_code == 1
        assert "Not a directory" in result.output

    def test_duplicate_root_is_refused(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "alpha", "Alpha")
        runner.invoke(app, ["project", "add", str(root)])

        result = runner.invoke(app, ["project", "add", str(root), "--id", "other"])

        assert result.exit_code == 1
        assert "already registered" in result.output


class TestProjectList:
    def test_empty_registry_explains_how_to_populate_it(self) -> None:
        result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "agentjobs project add" in result.output

    def test_lists_registered_projects(self, tmp_path: Path) -> None:
        runner.invoke(app, ["project", "add", str(make_project(tmp_path / "alpha", "Alpha"))])
        runner.invoke(app, ["project", "add", str(make_project(tmp_path / "beta", "Beta"))])

        result = runner.invoke(app, ["project", "list"])

        assert "alpha" in result.output
        assert "beta" in result.output

    def test_flags_a_project_whose_directory_is_gone(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "gone", "Gone")
        runner.invoke(app, ["project", "add", str(root)])
        for path in sorted(root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        root.rmdir()

        result = runner.invoke(app, ["project", "list"])

        assert "[missing]" in result.output


class TestProjectRemove:
    def test_unregisters_without_touching_files(self, tmp_path: Path) -> None:
        root = make_project(tmp_path / "alpha", "Alpha")
        runner.invoke(app, ["project", "add", str(root)])

        result = runner.invoke(app, ["project", "remove", "alpha"])

        assert result.exit_code == 0
        assert registry().list_projects() == []
        assert (root / ".agentjobs" / "config.yaml").exists()

    def test_unknown_id_exits_nonzero(self) -> None:
        result = runner.invoke(app, ["project", "remove", "ghost"])

        assert result.exit_code == 1
        assert "Unknown project" in result.output


class TestInitRegisters:
    def test_init_registers_the_project_it_creates(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "init",
                "--project-name",
                "Fresh Project",
                "--tasks-dir",
                "tasks",
                "--prompts-dir",
                "prompts",
                "--port",
                "8765",
                "--user",
                "jeff",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Registered as 'fresh-project'" in result.output
        assert [p.id for p in registry().list_projects()] == ["fresh-project"]

    def test_init_still_succeeds_when_registration_fails(self, tmp_path: Path, monkeypatch) -> None:
        # Registration is a convenience; a project must be initialized either way.
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            [
                "init",
                "--project-name",
                "First",
                "--tasks-dir",
                "tasks",
                "--prompts-dir",
                "prompts",
                "--port",
                "8765",
                "--user",
                "jeff",
            ],
        )

        # Re-running init in the same directory: the root is already registered.
        result = runner.invoke(
            app,
            [
                "init",
                "--project-name",
                "Second",
                "--tasks-dir",
                "tasks",
                "--prompts-dir",
                "prompts",
                "--port",
                "8765",
                "--user",
                "jeff",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "initialized successfully" in result.output
        # The warning proves the failure branch ran, rather than the test passing
        # because registration quietly succeeded.
        assert "Not registered for multi-project use" in result.output
        assert (tmp_path / ".agentjobs" / "config.yaml").exists()
