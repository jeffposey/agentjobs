"""YAML-backed persistence layer for AgentJobs tasks."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import ValidationError

from .models import Task

logger = logging.getLogger(__name__)


class TaskStorage:
    """YAML-based task storage."""

    def __init__(self, tasks_dir: Path):
        """Initialize storage with tasks directory."""
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        """Resolve the path for a given task identifier."""
        if task_id.endswith(".yaml"):
            filename = task_id
        else:
            filename = f"{task_id}.yaml"
        return self.tasks_dir / filename

    def load_task(self, task_id: str) -> Optional[Task]:
        """Load task from YAML file."""
        path = self._task_path(task_id)
        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - defensive logging path
            logger.error("Failed to parse YAML for %s: %s", path, exc)
            return None

        if not data:
            logger.warning("Empty task data in %s", path)
            return None

        try:
            return Task.model_validate(data)
        except ValidationError as exc:  # pragma: no cover - defensive logging path
            logger.error("Validation error loading %s: %s", path, exc)
            return None

    def save_task(self, task: Task) -> Task:
        """Save task to YAML file, returning the persisted Task instance."""
        now = datetime.now(tz=timezone.utc)
        task.updated = now

        path = self._task_path(task.id)
        task_dict = task.model_dump(mode="json", exclude_none=True)
        yaml_text = yaml.safe_dump(task_dict, sort_keys=False, allow_unicode=False)
        path.write_text(yaml_text, encoding="utf-8")
        return task

    def list_tasks(self) -> List[Task]:
        """List all tasks."""
        tasks: List[Task] = []
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            task = self.load_task(path.stem)
            if task is not None:
                tasks.append(task)
        return tasks

    def generate_task_id(self) -> str:
        """Generate the next task identifier in sequence."""
        highest = 0
        for path in self.tasks_dir.glob("task-*.yaml"):
            stem = path.stem
            try:
                number = int(stem.split("-", maxsplit=1)[1])
            except (IndexError, ValueError):
                continue
            highest = max(highest, number)
        return f"task-{highest + 1:03d}"

    def delete_task(self, task_id: str) -> bool:
        """Delete task (archive)."""
        path = self._task_path(task_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def search_tasks(self, query: str) -> List[Task]:
        """Full-text search across tasks."""
        normalized = query.lower()
        results: List[Task] = []
        for task in self.list_tasks():
            haystacks = [
                task.title,
                task.description,
                " ".join(task.tags),
            ]
            if any(normalized in (haystack or "").lower() for haystack in haystacks):
                results.append(task)
        return results
