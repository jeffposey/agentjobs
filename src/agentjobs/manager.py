"""Business logic for managing AgentJobs tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import (
    Priority,
    Prompts,
    StatusUpdate,
    Task,
    TaskStatus,
)
from .storage import TaskStorage


class TaskManager:
    """Core task management logic."""

    def __init__(self, storage: TaskStorage):
        self.storage = storage

    def _ensure_task_exists(self, task_id: str) -> Task:
        """Retrieve an existing task or raise a descriptive error."""
        task = self.storage.load_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        return task

    def create_task(
        self,
        id: str,
        title: str,
        description: str,
        priority: Priority = Priority.MEDIUM,
        category: str = "general",
        **kwargs,
    ) -> Task:
        """Create new task."""
        if self.storage.load_task(id):
            raise ValueError(f"Task '{id}' already exists.")

        now = datetime.now(tz=timezone.utc)
        prompts_data = kwargs.pop("prompts", None)
        if prompts_data is None:
            prompts = Prompts(starter=description)
        else:
            prompts = Prompts.model_validate(prompts_data)

        task = Task(
            id=id,
            title=title,
            created=now,
            updated=now,
            status=kwargs.pop("status", TaskStatus.PLANNED),
            priority=priority,
            category=category,
            description=description,
            prompts=prompts,
            **kwargs,
        )
        return self.storage.save_task(task)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        return self.storage.load_task(task_id)

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        author: str,
        summary: str,
        details: Optional[str] = None,
    ) -> Task:
        """Update task status with status update entry."""
        task = self._ensure_task_exists(task_id)
        now = datetime.now(tz=timezone.utc)
        task.status = status
        update = StatusUpdate(
            timestamp=now,
            author=author,
            status=status,
            summary=summary,
            details=details,
        )
        task.status_updates.append(update)
        return self.storage.save_task(task)

    def get_next_task(self, priority: Optional[Priority] = None) -> Optional[Task]:
        """Get highest priority available task."""
        candidates = [
            task
            for task in self.storage.list_tasks()
            if task.status == TaskStatus.PLANNED
        ]
        if priority is not None:
            candidates = [task for task in candidates if task.priority == priority]
        if not candidates:
            return None
        candidates.sort(key=lambda task: task.priority_rank())
        return candidates[0]

    def add_progress_update(
        self,
        task_id: str,
        author: str,
        summary: str,
        details: Optional[str] = None,
    ) -> Task:
        """Add progress update to task."""
        task = self._ensure_task_exists(task_id)
        update = StatusUpdate(
            timestamp=datetime.now(tz=timezone.utc),
            author=author,
            status=task.status,
            summary=summary,
            details=details,
        )
        task.status_updates.append(update)
        return self.storage.save_task(task)

    def mark_deliverable_complete(
        self,
        task_id: str,
        deliverable_path: str,
    ) -> Task:
        """Mark deliverable as completed."""
        task = self._ensure_task_exists(task_id)
        for deliverable in task.deliverables:
            if deliverable.path == deliverable_path:
                deliverable.status = "completed"
                break
        else:
            raise ValueError(
                f"Deliverable '{deliverable_path}' not found for task '{task_id}'."
            )
        return self.storage.save_task(task)
