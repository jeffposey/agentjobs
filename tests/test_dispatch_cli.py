"""CLI tests for ``agentjobs dispatch enable|disable|config``.

These are the only writes to ``~/.agentjobs/dispatch.yaml`` that a browser or a
command line may perform. They flip enablement among runners the machine already
defines; nothing here can introduce a new command to execute, which is what keeps
the reachable execution surface exactly as wide as that hand-written file says.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.dispatch.address import DEFAULT_API_BASE, ApiBaseProbe
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.config import (
    CONFIG_FILENAME,
    SENTINEL_FILENAME,
    ProjectNotEnabledError,
    assert_dispatch_permitted,
    load_dispatch_config,
)
from agentjobs.dispatch.scaffold import EXAMPLE_CONFIG
from agentjobs.projects import ProjectRegistry

from test_dispatch_auth import (
    SESSION,
    auth_failure_line,
    real_reply_line,
    write_transcript,
)
from test_dispatch_config import home, write_config, write_grouped_config

runner = CliRunner()


def _later():
    """Two minutes after the fixture failure -- long enough to be a recovery."""
    from test_dispatch_auth import WHEN

    return WHEN + timedelta(minutes=2)


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
        assert "posture=auto" in result.output
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
        assert "posture=auto" in result.output

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


class TestAuthCheckCommand:
    """The supervisor-facing half of task-224.

    A supervisor's children are its own subprocesses and appear in no ledger, so the
    dispatch poller cannot see them. This command is how such a supervisor tells "the
    child died" -- where one restart is right -- from "everything on this machine is
    logged out" -- where a restart is guaranteed to die the same way.
    """

    def _claude_home(self, tmp_path: Path, monkeypatch, lines: list) -> None:
        claude = tmp_path / "claude"
        write_transcript(claude, lines)
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(claude))

    def test_a_stalled_session_exits_one_and_names_the_command_that_fixes_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._claude_home(tmp_path, monkeypatch, [auth_failure_line()])

        result = runner.invoke(app, ["dispatch", "auth-check", SESSION])

        assert result.exit_code == 1, result.output
        assert "expired login" in result.output
        assert "claude auth login" in result.output

    def test_a_healthy_session_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        self._claude_home(
            tmp_path,
            monkeypatch,
            [
                auth_failure_line(),
                real_reply_line(at=_later()),
            ],
        )

        result = runner.invoke(app, ["dispatch", "auth-check", SESSION])

        assert result.exit_code == 0, result.output
        assert "no expired login" in result.output

    def test_a_session_nobody_has_a_transcript_for_is_not_an_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Every non-Claude runner lands here, and a missing transcript proves nothing."""
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(tmp_path / "empty"))

        result = runner.invoke(app, ["dispatch", "auth-check", "deadbeef"])

        assert result.exit_code == 0, result.output

    def test_with_no_argument_it_says_so_when_no_session_run_is_live(self) -> None:
        result = runner.invoke(app, ["dispatch", "auth-check"])

        assert result.exit_code == 0, result.output
        assert "No live session runs" in result.output


class TestDispatchReapCommand:
    """`dispatch reap` clears finished sessions, and never forces a refusal.

    The reaping itself is covered in test_dispatch_lifecycle.py against a fake session
    manager. What these add is the command's own job: telling a human which sessions
    were removed, which were not, and why that is worth reading.

    These said *worktrees* until task-186. Dispatch no longer passes `-w`, so a
    dispatched session owns no worktree and the command cannot honestly claim to remove
    one; what it removes is the session's row and the pid that row holds.
    """

    def test_it_says_so_when_there_is_nothing_to_reap(self) -> None:
        result = runner.invoke(app, ["dispatch", "reap"])

        assert result.exit_code == 0
        assert "Nothing to reap." in result.stdout

    def test_a_removed_session_is_reported(self, monkeypatch) -> None:
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

    def test_a_session_that_was_not_removed_is_flagged_and_counted(self, monkeypatch) -> None:
        """A refused reap is never forced, and never counted as success."""
        from agentjobs.dispatch.ledger import DispatchLedger, StopResult

        monkeypatch.setattr(
            DispatchLedger,
            "reap_finished",
            lambda self: [
                StopResult("run_kept0001", False, "not removed: session s1 is still attached"),
                StopResult("run_gone0002", True, "removed session s2"),
            ],
        )

        result = runner.invoke(app, ["dispatch", "reap"])

        assert result.exit_code == 0
        assert "still attached" in result.stdout
        assert "1 session(s) not removed" in result.stdout
        # It must not promise to have removed worktrees it never had. task-186.
        assert "worktree" not in result.stdout


# ----- runner groups on the command line (task-177) ---------------------------


class TestGroupsOnTheCommandLine:
    """What a human setting dispatch up actually types, and what they are told."""

    def make_project(self, tmp_path: Path, project_id: str) -> Path:
        return TestDispatchCli().make_project(tmp_path, project_id)

    def test_config_lists_groups_their_members_and_the_disabled_ones(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {
                    "members": [
                        "small",
                        {"runner": "spare", "enabled": False, "note": "kept in reserve"},
                    ]
                }
            },
        )

        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert "Runner groups:" in result.output
        assert "[on ] small" in result.output
        assert "[off] spare" in result.output

    def test_config_explains_which_candidates_were_skipped_and_why(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {
                    "members": [
                        "big",
                        {"runner": "spare", "enabled": False, "note": "kept in reserve"},
                        "small",
                    ]
                }
            },
        )

        result = runner.invoke(app, ["dispatch", "config", "--project", "agentjobs"])

        assert result.exit_code == 0, result.output
        assert "from group 'default'" in result.output
        assert "skipped big: executable_not_found" in result.output
        assert "skipped spare: disabled" in result.output

    def test_a_flat_config_never_mentions_groups(self) -> None:
        """Someone who does not use groups should not learn from the CLI that they exist."""
        write_config()

        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert "Runner groups:" not in result.output

    def test_enable_points_a_project_at_a_group(self, tmp_path: Path) -> None:
        write_grouped_config(projects={})
        self.make_project(tmp_path, "grouped")

        result = runner.invoke(app, ["dispatch", "enable", "grouped", "--group", "default"])

        assert result.exit_code == 0, result.output
        assert "using group 'default'" in result.output
        config = load_dispatch_config(home())
        assert config is not None
        assert config.project("grouped").group == "default"

    def test_enable_refuses_a_group_the_machine_does_not_define(self, tmp_path: Path) -> None:
        write_grouped_config(projects={})
        self.make_project(tmp_path, "grouped")

        result = runner.invoke(app, ["dispatch", "enable", "grouped", "--group", "invented"])

        assert result.exit_code == 1
        assert "written by hand" in result.output


class TestTheExampleConfig:
    """sc-5: a starting point exists, and nothing writes config nobody asked for."""

    def test_it_prints_without_writing_anything(self) -> None:
        result = runner.invoke(app, ["dispatch", "example"])

        assert result.exit_code == 0, result.output
        assert "runner_groups:" in result.output
        assert not (home() / CONFIG_FILENAME).exists()

    def test_what_it_prints_is_a_config_this_build_can_load(self) -> None:
        """A worked example that does not parse is worse than none at all."""
        path = home() / CONFIG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")

        config = load_dispatch_config(home())

        assert config is not None
        assert set(config.runner_groups) == {"standard", "deep", "quick", "review"}
        assert config.default_group == "standard"

    def test_the_example_is_switched_off_at_every_level(self) -> None:
        """--write must not be able to leave a machine able to dispatch."""
        path = home() / CONFIG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")

        config = load_dispatch_config(home())

        assert config is not None
        assert config.enabled is False
        assert config.projects == {}

    def test_it_writes_only_when_asked(self) -> None:
        result = runner.invoke(app, ["dispatch", "example", "--write"])

        assert result.exit_code == 0, result.output
        assert (home() / CONFIG_FILENAME).read_text(encoding="utf-8") == EXAMPLE_CONFIG

    def test_it_refuses_to_overwrite_an_existing_config(self) -> None:
        write_config()
        before = (home() / CONFIG_FILENAME).read_text(encoding="utf-8")

        result = runner.invoke(app, ["dispatch", "example", "--write"])

        assert result.exit_code == 1
        assert "already exists" in result.output
        assert (home() / CONFIG_FILENAME).read_text(encoding="utf-8") == before


class TestTheAgentAddressIsVisibleBeforeADispatch:
    """``dispatch config`` reported the address nowhere at all until task-193.

    So the only place it was ever shown was ``dispatch run``, one line after the run
    had started -- which is to say, after the money was spent and after the only
    remaining symptom of a wrong one was a task record that stopped changing.
    """

    def stub_probe(self, monkeypatch, *, answered: bool, is_agentjobs: bool, detail: str):
        monkeypatch.setattr(
            "agentjobs.cli.probe_api_base",
            lambda api_base, **_: ApiBaseProbe(
                api_base=api_base,
                answered=answered,
                is_agentjobs=is_agentjobs,
                detail=detail,
            ),
        )

    def test_it_names_the_address_and_the_source_that_produced_it(self, monkeypatch) -> None:
        write_config(api_base="http://127.0.0.1:8876")
        self.stub_probe(monkeypatch, answered=True, is_agentjobs=True, detail="AgentJobs answered")

        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert "Agent address:  http://127.0.0.1:8876" in result.output
        assert str(home() / CONFIG_FILENAME) in result.output
        assert "AgentJobs answered" in result.output

    def test_an_undeclared_address_is_reported_as_a_fallback(self, monkeypatch) -> None:
        """The state task-193 was filed for. A machine that has said nothing is not a
        machine that has said 8765."""
        self.stub_probe(monkeypatch, answered=False, is_agentjobs=False, detail="nothing answered")

        result = runner.invoke(app, ["dispatch", "config"])

        assert result.exit_code == 0, result.output
        assert DEFAULT_API_BASE in result.output
        assert "nothing on this machine declared an address" in result.output

    def test_a_dead_address_says_a_dispatch_would_be_refused(self, monkeypatch) -> None:
        write_config(api_base="http://127.0.0.1:8876")
        self.stub_probe(monkeypatch, answered=False, is_agentjobs=False, detail="nothing answered")

        result = runner.invoke(app, ["dispatch", "config"])

        assert "refused" in result.output
        assert "go quiet" in result.output

    def test_a_stranger_on_the_port_is_a_warning_not_a_refusal(self, monkeypatch) -> None:
        """The probe cannot tell a different service from an AgentJobs too old to serve
        ``/api/version``, and only one of those is broken."""
        write_config(api_base="http://127.0.0.1:8876")
        self.stub_probe(
            monkeypatch,
            answered=True,
            is_agentjobs=False,
            detail="answered HTTP 200, but not as AgentJobs",
        )

        result = runner.invoke(app, ["dispatch", "config"])

        assert "not as AgentJobs" in result.output
        assert "refused" not in result.output

    def test_it_is_reported_even_when_there_is_no_dispatch_config_at_all(self, monkeypatch) -> None:
        """The command returns early on an absent config, and this is exactly the
        machine most likely to be pointed at the wrong port."""
        self.stub_probe(monkeypatch, answered=False, is_agentjobs=False, detail="nothing answered")

        result = runner.invoke(app, ["dispatch", "config"])

        assert "Agent address:" in result.output
