"""Tests for the dispatch runner, in both modes.

The batch tests drive **real processes**, not mocks: a fake runner that exits non-zero,
one that hangs past the timeout, one that emits megabytes, and one that spawns a
grandchild and outlives it. A mocked ``Popen`` would pass while the thing that matters --
that a terminal entry exists no matter how the process ends, and that nothing is left
running -- silently did not work. Windows is the reference platform here.

The session tests drive a fake CLI that answers ``agents --json``, ``logs`` and ``stop``
the way Claude Code 2.1.228 was observed to (task-077 log entry 5). That is a deliberate
seam: session mode is defined operationally as "a runner whose executable answers
``agents --json``", so a fake that answers it exercises the real code path. The one thing
it cannot prove is that Claude Code still behaves that way, which is what the
undocumented-surface test at the bottom is for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchLimits,
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    ProjectDispatchSettings,
    RunnerCandidate,
    RunnerDriver,
    RunnerMode,
    RunnerSelection,
    SelectionSource,
    SkipReason,
)
from agentjobs.dispatch.runner import (
    CHILDREN_NAMED,
    GUIDE_PATH,
    PROMPT_STUB,
    REMOTE_CONTROL_URL,
    TRANSCRIPT_FILENAME,
    DispatchRunner,
    DispatchRunError,
    RunHandle,
    RunDirectory,
    SessionPhase,
    allow_rules,
    classify_session,
    compose_argv,
    describe_children,
    drop_repainted_lines,
    mcpjson_server_names,
    policy_clause,
    posture_flags,
    readable_tail,
    codex_desktop_executable,
    resolve_executable,
    settings_json,
    strip_ansi,
    supervisor_allow_rules,
    uncommitted_paths,
    working_tree_clean,
)
from agentjobs.dispatch.codex_app_server import (
    CodexAppServerError,
    CodexResumeFailure,
    CodexSessionStarted,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchOutcome,
    Lifecycle,
    LogEntryType,
    Outcome,
)
from agentjobs.storage import TaskStorage

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----- fixtures ---------------------------------------------------------------


def write_script(path: Path, source: str) -> Path:
    """Write a Python script and return it."""
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def make_resolution(
    argv: List[str],
    *,
    mode: RunnerMode = RunnerMode.BATCH,
    posture: Posture = Posture.SUPERVISED,
    driver: RunnerDriver = RunnerDriver.CLAUDE,
    timeout: int = 1800,
    stale: int = 3600,
    require_clean_tree: bool = False,
    push: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> DispatchResolution:
    """A resolution as task-068's config layer would produce it."""
    runner = RunnerConfig(name="fake", argv=argv, env=dict(env or {}), mode=mode, driver=driver)
    settings = ProjectDispatchSettings(
        project_id="sandbox",
        enabled=True,
        runner="fake",
        require_clean_tree=require_clean_tree,
        posture=posture,
        push=push,
    )
    limits = DispatchLimits(run_timeout_seconds=timeout, session_stale_seconds=stale)
    return DispatchResolution(
        project_id="sandbox",
        runner=runner,
        settings=settings,
        limits=limits,
        config=DispatchConfig(enabled=True, limits=limits),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A project root and an AgentJobs home, both throwaway."""
    (tmp_path / "project").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "tasks").mkdir()
    return tmp_path


@pytest.fixture
def manager(workspace: Path) -> TaskManager:
    return TaskManager(TaskStorage(workspace / "tasks"))


@pytest.fixture
def task(manager: TaskManager):
    """A task in the state a dispatcher actually finds one in: ready, then claimed."""
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return manager.claim_task(created.id, agent="claude")


def build(workspace: Path, manager: TaskManager, resolution: DispatchResolution) -> DispatchRunner:
    return DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=workspace / "project",
        home=workspace / "home",
        api_base="http://localhost:8899",
        grace_seconds=2.0,
    )


def terminal_entries(manager: TaskManager, task_id: str) -> list:
    task = manager.get_task(task_id)
    assert task is not None
    return [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]


def join(runner_handle, timeout: float = 60.0) -> None:
    """Wait for a batch supervisor to finish."""
    assert runner_handle.supervisor is not None
    runner_handle.supervisor.join(timeout=timeout)
    assert not runner_handle.supervisor.is_alive(), "supervisor thread never finished"


def test_codex_session_wake_target_uses_newest_completed_thread(
    workspace: Path, manager: TaskManager, task
) -> None:
    resolution = make_resolution(
        ["codex", "app-server", "--model", "gpt-5.6-luna", "{prompt}"],
        mode=RunnerMode.SESSION,
        driver=RunnerDriver.CODEX,
        posture=Posture.AUTO,
    )
    runner = build(workspace, manager, resolution)
    RunDirectory.create(
        workspace / "home",
        "run_previous",
        {
            "run_id": "run_previous",
            "task_id": task.id,
            "mode": "session",
            "driver": "codex",
            "status": "finished",
            "codex_status": "completed",
            "session_id": "thread-previous",
            "started_at": "2026-08-22T20:00:00+00:00",
        },
    )

    target = runner._codex_wake_target(task.id)

    assert target is not None
    assert target.previous_run_id == "run_previous"
    assert target.session_uuid == "thread-previous"


# ----- the permission posture -------------------------------------------------


class TestPosture:
    def test_every_allow_rule_uses_the_colon_form(self) -> None:
        """A rule without the colon matches nothing and looks exactly like it working."""
        rules = allow_rules()

        assert rules, "the seed allow-list must not be empty"
        for rule in rules:
            assert rule.endswith(":*)"), rule
            tool, _, rest = rule.partition("(")
            assert tool in {"Bash", "PowerShell"}, rule
            assert ":" in rest, rule

    def test_the_seed_list_covers_the_boring_commands(self) -> None:
        rules = " ".join(allow_rules())

        for prefix in ("poetry run pytest", "git commit", "npm run"):
            assert f"({prefix}:*)" in rules

    def test_the_merge_command_is_allowed_in_both_shells(self) -> None:
        """task-222. Every dispatched run that finishes its work ends in a merge.

        The rules are written out as literal strings rather than rebuilt with
        ``f"{tool}({prefix}:*)"``. A test that composes its expectation from the same
        expression the source uses passes against any form at all -- including the
        colon-less one the test above exists to catch -- so it would assert nothing
        about the thing that actually goes wrong here.
        """
        rules = allow_rules()

        assert "Bash(git merge:*)" in rules
        assert "PowerShell(git merge:*)" in rules

    def test_pushing_is_not_pre_approved(self) -> None:
        """The anti-rot half of the pair above, also task-222.

        ``git merge`` was added on Jeff's explicit authorisation recorded on that task,
        because the merge is the sanctioned end of the documented lifecycle and is
        gated on a human approval rather than on the classifier. Pushing is a separate
        act from merging in this repository and nothing authorises it. Asserting its
        absence next to the entry that was added means a later widening has to delete
        a test stating why, instead of slipping in beside a list that merely happens
        not to mention it.
        """
        rules = allow_rules()

        assert "Bash(git push:*)" not in rules
        assert "PowerShell(git push:*)" not in rules
        assert not any("git push" in rule for rule in rules)

    def test_read_only_gets_no_tools_and_no_worktree(self) -> None:
        flags = posture_flags(Posture.READ_ONLY, [])

        assert flags == ["--tools", "Read,Glob,Grep,WebFetch"]
        assert "-w" not in flags

    def test_supervised_gets_accept_edits_and_the_allow_list(self) -> None:
        flags = posture_flags(Posture.SUPERVISED, [])

        assert flags[:2] == ["--permission-mode", "acceptEdits"]
        settings = json.loads(flags[flags.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules()

    def test_autonomous_gets_bypass_and_never_the_allow_list(self) -> None:
        """An allow-list under bypassPermissions would imply a limit that is not there."""
        flags = posture_flags(Posture.AUTONOMOUS, [])

        assert flags[:2] == ["--permission-mode", "bypassPermissions"]
        assert "--settings" not in flags

    def test_auto_gets_the_auto_mode_and_the_allow_list(self) -> None:
        """The default posture, per task-020.

        The mode string matters more than it looks: ``auto`` is the one mode that gates
        every action without needing a terminal to answer with. Getting ``acceptEdits``
        here instead would park the run on its first unlisted command, which is the
        defect this posture exists to fix, and nothing in a passing suite would say so.
        """
        flags = posture_flags(Posture.AUTO, [])

        assert flags[:2] == ["--permission-mode", "auto"]
        settings = json.loads(flags[flags.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules()

    @pytest.mark.parametrize("posture", list(Posture))
    def test_no_posture_asks_the_cli_for_a_worktree(self, posture: Posture) -> None:
        """task-186. This assertion is the whole fix, so it is stated per posture.

        Every writing posture passed ``-w <task_id>`` until 2026-08-19. A session
        isolated that way refuses every git operation aimed at the shared checkout --
        by ``-C`` and by ``cd`` alike -- and the shared checkout is where task records
        are committed and where the merge gate runs. So a dispatched run could do the
        work and then neither record nor merge it, which is a defect no unit test on the
        flags would have caught, because the flags were exactly what was asked for.

        ``read_only`` is in the parametrisation deliberately: it never had a worktree,
        and the property that it still has none must not depend on the writing postures
        happening to be tested nearby.
        """
        assert "-w" not in posture_flags(posture, [])
        assert "--worktree" not in posture_flags(posture, [])

    def test_the_composed_argv_of_every_posture_is_exactly_this(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """The whole command, per posture, not just the flags in isolation.

        A flags-only assertion cannot see where the flags land, whether the prompt
        survived, or whether something else in the pipeline reintroduced ``-w``. This
        one names the full argv, so any of those shows up as a diff rather than as a
        run that behaves oddly in production.
        """
        expected = {
            Posture.READ_ONLY: ["--tools", "Read,Glob,Grep,WebFetch"],
            Posture.AUTO: [
                "--permission-mode",
                "auto",
                "--settings",
                settings_json(allow_list=True, mcp_servers=[]),
            ],
            Posture.SUPERVISED: [
                "--permission-mode",
                "acceptEdits",
                "--settings",
                settings_json(allow_list=True, mcp_servers=[]),
            ],
            Posture.AUTONOMOUS: ["--permission-mode", "bypassPermissions"],
        }
        assert set(expected) == set(Posture), "a new posture needs its argv named here"

        for posture, flags in expected.items():
            runner = build(
                workspace,
                manager,
                make_resolution(
                    ["claude", "--bg", "--remote-control", "{prompt}"], posture=posture
                ),
            )

            argv = runner.build_argv("task-070-example", "run_abcd1234")

            assert argv[:3] == [resolve_executable("claude"), "--bg", "--remote-control"]
            assert argv[3:-1] == flags, posture
            assert argv[-1] == runner.build_prompt("task-070-example", "run_abcd1234")

    def test_the_config_and_schema_posture_enums_stay_in_step(self) -> None:
        """Two enums spell the same concept, and a run needs both.

        ``runner._record_dispatch`` converts the config posture into the schema one by
        value, so a posture present in only one of them raises at dispatch time rather
        than at import. Adding ``auto`` to the config enum alone did exactly that.
        """
        from agentjobs.models_v2 import DispatchPosture

        assert {posture.value for posture in Posture} == {
            posture.value for posture in DispatchPosture
        }

    def test_each_posture_composes_a_distinct_permission_mode(self) -> None:
        """A posture that silently collapsed onto another's mode would look fine."""
        modes = {
            posture: posture_flags(posture, [])[1]
            for posture in (Posture.AUTO, Posture.SUPERVISED, Posture.AUTONOMOUS)
        }

        assert modes == {
            Posture.AUTO: "auto",
            Posture.SUPERVISED: "acceptEdits",
            Posture.AUTONOMOUS: "bypassPermissions",
        }


class TestMcpApproval:
    """task-019: the project's own MCP servers, pre-approved through ``--settings``.

    A ``--bg`` session has no terminal, so Claude Code's *"New MCP server found in this
    project"* prompt is unanswerable and the run parks until something kills it. Probed
    on 2.1.235 against a project declaring one otherwise-unknown server: ``auto`` and
    ``read_only`` both reached ``state: "blocked"``, ``bypassPermissions`` never saw the
    gate, and the same run with ``enabledMcpjsonServers`` in ``--settings`` reached
    ``state: "done"`` with no prompt in its transcript.
    """

    def write_mcp_json(self, project_root: Path, *names: str) -> None:
        payload = {"mcpServers": {name: {"command": "noop"} for name in names}}
        (project_root / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_names_are_read_from_the_project_in_file_order(self, tmp_path: Path) -> None:
        self.write_mcp_json(tmp_path, "agentjobs", "playwright")

        assert mcpjson_server_names(tmp_path) == ["agentjobs", "playwright"]

    def test_a_project_without_the_file_declares_no_servers(self, tmp_path: Path) -> None:
        assert mcpjson_server_names(tmp_path) == []

    @pytest.mark.parametrize(
        "content",
        ["", "not json at all", "[]", '"a string"', "{}", '{"mcpServers": []}'],
        ids=["empty", "garbage", "array", "string", "no-key", "wrong-type"],
    )
    def test_a_file_no_name_can_be_read_from_yields_no_names(
        self, tmp_path: Path, content: str
    ) -> None:
        """Dispatch does not own this file and must not fail a run over it.

        Every one of these is a file Claude Code cannot raise a server prompt from
        either, so returning nothing keeps argv unchanged rather than inventing an
        approval or refusing to spawn.
        """
        (tmp_path / ".mcp.json").write_text(content, encoding="utf-8")

        assert mcpjson_server_names(tmp_path) == []

    def test_a_directory_named_mcp_json_is_not_a_config(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").mkdir()

        assert mcpjson_server_names(tmp_path) == []

    def test_without_servers_the_settings_blob_is_what_it_always_was(self) -> None:
        """ac-2 at the JSON level: byte-identical, not merely equivalent."""
        assert settings_json(allow_list=True, mcp_servers=[]) == json.dumps(
            {"permissions": {"allow": allow_rules()}}
        )

    def test_the_composed_settings_json_is_exactly_this(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """ac-1 and ac-3: the whole decoded blob, not the presence of a key.

        A key can be present and carry the wrong names, the wrong shape, or a stale
        allow-list, and every one of those looks like the feature working until a run
        parks. ``allow_rules()`` has already cost an hour that way.
        """
        self.write_mcp_json(workspace / "project", "agentjobs", "playwright")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTO),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings == {
            "permissions": {"allow": allow_rules()},
            "enabledMcpjsonServers": ["agentjobs", "playwright"],
        }

    def test_the_names_are_the_projects_own_not_agentjobs(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """Dispatch runs against whatever project it was configured for."""
        self.write_mcp_json(workspace / "project", "some-other-projects-server")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTO),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["enabledMcpjsonServers"] == ["some-other-projects-server"]

    def test_read_only_is_approved_without_being_given_an_allow_list(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """``read_only`` blocks on the same dialog and had no ``--settings`` to extend.

        It gets one now, holding the approval and nothing else. An allow-list here would
        be a posture change, which task-019 puts out of scope.
        """
        self.write_mcp_json(workspace / "project", "agentjobs")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.READ_ONLY),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        assert argv[1:-1] == [
            "--bg",
            "--tools",
            "Read,Glob,Grep,WebFetch",
            "--settings",
            json.dumps({"enabledMcpjsonServers": ["agentjobs"]}),
        ]
        assert "permissions" not in json.loads(argv[argv.index("--settings") + 1])

    def test_autonomous_is_left_alone_because_bypass_never_sees_the_gate(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """Probed, not assumed: ``bypassPermissions`` reached ``done`` with no prompt.

        Handing it a ``--settings`` blob it does not need would imply a limit that is
        not there, which is the same reason it carries no allow-list.
        """
        self.write_mcp_json(workspace / "project", "agentjobs")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTONOMOUS),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        assert argv[1:-1] == ["--bg", "--permission-mode", "bypassPermissions"]

    @pytest.mark.parametrize("posture", list(Posture))
    def test_a_project_with_no_mcp_json_gets_todays_argv_unchanged(
        self, workspace: Path, manager: TaskManager, posture: Posture
    ) -> None:
        """ac-2, stated per posture and against literal argv rather than a snapshot.

        The flags are spelled out here on purpose: comparing against
        ``posture_flags(posture, [])`` would pass even if both sides regressed together.
        """
        today = {
            Posture.READ_ONLY: ["--tools", "Read,Glob,Grep,WebFetch"],
            Posture.AUTO: [
                "--permission-mode",
                "auto",
                "--settings",
                json.dumps({"permissions": {"allow": allow_rules()}}),
            ],
            Posture.SUPERVISED: [
                "--permission-mode",
                "acceptEdits",
                "--settings",
                json.dumps({"permissions": {"allow": allow_rules()}}),
            ],
            Posture.AUTONOMOUS: ["--permission-mode", "bypassPermissions"],
        }
        assert not (workspace / "project" / ".mcp.json").exists()
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=posture),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        assert argv[2:-1] == today[posture], posture


class TestArgvComposition:
    def test_posture_flags_land_before_the_prompt(self) -> None:
        argv = compose_argv(
            ["claude", "--bg", "{prompt}"],
            {"prompt": "read the record"},
            ["--permission-mode", "acceptEdits"],
        )

        assert argv == ["claude", "--bg", "--permission-mode", "acceptEdits", "read the record"]

    def test_flags_are_appended_when_there_is_no_prompt_element(self) -> None:
        argv = compose_argv(["claude", "agents"], {"prompt": "unused"}, ["--json"])

        assert argv == ["claude", "agents", "--json"]

    def test_a_hostile_prompt_is_still_exactly_one_element(self) -> None:
        prompt = 'go; rm -rf / && echo "x" `id`\nsecond line'

        argv = compose_argv(["claude", "-p", "{prompt}"], {"prompt": prompt}, [])

        assert argv == ["claude", "-p", prompt]


class TestCodexBatchRunner:
    @pytest.mark.parametrize(
        ("posture", "sandbox"),
        [
            (Posture.READ_ONLY, "read-only"),
            (Posture.AUTO, "workspace-write"),
            (Posture.AUTONOMOUS, "danger-full-access"),
        ],
    )
    def test_posture_becomes_a_codex_sandbox_and_requires_agentjobs_mcp(
        self, posture: Posture, sandbox: str
    ) -> None:
        assert posture_flags(posture, [], driver=RunnerDriver.CODEX) == [
            "--sandbox",
            sandbox,
            "-c",
            "mcp_servers.agentjobs.required=true",
        ]

    def test_supervised_is_refused_before_spawn(self) -> None:
        with pytest.raises(DispatchRunError, match="does not support posture 'supervised'"):
            posture_flags(Posture.SUPERVISED, [], driver=RunnerDriver.CODEX)

    def test_a_transient_pid_probe_miss_does_not_reap_the_live_app_server(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        """Windows can miss a just-started child once; polling must keep the run alive."""
        resolution = make_resolution(
            ["codex", "app-server", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        runner = DispatchRunner(
            manager=manager,
            resolution=resolution,
            project_root=workspace / "project",
            home=workspace / "home",
            clock=lambda: now,
        )
        directory = RunDirectory.create(
            workspace / "home",
            "run_codex_probe",
            {
                "run_id": "run_codex_probe",
                "task_id": task.id,
                "project_id": "sandbox",
                "mode": "session",
                "status": "running",
                "codex_status": "running",
                "pid": 4242,
                "session_id": "thread-1",
                "started_at": now.isoformat(),
            },
        )
        handle = RunHandle(
            run_id="run_codex_probe",
            task_id=task.id,
            mode=DispatchMode.SESSION,
            directory=directory,
            pid=4242,
            session_id="thread-1",
        )
        probes = iter([OSError("temporarily unavailable"), None])
        monkeypatch.setattr("agentjobs.dispatch.runner.os.kill", lambda _pid, _sig: next(probes))

        assert runner.poll_session(handle) is SessionPhase.RUNNING
        assert yaml.safe_load((directory.path / "meta.yaml").read_text())["status"] == "running"
        assert runner.poll_session(handle) is SessionPhase.RUNNING
        assert (
            yaml.safe_load((directory.path / "meta.yaml").read_text()).get("pid_missing_since")
            is None
        )

    def test_lost_codex_pid_reconciles_the_persisted_thread_without_a_second_turn(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-terra", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        runner = DispatchRunner(
            manager=manager,
            resolution=resolution,
            project_root=workspace / "project",
            home=workspace / "home",
            clock=lambda: now,
        )
        directory = RunDirectory.create(
            workspace / "home",
            "run_codex_reconcile",
            {
                "run_id": "run_codex_reconcile",
                "task_id": task.id,
                "project_id": "sandbox",
                "mode": "session",
                "driver": "codex",
                "posture": "auto",
                "status": "running",
                "codex_status": "running",
                "codex_lifecycle": "running_turn",
                "pid": 4242,
                "thread_id": "thread-1",
                "session_id": "thread-1",
                "argv": ["codex", "app-server", "{prompt}"],
                "pid_missing_since": (now - timedelta(minutes=10)).isoformat(),
                "started_at": now.isoformat(),
            },
        )
        handle = RunHandle(
            run_id="run_codex_reconcile",
            task_id=task.id,
            mode=DispatchMode.SESSION,
            directory=directory,
            pid=4242,
            session_id="thread-1",
        )
        reads: list[str] = []

        class FakeAppServer:
            def __init__(self, **_kwargs) -> None:
                pass

            def read_persisted_thread(self, thread_id: str):
                reads.append(thread_id)
                return {"thread": {"id": thread_id}}

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)
        monkeypatch.setattr(
            "agentjobs.dispatch.runner.os.kill", lambda _pid, _sig: (_ for _ in ()).throw(OSError())
        )

        assert runner.poll_session(handle) is SessionPhase.RUNNING
        meta = directory.read_meta()
        assert reads == ["thread-1"]
        assert meta["codex_status"] == "reconciling"
        assert meta["codex_lifecycle"] == "reconciled"
        assert meta["reconciled_thread_id"] == "thread-1"

    def test_codex_flags_land_before_the_prompt(self) -> None:
        prompt = "work task-277"
        argv = compose_argv(
            ["codex", "exec", "--json", "{prompt}"],
            {"prompt": prompt},
            posture_flags(Posture.AUTO, [], driver=RunnerDriver.CODEX),
        )

        assert argv == [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-c",
            "mcp_servers.agentjobs.required=true",
            prompt,
        ]

    def test_desktop_cli_path_is_read_from_current_codex_configuration(
        self, tmp_path: Path
    ) -> None:
        executable = tmp_path / "bin" / "codex.exe"
        executable.parent.mkdir()
        executable.write_text("placeholder", encoding="utf-8")
        config = tmp_path / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            "[mcp_servers.node_repl.env]\n" f"CODEX_CLI_PATH = '{executable.as_posix()}'\n",
            encoding="utf-8",
        )

        assert codex_desktop_executable(tmp_path) == str(executable)

    def test_app_server_preflight_runs_before_the_task_turn(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-terra", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        runner = build(workspace, manager, resolution)
        events: list[str] = []

        class FakeAppServer:
            pid = 1238

            def __init__(self, **_kwargs) -> None:
                pass

            def preflight_required_mcp(self):
                events.append("preflight")
                return type("Preflight", (), {"server_name": "agentjobs", "status": "ready"})()

            def start(self, _prompt, *, resume_thread_id=None):
                events.append("start")
                return CodexSessionStarted("thread-1", "session-1", "turn-1", self.pid)

            def supervise(self, *, turn_id, on_message=None, timeout=None):
                events.append("supervise")
                assert on_message is not None
                on_message({"method": "item/started", "params": {"turnId": turn_id}})
                return {"turn": {"id": turn_id, "status": "completed"}}

            def inspect_persisted_thread(self, thread_id):
                events.append("inspect")
                return {"thread_id": thread_id, "source": "appServer"}

            def terminate(self) -> None:
                events.append("terminate")

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        assert handle.supervisor is not None
        handle.supervisor.join(timeout=5)

        assert events[:2] == ["preflight", "start"]
        assert handle.directory.read_meta()["mcp_preflight_status"] == "ready"
        assert handle.directory.read_meta()["mcp_preflight_server"] == "agentjobs"
        assert handle.directory.read_meta()["persistence_status"] == "persisted"
        assert handle.directory.read_meta()["desktop_visibility"] == "not_observed"
        transcript = handle.directory.path / "transcript.log"
        assert '"method": "item/started"' in transcript.read_text(encoding="utf-8")

    def test_app_server_preflight_failure_prevents_the_task_turn(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-terra", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        runner = build(workspace, manager, resolution)
        events: list[str] = []

        class FakeAppServer:
            def __init__(self, **_kwargs) -> None:
                pass

            def preflight_required_mcp(self):
                events.append("preflight")
                raise CodexAppServerError("AgentJobs MCP is not ready: connection refused")

            def start(self, _prompt, *, resume_thread_id=None):
                events.append("start")
                pytest.fail("the task turn must not start after preflight fails")

            def terminate(self) -> None:
                events.append("terminate")

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)

        with pytest.raises(DispatchRunError, match="Could not start a Codex App Server session"):
            runner.start(task, actor="Jeff Posey", caused_by=1)

        runs = list((workspace / "home" / "runs").iterdir())
        assert len(runs) == 1
        meta = yaml.safe_load((runs[0] / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["codex_phase"] == "preflight"
        assert meta["mcp_preflight_error"] == "AgentJobs MCP is not ready: connection refused"
        assert events == ["preflight", "terminate"]

    def test_busy_codex_resume_parks_the_persisted_thread_without_fresh_start(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-luna", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        runner = build(workspace, manager, resolution)
        RunDirectory.create(
            workspace / "home",
            "run_previous",
            {
                "run_id": "run_previous",
                "task_id": task.id,
                "mode": "session",
                "driver": "codex",
                "status": "finished",
                "codex_status": "completed",
                "session_id": "thread-previous",
                "started_at": "2026-08-22T20:00:00+00:00",
            },
        )
        starts: list[str | None] = []

        class FakeAppServer:
            def __init__(self, **_kwargs) -> None:
                pass

            def preflight_required_mcp(self):
                return type("Preflight", (), {"server_name": "agentjobs", "status": "ready"})()

            def start(self, _prompt, *, resume_thread_id=None):
                starts.append(resume_thread_id)
                raise CodexAppServerError(
                    "thread has active writer", resume_failure=CodexResumeFailure.BUSY
                )

            def terminate(self) -> None:
                pass

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert starts == ["thread-previous"]
        assert handle.session_id == "thread-previous"
        assert handle.supervisor is None
        assert runner.poll_session(handle) is SessionPhase.PARKED
        meta = handle.directory.read_meta()
        assert meta["status"] == "parked"
        assert meta["codex_status"] == "resume_busy"
        assert meta["resume_failure"] == "busy"
        assert meta["thread_id"] == "thread-previous"
        assert not terminal_entries(manager, task.id)

    def test_missing_codex_resume_starts_an_audited_fresh_thread(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-luna", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        runner = build(workspace, manager, resolution)
        RunDirectory.create(
            workspace / "home",
            "run_previous",
            {
                "run_id": "run_previous",
                "task_id": task.id,
                "mode": "session",
                "driver": "codex",
                "status": "finished",
                "codex_status": "completed",
                "session_id": "thread-previous",
                "started_at": "2026-08-22T20:00:00+00:00",
            },
        )
        starts: list[str | None] = []

        class FakeAppServer:
            pid = 1239

            def __init__(self, **_kwargs) -> None:
                pass

            def preflight_required_mcp(self):
                return type("Preflight", (), {"server_name": "agentjobs", "status": "ready"})()

            def start(self, _prompt, *, resume_thread_id=None):
                starts.append(resume_thread_id)
                if resume_thread_id:
                    raise CodexAppServerError(
                        "thread is missing", resume_failure=CodexResumeFailure.MISSING
                    )
                return CodexSessionStarted("thread-fresh", "session-fresh", "turn-fresh", self.pid)

            def supervise(self, *, turn_id, on_message=None, timeout=None):
                return {"turn": {"id": turn_id, "status": "completed"}}

            def inspect_persisted_thread(self, thread_id):
                return {"thread_id": thread_id, "source": "appServer"}

            def terminate(self) -> None:
                pass

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        assert handle.supervisor is not None
        handle.supervisor.join(timeout=5)

        assert starts == ["thread-previous", None]
        meta = handle.directory.read_meta()
        assert meta["resume_replacement"] is True
        assert meta["resume_failure"] == "missing"
        assert meta["replaced_thread_id"] == "thread-previous"
        assert meta["fresh_thread_id"] == "thread-fresh"
        stored = manager.get_task(task.id)
        assert stored is not None
        dispatch = next(entry for entry in stored.log if entry.type is LogEntryType.DISPATCH)
        body = dispatch.body or ""
        assert "thread-previous" in body
        assert "thread-fresh" in body

    def test_generic_codex_resume_error_never_starts_a_fresh_thread(
        self, workspace: Path, manager: TaskManager, task, monkeypatch
    ) -> None:
        resolution = make_resolution(
            ["codex", "app-server", "--model", "gpt-5.6-luna", "{prompt}"],
            mode=RunnerMode.SESSION,
            driver=RunnerDriver.CODEX,
            posture=Posture.AUTO,
        )
        runner = build(workspace, manager, resolution)
        RunDirectory.create(
            workspace / "home",
            "run_previous",
            {
                "run_id": "run_previous",
                "task_id": task.id,
                "mode": "session",
                "driver": "codex",
                "status": "finished",
                "codex_status": "completed",
                "session_id": "thread-previous",
                "started_at": "2026-08-22T20:00:00+00:00",
            },
        )
        starts: list[str | None] = []

        class FakeAppServer:
            def __init__(self, **_kwargs) -> None:
                pass

            def preflight_required_mcp(self):
                return type("Preflight", (), {"server_name": "agentjobs", "status": "ready"})()

            def start(self, _prompt, *, resume_thread_id=None):
                starts.append(resume_thread_id)
                raise CodexAppServerError(
                    "unexpected resume failure", resume_failure=CodexResumeFailure.GENERIC
                )

            def terminate(self) -> None:
                pass

        monkeypatch.setattr("agentjobs.dispatch.runner.CodexAppServerProcess", FakeAppServer)

        with pytest.raises(DispatchRunError, match="Could not start a Codex App Server session"):
            runner.start(task, actor="Jeff Posey", caused_by=1)

        assert starts == ["thread-previous"]
        failed = next(
            path for path in (workspace / "home" / "runs").iterdir() if path.name != "run_previous"
        )
        meta = yaml.safe_load((failed / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["status"] == "failed"
        assert meta["codex_lifecycle"] == "terminal_failure"


class TestPromptStub:
    def test_the_guide_it_points_at_exists_and_links_to_the_contract(self) -> None:
        """The stub once named a v1-era file, so every run started at a stale document."""
        guide = REPO_ROOT / GUIDE_PATH

        assert guide.is_file(), f"{GUIDE_PATH} does not exist"
        text = guide.read_text(encoding="utf-8")
        assert "resumption contract" in text.lower()
        assert "schema-design.md" in text

    def test_the_stub_is_a_pointer_not_a_composition(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert task.id in prompt
        assert GUIDE_PATH in prompt
        assert "run_abcd1234" in prompt
        # It must not restate the record, which is the whole argument for a stub.
        assert task.spec.description not in prompt
        # A ceiling, not a target. Raised from 500 by task-192, which had to spell the
        # worktree command out rather than gesture at it, and from 800 by task-021,
        # which appends the merge and push policy for the reason `policy_clause` gives:
        # it is the one thing in the prompt with nothing to point at. The assertion
        # above is the one that enforces "pointer, not composition".
        assert len(prompt) < 1300

    def test_the_stub_tells_the_run_to_take_its_own_worktree(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """task-186. The one instruction that cannot be deferred to the guide.

        Dispatch stopped passing ``-w``, so nothing isolates a dispatched run from the
        project's shared working tree but its own first act. Every other thing the run
        needs to know it can go and read; this it has to know *before* it reads
        anything. Asserted on the rendered prompt rather than on ``PROMPT_STUB``,
        because what reaches the agent is what matters.
        """
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "worktree" in prompt
        assert "not isolated" in prompt.lower()

    def test_the_stub_names_the_shell_command_and_forbids_the_builtin_tool(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """task-192. Prose here is not a weaker version of the command; it is a hang.

        "Take your own git worktree" is satisfied by Claude Code's ``EnterWorktree``
        tool, which asks to relocate the session's permission root -- an escalation the
        ``auto`` classifier declines and a ``--bg`` run cannot answer. run_6f1f0741
        parked on it on 2026-08-20 before writing a line. So the stub must carry the
        literal command, and must say not to use the tool.
        """
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "git worktree add" in prompt
        # task-200: under `worktrees/`, not loose beside the clone.
        assert "git worktree add ../worktrees/" in prompt
        # The branch it names is this task's, so the command is runnable as written.
        assert task.id in prompt.split("git worktree add", 1)[1]
        assert "not a built-in worktree tool" in prompt
        assert "permission root" in prompt

    def test_the_stub_disowns_the_harness_instruction_it_contradicts(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """task-303. Forbidding the tool is not enough when something else demands it.

        A Claude Code ``--bg`` session is opened with a preamble telling it to call
        ``EnterWorktree`` and saying the instruction is enforced. That preamble arrives
        before this prompt does, so a stub that merely says "not a built-in worktree
        tool" leaves the session adjudicating a contradiction with no context. Three
        auditors on 2026-08-21 each resolved it alone and each invented the same
        workaround. The stub names the conflict instead.
        """
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "harness" in prompt
        assert "ignore that instruction" in prompt

    def test_the_repository_turns_off_the_background_isolation_guard(self) -> None:
        """task-303, ac-1 and ac-4. The settings key is the half that is enforced.

        Without it the harness refuses a background session's ``Write`` into the shared
        checkout -- which is where this project requires task records to be committed,
        so the refusal is not a nuisance but a block on the workflow. Probed on Claude
        Code 2.1.238, 2026-08-25: refused with the key absent, accepted with it present,
        and the change took effect mid-session. This test pins the key so that removing
        it is a failure rather than a silent return of the block.
        """
        settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

        assert settings["worktree"]["bgIsolation"] == "none"

    def test_both_prose_documents_name_the_harness_conflict(self) -> None:
        """task-303, ac-2. The stub is one clause; the reasoning lives in prose.

        ALLAGENTS is what a session reads from its automatically loaded context and the
        guide is what the stub points at, so a session that reads either one has to
        arrive at the same answer about a preamble it was given before both.
        """
        for name in ("ALLAGENTS.md", GUIDE_PATH):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")

            assert "bgIsolation" in text, name
            # The version and date sit next to the result, so a later reader can tell
            # whether the probe still describes the harness they are running.
            assert "2.1.238" in text, name

    def test_the_guide_states_the_worktree_requirement_too(self) -> None:
        """The stub is one clause; the guide is where the reasoning lives.

        Both, deliberately -- and this test exists so that deleting either half is a
        failure rather than a quiet drift back to a prompt that assumes containment
        somebody else arranged.
        """
        text = (REPO_ROOT / GUIDE_PATH).read_text(encoding="utf-8")

        assert "git worktree add" in text
        heading = "## Before you write anything: take your own worktree"
        assert heading in text
        # Unmissable means near the top, not beside the claim halfway down.
        assert text.index(heading) < len(text) // 3
        # task-192: the guide must forbid the built-in tool too, or an agent that reads
        # the pointer instead of the stub still parks.
        assert "EnterWorktree" in text
        assert "permission root" in text


# ----- the supervisor stub ----------------------------------------------------


PARENT_PROTOCOL_HEADING = (
    "## Working a parent task: you supervise the children, you do not work them"
)


@pytest.fixture
def epic(manager: TaskManager):
    """A parent with two open children and one closed one, then claimed.

    Three children rather than two because the interesting boundary is *open*: a parent
    whose children are all closed is an ordinary task again, and a fixture with only
    open children cannot tell "lists its open children" from "lists everything below it".
    """
    parent = manager.create_task(
        title="An epic",
        category="infrastructure",
        summary="A parent to supervise.",
        description="Drive the children.",
        lifecycle=Lifecycle.READY,
    )
    for title in ("First child", "Second child"):
        manager.create_task(
            title=title,
            category="infrastructure",
            summary="A child.",
            description="Do the child's thing.",
            lifecycle=Lifecycle.READY,
            parent=parent.id,
        )
    done = manager.create_task(
        title="Third child",
        category="infrastructure",
        summary="A finished child.",
        description="Already done.",
        lifecycle=Lifecycle.READY,
        parent=parent.id,
    )
    manager.close_task(done.id, actor="claude", outcome=Outcome.COMPLETED)
    return manager.claim_task(parent.id, agent="claude")


class TestMergeAndPushPolicyReachesTheAgent:
    """task-021. A policy the run cannot see is a policy that does not exist.

    Asserted on the rendered prompt throughout, and in one case on the composed argv,
    because what reaches the agent is the only thing that matters here. The clause is
    derived from machine-local configuration the agent cannot read, and the repository's
    own committed prose says the opposite by default -- so a run that is not told is a
    run that will correctly obey the prose and make ``autonomous`` mean nothing.
    """

    def test_a_review_posture_is_told_to_stop_and_not_merge(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTO))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "stops at the merge gate" in prompt
        assert "human/review" in prompt
        assert "Do not merge." in prompt
        assert "--posture-release" not in prompt

    def test_supervised_says_exactly_what_auto_says_about_merging(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """The two differ in execution gating, never in who authorises the merge."""
        auto = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTO))
        supervised = build(
            workspace, manager, make_resolution(["fake"], posture=Posture.SUPERVISED)
        )

        assert auto.build_prompt(task.id, "r").replace("`auto`", "X") == supervised.build_prompt(
            task.id, "r"
        ).replace("`supervised`", "X")

    def test_an_autonomous_run_is_given_the_command_not_an_outcome(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """task-192's lesson applied: an instruction with a cheaper reading gets it.

        "Merge your own work" is satisfied by ``git merge``, which skips the gate that is
        the entire safety argument. So the clause names the command and forbids the hand
        merge, and this asserts both.
        """
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTONOMOUS))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "releases the merge gate" in prompt
        assert f"agentjobs finish {task.id} --project sandbox --posture-release" in prompt
        assert "Do not merge by hand" in prompt
        assert "Record the evidence on the task first" in prompt
        # The objective floor, named where the agent will read it.
        assert "full gate" in prompt

    def test_read_only_gets_no_merge_clause_at_all(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """It has no branch. A sentence about one is noise in a prompt with no shell."""
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.READ_ONLY))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "merge gate" not in prompt
        assert "push" not in prompt.lower()

    def test_the_push_clause_defaults_to_never(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTONOMOUS))

        assert "Never push" in runner.build_prompt(task.id, "run_abcd1234")

    def test_a_project_that_permits_pushing_is_told_so(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(
            workspace, manager, make_resolution(["fake"], posture=Posture.AUTO, push=True)
        )

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "Never push" not in prompt
        assert "pushing `main` is permitted" in prompt

    def test_pushing_is_independent_of_the_merge_policy(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """The two are orthogonal by design: a stopping posture may still push."""
        runner = build(
            workspace, manager, make_resolution(["fake"], posture=Posture.AUTO, push=True)
        )

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "stops at the merge gate" in prompt
        assert "is permitted" in prompt

    def test_a_supervisor_is_told_what_its_children_will_do(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """A supervisor holds no branch, so the worker's clause would be wrong for it."""
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTONOMOUS))

        prompt = runner.build_prompt(epic.id, "run_abcd1234")

        assert "a child you start merges its own work" in prompt.lower()
        assert "--posture-release" not in prompt

    def test_a_supervising_review_posture_says_it_approves_nothing(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"], posture=Posture.AUTO))

        prompt = runner.build_prompt(epic.id, "run_abcd1234")

        assert "You approve nothing yourself" in prompt

    def test_the_clause_survives_into_the_composed_argv(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """ac-3, asserted where it is actually checkable: the argv a run is started with.

        ``build_prompt`` returning the clause proves nothing on its own -- the prompt is
        spliced into a template, and a template that dropped it would still pass every
        assertion above.
        """
        runner = build(
            workspace,
            manager,
            make_resolution(["fake", "--model", "x", "{prompt}"], posture=Posture.AUTONOMOUS),
        )

        argv = runner.build_argv(task.id, "run_abcd1234")

        assert any("--posture-release" in element for element in argv)


class TestDescribeChildren:
    def test_no_children_reads_as_none_rather_than_empty(self) -> None:
        """So a caller from anywhere else cannot render "open children: ."."""
        assert describe_children([]) == "none"

    def test_a_short_list_is_named_in_full(self) -> None:
        assert describe_children(["task-001", "task-002"]) == "task-001, task-002"

    def test_a_long_list_is_capped_and_the_rest_counted(self) -> None:
        ids = [f"task-{n:03d}" for n in range(1, CHILDREN_NAMED + 4)]

        described = describe_children(ids)

        assert described.startswith("task-001, task-002")
        assert described.endswith(" and 3 more")
        assert ids[CHILDREN_NAMED] not in described


class TestSupervisorStub:
    def test_a_task_with_open_children_is_told_to_supervise(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """task-164. The record picks the stub: an open child means an epic."""
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(epic.id, "run_abcd1234")

        assert "supervising parent task" in prompt
        assert "supervisor, not the worker" in prompt
        assert "separate session for one eligible child" in prompt
        for child in manager.get_subtasks(epic.id):
            assert (child.id in prompt) is child.is_open

    def test_the_supervisor_is_told_not_to_take_a_worktree(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """The inversion that makes this a second stub rather than a longer one.

        A supervisor that obeyed ``PROMPT_STUB`` would check out a branch in the shared
        clone -- the collision the worktree rule exists to prevent -- and would then
        commit the parent's records where the dashboard cannot see them.
        """
        prompt = build(workspace, manager, make_resolution(["fake"])).build_prompt(
            epic.id, "run_abcd1234"
        )

        assert "do not take a worktree" in prompt
        assert "check nothing out" in prompt
        assert "git worktree add" not in prompt

    def test_a_parent_whose_children_all_closed_gets_the_worker_stub(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """Nothing is left to supervise, so it is an ordinary task again."""
        for child in manager.get_subtasks(epic.id):
            if child.is_open:
                manager.close_task(child.id, actor="claude", outcome=Outcome.COMPLETED)
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(epic.id, "run_abcd1234")

        assert "supervising parent task" not in prompt
        assert "git worktree add ../worktrees/" in prompt

    def test_a_leaf_task_is_unaffected(self, workspace: Path, manager: TaskManager, task) -> None:
        """The regression that matters: an ordinary dispatch gets the stub it always had."""
        runner = build(workspace, manager, make_resolution(["fake"]))

        stub = PROMPT_STUB.format(
            agent=runner.runner.actor_id,
            task_id=task.id,
            project_id="sandbox",
            project_root=workspace / "project",
            api_base="http://localhost:8899",
            run_id="run_abcd1234",
            children="none",
        )
        clause = policy_clause(
            Posture.SUPERVISED,
            push=False,
            task_id=task.id,
            project_id="sandbox",
            project_root=workspace / "project",
        )
        assert runner.build_prompt(task.id, "run_abcd1234") == f"{stub} {clause}"

    def test_the_supervisor_stub_is_still_a_pointer(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(epic.id, "run_abcd1234")

        assert GUIDE_PATH in prompt
        assert "run_abcd1234" in prompt
        assert epic.spec.description not in prompt
        # 900 -> 1100 (task-021, the merge policy) -> 1450 (task-022, the walk command).
        # Both rises are the same kind of thing and neither is drift: the stub stays a
        # pointer to the record, and what has been added to it twice is an *instruction
        # the record cannot carry* -- derived from machine-local config the agent may not
        # read, and naming the exact command, because an instruction a model can satisfy
        # several ways gets satisfied in the cheapest one. The assertion that matters is
        # the line above this block, which is that the spec is still not in here.
        assert len(prompt) < 1450

    def test_open_child_ids_survives_a_task_it_cannot_resolve(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """Decorating a prompt must never be able to refuse a dispatch."""
        runner = build(workspace, manager, make_resolution(["fake"]))

        assert runner.open_child_ids("task-999-not-a-task") == []

    def test_the_guide_states_the_parent_protocol(self) -> None:
        """The stub is one clause; the four supervision states live in the guide.

        Pinned because the stub's whole value is the pointer being good: an agent told
        to "follow the parent-task protocol" at a guide with no such section is worse
        off than one told nothing.
        """
        text = (REPO_ROOT / GUIDE_PATH).read_text(encoding="utf-8")

        assert PARENT_PROTOCOL_HEADING in text
        protocol = text.split(PARENT_PROTOCOL_HEADING, 1)[1]
        for state in ("Child finished", "Child parked", "Child died", "Parent idle"):
            assert f"**{state}.**" in protocol, state
        # The two rules 2026-08-19 established the hard way.
        assert "mechanism, not an intention" in protocol
        assert "not the process" in protocol


# ----- batch mode -------------------------------------------------------------


class TestTheRunIsMeasurable:
    """A dispatched run has to be able to say where its time went (task-233).

    Before this, ``meta.yaml`` held a start and a finish and the only other artefact
    was ``transcript.log`` -- a raw TTY capture in which a line appears as many times as
    the terminal repainted it. So the run was a black box, and the ranking of what to
    fix was inference.

    The mechanism is two environment variables on the spawned session, which every
    child of the agent inherits: the gate, the CLI, the MCP server. The test below is
    end to end on purpose. Asserting that ``_environment`` returns a dict containing the
    keys would pass with the spawn sites still calling the no-argument form.
    """

    def test_a_spawned_agent_can_write_a_phase_record_into_its_own_run(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.phases import read_phases

        script = write_script(
            tmp_path / "agent.py",
            """
            from agentjobs.dispatch.phases import record_phase_from_env

            record_phase_from_env("gate_finished", passed=True, seconds=12.5)
            """,
        )
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        directory = workspace / "home" / "runs" / handle.run_id
        (record,) = read_phases(directory)
        assert record["kind"] == "gate_finished"
        assert record["seconds"] == 12.5
        assert record["run_id"] == handle.run_id

    def test_the_polling_helpers_are_not_told_they_are_inside_a_run(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """They ask the runner about a session; they are not doing work inside one.

        A phase record written by a poll would attribute the poller's time to whichever
        run it happened to be asking about.
        """
        from agentjobs.dispatch.phases import RUN_DIR_ENV, RUN_ID_ENV

        runner = build(workspace, manager, make_resolution(["true", "{prompt}"]))
        environment = runner._environment()

        assert RUN_DIR_ENV not in environment
        assert RUN_ID_ENV not in environment

    def test_a_run_identity_is_granted_and_never_inherited(
        self, workspace: Path, manager: TaskManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatcher can itself be inside a run -- an agent supervising a child is the
        ordinary case -- and an inherited pair would file the child's gate under the
        parent's run."""
        from agentjobs.dispatch.phases import RUN_DIR_ENV, RUN_ID_ENV

        monkeypatch.setenv(RUN_DIR_ENV, str(workspace / "someone-elses-run"))
        monkeypatch.setenv(RUN_ID_ENV, "run_theirs")
        runner = build(workspace, manager, make_resolution(["true", "{prompt}"]))

        assert RUN_DIR_ENV not in runner._environment()
        assert RUN_ID_ENV not in runner._environment()


class TestBatchOutcomes:
    def test_a_clean_exit_that_moved_the_ball_is_completed(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(tmp_path / "ok.py", "print('done')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        # The agent would move the ball itself; do it for the fake one.
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.COMPLETED.value
        assert results[0].data["exit_code"] == 0

    def test_a_clean_exit_that_never_moved_the_ball_is_a_failure(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """Exit 0 with an unmoved ball means the agent stopped without saying what it needs."""
        script = write_script(tmp_path / "quiet.py", "print('nothing to say')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FINISHED_WITHOUT_HANDOFF.value
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN

    def test_a_non_zero_exit_is_failed_and_inlines_the_output(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(
            tmp_path / "boom.py",
            """
            import sys
            print("about to fail")
            print("stack-ish detail", file=sys.stderr)
            sys.exit(3)
            """,
        )
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FAILED.value
        assert results[0].data["exit_code"] == 3
        assert "about to fail" in (results[0].body or "")

    def test_a_hang_past_the_timeout_is_terminated_and_reported(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(
            tmp_path / "hang.py",
            """
            import time
            print("sleeping", flush=True)
            time.sleep(600)
            """,
        )
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(script), "{prompt}"], timeout=2),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.TIMEOUT.value

    def test_megabytes_of_output_go_to_disk_and_do_not_stall_the_run(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """Buffering in memory loses exactly the case that matters: a crash mid-flood."""
        script = write_script(
            tmp_path / "flood.py",
            """
            for index in range(20000):
                print("x" * 100)
            """,
        )
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        stdout = handle.directory.path / "stdout.log"
        assert stdout.stat().st_size > 1_000_000
        assert len(terminal_entries(manager, task.id)) == 1

    def test_an_exception_in_the_supervisor_itself_still_writes_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path, monkeypatch
    ) -> None:
        """The task-047 shape: a supervisor that dies silently is worse than none."""
        script = write_script(tmp_path / "ok.py", "print('done')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected supervisor failure")

        monkeypatch.setattr(DispatchRunner, "_classify_batch_exit", explode)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.CRASHED.value
        assert "injected supervisor failure" in (results[0].body or "")

    def test_a_runner_that_cannot_be_spawned_still_gets_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["definitely-not-a-real-binary"]))

        with pytest.raises(DispatchRunError):
            runner.start(task, actor="Jeff Posey", caused_by=1)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.CRASHED.value


class TestProcessGroup:
    def test_the_timeout_kills_the_grandchild_too(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """An agent that shelled out to pytest must not leave the pytest behind."""
        marker = tmp_path / "grandchild.pid"
        grandchild = write_script(
            tmp_path / "grandchild.py",
            f"""
            import os, time
            open(r"{marker}", "w").write(str(os.getpid()))
            time.sleep(600)
            """,
        )
        parent = write_script(
            tmp_path / "parent.py",
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, r"{grandchild}"])
            time.sleep(600)
            """,
        )
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(parent), "{prompt}"], timeout=3),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        deadline = 30
        while not marker.exists() and deadline:
            deadline -= 1
            subprocess.run([sys.executable, "-c", "import time; time.sleep(0.2)"], check=False)
        assert marker.exists(), "the grandchild never started, so this proves nothing"
        grandchild_pid = int(marker.read_text())

        join(handle)

        # Waited for rather than asserted outright, because the kill is asynchronous and
        # this assertion is not. `_terminate` signals the *process group*, so the parent
        # and the grandchild are killed concurrently and independently; `join` returns
        # when the supervisor thread finishes, which tracks the parent. Nothing makes the
        # grandchild's exit precede that, and under load it does not.
        #
        # Measured on 2026-08-25, this machine, while the full suite ran under `-n auto`:
        # six runs of this scenario, and in one of them the grandchild was still listed
        # at the moment `join` returned and gone by the next poll. That is the flake that
        # failed the gate for task-308's finish -- the branch under it touched neither
        # this test nor the kill path.
        #
        # This still fails if the grandchild genuinely survives, which is the whole point
        # of the test. It no longer also fails when the grandchild dies a second late.
        assert _dies_within(grandchild_pid, 30.0), f"pid {grandchild_pid} survived the timeout"


def _dies_within(pid: int, seconds: float) -> bool:
    """Whether ``pid`` is gone within ``seconds``. Polls; never sleeps the full budget.

    The budget is generous on purpose. It is not a measurement of how fast a kill ought
    to be -- nothing here asserts a deadline -- it is only large enough that a loaded
    machine cannot exhaust it, so that a failure means "still running", never "slow".
    `_pid_alive` shells out to `tasklist`, which is itself about a second per call here,
    and that cost is inside the budget rather than beside it.
    """
    deadline = time.monotonic() + seconds
    while True:
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _pid_alive(pid: int) -> bool:
    """True when a pid is still running. Windows is the reference platform."""
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ----- session mode -----------------------------------------------------------


FAKE_CLI = """
import json, sys, pathlib

# A real CLI writes UTF-8; without this the box-drawing below raises inside this
# process on a cp1252 console and the transcript silently truncates mid-frame.
sys.stdout.reconfigure(encoding="utf-8")

state_file = pathlib.Path(__file__).with_name("ledger.json")
argv = sys.argv[1:]

if argv and argv[0] == "agents":
    print(json.dumps(json.loads(state_file.read_text())))
    raise SystemExit(0)

if argv and argv[0] == "logs":
    # Shaped like the real thing: escape sequences, box-drawing frame, and the
    # Remote Control URL that appears only here and never in the ledger.
    print("[38;2;153;153;153m/remote-control is active · Continue here, on your phone, or at")
    print("https://claude.ai/code/session_0142VngQLPx14GUrbyfLWPuC[m")
    print("╭" + "─" * 40 + "╮")
    print("[1mClaude needs your permission to run:[m")
    print("  poetry run alembic upgrade head")
    print("╰" + "─" * 40 + "╯")
    raise SystemExit(0)

if argv and argv[0] == "stop":
    rows = json.loads(state_file.read_text())
    state_file.write_text(json.dumps([r for r in rows if r["id"] != argv[1]]))
    print("stopped")
    raise SystemExit(0)

# Launch: behave like `--bg`, which prints a short id and returns immediately.
state_file.write_text(json.dumps([{
    "id": "b55b35ad", "sessionId": "session_0142Vng", "pid": 4242,
    "kind": "background", "status": "busy", "state": "working",
}]))
print("backgrounded \\u00b7 b55b35ad \\u00b7 aj-task")
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    return write_script(tmp_path / "fakecli.py", FAKE_CLI)


def session_resolution(fake_cli: Path, **kwargs: object) -> DispatchResolution:
    return make_resolution(
        [sys.executable, str(fake_cli), "--bg", "--remote-control", "{prompt}"],
        mode=RunnerMode.SESSION,
        **kwargs,  # type: ignore[arg-type]
    )


def set_ledger(fake_cli: Path, rows: List[dict]) -> None:
    (fake_cli.parent / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")


class TestSessionClassification:
    @pytest.mark.parametrize(
        "status,state,expected",
        [
            ("busy", "working", SessionPhase.RUNNING),
            ("waiting", "blocked", SessionPhase.PARKED),
            ("idle", "done", SessionPhase.FINISHED),
            ("idle", "blocked", SessionPhase.FINISHED),
            (None, "stopped", SessionPhase.STOPPED),
        ],
    )
    def test_the_observed_pairs_map_as_verified(
        self, status: Optional[str], state: Optional[str], expected: SessionPhase
    ) -> None:
        assert classify_session(status, state) is expected

    def test_an_unrecognised_pair_is_treated_as_still_running(self) -> None:
        """Declaring a live run over would write a terminal entry for a working session."""
        assert classify_session("something-new", "unheard-of") is SessionPhase.RUNNING

    def test_idle_blocked_is_finished_not_parked(self) -> None:
        """It finished after a denial; reading it as parked asks a human a dead question."""
        assert classify_session("idle", "blocked") is SessionPhase.FINISHED


class TestSessionMode:
    def test_it_captures_the_id_the_cli_assigned(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """--bg ignores --session-id and manages the id itself, so we read it back."""
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert handle.session_id == "b55b35ad"
        assert handle.run_id != handle.session_id
        after = manager.get_task(task.id)
        assert after is not None
        dispatched = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        assert dispatched.data["session_id"] == "b55b35ad"
        assert dispatched.data["run_id"] == handle.run_id
        assert dispatched.data["mode"] == "session"

    def test_no_session_id_argument_is_ever_passed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))

        argv = runner.build_argv(task.id, "run_abcd1234")

        assert "--session-id" not in argv

    def test_a_launcher_that_prints_no_id_is_a_hard_failure(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """A session nothing can follow is worse than one that never started."""
        silent = write_script(tmp_path / "silent.py", "print('started, good luck')\n")
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(silent), "{prompt}"], mode=RunnerMode.SESSION),
        )

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "could not read its id" in str(caught.value)

    def test_a_parked_session_becomes_a_question_a_human_can_answer(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Without this, the supervised posture is not a safety property, it is a hang."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(
            fake_cli,
            [{"id": "b55b35ad", "status": "waiting", "state": "blocked", "pid": 4242}],
        )

        phase = runner.poll_session(handle)

        assert phase is SessionPhase.PARKED
        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.HUMAN
        assert after.ball_reason is BallReason.INPUT
        prompt = after.ball_prompt or ""
        assert "poetry run alembic upgrade head" in prompt
        assert "b55b35ad" in prompt
        # Leads with the thing a human can actually act on from a phone.
        assert "https://claude.ai/code/session_0142VngQLPx14GUrbyfLWPuC" in prompt
        # And is readable: no escape sequences, no frame-only lines.
        assert "" not in prompt
        assert "[38;2;" not in prompt
        assert "───" not in prompt

    def test_a_parked_session_is_never_escalated_or_killed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """A timeout is not a human act, so it cannot grant autonomy (design section 2)."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=0))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "waiting", "state": "blocked"}])

        runner.poll_session(handle)
        runner.poll_session(handle)

        assert terminal_entries(manager, task.id) == []
        rows = json.loads((fake_cli.parent / "ledger.json").read_text())
        assert rows, "the parked session was stopped, which the design forbids"

    def test_a_finished_session_that_moved_the_ball_is_completed_and_reaped(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.COMPLETED.value
        assert json.loads((fake_cli.parent / "ledger.json").read_text()) == [], "not reaped"

    def test_a_finished_session_inside_the_staleness_window_is_left_alone(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An agent pausing mid-work looks identical to one that stopped for good."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=3600))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        assert terminal_entries(manager, task.id) == []

    def test_a_stale_session_is_reported_but_not_killed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Staleness replaces the wall-clock kill precisely so the session stays attachable."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=0))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FINISHED_WITHOUT_HANDOFF.value
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN
        rows = json.loads((fake_cli.parent / "ledger.json").read_text())
        assert rows, "a stale session must stay attachable, not be stopped"

    def test_a_session_that_vanished_from_the_ledger_gets_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [])

        phase = runner.poll_session(handle)

        assert phase is SessionPhase.GONE
        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.INTERRUPTED.value

    def test_the_ledger_is_scoped_to_this_project(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An unrelated session elsewhere must never be mistaken for a dispatched run."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        runner.start(task, actor="Jeff Posey", caused_by=1)

        recorded = (workspace / "home" / "runs").iterdir()
        assert any(recorded), "no run directory was written"
        # The scoping itself: --cwd is always passed.
        argv_seen: List[str] = []
        original = subprocess.run

        def capture(argv, *args, **kwargs):
            argv_seen.extend(argv)
            return original(argv, *args, **kwargs)

        import agentjobs.dispatch.runner as runner_module

        runner_module.subprocess.run = capture  # type: ignore[assignment]
        try:
            runner.ledger()
        finally:
            runner_module.subprocess.run = original

        assert "--cwd" in argv_seen
        assert str(workspace / "project") in argv_seen


class TestTranscriptCapture:
    """The run directory has to hold the session's output, because nothing else will.

    ``stdout.log`` for a session run is the launcher's backgrounding banner and can never
    be anything else, and the session's own transcript lives in a store AgentJobs does
    not own and does not outlive the reap.
    """

    def test_a_capture_writes_the_transcript_beside_the_run_metadata(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        runner.capture_transcript(handle)

        written = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "poetry run alembic upgrade head" in written
        # Raw, escape sequences and all. Stripping happens where it is rendered, so the
        # stored copy stays the thing the terminal actually showed.
        assert "\x1b[" in written

    def test_an_unreadable_transcript_does_not_erase_the_last_good_one(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """ "Could not read it just now" is not evidence the session produced nothing."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        runner.capture_transcript(handle)
        original = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")

        runner.transcript = lambda session_id: ""  # type: ignore[method-assign]
        runner.capture_transcript(handle)

        assert (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8") == original

    def test_polling_captures_before_it_settles_and_reaps(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Ordering is the whole point: `claude logs` on a reaped session reads nothing,
        so capturing after settling would leave every completed run blank."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please look.",
        )
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        assert json.loads((fake_cli.parent / "ledger.json").read_text()) == [], "not reaped"
        kept = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "poetry run alembic upgrade head" in kept


class TestTranscriptRendering:
    def test_escape_sequences_are_removed(self) -> None:
        """A ball prompt full of CSI sequences is unusable where it must be answered."""
        assert strip_ansi("[1mbold[m plain") == "bold plain"

    def test_frame_only_lines_are_dropped(self) -> None:
        raw = "\n".join(["╭" + "─" * 10 + "╮", "real content", "╰" + "─" * 10 + "╯"])

        assert readable_tail(raw, 40) == "real content"

    def test_the_tail_keeps_the_end_not_the_beginning(self) -> None:
        raw = "\n".join(f"line {index}" for index in range(100))

        assert readable_tail(raw, 3) == "line 97\nline 98\nline 99"

    def test_the_remote_control_url_is_found_through_the_escape_codes(self) -> None:
        raw = "[38;2;1;2;3mopen https://claude.ai/code/session_ABC123[m now"

        match = REMOTE_CONTROL_URL.search(strip_ansi(raw))

        assert match is not None
        assert match.group(0) == "https://claude.ai/code/session_ABC123"

    def test_a_transcript_without_a_url_is_not_an_error(self) -> None:
        assert REMOTE_CONTROL_URL.search("nothing here") is None


class TestExecutableResolution:
    def test_a_windows_shim_is_resolved_rather_than_shelled_out_to(self) -> None:
        """`claude` is a .CMD on Windows; Popen without a shell cannot find it by name."""
        resolved = resolve_executable("python")

        assert resolved != "python" or os.name != "nt"
        assert Path(resolved).exists() or resolved == "python"

    def test_an_unresolvable_name_is_returned_unchanged(self) -> None:
        """So the failure is subprocess's, naming the program, not a silent substitution."""
        assert resolve_executable("definitely-not-installed-xyz") == "definitely-not-installed-xyz"

    def test_the_prompt_a_human_is_told_to_type_is_not_the_resolved_path(
        self, workspace: Path, manager: TaskManager, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))

        assert runner.display_command() == sys.executable
        assert runner.build_argv("task-1", "run_1")[0] == resolve_executable(sys.executable)


class TestSpawnPreconditions:
    def test_the_sentinel_refuses_a_spawn_even_after_resolution_succeeded(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """The panic button must stop the next run, not the next config reload."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        (workspace / "home" / "DISPATCH_DISABLED").write_text("", encoding="utf-8")

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "DISPATCH_DISABLED" in str(caught.value)
        assert terminal_entries(manager, task.id) == []

    def test_a_dirty_tree_refuses_the_run(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An agent committing on top of uncommitted human work entangles the two."""
        project = workspace / "project"
        subprocess.run(["git", "init"], cwd=project, capture_output=True, check=True)
        (project / "in-flight.txt").write_text("someone is mid-edit", encoding="utf-8")
        runner = build(workspace, manager, session_resolution(fake_cli, require_clean_tree=True))

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "uncommitted" in str(caught.value)


class TestUncommittedPaths:
    """The primitive both clean-tree gates ask, including what it deliberately ignores."""

    @staticmethod
    def repo(root: Path) -> Path:
        """A committed repository with a tasks directory tracked inside it."""
        (root / "tasks").mkdir(parents=True)
        (root / "src").mkdir()
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
        (root / "tasks" / "task-001.yaml").write_text("id: task-001\n", encoding="utf-8")
        (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
        return root

    def test_a_clean_tree_has_no_paths(self, tmp_path: Path) -> None:
        root = self.repo(tmp_path / "proj")
        assert uncommitted_paths(root) == []
        assert working_tree_clean(root)

    def test_git_that_cannot_answer_is_not_clean(self, tmp_path: Path) -> None:
        """Distinct from "nothing is uncommitted", and it has to refuse rather than pass."""
        nowhere = tmp_path / "not-a-repo"
        nowhere.mkdir()
        assert uncommitted_paths(nowhere) is None
        assert working_tree_clean(nowhere) is False

    def test_the_ignored_directory_drops_out_and_nothing_else_does(self, tmp_path: Path) -> None:
        root = self.repo(tmp_path / "proj")
        (root / "tasks" / "task-001.yaml").write_text(
            "id: task-001\nclaimed: y\n", encoding="utf-8"
        )
        (root / "tasks" / "task-002.yaml").write_text("id: task-002\n", encoding="utf-8")
        (root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        assert sorted(uncommitted_paths(root) or []) == [
            "src/app.py",
            "tasks/task-001.yaml",
            "tasks/task-002.yaml",
        ]
        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/app.py"]

    def test_a_filename_with_a_space_survives_the_parse(self, tmp_path: Path) -> None:
        """`git status --porcelain` quotes and escapes these; the -z form does not.

        A path this misparsed would be dropped silently from a safety check, so it is
        pinned rather than left to the format.
        """
        root = self.repo(tmp_path / "proj")
        (root / "src" / "two words.py").write_text("x = 3\n", encoding="utf-8")

        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/two words.py"]

    def test_a_rename_reports_the_new_path_once(self, tmp_path: Path) -> None:
        """-z emits the original path as a second field, which must not be read as dirt."""
        root = self.repo(tmp_path / "proj")
        subprocess.run(
            ["git", "-C", str(root), "mv", "src/app.py", "src/renamed.py"],
            capture_output=True,
            check=True,
        )

        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/renamed.py"]

    def test_paths_resolve_against_the_repository_root_not_the_directory_asked(
        self, tmp_path: Path
    ) -> None:
        """Porcelain paths are repo-relative wherever git ran, so the exclusion must be too."""
        root = self.repo(tmp_path / "proj")
        (root / "tasks" / "task-001.yaml").write_text(
            "id: task-001\nclaimed: y\n", encoding="utf-8"
        )
        (root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        assert uncommitted_paths(root / "src", ignore=[root / "tasks"]) == ["src/app.py"]


# ----- the shapes this module refuses to have ---------------------------------


class TestShapesRefused:
    def test_there_is_no_asyncio_and_no_shell(self) -> None:
        """task-047's detached-coroutine shape, and shell=True, are both out by rule.

        Read from the syntax tree rather than by string search, so the prose explaining
        why these are forbidden cannot fail the test that forbids them.
        """
        import ast

        tree = ast.parse(
            (REPO_ROOT / "src" / "agentjobs" / "dispatch" / "runner.py").read_text(encoding="utf-8")
        )

        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "asyncio" not in imported

        shell_kwargs = [
            keyword
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "shell"
        ]
        assert shell_kwargs == []

    def test_the_transcript_is_never_parsed_for_state(self) -> None:
        """Structured state comes from the ledger; `logs` is an ANSI pty scrape."""
        source = (REPO_ROOT / "src" / "agentjobs" / "dispatch" / "runner.py").read_text(
            encoding="utf-8"
        )
        body = source.split("def transcript(", 1)[1].split("\n    def ", 1)[0]

        # All that is done with `logs` output is hand it back for a human to read.
        assert "json.loads" not in body
        assert "classify" not in body


class TestRepaintCollapsing:
    """A TUI repaints its whole screen, so its pty capture holds the same frame many
    times. Forty lines of a real session were thirteen distinct lines painted three
    times over, with the newest work pushed off the end by copies of itself."""

    def test_a_repainted_screen_is_shown_once(self) -> None:
        frame = "> reading the task record\n  running tests\n"

        collapsed = drop_repainted_lines(frame * 3)

        assert collapsed.splitlines() == ["> reading the task record", "  running tests"]

    def test_the_newest_copy_is_the_one_kept(self) -> None:
        """Order has to follow the newest frame; keeping the first copy would show the
        opening screen and drop everything that happened after it."""
        collapsed = drop_repainted_lines("opened\nstep one\nopened\nstep one\nstep two\n")

        assert collapsed.splitlines() == ["opened", "step one", "step two"]

    def test_nothing_is_lost_when_a_transcript_never_repeats(self) -> None:
        text = "one\ntwo\nthree"

        assert drop_repainted_lines(text) == text


# ----- the group audit trail (task-177) ---------------------------------------


def grouped_resolution(argv: List[str]) -> DispatchResolution:
    """A resolution as the group selector produces one: a winner plus its rivals."""
    winner = RunnerConfig(name="second", argv=argv, env={}, mode=RunnerMode.BATCH)
    settings = ProjectDispatchSettings(
        project_id="sandbox", enabled=True, group="default", require_clean_tree=False
    )
    limits = DispatchLimits()
    selection = RunnerSelection(
        runner=winner,
        source=SelectionSource.DISPATCH,
        group="default",
        candidates=[
            RunnerCandidate(
                runner="first",
                eligible=False,
                skipped_because=SkipReason.DISABLED,
                detail="no key on this machine yet",
            ),
            RunnerCandidate(runner="second", eligible=True),
            RunnerCandidate(runner="third", eligible=True),
        ],
    )
    return DispatchResolution(
        project_id="sandbox",
        runner=winner,
        settings=settings,
        limits=limits,
        config=DispatchConfig(enabled=True, limits=limits),
        selection=selection,
    )


class TestSelectionIsRecorded:
    """sc-3: the dispatch entry answers "why that runner" without the local run dir."""

    def test_the_entry_names_the_group_the_winner_and_the_rivals(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        script = write_script(workspace / "ok.py", "print('done')")
        runner = build(
            workspace, manager, grouped_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        after = manager.get_task(task.id)
        assert after is not None
        entry = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        selection = entry.data["selection"]
        assert entry.data["runner"] == "second"
        assert selection["group"] == "default"
        assert selection["source"] == "dispatch"
        assert [c["runner"] for c in selection["candidates"]] == ["first", "second", "third"]
        skipped = selection["candidates"][0]
        assert skipped["eligible"] is False
        assert skipped["skipped_because"] == "disabled"
        assert skipped["detail"] == "no key on this machine yet"

    def test_the_handle_says_what_was_chosen_and_from_where(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        script = write_script(workspace / "ok2.py", "print('done')")
        runner = build(
            workspace, manager, grouped_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        assert handle.runner == "second"
        assert handle.group == "default"

    def test_a_flat_resolution_writes_no_selection_key_at_all(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """The compatibility claim, asserted on the bytes rather than on intent."""
        script = write_script(workspace / "ok3.py", "print('done')")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        after = manager.get_task(task.id)
        assert after is not None
        entry = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        assert "selection" not in entry.data
        assert handle.group is None


# ----- the supervisor's MCP grant (task-220) ----------------------------------


class TestSupervisorMcpGrant:
    """run_d5ab5caf parked before it launched anything, on its own log writes.

    Two of the three classifier blocks that armed the breaker were the same
    ``task_log_append``: the supervisor writing a child's brief, which said the child
    could merge without human review. That is what a supervisor does and an ordinary run
    never does, so the grant is scoped to that role and nothing else.
    """

    def write_mcp_json(self, project_root: Path, *names: str) -> None:
        payload = {"mcpServers": {name: {"command": "noop"} for name in names}}
        (project_root / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_the_rule_is_server_level_so_it_cannot_go_stale(self) -> None:
        """ac-5. A bare ``mcp__<server>`` matches every tool, including future ones.

        Asserted as a literal rather than derived from the same expression the source
        uses, because the whole failure class here is a rule whose *form* matches
        nothing -- and a test written as ``f"mcp__{name}"`` on both sides would pass
        against any form at all. Compare ``allow_rules()``'s colon.
        """
        assert supervisor_allow_rules(["agentjobs"]) == ["mcp__agentjobs"]
        assert supervisor_allow_rules(["a", "b"]) == ["mcp__a", "mcp__b"]

    def test_no_servers_means_no_rules(self) -> None:
        assert supervisor_allow_rules([]) == []

    def test_a_supervisor_gets_the_grant_appended_to_the_ordinary_rules(self) -> None:
        """The base list is unchanged and the grant is additive, in that order."""
        blob = json.loads(
            settings_json(allow_list=True, mcp_servers=["agentjobs"], supervisor=True)
        )

        assert blob["permissions"]["allow"] == allow_rules() + ["mcp__agentjobs"]

    def test_a_leaf_task_blob_is_byte_identical_to_before(self) -> None:
        """ac-4 at the JSON level. Ordinary dispatch must not move at all."""
        assert settings_json(allow_list=True, mcp_servers=["agentjobs"]) == json.dumps(
            {
                "permissions": {"allow": allow_rules()},
                "enabledMcpjsonServers": ["agentjobs"],
            }
        )

    def test_a_supervisor_of_a_project_with_no_mcp_json_is_unchanged(self) -> None:
        """Nothing to name, so nothing is added -- not an empty list, no key churn."""
        assert settings_json(allow_list=True, mcp_servers=[], supervisor=True) == settings_json(
            allow_list=True, mcp_servers=[]
        )

    def test_read_only_never_reaches_the_grant(self) -> None:
        """The placement inside the allow-list branch is the safety property."""
        flags = posture_flags(Posture.READ_ONLY, ["agentjobs"], supervisor=True)

        blob = json.loads(flags[flags.index("--settings") + 1])
        assert "permissions" not in blob

    def test_autonomous_never_reaches_the_grant(self) -> None:
        assert posture_flags(Posture.AUTONOMOUS, ["agentjobs"], supervisor=True) == [
            "--permission-mode",
            "bypassPermissions",
        ]

    def test_an_epic_dispatch_carries_the_grant_end_to_end(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """ac-2 through the real path: the record's open children reach the argv."""
        self.write_mcp_json(workspace / "project", "agentjobs")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTO),
        )

        argv = runner.build_argv(epic.id, "run_abcd1234")

        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules() + ["mcp__agentjobs"]

    def test_a_leaf_dispatch_carries_no_grant_end_to_end(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """ac-4 through the real path, against the same project and .mcp.json."""
        self.write_mcp_json(workspace / "project", "agentjobs")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTO),
        )

        argv = runner.build_argv("task-019-example", "run_abcd1234")

        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules()

    def test_the_prompt_and_the_grant_cannot_disagree(
        self, workspace: Path, manager: TaskManager, epic
    ) -> None:
        """One read of the record decides both, so a supervisor prompt implies the grant.

        This is the pairing that broke: a session told to supervise, with a worker's
        permissions. Asserting them together is what makes a future refactor that splits
        the two reads fail here rather than in a parked run at 3am.
        """
        self.write_mcp_json(workspace / "project", "agentjobs")
        runner = build(
            workspace,
            manager,
            make_resolution(["claude", "--bg", "{prompt}"], posture=Posture.AUTO),
        )

        argv = runner.build_argv(epic.id, "run_abcd1234")

        settings = json.loads(argv[argv.index("--settings") + 1])
        assert any("supervisor, not the worker" in arg for arg in argv)
        assert "mcp__agentjobs" in settings["permissions"]["allow"]


DAEMON_HOP_CLI = """
import json
import os
import sys

# What the daemon does to the launcher's environment: discards it. This is the steady
# state -- 12 launches in 61 on the machine where task-249 was found were the one that
# started the daemon, and the other 49 got whatever the running daemon had.
os.environ.pop("AGENTJOBS_RUN_ID", None)
os.environ.pop("AGENTJOBS_RUN_DIR", None)

# What the daemon does deliver: argv. Everything the worker knows comes through here.
argv = sys.argv[1:]
if "--settings" in argv:
    value = argv[argv.index("--settings") + 1]
    if os.path.isfile(value):
        value = open(value, encoding="utf-8").read()
    os.environ.update(json.loads(value).get("env", {}))

from agentjobs.dispatch.phases import record_phase_from_env

record_phase_from_env("gate_finished", passed=True, seconds=95.8, scope="full")
print("Starting background service")
print("backgrounded, b55b35ad")
"""


class TestIdentitySurvivesTheDaemonHop:
    """Task-249, and the acceptance criterion it exists for.

    ``--bg`` does not start the worker. It contacts a persistent daemon, and the daemon
    spawns the worker from the daemon's own environment -- so everything
    ``_environment`` sets is discarded unless this launch happened to start the daemon.
    ``TestTheRunIsMeasurable`` above cannot see any of this: its fake runner is
    ``sys.executable`` and inherits ``Popen``'s environment directly, which is the batch
    case and the one that always worked.

    The launcher below models the hop instead: it drops the two variables before doing
    anything else, and then knows only what argv told it. A phase record that lands in
    the right run directory after that could only have got there through ``--settings``.
    """

    def test_a_worker_whose_environment_was_discarded_still_records_its_own_run(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.phases import read_phases

        launcher = write_script(tmp_path / "daemon_hop.py", DAEMON_HOP_CLI)
        runner = build(
            workspace,
            manager,
            make_resolution(
                [sys.executable, str(launcher), "--bg", "{prompt}"], mode=RunnerMode.SESSION
            ),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        directory = workspace / "home" / "runs" / handle.run_id
        (record,) = read_phases(directory)
        assert record["kind"] == "gate_finished"
        assert record["run_id"] == handle.run_id
        assert record["seconds"] == 95.8

    def test_the_identity_is_merged_into_the_settings_argv_already_carried(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """`posture_flags` already puts the permission envelope in `--settings`, and the
        flag is not repeatable. One flag has to reach the launcher, holding both."""
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        argv = cast(
            List[str], RunDirectory(workspace / "home" / "runs" / handle.run_id).read_meta()["argv"]
        )
        assert argv.count("--settings") == 1
        document = json.loads(argv[argv.index("--settings") + 1])
        assert document["env"]["AGENTJOBS_RUN_ID"] == handle.run_id

    def test_the_flag_is_spliced_before_the_prompt(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Where the posture flags go, and for the same reason: a CLI expects its
        options before a positional argument."""
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        argv = cast(
            List[str], RunDirectory(workspace / "home" / "runs" / handle.run_id).read_meta()["argv"]
        )
        prompt_at = max(index for index, element in enumerate(argv) if "task-" in element)
        assert argv.index("--settings") < prompt_at

    def test_a_runners_own_env_travels_with_it(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """This is where the design doc tells operators to put secrets, on the grounds
        that argv is recorded verbatim. Until now it reached a session about once per
        daemon lifetime."""
        runner = build(
            workspace,
            manager,
            session_resolution(fake_cli, env={"AGENT_TOKEN": "sk-live-xyz"}),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        meta = RunDirectory(workspace / "home" / "runs" / handle.run_id).read_meta()
        argv = cast(List[str], meta["argv"])
        document = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        assert document["env"]["AGENT_TOKEN"] == "sk-live-xyz"
        # The property the advice actually claims: not in the thing that gets recorded.
        assert "sk-live-xyz" not in json.dumps(argv)
        assert "sk-live-xyz" not in json.dumps(meta["session_settings"], default=str)
        # ...and the envelope is still readable from the record, which argv no longer
        # carries once the document has gone to a file.
        recorded = cast(Dict[str, Any], meta["session_settings"])
        assert cast(Dict[str, Any], recorded["permissions"])["allow"]

    def test_the_run_records_how_its_identity_was_delivered(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """So a report can tell 'no gate ran' from 'the gate had nowhere to write'."""
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        meta = RunDirectory(workspace / "home" / "runs" / handle.run_id).read_meta()
        assert meta["session_env"] == "delivered"

    def test_whether_this_launch_started_the_daemon_is_recorded(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """Read off the launcher's own banner. Nothing branches on it; it is the
        evidence for whether a run's environment could have been its own."""
        from agentjobs.dispatch.runner import RunDirectory as Directory

        launcher = write_script(tmp_path / "daemon_hop.py", DAEMON_HOP_CLI)
        runner = build(
            workspace,
            manager,
            make_resolution(
                [sys.executable, str(launcher), "--bg", "{prompt}"], mode=RunnerMode.SESSION
            ),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert Directory(workspace / "home" / "runs" / handle.run_id).read_meta()["daemon_started"]

    def test_a_launch_that_joined_a_running_daemon_says_so(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        meta = RunDirectory(workspace / "home" / "runs" / handle.run_id).read_meta()
        assert meta["daemon_started"] is False
