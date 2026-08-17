"""The direct-write guard as Claude Code sees it.

The write matrix is the same one Codex runs, imported from `task_write_guard_matrix`
rather than copied -- a second table would drift, and the drift would show up as a
client quietly less protected than its documentation claims. What is here is Claude's
half: its decision envelope, its exit-code contract, and the one behaviour that really
is different from Codex's.

That difference is worth stating up front, because it looks like an omission. **On an
allow this hook prints nothing.** Claude's `permissionDecision: "allow"` does not mean
"no objection", it means "skip the permission system for this call" -- and this hook
matches Edit, Write, NotebookEdit and Bash, so emitting it on every event would
auto-approve nearly everything the session does. Silence is how a hook says it has no
opinion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from task_write_guard_matrix import CLAUDE, CLAUDE_HOOK, HOOKS, WriteGuardMatrix

#: Registers the shared module's `project` fixture without importing its name, which
#: would shadow every test's own parameter.
pytest_plugins = ["task_write_guard_matrix"]

HOOK = CLAUDE_HOOK
SHARED = HOOKS / "task_write_guard.py"
HOOKS_CONFIG = json.loads((HOOKS / "hooks-claude.json").read_text(encoding="utf-8"))
MANIFEST = HOOKS.parents[0] / ".claude-plugin" / "plugin.json"


class TestClaudeWriteMatrix(WriteGuardMatrix):
    """Every shared case, decided and then rendered in Claude's decision envelope."""

    CLIENT = CLAUDE


# ---------------------------------------------------------------------------
# The hook protocol, as Claude Code actually invokes it
# ---------------------------------------------------------------------------
class TestHookProtocol:
    def _run(
        self, event: Dict[str, Any], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event) if isinstance(event, dict) else event,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **(env or {})},
            cwd=cwd,
        )

    def test_a_denial_round_trips_in_claudes_envelope(self, project):
        completed = self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(project["file"])},
                "cwd": str(project["root"]),
            },
            {"AGENTJOBS_HOME": str(project["root"].parent / "home")},
        )

        assert completed.returncode == 0, completed.stderr
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert output["permissionDecision"] == "deny"
        assert output["permissionDecisionReason"]

    def test_an_allowed_call_prints_nothing_at_all(self, project):
        """The behaviour this whole entry point exists to get right.

        A printed `permissionDecision: "allow"` would bypass Claude's permission system
        for every Edit, Write, NotebookEdit and Bash call in the session -- the hook
        matches all four. Exiting 0 silently leaves normal permission handling alone.
        """
        completed = self._run(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "src/main.py"},
                "cwd": str(project["root"]),
            },
            {"AGENTJOBS_HOME": str(project["root"].parent / "home")},
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == ""

    def test_it_never_emits_an_allow_or_an_ask_decision(self, project):
        """Stated separately from the silence test because this is the failure that
        would be invisible: a session where every write is silently pre-approved looks
        exactly like a session where the user trusts the tools."""
        for decision in ({"decision": "allow"}, {"decision": "anything else"}):
            assert CLAUDE.serialise(decision) is None

    def test_malformed_input_allows_rather_than_breaking_the_session(self):
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json at all",
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 0
        assert completed.stdout.strip() == ""

    def test_an_empty_event_allows(self):
        completed = self._run({})

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == ""

    def test_it_never_exits_two(self, project):
        """Claude treats exit 2 as an unconditional block its own JSON cannot override,
        which would make the guard's fail-open behaviour unreachable."""
        events = [
            {"tool_name": "Write", "tool_input": {"file_path": str(project["file"])}},
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
            {},
        ]
        for event in events:
            assert self._run(event).returncode == 0, event

    def test_the_hook_needs_no_third_party_import(self):
        """It runs before every matching tool call and must not depend on an install."""
        for path in (HOOK, SHARED):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("import yaml", "import httpx", "import agentjobs", "import requests"):
                assert forbidden not in source, f"{path.name}: {forbidden}"

    def test_the_entry_point_can_be_launched_from_any_directory(self, tmp_path):
        """`${CLAUDE_PLUGIN_ROOT}` gives an absolute path, not a working directory, so
        the shared-module import has to resolve without one."""
        completed = self._run({}, cwd=str(tmp_path))

        assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# Registration: Claude's own tool vocabulary, not Codex's
# ---------------------------------------------------------------------------
class TestHookRegistration:
    def test_the_matcher_names_claudes_write_capable_tools(self):
        """Codex's matcher is lowercase and names `apply_patch`; none of that reaches
        anything in Claude, where the tools are Edit, Write, NotebookEdit and Bash."""
        matcher = HOOKS_CONFIG["hooks"]["PreToolUse"][0]["matcher"]

        for tool in ("Edit", "Write", "NotebookEdit", "Bash"):
            assert tool in matcher, tool

    def test_the_matcher_does_not_carry_codex_tool_names_over(self):
        matcher = HOOKS_CONFIG["hooks"]["PreToolUse"][0]["matcher"]

        for absent in ("apply_patch", "local_shell", "powershell", "run_command"):
            assert absent not in matcher, absent

    def test_it_registers_the_claude_entry_point_and_not_the_codex_one(self):
        command = HOOKS_CONFIG["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        assert HOOKS_CONFIG["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"
        assert "guard_task_yaml_claude.py" in command

    def test_the_registration_uses_the_plugin_root_variable(self):
        content = (HOOKS / "hooks-claude.json").read_text(encoding="utf-8")

        assert "CLAUDE_PLUGIN_ROOT" in content
        assert "CODEX_PLUGIN_ROOT" not in content
        for marker in ("C:/Users", "C:\\\\Users", "/home/", "/Users/"):
            assert marker not in content, marker

    def test_the_hook_is_bounded(self):
        """Synchronous and on the critical path: it must not hang a session."""
        assert HOOKS_CONFIG["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] <= 30


class TestHonestyAboutLimits:
    def test_the_entry_point_repeats_the_caveat_and_points_at_the_shared_module(self):
        source = Path(HOOK).read_text(encoding="utf-8")

        assert "not a security boundary" in source
        assert "task_write_guard" in source

    def test_it_explains_why_an_allow_is_silence(self):
        """A future reader will otherwise 'fix' the missing allow and hand the session
        a blanket approval for every file-writing tool."""
        source = Path(HOOK).read_text(encoding="utf-8")

        assert "skip the permission system" in source


class TestOnlySerialisationIsForked:
    def test_the_entry_point_holds_no_copy_of_the_decision_tables(self):
        source = HOOK.read_text(encoding="utf-8")

        for table in ("POWERSHELL_WRITERS", "POSIX_WRITERS", "READ_ONLY", "INTERPRETERS"):
            assert f"{table} = " not in source, table

    def test_both_entry_points_import_the_same_shared_module(self):
        for path in (HOOK, HOOKS / "guard_task_yaml.py"):
            assert "from task_write_guard import" in path.read_text(encoding="utf-8"), path.name
