"""The direct-write guard: what it denies, what it allows, and what it cannot see.

The matrix matters more than any single case. An agent that wants to write a task file
has many ways to try, and a guard that catches ``apply_patch`` while missing
``Set-Content`` is a guard that produces false confidence -- which is worse than no
guard, because people stop looking.

Every case drives the real hook the way Codex does: one JSON event on stdin, one JSON
decision on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

HOOK = (
    Path(__file__).resolve().parents[1] / "plugins" / "agentjobs" / "hooks" / "guard_task_yaml.py"
)


def load_guard():
    """Import the hook as a module, so cases can call evaluate() directly."""
    spec = importlib.util.spec_from_file_location("guard_task_yaml", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


guard = load_guard()


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Iterator[Dict[str, Any]]:
    """A registered AgentJobs project with one task file, and the hook pointed at it."""
    root = tmp_path / "myproject"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        "project_name: My Project\ntasks_directory: tasks/myproject\n", encoding="utf-8"
    )
    tasks = root / "tasks" / "myproject"
    tasks.mkdir(parents=True)
    task_file = tasks / "task-001-work.yaml"
    task_file.write_text("schema: 2\nid: task-001-work\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    (home / "projects.yaml").write_text(
        f"projects:\n  - id: myproject\n    root: {root}\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv("AGENTJOBS_TASKS_DIR", raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)

    yield {
        "root": root,
        "tasks": tasks,
        "file": task_file,
        "directories": guard.managed_directories(),
    }


def decide(project, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Run one event through the guard."""
    return guard.evaluate(
        {"tool_name": tool, "tool_input": arguments, "cwd": str(project["root"])},
        project["directories"],
        project["root"],
    )


def denied(result: Dict[str, Any]) -> bool:
    """Whether the guard refused."""
    return result["permission_decision"] == "deny"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class TestManagedDirectories:
    def test_it_finds_the_directory_from_the_registry_and_project_config(self, project):
        assert any(
            guard._canonical(item) == guard._canonical(project["tasks"])
            for item in project["directories"]
        )

    def test_an_environment_pinned_single_project_is_covered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "solo-tasks"))
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "empty-home"))

        directories = guard.managed_directories()

        assert any("solo-tasks" in str(item) for item in directories)

    def test_a_missing_registry_is_not_an_error(self, tmp_path, monkeypatch):
        """A machine with no AgentJobs installed must not break every tool call."""
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "nothing"))
        monkeypatch.delenv("AGENTJOBS_TASKS_DIR", raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)

        assert guard.managed_directories() == []


# ---------------------------------------------------------------------------
# ac-1: the write matrix
# ---------------------------------------------------------------------------
class TestFileWriteTools:
    @pytest.mark.parametrize(
        "tool", ["apply_patch", "edit", "write", "create_file", "str_replace_editor", "multi_edit"]
    )
    def test_a_write_tool_aimed_at_a_task_file_is_denied(self, project, tool):
        result = decide(project, tool, {"file_path": str(project["file"])})

        assert denied(result)

    def test_apply_patch_is_read_out_of_the_patch_body(self, project):
        """apply_patch names its target inside the patch, not in a path argument."""
        patch = (
            "*** Begin Patch\n"
            "*** Update File: tasks/myproject/task-001-work.yaml\n"
            "-lifecycle: ready\n"
            "+lifecycle: active\n"
            "*** End Patch\n"
        )

        assert denied(decide(project, "apply_patch", {"patch": patch}))

    def test_a_write_tool_aimed_elsewhere_is_allowed(self, project):
        result = decide(project, "write", {"file_path": str(project["root"] / "src" / "main.py")})

        assert not denied(result)

    def test_a_yaml_outside_the_managed_directory_is_allowed(self, project):
        """The guard covers task records, not every YAML file in the repository."""
        other = project["root"] / "config" / "settings.yaml"

        assert not denied(decide(project, "write", {"file_path": str(other)}))


class TestPathVariants:
    """One file, many spellings. Each has to resolve to the same managed record."""

    def test_a_relative_path_is_denied(self, project):
        assert denied(decide(project, "write", {"file_path": "tasks/myproject/task-001-work.yaml"}))

    def test_an_absolute_path_is_denied(self, project):
        assert denied(decide(project, "write", {"file_path": str(project["file"].resolve())}))

    def test_a_backslash_path_is_denied(self, project):
        assert denied(
            decide(project, "write", {"file_path": "tasks\\myproject\\task-001-work.yaml"})
        )

    def test_a_dot_relative_path_is_denied(self, project):
        assert denied(
            decide(
                project, "write", {"file_path": "./tasks/myproject/../myproject/task-001-work.yaml"}
            )
        )

    @pytest.mark.skipif(os.name != "nt", reason="only Windows treats these as one file")
    def test_a_differently_cased_path_is_denied_on_windows(self, project):
        assert denied(decide(project, "write", {"file_path": "Tasks/MyProject/TASK-001-WORK.YAML"}))

    def test_a_quoted_path_in_a_command_is_denied(self, project):
        command = f'Set-Content -Path "{project["file"]}" -Value "broken"'

        assert denied(decide(project, "powershell", {"command": command}))


class TestShellRedirection:
    @pytest.mark.parametrize("operator", [">", ">>"])
    def test_redirection_into_a_task_file_is_denied(self, project, operator):
        command = f"echo lifecycle: active {operator} tasks/myproject/task-001-work.yaml"

        assert denied(decide(project, "bash", {"command": command}))

    def test_redirection_with_no_space_is_denied(self, project):
        command = "echo x >tasks/myproject/task-001-work.yaml"

        assert denied(decide(project, "bash", {"command": command}))

    def test_redirection_elsewhere_is_allowed(self, project):
        assert not denied(decide(project, "bash", {"command": "echo x > notes.txt"}))


class TestPowerShellWriters:
    @pytest.mark.parametrize(
        "command",
        [
            "Set-Content -Path tasks/myproject/task-001-work.yaml -Value x",
            "Add-Content tasks/myproject/task-001-work.yaml 'x'",
            "'x' | Out-File tasks/myproject/task-001-work.yaml",
            "New-Item -Path tasks/myproject/task-001-work.yaml -ItemType File",
            "Remove-Item tasks/myproject/task-001-work.yaml",
            "Move-Item tasks/myproject/task-001-work.yaml other.yaml",
            "Copy-Item other.yaml tasks/myproject/task-001-work.yaml",
            "Rename-Item tasks/myproject/task-001-work.yaml new.yaml",
            "Clear-Content tasks/myproject/task-001-work.yaml",
            "sc tasks/myproject/task-001-work.yaml x",
            "ri tasks/myproject/task-001-work.yaml",
        ],
    )
    def test_powershell_writers_and_aliases_are_denied(self, project, command):
        assert denied(decide(project, "powershell", {"command": command}))


class TestPosixWriters:
    @pytest.mark.parametrize(
        "command",
        [
            "echo x | tee tasks/myproject/task-001-work.yaml",
            "sed -i 's/ready/active/' tasks/myproject/task-001-work.yaml",
            "sed -i.bak s/a/b/ tasks/myproject/task-001-work.yaml",
            "mv tasks/myproject/task-001-work.yaml /tmp/x.yaml",
            "cp other.yaml tasks/myproject/task-001-work.yaml",
            "rm tasks/myproject/task-001-work.yaml",
            "rm -f tasks/myproject/task-001-work.yaml",
            "truncate -s 0 tasks/myproject/task-001-work.yaml",
            "touch tasks/myproject/task-001-work.yaml",
        ],
    )
    def test_posix_writers_are_denied(self, project, command):
        assert denied(decide(project, "bash", {"command": command}))

    def test_sed_without_in_place_editing_is_allowed(self, project):
        """`sed` without -i prints; it does not write."""
        command = "sed 's/ready/active/' tasks/myproject/task-001-work.yaml"

        assert not denied(decide(project, "bash", {"command": command}))


class TestInterpreters:
    @pytest.mark.parametrize(
        "command",
        [
            "python -c \"open('tasks/myproject/task-001-work.yaml','w').write('x')\"",
            "node -e \"require('fs').writeFileSync('tasks/myproject/task-001-work.yaml','x')\"",
            "ruby -e \"File.write('tasks/myproject/task-001-work.yaml','x')\"",
        ],
    )
    def test_an_interpreter_naming_a_task_path_is_denied(self, project, command):
        assert denied(decide(project, "bash", {"command": command}))

    def test_an_interpreter_not_naming_a_task_path_is_allowed(self, project):
        """Ordinary work must not be blocked; this is the false-positive that matters."""
        assert not denied(decide(project, "bash", {"command": "python scripts/build.py"}))

    def test_a_script_run_that_mentions_no_task_file_is_allowed(self, project):
        assert not denied(decide(project, "bash", {"command": "python -m pytest tests/"}))


class TestCompoundCommands:
    def test_a_write_hidden_after_a_read_is_denied(self, project):
        command = "cat tasks/myproject/task-001-work.yaml && rm tasks/myproject/task-001-work.yaml"

        assert denied(decide(project, "bash", {"command": command}))

    def test_a_write_hidden_after_a_pipe_is_denied(self, project):
        command = "echo x | tee tasks/myproject/task-001-work.yaml"

        assert denied(decide(project, "bash", {"command": command}))


class TestGit:
    def test_a_writing_git_subcommand_is_denied(self, project):
        command = "git checkout -- tasks/myproject/task-001-work.yaml"

        assert denied(decide(project, "bash", {"command": command}))

    def test_git_diff_is_allowed(self, project):
        assert not denied(
            decide(project, "bash", {"command": "git diff tasks/myproject/task-001-work.yaml"})
        )


# ---------------------------------------------------------------------------
# ac-2: reading is never blocked
# ---------------------------------------------------------------------------
class TestReadsAreAllowed:
    @pytest.mark.parametrize(
        "command",
        [
            "cat tasks/myproject/task-001-work.yaml",
            "Get-Content tasks/myproject/task-001-work.yaml",
            "gc tasks/myproject/task-001-work.yaml",
            "rg lifecycle tasks/myproject/task-001-work.yaml",
            "grep -n ball tasks/myproject/task-001-work.yaml",
            "head -20 tasks/myproject/task-001-work.yaml",
            "Select-String ready tasks/myproject/task-001-work.yaml",
            "diff tasks/myproject/task-001-work.yaml other.yaml",
        ],
    )
    def test_reading_a_task_file_is_allowed(self, project, command):
        """Reviewing a task means opening it. Blocking that would break review."""
        assert not denied(decide(project, "bash", {"command": command}))

    @pytest.mark.parametrize("tool", ["read", "grep", "glob", "view_file"])
    def test_read_tools_are_never_intercepted(self, project, tool):
        assert not denied(decide(project, tool, {"file_path": str(project["file"])}))

    @pytest.mark.parametrize(
        "command",
        [
            "agentjobs list",
            "agentjobs show task-001-work",
            "agentjobs serve",
            "agentjobs mcp",
        ],
    )
    def test_agentjobs_commands_are_allowed(self, project, command):
        assert not denied(decide(project, "bash", {"command": command}))

    def test_an_agentjobs_command_naming_a_task_path_is_allowed(self, project):
        command = f"agentjobs validate {project['file']}"

        assert not denied(decide(project, "bash", {"command": command}))


# ---------------------------------------------------------------------------
# ac-3: the denial has to be actionable
# ---------------------------------------------------------------------------
class TestDenialMessage:
    def test_it_names_the_file_and_the_managed_directory(self, project):
        result = decide(project, "write", {"file_path": str(project["file"])})

        reason = result["permission_decision_reason"]
        assert "task-001-work.yaml" in reason
        assert "myproject" in reason

    def test_it_names_the_managed_tools_to_use_instead(self, project):
        reason = decide(project, "write", {"file_path": str(project["file"])})[
            "permission_decision_reason"
        ]

        for tool in ("task_claim", "task_handoff", "task_log_append", "task_update_content"):
            assert tool in reason, tool
        assert "projects_list" in reason

    def test_it_says_reading_is_still_allowed(self, project):
        """Otherwise an agent concludes the whole file is off limits and stops."""
        reason = decide(project, "write", {"file_path": str(project["file"])})[
            "permission_decision_reason"
        ]

        assert "Reading these files is allowed" in reason

    def test_it_names_the_fallback_when_mcp_is_unavailable(self, project):
        reason = decide(project, "write", {"file_path": str(project["file"])})[
            "permission_decision_reason"
        ]

        assert "agentjobs CLI and REST API" in reason


# ---------------------------------------------------------------------------
# The hook protocol, as Codex actually invokes it
# ---------------------------------------------------------------------------
class TestHookProtocol:
    def _run(self, event: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **env},
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def test_a_denial_round_trips_through_stdin_and_stdout(self, project):
        decision = self._run(
            {
                "tool_name": "write",
                "tool_input": {"file_path": str(project["file"])},
                "cwd": str(project["root"]),
            },
            {"AGENTJOBS_HOME": str(project["root"].parent / "home")},
        )

        assert decision["permission_decision"] == "deny"
        assert decision["permission_decision_reason"]

    def test_an_allowed_call_round_trips(self, project):
        decision = self._run(
            {
                "tool_name": "write",
                "tool_input": {"file_path": "src/main.py"},
                "cwd": str(project["root"]),
            },
            {"AGENTJOBS_HOME": str(project["root"].parent / "home")},
        )

        assert decision["permission_decision"] == "allow"

    def test_malformed_input_allows_rather_than_breaking_the_session(self, project):
        """A guard that fails closed on an event shape it does not know is worse than
        the accident it prevents."""
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json at all",
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 0
        assert json.loads(completed.stdout)["permission_decision"] == "allow"

    def test_an_empty_event_allows(self, project):
        assert self._run({}, {})["permission_decision"] == "allow"

    def test_the_hook_needs_no_third_party_import(self):
        """It runs before every matching tool call and must not depend on an install."""
        source = HOOK.read_text(encoding="utf-8")

        for forbidden in ("import yaml", "import httpx", "import agentjobs", "import requests"):
            assert forbidden not in source, forbidden


class TestHookRegistration:
    def test_the_plugin_registers_the_hook_for_the_write_tools(self):
        config = json.loads((HOOK.parent / "hooks.json").read_text(encoding="utf-8"))

        entry = config["hooks"]["PreToolUse"][0]
        for tool in ("apply_patch", "bash", "powershell", "write"):
            assert tool in entry["matcher"], tool
        assert entry["hooks"][0]["type"] == "command"
        assert "guard_task_yaml.py" in entry["hooks"][0]["command"]

    def test_the_registration_uses_no_machine_specific_path(self):
        content = (HOOK.parent / "hooks.json").read_text(encoding="utf-8")

        assert "CODEX_PLUGIN_ROOT" in content
        for marker in ("C:/Users", "C:\\\\Users", "/home/", "/Users/"):
            assert marker not in content, marker

    def test_the_hook_is_bounded(self):
        """Synchronous and on the critical path: it must not hang a session."""
        config = json.loads((HOOK.parent / "hooks.json").read_text(encoding="utf-8"))

        assert config["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] <= 30


class TestHonestyAboutLimits:
    def test_the_hook_documents_that_it_is_not_a_security_boundary(self):
        source = HOOK.read_text(encoding="utf-8")

        assert "not a security boundary" in source
        for bypass in ("disabled", "obfuscated", "hosted"):
            assert bypass in source, bypass
