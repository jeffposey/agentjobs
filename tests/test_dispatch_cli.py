"""CLI tests for ``agentjobs dispatch enable|disable|config``.

These are the only writes to ``~/.agentjobs/dispatch.yaml`` that a browser or a
command line may perform. They flip enablement among runners the machine already
defines; nothing here can introduce a new command to execute, which is what keeps
the reachable execution surface exactly as wide as that hand-written file says.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.dispatch.config import (
    SENTINEL_FILENAME,
    ProjectNotEnabledError,
    assert_dispatch_permitted,
    load_dispatch_config,
)
from agentjobs.projects import ProjectRegistry

from test_dispatch_config import home, write_config

runner = CliRunner()


class TestDispatchCli:
    def make_project(self, tmp_path: Path, project_id: str) -> Path:
        """Register a project so the CLI can resolve the id."""
        root = tmp_path / project_id
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": project_id, "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        ProjectRegistry(home=home()).add(root, project_id=project_id)
        return root

    def test_config_reports_every_gate_when_nothing_is_configured(self) -> None:
        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert "absent" in result.output
        assert "Master switch:  off" in result.output

    def test_config_shows_runners_projects_and_limits(self) -> None:
        write_config()

        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert "claude" in result.output
        assert "batch" in result.output
        assert "session" in result.output
        assert "posture=supervised" in result.output
        assert "max_concurrent_runs=1" in result.output

    def test_config_for_one_project_reports_the_refusing_gate(self) -> None:
        write_config(enabled=False)

        result = runner.invoke(app, ["dispatch", "config", "--project", "agentjobs"])

        assert result.exit_code == 0, result.output
        assert "refused (disabled)" in result.output

    def test_config_for_one_project_reports_permission(self) -> None:
        write_config()

        result = runner.invoke(app, ["dispatch", "config", "--project", "agentjobs"])

        assert "permitted" in result.output
        assert "supervised" in result.output

    def test_enable_refuses_an_unregistered_project(self, tmp_path: Path) -> None:
        write_config()

        result = runner.invoke(app, ["dispatch", "enable", "nosuchproject"])

        assert result.exit_code == 1
        assert "Unknown project" in result.output

    def test_enable_refuses_a_runner_that_does_not_exist(self, tmp_path: Path) -> None:
        self.make_project(tmp_path, "alpha")
        write_config(projects={})

        result = runner.invoke(app, ["dispatch", "enable", "alpha", "--runner", "ghost"])

        assert result.exit_code == 1
        assert "ghost" in result.output
        config = load_dispatch_config()
        assert config is not None
        assert config.project("alpha").enabled is False

    def test_enable_then_disable_round_trips(self, tmp_path: Path) -> None:
        self.make_project(tmp_path, "alpha")
        write_config(projects={})

        enabled = runner.invoke(app, ["dispatch", "enable", "alpha", "--runner", "codex"])
        assert enabled.exit_code == 0, enabled.output
        assert assert_dispatch_permitted("alpha").runner.name == "codex"

        disabled = runner.invoke(app, ["dispatch", "disable", "alpha"])
        assert disabled.exit_code == 0, disabled.output
        with pytest.raises(ProjectNotEnabledError):
            assert_dispatch_permitted("alpha")

    def test_enable_warns_when_the_master_switch_is_still_off(self, tmp_path: Path) -> None:
        self.make_project(tmp_path, "alpha")
        write_config(enabled=False, projects={})

        result = runner.invoke(app, ["dispatch", "enable", "alpha", "--runner", "claude"])

        assert result.exit_code == 0, result.output
        assert "master switch is still off" in result.output

    def test_enable_warns_when_the_sentinel_is_present(self, tmp_path: Path) -> None:
        self.make_project(tmp_path, "alpha")
        write_config(projects={})
        (home() / SENTINEL_FILENAME).write_text("", encoding="utf-8")

        result = runner.invoke(app, ["dispatch", "enable", "alpha", "--runner", "claude"])

        assert result.exit_code == 0, result.output
        assert SENTINEL_FILENAME in result.output
