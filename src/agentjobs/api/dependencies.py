"""FastAPI dependency helpers for AgentJobs API."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from fastapi.templating import Jinja2Templates

from agentjobs.manager import TaskManager
from agentjobs.storage import TaskStorage, WebhookStorage
from agentjobs.webhooks import WebhookManager

TASKS_DIR_ENV = "AGENTJOBS_TASKS_DIR"
PROJECT_ROOT_ENV = "AGENTJOBS_PROJECT_ROOT"
_CONFIG_RELATIVE = Path(".agentjobs") / "config.yaml"
_TEMPLATES: Optional[Jinja2Templates] = None


def _resolve_project_root() -> Path:
    """Resolve the project root directory for AgentJobs runtime."""
    root = os.environ.get(PROJECT_ROOT_ENV)
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd()


def _load_config(base_dir: Path) -> dict:
    """Load AgentJobs configuration from disk when present."""
    config_path = base_dir / _CONFIG_RELATIVE
    if not config_path.exists():
        return {}
    content = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(content) or {}


def _resolve_tasks_dir() -> Path:
    """Determine the tasks directory from env vars or configuration."""
    env_dir = os.environ.get(TASKS_DIR_ENV)
    if env_dir:
        path = Path(env_dir).expanduser()
        if not path.is_absolute():
            path = _resolve_project_root() / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    base_dir = _resolve_project_root()
    config = _load_config(base_dir)
    tasks_dir_value: Optional[str] = config.get("tasks_directory")
    if not tasks_dir_value:
        tasks_dir_value = "tasks"
    tasks_dir = Path(tasks_dir_value)
    if not tasks_dir.is_absolute():
        tasks_dir = base_dir / tasks_dir
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


@lru_cache(maxsize=1)
def _get_storage() -> TaskStorage:
    """Create a cached TaskStorage instance."""
    return TaskStorage(_resolve_tasks_dir())


@lru_cache(maxsize=1)
def _get_webhook_storage() -> WebhookStorage:
    """Create a cached WebhookStorage instance."""
    base_dir = _resolve_project_root()
    webhooks_path = base_dir / ".agentjobs" / "webhooks.yaml"
    return WebhookStorage(webhooks_path)


@lru_cache(maxsize=1)
def _get_webhook_manager() -> WebhookManager:
    """Create a cached WebhookManager instance."""
    return WebhookManager(_get_webhook_storage())


def get_task_manager() -> TaskManager:
    """Provide a TaskManager instance for request handling."""
    return TaskManager(_get_storage(), _get_webhook_manager())


def get_webhook_manager() -> WebhookManager:
    """Provide a WebhookManager instance for request handling."""
    return _get_webhook_manager()


def get_templates() -> Jinja2Templates:
    """Provide a shared Jinja2Templates instance for web views."""
    global _TEMPLATES
    if _TEMPLATES is None:
        template_dir = Path(__file__).parent / "templates"
        _TEMPLATES = Jinja2Templates(directory=str(template_dir))
    return _TEMPLATES


def reset_dependency_cache() -> None:
    """Clear cached storage when environment configuration changes."""
    _get_storage.cache_clear()
    _get_webhook_storage.cache_clear()
    _get_webhook_manager.cache_clear()
    global _TEMPLATES
    _TEMPLATES = None
