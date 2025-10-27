"""YAML-backed persistence layer for AgentJobs tasks."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import ValidationError

from .models import Comment, Task, Webhook

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
                task.human_summary,
                task.description,
                " ".join(task.tags),
            ]
            if any(normalized in (haystack or "").lower() for haystack in haystacks):
                results.append(task)
        return results


class WebhookStorage:
    """YAML-based webhook storage."""

    def __init__(self, webhooks_path: Path):
        """Initialize webhook storage with path to webhooks.yaml file."""
        self.webhooks_path = Path(webhooks_path)
        self.webhooks_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.webhooks_path.exists():
            self._write_webhooks([])

    def _read_webhooks(self) -> List[dict]:
        """Read webhooks from YAML file."""
        try:
            content = self.webhooks_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or []
        except yaml.YAMLError as exc:  # pragma: no cover
            logger.error("Failed to parse webhooks YAML: %s", exc)
            return []
        return data if isinstance(data, list) else []

    def _write_webhooks(self, webhooks: List[dict]) -> None:
        """Write webhooks to YAML file."""
        yaml_text = yaml.safe_dump(webhooks, sort_keys=False, allow_unicode=False)
        self.webhooks_path.write_text(yaml_text, encoding="utf-8")

    def list_webhooks(self) -> List[Webhook]:
        """List all webhooks."""
        webhooks: List[Webhook] = []
        for data in self._read_webhooks():
            try:
                webhook = Webhook.model_validate(data)
                webhooks.append(webhook)
            except ValidationError as exc:  # pragma: no cover
                logger.error("Validation error loading webhook: %s", exc)
        return webhooks

    def get_webhook(self, webhook_id: str) -> Optional[Webhook]:
        """Get webhook by ID."""
        for data in self._read_webhooks():
            if data.get("id") == webhook_id:
                try:
                    return Webhook.model_validate(data)
                except ValidationError as exc:  # pragma: no cover
                    logger.error("Validation error loading webhook %s: %s", webhook_id, exc)
                    return None
        return None

    def save_webhook(self, webhook: Webhook) -> Webhook:
        """Save or update a webhook."""
        webhooks = self._read_webhooks()
        webhooks = [w for w in webhooks if w.get("id") != webhook.id]
        webhooks.append(webhook.model_dump(mode="json"))
        self._write_webhooks(webhooks)
        return webhook

    def create_webhook(
        self,
        url: str,
        events: List[str],
        secret: str,
        active: bool = True,
    ) -> Webhook:
        """Create a new webhook."""
        webhook = Webhook(
            id=f"wh_{uuid.uuid4().hex[:10]}",
            url=url,
            events=events,
            secret=secret,
            active=active,
            created=datetime.now(tz=timezone.utc),
        )
        return self.save_webhook(webhook)

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        webhooks = self._read_webhooks()
        original_count = len(webhooks)
        webhooks = [w for w in webhooks if w.get("id") != webhook_id]
        if len(webhooks) < original_count:
            self._write_webhooks(webhooks)
            return True
        return False
