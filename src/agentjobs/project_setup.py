"""Shared, non-interactive setup for a new AgentJobs project."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .projects import CONFIG_RELATIVE, ProjectError

MCP_CONFIG_FILENAME = ".mcp.json"
"""Claude Code's project-scoped MCP server file. Its keys are what dispatch approves."""

MCP_SERVER_NAME = "agentjobs"
"""The server name written into that file, and the name dispatch pre-approves."""

DEFAULT_CONFIG: Dict[str, Any] = {
    "project_name": "AgentJobs Project",
    "tasks_directory": "tasks",
    "prompts_directory": "prompts",
    "gui": {"host": "localhost", "port": 8765, "theme": "dark"},
    "actors": [
        {"name": "claude", "kind": "agent", "display_name": "Claude (Lead Engineer)"},
        {"name": "codex", "kind": "agent", "display_name": "Codex (Workhorse)"},
    ],
    "default_user": None,
    "categories": [
        "infrastructure",
        "strategy_development",
        "validation",
        "documentation",
    ],
    "defaults": {"priority": "medium", "lifecycle": "draft"},
}


def build_project_config(
    *,
    project_name: str,
    tasks_directory: str = "tasks",
    prompts_directory: str = "prompts",
    port: int = 8765,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the initial config used by both interactive and web setup."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project_name"] = project_name
    config["tasks_directory"] = tasks_directory
    config["prompts_directory"] = prompts_directory
    config["gui"]["port"] = port
    if user:
        config["actors"] = list(config["actors"]) + [
            {"name": user, "kind": "human", "display_name": user}
        ]
        config["default_user"] = user
    return config


def _directory_within(root: Path, value: str, *, field: str) -> Path:
    """Resolve a configured directory while keeping web-triggered writes in root."""
    configured = Path(value)
    if configured.is_absolute():
        raise ProjectError(f"{field} must be a relative path inside the project directory.")
    resolved = (root / configured).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f"{field} must stay inside the project directory.") from exc
    return resolved


def initialize_project(
    root: Path,
    config: Dict[str, Any],
    *,
    contain_directories: bool = False,
) -> Path:
    """Write a new config and tasks directory, never replacing an existing config.

    The root itself must already exist. Web callers enable ``contain_directories`` so
    configured directories cannot turn one approved root into writes elsewhere. The
    CLI retains support for existing absolute-directory workflows.
    """
    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.is_dir():
        raise ProjectError(f"Not a directory: {resolved_root}")

    config_path = resolved_root / CONFIG_RELATIVE
    if config_path.exists():
        raise ProjectError(
            f"Refusing to initialize {resolved_root}: {CONFIG_RELATIVE} already exists; "
            "no files were changed."
        )

    tasks_value = str(config.get("tasks_directory") or "tasks")
    if contain_directories:
        tasks_path = _directory_within(resolved_root, tasks_value, field="tasks_directory")
        prompts_value = str(config.get("prompts_directory") or "prompts")
        _directory_within(resolved_root, prompts_value, field="prompts_directory")
    else:
        tasks_path = Path(tasks_value)
        if not tasks_path.is_absolute():
            tasks_path = resolved_root / tasks_path

    tasks_path.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return config_path


def mcp_server_entry(base_url: str) -> Dict[str, Any]:
    """The STDIO server declaration a consumer project should carry.

    The console script rather than an interpreter path: a project's ``.mcp.json`` is
    committed and travels to other machines and other checkouts, and a virtualenv path
    stops being true the moment either changes. AgentJobs' own clone is the exception
    that proves it -- that repository develops the tool instead of consuming it, so its
    file names a venv interpreter and is gitignored for exactly that reason.
    """
    return {
        "command": "agentjobs",
        "args": ["mcp"],
        "env": {"AGENTJOBS_URL": base_url.strip().rstrip("/")},
    }


def ensure_mcp_server_entry(root: Path, base_url: str) -> Optional[Path]:
    """Declare the AgentJobs MCP server in a project's own ``.mcp.json``.

    Registering a project used to leave it with no MCP wiring at all, so an agent
    dispatched into it started with no AgentJobs tools and fell back to the CLI or the
    REST API. That fallback is correct and must keep working -- it is the right
    behaviour for a client with no MCP support -- but it should not be what a project
    AgentJobs itself set up gets by default (task-202).

    Returns the file's path when this call wrote it, and ``None`` when an ``agentjobs``
    entry was already there. **An existing entry is never rewritten**, whatever it says:
    a project that has pinned an interpreter, a port or a wrapper of its own has made a
    decision, and silently correcting it during some other command is how a machine's
    working configuration disappears. Other servers in the file, and any other top-level
    key, are preserved untouched.

    Raises :class:`ProjectError` if the file exists and cannot be read or parsed as a
    JSON object. Callers treat that as a warning rather than a failure: the project is
    initialized either way, and refusing to register a project over a malformed file
    someone else owns would turn a cosmetic problem into an outage.
    """
    path = Path(root) / MCP_CONFIG_FILENAME
    document: Dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectError(f"Cannot read {path}: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ProjectError(f"Cannot parse {path} as JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProjectError(f"Cannot use {path}: its top level is not a JSON object.")
        document = parsed

    servers = document.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ProjectError(f"Cannot use {path}: 'mcpServers' is not a JSON object.")
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        return None

    document["mcpServers"] = {**(servers or {}), MCP_SERVER_NAME: mcp_server_entry(base_url)}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path
