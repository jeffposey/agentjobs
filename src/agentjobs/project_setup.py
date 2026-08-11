"""Shared, non-interactive setup for a new AgentJobs project."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .projects import CONFIG_RELATIVE, ProjectError

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
