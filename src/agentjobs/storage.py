"""YAML-backed persistence layer for AgentJobs tasks."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import ValidationError

from .models import Task, Webhook
from .projects import contained_path

logger = logging.getLogger(__name__)


def _describe_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error as 'field: message', naming the field that is wrong.

    Pydantic's default rendering is several lines per error with a docs URL. The point
    of this task is that a reader learns *which field* broke without opening a log
    aggregator, so the first few errors are compressed onto one line.
    """
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "(root)"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    remaining = len(exc.errors()) - 3
    if remaining > 0:
        parts.append(f"and {remaining} more problem(s)")
    return "; ".join(parts)


class TaskLoadError(Exception):
    """A task file exists but cannot be read as a task.

    Carries the path and a field-level description so the message answers "which file,
    which field, what is wrong" without further digging.
    """

    def __init__(self, path: Path, reason: str, *, errors: Optional[List[Any]] = None):
        """Initialize with the offending file and a human-readable reason."""
        self.path = Path(path)
        self.task_id = self.path.stem
        self.reason = reason
        self.errors = errors or []
        super().__init__(f"{self.path.name}: {reason}")

    def as_dict(self) -> Dict[str, Any]:
        """Serialisable form, for API responses and templates."""
        return {
            "task_id": self.task_id,
            "path": str(self.path),
            "filename": self.path.name,
            "reason": self.reason,
        }


@dataclass
class LoadResult:
    """Tasks that loaded, plus the files that did not."""

    tasks: List[Task] = dc_field(default_factory=list)
    errors: List[TaskLoadError] = dc_field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


class TaskStorage:
    """YAML-based task storage."""

    def __init__(self, tasks_dir: Path):
        """Initialize storage with tasks directory."""
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        """Resolve the path for a given task identifier.

        Task ids reach this method from URL path parameters, and one server now serves
        task directories drawn from a machine-wide registry. So the composed path is
        checked for containment rather than trusted: an id that resolves outside this
        project's tasks directory raises instead of reading or writing the file.
        """
        if task_id.endswith(".yaml"):
            filename = task_id
        else:
            filename = f"{task_id}.yaml"
        return contained_path(self.tasks_dir, filename)

    def load_task(self, task_id: str) -> Optional[Task]:
        """Load a task from its YAML file.

        Returns None when the file does not exist -- that is a legitimate answer to
        "is there a task with this id".

        Raises TaskLoadError when the file exists but cannot be read as a task. That
        is *not* a legitimate answer: it used to return None here too, which made a
        broken file indistinguishable from a missing one and dropped it out of every
        listing with nothing but a log line as evidence. A task that silently vanishes
        is the worst available failure mode, because the data it described is invisible
        precisely when someone needs to notice it is wrong.
        """
        path = self._task_path(task_id)
        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise TaskLoadError(path, f"invalid YAML: {exc}") from exc
        except OSError as exc:
            raise TaskLoadError(path, f"could not read the file: {exc}") from exc

        if not data:
            raise TaskLoadError(path, "the file is empty")
        if not isinstance(data, dict):
            raise TaskLoadError(
                path, f"expected a mapping at the top level, found {type(data).__name__}"
            )

        try:
            return Task.model_validate(data)
        except ValidationError as exc:
            raise TaskLoadError(path, _describe_validation_error(exc), errors=exc.errors()) from exc

    def save_task(self, task: Task) -> Task:
        """Save task to YAML file, returning the persisted Task instance."""
        now = datetime.now(tz=timezone.utc)
        task.updated = now

        path = self._task_path(task.id)
        task_dict = task.model_dump(mode="json", exclude_none=True)
        yaml_text = yaml.safe_dump(task_dict, sort_keys=False, allow_unicode=False)
        path.write_text(yaml_text, encoding="utf-8")
        return task

    def load_all(self) -> "LoadResult":
        """Load every task, keeping the broken ones instead of dropping them.

        One unreadable file must not take down the listing of the other thirty-seven,
        so errors are collected rather than raised. They are *returned* rather than
        logged, so that callers have to decide what to do with them -- which is what
        makes a broken file visible in the UI instead of only in a log nobody reads.
        """
        result = LoadResult()
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            try:
                task = self.load_task(path.stem)
            except TaskLoadError as exc:
                logger.error("%s", exc)
                result.errors.append(exc)
                continue
            if task is not None:
                result.tasks.append(task)
        return result

    def list_tasks(self) -> List[Task]:
        """Every task that loads. Use load_all() when the broken ones matter too."""
        return self.load_all().tasks

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
