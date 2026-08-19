"""The direct-write matrix, written once and run against every client that ships.

The matrix matters more than any single case. An agent that wants to write a task file
has many ways to try, and a guard that catches ``apply_patch`` while missing
``Set-Content`` is a guard that produces false confidence -- which is worse than no
guard, because people stop looking.

There is one copy of the cases and one copy of the writer tables, and that is the
point. A second table drifts from the first, and the drift shows up as a client that
is quietly less protected than its documentation says.

This module is not collected: it holds no ``Test``-prefixed class of its own. Each
client's test file subclasses `WriteGuardMatrix`, names its adapter, and adds the
tests that really are client-specific -- the wire protocol, the hook registration, and
in Claude's case the fact that an allow prints nothing at all.

Every case runs the shared decision *and* the client's own serialiser, so a case that
passes proves the denial survives into the shape that client actually reads.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "agentjobs" / "hooks"
CODEX_HOOK = HOOKS / "guard_task_yaml.py"
CLAUDE_HOOK = HOOKS / "guard_task_yaml_claude.py"

# The entry points import their neighbour by name, exactly as they do when a client
# launches them by absolute path. Putting the directory on sys.path here means the
# suite exercises that same import rather than a special one it arranged itself.
sys.path.insert(0, str(HOOKS))

import task_write_guard as guard  # noqa: E402


def load_hook(path: Path):
    """Import one entry point as a module, so its serialiser can be called directly."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    """How one client renders a decision, and how to read it back.

    `serialise` returns the text the hook prints, or None when the client's protocol
    says to print nothing.
    """

    def __init__(
        self,
        name: str,
        hook: Path,
        serialise: Callable[[Dict[str, Any]], Optional[str]],
        read_denial: Callable[[Optional[str]], Optional[str]],
    ) -> None:
        self.name = name
        self.hook = hook
        self.serialise = serialise
        self._read_denial = read_denial

    def denial_reason(self, decision: Dict[str, Any]) -> Optional[str]:
        """The refusal text this client would show, or None if it was not a refusal."""
        return self._read_denial(self.serialise(decision))


def _codex_denial(payload: Optional[str]) -> Optional[str]:
    if payload is None:
        return None
    decision = json.loads(payload)
    if decision.get("permission_decision") != "deny":
        return None
    return str(decision["permission_decision_reason"])


def _claude_denial(payload: Optional[str]) -> Optional[str]:
    """Claude prints nothing at all unless it is denying."""
    if payload is None:
        return None
    output = json.loads(payload)["hookSpecificOutput"]
    if output.get("permissionDecision") != "deny":
        return None
    return str(output["permissionDecisionReason"])


CODEX = Client("codex", CODEX_HOOK, load_hook(CODEX_HOOK).serialise, _codex_denial)
CLAUDE = Client("claude", CLAUDE_HOOK, load_hook(CLAUDE_HOOK).serialise, _claude_denial)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Iterator[Dict[str, Any]]:
    """A registered AgentJobs project with one task file, and the guard pointed at it."""
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


class WriteGuardMatrix:
    """Every case, run through one client's decision shape.

    Subclass it, set `CLIENT`, and name the subclass `Test...` so pytest collects it.
    """

    CLIENT: Client

    def refusal(self, project, tool: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Run one event through the guard and this client's serialiser."""
        decision = guard.evaluate(
            {"tool_name": tool, "tool_input": arguments, "cwd": str(project["root"])},
            project["directories"],
            project["root"],
        )
        return self.CLIENT.denial_reason(decision)

    def denied(self, project, tool: str, arguments: Dict[str, Any]) -> bool:
        return self.refusal(project, tool, arguments) is not None

    # -----------------------------------------------------------------------
    # The write matrix
    # -----------------------------------------------------------------------
    @pytest.mark.parametrize(
        "tool",
        [
            "apply_patch",
            "edit",
            "write",
            "create_file",
            "str_replace_editor",
            "multi_edit",
            # Claude's own vocabulary. The guard lowercases the tool name, so these
            # are the same rows -- but a client whose matcher named the wrong case
            # would still register, and only this asserts the decision is reached.
            "Edit",
            "Write",
            "NotebookEdit",
        ],
    )
    def test_a_write_tool_aimed_at_a_task_file_is_denied(self, project, tool):
        assert self.denied(project, tool, {"file_path": str(project["file"])})

    @pytest.mark.parametrize(
        "spelling", ["notebook_edit", "NotebookEdit", "notebookedit", "NOTEBOOK_EDIT"]
    )
    def test_one_tool_is_caught_however_its_client_spells_it(self, project, spelling):
        """Found by running this matrix against Claude for the first time.

        The table was written in Codex's snake_case and matched on a plain lowercase,
        so Claude's `NotebookEdit` folded to `notebookedit` and matched nothing. The
        hook registered the tool and then allowed every write it made -- the precise
        shape of failure that makes a guard worse than none, because the matcher looks
        right in the configuration file.
        """
        assert self.denied(project, spelling, {"file_path": str(project["file"])})

    def test_apply_patch_is_read_out_of_the_patch_body(self, project):
        """apply_patch names its target inside the patch, not in a path argument."""
        patch = (
            "*** Begin Patch\n"
            "*** Update File: tasks/myproject/task-001-work.yaml\n"
            "-lifecycle: ready\n"
            "+lifecycle: active\n"
            "*** End Patch\n"
        )

        assert self.denied(project, "apply_patch", {"patch": patch})

    def test_a_write_tool_aimed_elsewhere_is_allowed(self, project):
        target = str(project["root"] / "src" / "main.py")

        assert not self.denied(project, "write", {"file_path": target})

    def test_a_yaml_outside_the_managed_directory_is_allowed(self, project):
        """The guard covers task records, not every YAML file in the repository."""
        other = project["root"] / "config" / "settings.yaml"

        assert not self.denied(project, "write", {"file_path": str(other)})

    # -----------------------------------------------------------------------
    # One file, many spellings. Each has to resolve to the same managed record.
    # -----------------------------------------------------------------------
    def test_a_relative_path_is_denied(self, project):
        assert self.denied(project, "write", {"file_path": "tasks/myproject/task-001-work.yaml"})

    def test_an_absolute_path_is_denied(self, project):
        assert self.denied(project, "write", {"file_path": str(project["file"].resolve())})

    def test_a_backslash_path_is_denied(self, project):
        assert self.denied(project, "write", {"file_path": "tasks\\myproject\\task-001-work.yaml"})

    def test_a_dot_relative_path_is_denied(self, project):
        assert self.denied(
            project, "write", {"file_path": "./tasks/myproject/../myproject/task-001-work.yaml"}
        )

    @pytest.mark.skipif(os.name != "nt", reason="only Windows treats these as one file")
    def test_a_differently_cased_path_is_denied_on_windows(self, project):
        assert self.denied(project, "write", {"file_path": "Tasks/MyProject/TASK-001-WORK.YAML"})

    def test_a_quoted_path_in_a_command_is_denied(self, project):
        command = f'Set-Content -Path "{project["file"]}" -Value "broken"'

        assert self.denied(project, "powershell", {"command": command})

    # -----------------------------------------------------------------------
    # Shell redirection
    # -----------------------------------------------------------------------
    @pytest.mark.parametrize("operator", [">", ">>"])
    def test_redirection_into_a_task_file_is_denied(self, project, operator):
        command = f"echo lifecycle: active {operator} tasks/myproject/task-001-work.yaml"

        assert self.denied(project, "bash", {"command": command})

    def test_redirection_with_no_space_is_denied(self, project):
        command = "echo x >tasks/myproject/task-001-work.yaml"

        assert self.denied(project, "bash", {"command": command})

    def test_redirection_elsewhere_is_allowed(self, project):
        assert not self.denied(project, "bash", {"command": "echo x > notes.txt"})

    # -----------------------------------------------------------------------
    # PowerShell and POSIX writers
    # -----------------------------------------------------------------------
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
        assert self.denied(project, "powershell", {"command": command})

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
        assert self.denied(project, "bash", {"command": command})

    def test_sed_without_in_place_editing_is_allowed(self, project):
        """`sed` without -i prints; it does not write."""
        command = "sed 's/ready/active/' tasks/myproject/task-001-work.yaml"

        assert not self.denied(project, "bash", {"command": command})

    # -----------------------------------------------------------------------
    # Interpreters
    # -----------------------------------------------------------------------
    @pytest.mark.parametrize(
        "command",
        [
            "python -c \"open('tasks/myproject/task-001-work.yaml','w').write('x')\"",
            "node -e \"require('fs').writeFileSync('tasks/myproject/task-001-work.yaml','x')\"",
            "ruby -e \"File.write('tasks/myproject/task-001-work.yaml','x')\"",
        ],
    )
    def test_an_interpreter_naming_a_task_path_is_denied(self, project, command):
        assert self.denied(project, "bash", {"command": command})

    def test_an_interpreter_not_naming_a_task_path_is_allowed(self, project):
        """Ordinary work must not be blocked; this is the false-positive that matters."""
        assert not self.denied(project, "bash", {"command": "python scripts/build.py"})

    def test_a_script_run_that_mentions_no_task_file_is_allowed(self, project):
        assert not self.denied(project, "bash", {"command": "python -m pytest tests/"})

    def test_an_interpreter_only_reading_a_task_path_is_still_denied(self, project):
        """Deliberate, and the cost is real enough to be worth a test of its own.

        Naming a managed path inside an interpreter one-liner is refused even when the
        one-liner only reads, because the guard cannot tell the two apart from the
        outside. The refusal says exactly how to proceed; the opposite error is a write
        nobody sees. Documented at task_write_guard.evaluate, and the reason a client
        whose shell tool is used heavily will meet more false denials than one whose
        is not.
        """
        command = "python -c \"print(open('tasks/myproject/task-001-work.yaml').read())\""

        assert self.denied(project, "bash", {"command": command})

    # -----------------------------------------------------------------------
    # Compound commands and git
    # -----------------------------------------------------------------------
    def test_a_write_hidden_after_a_read_is_denied(self, project):
        command = "cat tasks/myproject/task-001-work.yaml && rm tasks/myproject/task-001-work.yaml"

        assert self.denied(project, "bash", {"command": command})

    def test_a_write_hidden_after_a_pipe_is_denied(self, project):
        command = "echo x | tee tasks/myproject/task-001-work.yaml"

        assert self.denied(project, "bash", {"command": command})

    def test_a_writing_git_subcommand_is_denied(self, project):
        command = "git checkout -- tasks/myproject/task-001-work.yaml"

        assert self.denied(project, "bash", {"command": command})

    def test_git_diff_is_allowed(self, project):
        command = "git diff tasks/myproject/task-001-work.yaml"

        assert not self.denied(project, "bash", {"command": command})

    # -----------------------------------------------------------------------
    # Reading is never blocked
    # -----------------------------------------------------------------------
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
        assert not self.denied(project, "bash", {"command": command})

    @pytest.mark.parametrize("tool", ["read", "grep", "glob", "view_file", "Read", "Glob"])
    def test_read_tools_are_never_intercepted(self, project, tool):
        assert not self.denied(project, tool, {"file_path": str(project["file"])})

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
        assert not self.denied(project, "bash", {"command": command})

    def test_an_agentjobs_command_naming_a_task_path_is_allowed(self, project):
        command = f"agentjobs validate {project['file']}"

        assert not self.denied(project, "bash", {"command": command})

    # -----------------------------------------------------------------------
    # The denial has to be actionable, in whatever field this client reads
    # -----------------------------------------------------------------------
    def test_it_names_the_file_and_the_managed_directory(self, project):
        reason = self.refusal(project, "write", {"file_path": str(project["file"])})

        assert reason is not None
        assert "task-001-work.yaml" in reason
        assert "myproject" in reason

    def test_it_names_the_managed_tools_to_use_instead(self, project):
        reason = self.refusal(project, "write", {"file_path": str(project["file"])})

        for tool in ("task_claim", "task_handoff", "task_log_append", "task_update_content"):
            assert tool in reason, tool
        assert "projects_list" in reason

    def test_it_says_reading_is_still_allowed(self, project):
        """Otherwise an agent concludes the whole file is off limits and stops."""
        reason = self.refusal(project, "write", {"file_path": str(project["file"])})

        assert "Reading these files is allowed" in reason

    def test_it_names_the_fallback_when_mcp_is_unavailable(self, project):
        reason = self.refusal(project, "write", {"file_path": str(project["file"])})

        assert "agentjobs CLI and REST API" in reason
