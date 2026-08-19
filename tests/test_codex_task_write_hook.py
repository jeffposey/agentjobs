"""The direct-write guard as Codex sees it.

The write matrix itself lives in `task_write_guard_matrix` and is shared with the
Claude entry point, because the cases are about the guard and not about either client.
What is here is Codex's half: its wire protocol, its hook registration, and the honesty
the shipped files owe a reader.

Discovery of the managed directories is also asserted here, once. It belongs to the
shared module rather than to either client, and running it twice would prove nothing
the second time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from task_write_guard_matrix import CODEX, CODEX_HOOK, HOOKS, WriteGuardMatrix, guard

#: Registers the shared module's `project` fixture without importing its name, which
#: would shadow every test's own parameter.
pytest_plugins = ["task_write_guard_matrix"]

HOOK = CODEX_HOOK
SHARED = HOOKS / "task_write_guard.py"


class TestCodexWriteMatrix(WriteGuardMatrix):
    """Every shared case, decided and then rendered in Codex's decision shape."""

    CLIENT = CODEX


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
        decision: Dict[str, Any] = json.loads(completed.stdout)
        return decision

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
        """Codex expects a decision on every event, so an allow is stated, not implied."""
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
        for path in (HOOK, SHARED):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("import yaml", "import httpx", "import agentjobs", "import requests"):
                assert forbidden not in source, f"{path.name}: {forbidden}"

    def test_the_entry_point_can_be_launched_from_any_directory(self, tmp_path):
        """It imports its neighbour by name, so a client launching it by absolute path
        from an unrelated working directory must still resolve the shared module."""
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input="{}",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
        )

        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["permission_decision"] == "allow"


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

    def test_the_registration_still_names_the_original_entry_point(self):
        """Its path is what an installed Codex configuration already points at. The
        Claude entry point was added beside it rather than renaming this one."""
        assert HOOK.name == "guard_task_yaml.py"
        assert HOOK.exists()

    def test_the_hook_is_bounded(self):
        """Synchronous and on the critical path: it must not hang a session."""
        config = json.loads((HOOK.parent / "hooks.json").read_text(encoding="utf-8"))

        assert config["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] <= 30


class TestHonestyAboutLimits:
    def test_the_shared_module_documents_that_it_is_not_a_security_boundary(self):
        source = SHARED.read_text(encoding="utf-8")

        assert "not a security boundary" in source
        for bypass in ("disabled", "obfuscated", "hosted"):
            assert bypass in source, bypass

    def test_the_entry_point_repeats_the_caveat_and_points_at_the_shared_module(self):
        """Whoever is asked to review and trust a hook opens the file the config names,
        which is this one. It cannot be the only file without the caveat."""
        source = Path(HOOK).read_text(encoding="utf-8")

        assert "not a security boundary" in source
        assert "task_write_guard" in source


class TestOnlySerialisationIsForked:
    """The constraint the whole split exists to satisfy."""

    def test_the_entry_point_holds_no_copy_of_the_decision_tables(self):
        source = HOOK.read_text(encoding="utf-8")

        for table in ("POWERSHELL_WRITERS", "POSIX_WRITERS", "READ_ONLY", "INTERPRETERS"):
            assert f"{table} = " not in source, table

    def test_the_shared_module_names_no_client_protocol_field(self):
        """If a client's field name appears here, the neutral shape has leaked."""
        source = SHARED.read_text(encoding="utf-8")

        for field in ("permission_decision", "hookSpecificOutput", "permissionDecision"):
            assert field not in source, field
