#!/usr/bin/env python3
"""Decide whether a tool call would write AgentJobs task YAML.

This module is the whole decision: which tools write, which shell commands write,
which paths are managed, and what an agent is told instead. It is client-agnostic and
holds no protocol. The thin entry points beside it — `guard_task_yaml.py` for Codex,
`guard_task_yaml_claude.py` for Claude Code — read one event, call `evaluate`, and
serialise the answer in their own client's shape.

The split exists because the alternative is two copies of the writer tables, and two
copies disagree. A guard that catches `apply_patch` in one client and misses
`Set-Content` in the other produces false confidence, which is worse than no guard,
because people stop looking.

**This is a guardrail, not a security boundary, and the difference matters.** It sees
the tool calls a client routes through it and nothing else: a hosted or specialised
tool it never observes, a hook the user has disabled or not trusted, an obfuscated
script, a path assembled at runtime from variables, or any process started outside the
client will all get past it. Its job is to catch the realistic accident — an agent
reaching for `apply_patch` or `Edit` because that is the tool it knows — and turn it
into a sentence naming the managed tools instead. Treating it as enforcement would be
a mistake that the layers in task-118 exist to cover.

Reading task files is deliberately untouched. Reviewing a task means opening it.

Deliberately dependency-free and offline: it runs before every matching tool call, so
it has to be fast, and it must never fail because a package moved.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def _normalise_tool(name: str) -> str:
    """Fold a tool name to the form the tables below are keyed on.

    Clients spell the same tool differently and the difference is not cosmetic: Codex
    sends `notebook_edit`, Claude sends `NotebookEdit`, and lowercasing alone leaves
    `notebookedit` matching neither. That gap was real -- the Claude matcher registered
    the tool and the guard then allowed every write it made. Dropping separators as
    well as case closes it for every tool at once, rather than one entry at a time as
    each client's spelling is discovered.
    """
    return name.replace("_", "").replace("-", "").replace(" ", "").lower()


def _tool_set(*names: str) -> Set[str]:
    """Build a lookup keyed on normalised names, written out in a readable spelling."""
    return {_normalise_tool(name) for name in names}


#: Tools whose whole purpose is writing a file. Any of these aimed at a managed task
#: file is denied outright. Both clients' vocabularies are here.
FILE_WRITE_TOOLS = _tool_set(
    "apply_patch",
    "edit",
    "write",
    "create_file",
    "str_replace_editor",
    "notebook_edit",
    "multi_edit",
)

#: Shell-ish tools whose command string has to be inspected.
SHELL_TOOLS = _tool_set("bash", "shell", "powershell", "pwsh", "cmd", "run_command", "local_shell")

#: PowerShell commands and aliases that write, move or delete a file.
POWERSHELL_WRITERS = {
    "set-content",
    "sc",
    "add-content",
    "ac",
    "out-file",
    "new-item",
    "ni",
    "remove-item",
    "ri",
    "rd",
    "erase",
    "del",
    "move-item",
    "mi",
    "mv",
    "move",
    "copy-item",
    "ci",
    "cp",
    "copy",
    "rename-item",
    "rni",
    "ren",
    "clear-content",
    "clc",
    "tee-object",
    "tee",
    "export-csv",
    "convertto-json",
}

#: POSIX writers, movers and deleters.
POSIX_WRITERS = {
    "tee",
    "mv",
    "cp",
    "rm",
    "truncate",
    "install",
    "dd",
    "shred",
    "ln",
    "touch",
    "chmod",
    "chown",
}

#: Interpreters. Denied only when the command also names a managed task path -- a bare
#: `python script.py` is ordinary work and must not be blocked.
INTERPRETERS = {
    "python",
    "python3",
    "py",
    "node",
    "deno",
    "bun",
    "ruby",
    "perl",
    "php",
    "pwsh",
    "powershell",
    "bash",
    "sh",
    "zsh",
    "awk",
    "gawk",
}

#: Read-only commands. Named explicitly so that naming a task path in one of them is
#: never mistaken for a write.
READ_ONLY = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "type",
    "get-content",
    "gc",
    "select-string",
    "sls",
    "rg",
    "grep",
    "ag",
    "ack",
    "find",
    "findstr",
    "ls",
    "dir",
    "get-childitem",
    "gci",
    "wc",
    "diff",
    "stat",
    "file",
    "test-path",
    "sort",
    "uniq",
    "cut",
    "jq",
    "yq",
}

#: `sed` writes only with -i; `git` writes only with a handful of subcommands.
SED = {"sed", "gsed"}
GIT_WRITE_SUBCOMMANDS = {"apply", "checkout", "restore", "clean", "stash", "mv", "rm"}

#: The managed operations. A command that is itself an AgentJobs call is always fine,
#: whatever paths it mentions.
AGENTJOBS_COMMANDS = {"agentjobs"}

_REDIRECT = re.compile(r"(?:^|\s)\d*>{1,2}(?:\s*|&\d\s*)([^\s;|&]+)")

#: A task-file path anywhere in a command string, quotes and separators included, so
#: one buried inside an interpreter's argument is still seen.
_YAML_PATH = re.compile(r"[A-Za-z0-9_.:~+@$%()\\/-]+\.ya?ml\b")


class Decision:
    """The two answers this module gives.

    The strings are the ones both clients happen to use, but the decision returned by
    `evaluate` is deliberately *not* either client's payload -- see `evaluate`.
    """

    ALLOW = "allow"
    DENY = "deny"


def managed_directories(env: Optional[Dict[str, str]] = None) -> List[Path]:
    """Every task directory AgentJobs is configured to manage on this machine.

    Read from the machine registry and, when set, the environment overrides that pin
    a single-project install. Parsed by hand rather than by importing AgentJobs: the
    hook must work when the package is not importable from the client process, and it
    must not pay an import cost on every tool call.
    """
    environ = os.environ if env is None else env
    directories: List[Path] = []

    tasks_dir = environ.get("AGENTJOBS_TASKS_DIR")
    if tasks_dir:
        directories.append(Path(tasks_dir))

    project_root = environ.get("AGENTJOBS_PROJECT_ROOT")
    if project_root:
        directories.extend(_directories_from_project(Path(project_root)))

    home = environ.get("AGENTJOBS_HOME")
    registry = (
        Path(home) / "projects.yaml" if home else Path.home() / ".agentjobs" / "projects.yaml"
    )
    for root in _registry_roots(registry):
        directories.extend(_directories_from_project(root))

    resolved: List[Path] = []
    seen: Set[str] = set()
    for directory in directories:
        key = _canonical(directory)
        if key and key not in seen:
            seen.add(key)
            resolved.append(directory)
    return resolved


def _registry_roots(registry: Path) -> List[Path]:
    """Project roots from the registry file, read with a minimal YAML scan.

    Only ``root:`` lines are needed, so this looks for them rather than pulling in a
    YAML parser the client's environment may not have.
    """
    try:
        content = registry.read_text(encoding="utf-8")
    except OSError:
        return []
    roots: List[Path] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("root:"):
            value = stripped[len("root:") :].strip().strip("'\"")
            if value:
                roots.append(Path(value))
    return roots


def _directories_from_project(root: Path) -> List[Path]:
    """A project's tasks directory, from its config or the default."""
    config = root / ".agentjobs" / "config.yaml"
    relative = "tasks"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("tasks_directory:"):
                value = stripped[len("tasks_directory:") :].strip().strip("'\"")
                if value:
                    relative = value
                break
    except OSError:
        pass
    candidate = Path(relative)
    return [candidate if candidate.is_absolute() else root / candidate]


def _canonical(path: Path) -> str:
    """A comparable form of a path: absolute, separator- and case-normalised.

    Case folding is applied on Windows only, where ``Tasks\\Task-001.YAML`` and
    ``tasks/task-001.yaml`` are the same file and a case-sensitive comparison would
    wave the first one through.
    """
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - unresolvable path
        resolved = path
    text = str(resolved).replace("\\", "/").rstrip("/")
    return text.casefold() if os.name == "nt" else text


def is_managed_task_file(candidate: str, directories: Sequence[Path], cwd: Path) -> Optional[Path]:
    """Return the managed directory owning this path, or None.

    Only ``*.yaml`` counts: the directories hold task records, and a note or a script
    someone keeps beside them is not this hook's business.
    """
    text = candidate.strip().strip("\"'")
    if not text or not text.lower().endswith((".yaml", ".yml")):
        return None
    path = Path(text)
    absolute = path if path.is_absolute() else cwd / path
    target = _canonical(absolute)
    for directory in directories:
        prefix = _canonical(directory)
        if prefix and (target == prefix or target.startswith(prefix + "/")):
            return directory
    return None


def _tokens(command: str) -> List[str]:
    """Split a command, tolerating quoting styles shlex cannot parse."""
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _words(command: str) -> List[str]:
    """Lowercased leading words of each segment of a compound command."""
    segments = re.split(r"[;&|]{1,2}|\n", command)
    leads = []
    for segment in segments:
        parts = _tokens(segment)
        if parts:
            leads.append(parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower())
    return leads


def _candidate_paths(command: str) -> List[str]:
    """Every task-file path a command mentions, wherever it is hiding.

    Scanned with a regex over the whole command rather than by tokenising it. Two
    real escapes made that necessary: an interpreter one-liner keeps the path inside
    a quoted string, so the token is ``open('tasks/x.yaml','w')`` and never ends in
    ``.yaml``; and a quoted Windows path tokenises with its quotes attached on
    Windows, so it does not either. Both walked straight past a token-based scan.
    """
    candidates = [match.group(0) for match in _YAML_PATH.finditer(command)]
    candidates.extend(match.group(1) for match in _REDIRECT.finditer(command))
    return [candidate.strip("\"'") for candidate in candidates]


def _is_write_command(command: str) -> Tuple[bool, str]:
    """Whether a shell command would write, and why we think so."""
    lowered = command.lower()
    leads = _words(command)

    if _REDIRECT.search(command):
        return True, "shell redirection"
    for lead in leads:
        if lead in AGENTJOBS_COMMANDS:
            continue
        if lead in POWERSHELL_WRITERS:
            return True, f"PowerShell writer `{lead}`"
        if lead in POSIX_WRITERS:
            return True, f"`{lead}`"
        if lead in SED and re.search(r"(?:^|\s)-[a-z]*i", lowered):
            return True, "`sed -i`"
        if lead == "git":
            tokens = [token.lower() for token in _tokens(command)]
            if any(sub in tokens for sub in GIT_WRITE_SUBCOMMANDS):
                return True, "a writing git subcommand"
    return False, ""


def _is_read_only_command(command: str) -> bool:
    """Whether every segment of a command is a known read-only operation."""
    leads = _words(command)
    if not leads:
        return False
    if _REDIRECT.search(command):
        return False
    return all(lead in READ_ONLY or lead in AGENTJOBS_COMMANDS for lead in leads)


def _denial(paths: Iterable[str], directory: Path, reason: str) -> str:
    """The message an agent receives instead of the write."""
    named = ", ".join(sorted(set(paths)))
    return (
        f"Refused: {reason} would write AgentJobs task records ({named}) in the "
        f"managed directory {directory}.\n\n"
        "Task YAML is generated state. Use the AgentJobs MCP tools instead: "
        "task_create_ready or task_create_draft to add work, task_claim, "
        "task_handoff, task_release and task_close to move it, task_log_append to "
        "record progress, decisions and questions, and task_update_content to edit "
        "the spec. Call projects_list first for the project id and actor.\n\n"
        "If MCP is unavailable, the agentjobs CLI and REST API reach the same "
        "validated path. Reading these files is allowed; only writing is managed."
    )


def evaluate(event: Dict[str, Any], directories: Sequence[Path], cwd: Path) -> Dict[str, Any]:
    """Decide one PreToolUse event, in a shape neither client speaks.

    Returns ``{"decision": "allow"}`` or ``{"decision": "deny", "reason": ...}``. The
    neutral shape is the whole point of the split: if this returned one client's
    payload, the other client's entry point would be translating between two protocols
    instead of adding one, and the shape everybody shares would quietly become that
    client's.

    The event itself needs no translation. Both clients send ``tool_name`` and
    ``tool_input``; Codex's ``tool``/``arguments`` are read as a fallback.
    """
    tool = str(event.get("tool_name") or event.get("tool") or "").strip()
    arguments = event.get("tool_input") or event.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    normalised = _normalise_tool(tool)

    if normalised in FILE_WRITE_TOOLS:
        targets = [
            str(value)
            for key, value in arguments.items()
            if key in {"file_path", "path", "filename", "target_file", "notebook_path"}
            and isinstance(value, str)
        ]
        # apply_patch carries its paths inside the patch body.
        patch = arguments.get("patch") or arguments.get("input") or arguments.get("content")
        if isinstance(patch, str):
            targets.extend(re.findall(r"[^\s\"']+\.ya?ml", patch))
        hits = [(target, is_managed_task_file(target, directories, cwd)) for target in targets]
        managed = [(target, owner) for target, owner in hits if owner is not None]
        if managed:
            return _deny([target for target, _ in managed], managed[0][1], f"the {tool} tool")
        return {"decision": Decision.ALLOW}

    if normalised in SHELL_TOOLS or "command" in arguments:
        command = arguments.get("command") or arguments.get("cmd") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = str(command)
        if not command.strip():
            return {"decision": Decision.ALLOW}

        candidates = _candidate_paths(command)
        managed = [
            (candidate, is_managed_task_file(candidate, directories, cwd))
            for candidate in candidates
        ]
        managed = [(path, owner) for path, owner in managed if owner is not None]
        if not managed:
            return {"decision": Decision.ALLOW}
        if _is_read_only_command(command):
            return {"decision": Decision.ALLOW}

        writes, reason = _is_write_command(command)
        if writes:
            return _deny([path for path, _ in managed], managed[0][1], reason)

        leads = _words(command)
        if any(lead in INTERPRETERS for lead in leads):
            # Conservative by design: an interpreter that explicitly names a managed
            # task file is denied even though it might only be reading it. The cost is
            # a false denial that says exactly how to proceed; the cost of the other
            # error is an unlogged write nobody notices.
            return _deny(
                [path for path, _ in managed],
                managed[0][1],
                "an interpreter invoked against a managed task path",
            )

    return {"decision": Decision.ALLOW}


def _deny(paths: Iterable[str], directory: Optional[Path], reason: str) -> Dict[str, Any]:
    """Build the deny decision."""
    return {
        "decision": Decision.DENY,
        "reason": _denial(paths, directory or Path("."), reason),
    }


def run(serialise: Callable[[Dict[str, Any]], Optional[str]]) -> int:
    """Read one event from stdin, decide, and print what `serialise` returns.

    Shared by both entry points because the failure handling is the part that must not
    differ. Any internal failure allows the call: a guardrail that breaks tool use when
    it meets an event shape it does not recognise is worse than the accident it
    prevents, and the layers in task-118 catch what slips through.

    `serialise` returning None means "print nothing", which is how a client signals it
    has no opinion and normal permission handling should continue.
    """
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return _emit(serialise, {"decision": Decision.ALLOW})
    try:
        cwd = Path(str(event.get("cwd") or Path.cwd()))
        decision = evaluate(event, managed_directories(), cwd)
    except Exception as exc:  # noqa: BLE001 - never break the session
        print(f"agentjobs guard: allowing after an internal error: {exc}", file=sys.stderr)
        decision = {"decision": Decision.ALLOW}
    return _emit(serialise, decision)


def _emit(serialise: Callable[[Dict[str, Any]], Optional[str]], decision: Dict[str, Any]) -> int:
    """Print a serialised decision, if the client has one to make."""
    payload = serialise(decision)
    if payload is not None:
        print(payload)
    return 0
