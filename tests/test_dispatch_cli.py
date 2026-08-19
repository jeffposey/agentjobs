"""CLI tests for ``agentjobs dispatch enable|disable|config``.

These are the only writes to ``~/.agentjobs/dispatch.yaml`` that a browser or a
command line may perform. They flip enablement among runners the machine already
defines; nothing here can introduce a new command to execute, which is what keeps
the reachable execution surface exactly as wide as that hand-written file says.
"""

from __future__ import annotations

import sys
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


class TestDispatchRun:
    """`agentjobs dispatch run` reports the specific gate, not a generic failure.

    The guard chain itself is tested in test_dispatch_guards.py; what matters here is
    that the CLI surfaces *which* gate refused, because "dispatch is off" and "that was
    an agent's handoff" need different things done about them.
    """

    def test_refuses_and_names_the_gate_when_nothing_is_configured(self, tmp_path: Path) -> None:
        root = self.make_project(tmp_path, "alpha")
        task_id = self.seed(root)

        result = runner.invoke(app, ["dispatch", "run", task_id, "--project", "alpha"])

        assert result.exit_code == 1
        assert "not_configured" in result.output

    def test_refuses_an_agent_caused_dispatch_by_name(self, tmp_path: Path) -> None:
        root = self.make_project(tmp_path, "alpha")
        task_id = self.seed(root, last_actor="claude")
        write_config(
            runners={
                "fake": {
                    "argv": [sys.executable, "-c", "print(1)", "{prompt}"],
                    "actor": "claude",
                }
            },
            projects={"alpha": {"enabled": True, "runner": "fake"}},
        )

        result = runner.invoke(app, ["dispatch", "run", task_id, "--project", "alpha"])

        assert result.exit_code == 1
        assert "not_human_clocked" in result.output

    def test_an_unknown_project_is_refused_before_anything_else(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["dispatch", "run", "task-001", "--project", "nope"])

        assert result.exit_code == 1
        assert "Unknown project" in result.output

    # ----- helpers -----

    def make_project(self, tmp_path: Path, project_id: str) -> Path:
        """A registered project with the actor vocabulary the rule reads."""
        root = tmp_path / project_id
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "project_name": project_id,
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
        ProjectRegistry(home=home()).add(root, project_id=project_id)
        return root

    def seed(self, root: Path, *, last_actor: str = "Jeff Posey") -> str:
        """A ready task whose newest entry belongs to ``last_actor``."""
        from agentjobs.manager import TaskManager
        from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
        from agentjobs.storage import TaskStorage

        manager = TaskManager(TaskStorage(root / "tasks"))
        task = manager.create_task(
            title="Dispatchable",
            category="general",
            summary="s",
            description="d",
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
                ball_prompt="Review please.",
            )
        return task.id


class TestDispatchReapCommand:
    """`dispatch reap` clears finished worktrees, and never forces a refusal.

    The reaping itself is covered in test_dispatch_lifecycle.py against a fake session
    manager. What these add is the command's own job: telling a human which worktrees
    were removed, which were kept, and that a kept one is worth looking at.
    """

    def test_it_says_so_when_there_is_nothing_to_reap(self) -> None:
        result = runner.invoke(app, ["dispatch", "reap"])

        assert result.exit_code == 0
        assert "Nothing to reap." in result.stdout

    def test_a_removed_worktree_is_reported(self, monkeypatch) -> None:
        from agentjobs.dispatch.ledger import DispatchLedger, StopResult

        monkeypatch.setattr(
            DispatchLedger,
            "reap_finished",
            lambda self: [StopResult("run_a1b2c3d4", True, "removed session s1")],
        )

        result = runner.invoke(app, ["dispatch", "reap"])

        assert result.exit_code == 0
        assert "run_a1b2c3d4" in result.stdout
        assert "removed session s1" in result.stdout
        assert "kept" not in result.stdout

    def test_a_kept_worktree_is_flagged_and_counted(self, monkeypatch) -> None:
        """A refused reap means a run produced work nobody has looked at."""
        from agentjobs.dispatch.ledger import DispatchLedger, StopResult

        monkeypatch.setattr(
            DispatchLedger,
            "reap_finished",
            lambda self: [
                StopResult(
                    "run_kept0001", False, "not removed: worktree holds uncommitted changes"
                ),
                StopResult("run_gone0002", True, "removed session s2"),
            ],
        )

        result = runner.invoke(app, ["dispatch", "reap"])

        assert result.exit_code == 0
        assert "uncommitted changes" in result.stdout
        assert "1 worktree(s) kept" in result.stdout
        assert "Look at what is in them" in result.stdout
