"""What an operator sees and sets for durable execution (task-416): roles, switches, listings."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.dispatch.config import (
    DispatchConfigError,
    load_dispatch_config,
    timeout_roles,
)
from agentjobs.dispatch.ledger import DispatchLedger, LedgerError
from agentjobs.dispatch.runner import DispatchRunError


def write_config(home: Path, **extra: object) -> None:
    config = {"version": 1, "enabled": True, "runners": {}, "projects": {}, **extra}
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


class TestTimeoutRoles:
    def test_every_role_names_its_unrenamed_key_its_value_and_where_it_came_from(
        self, tmp_path: Path
    ) -> None:
        write_config(
            tmp_path,
            limits={"run_timeout_seconds": 5400, "auto": {"cooldown_seconds": 90}},
            execution={"launch_observation_seconds": 30},
        )
        config = load_dispatch_config(tmp_path)
        roles = {role.role: role for role in timeout_roles(config)}
        assert set(roles) == {
            "schedule-to-start",
            "start-to-close",
            "schedule-to-close",
            "heartbeat",
            "idle-without-handoff",
            "cooldown",
        }
        assert (roles["start-to-close"].key, roles["start-to-close"].value) == (
            "limits.run_timeout_seconds",
            5400,
        )
        assert roles["start-to-close"].source.endswith("dispatch.yaml")
        assert roles["schedule-to-start"].value == 30
        assert roles["heartbeat"].key == "limits.session_stall_seconds"
        assert roles["heartbeat"].source == "default"
        assert roles["cooldown"].value == 90 and roles["cooldown"].source != "default"

    def test_the_controller_defaults_to_shadow_and_refuses_an_unknown_mode(
        self, tmp_path: Path
    ) -> None:
        write_config(tmp_path)
        config = load_dispatch_config(tmp_path)
        assert config is not None and config.execution.controller == "shadow"
        write_config(tmp_path, execution={"controller": "yolo"})
        with pytest.raises(DispatchConfigError):
            load_dispatch_config(tmp_path)

    def test_dispatch_config_prints_the_roles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.cli import app

        write_config(tmp_path, execution={"controller": "active"})
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path))
        result = CliRunner().invoke(app, ["dispatch", "config"])
        assert result.exit_code == 0, result.output
        assert "Controller:     active" in result.output
        assert "start-to-close" in result.output and "limits.run_timeout_seconds" in result.output
        assert "[default]" in result.output


class TestAnUnreadableListingIsNotAnEmptyOne:
    """A real empty listing prints `[]` (Claude Code 2.1.270); silence is a failure."""

    def silent_cli(self, tmp_path: Path) -> Path:
        script = tmp_path / "silent.py"
        script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        return script

    def test_the_runner_raises_on_silence(self, tmp_path: Path) -> None:
        from test_dispatch_runner import make_resolution
        from agentjobs.dispatch.config import RunnerMode
        from agentjobs.dispatch.runner import DispatchRunner

        script = self.silent_cli(tmp_path)
        runner = DispatchRunner(
            manager=None,  # type: ignore[arg-type]
            resolution=make_resolution(
                [sys.executable, str(script), "--bg", "{prompt}"], mode=RunnerMode.SESSION
            ),
            project_root=tmp_path,
            home=tmp_path,
        )
        with pytest.raises(DispatchRunError):
            runner.ledger()

    def test_the_startup_sweep_raises_on_silence(self, tmp_path: Path) -> None:
        script = self.silent_cli(tmp_path)
        ledger = DispatchLedger(tmp_path, session_command=[sys.executable, str(script)])
        with pytest.raises(LedgerError):
            ledger.session_ledger()


def test_execution_tick_reports_nothing_to_do_on_a_quiet_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentjobs.cli import app

    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["execution", "tick"])
    assert result.exit_code == 0, result.output
    assert "Nothing to do." in result.output
    status = CliRunner().invoke(app, ["execution", "status"])
    assert status.exit_code == 0 and "Epic walks: 0" in status.output
