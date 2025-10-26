"""Business logic for managing AgentJobs tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import (
    Priority,
    Prompt,
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

    def list_tasks(
        self,
        *,
        status: Optional[TaskStatus] = None,
        priority: Optional[Priority] = None,
    ) -> List[Task]:
        """Return all tasks optionally filtered by status and priority."""
        tasks = self.storage.list_tasks()
        if status is not None:
            tasks = [task for task in tasks if task.status == status]
        if priority is not None:
            tasks = [task for task in tasks if task.priority == priority]
        return tasks

    def create_task(
        self,
        *,
        id: Optional[str] = None,
        title: str,
        description: str,
        priority: Priority = Priority.MEDIUM,
        category: str = "general",
        **kwargs,
    ) -> Task:
        """Create a new task, generating an identifier when omitted."""
        task_id = id or self.storage.generate_task_id()
        if self.storage.load_task(task_id):
            raise ValueError(f"Task '{task_id}' already exists.")

        now = datetime.now(tz=timezone.utc)
        prompts_payload = kwargs.pop("prompts", None)
        prompts = (
            Prompts.model_validate(prompts_payload)
            if prompts_payload is not None
            else Prompts(starter=description)
        )

        status_value = kwargs.pop("status", TaskStatus.DRAFT)
        task_kwargs: Dict[str, object] = {
            "id": task_id,
            "title": title,
            "description": description,
            "priority": priority,
            "category": category,
            "created": now,
            "updated": now,
            "status": status_value,
            "prompts": prompts,
        }
        task_kwargs.update(kwargs)

        task = Task.model_validate(task_kwargs)
        return self.storage.save_task(task)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        return self.storage.load_task(task_id)

    def replace_task(self, task_id: str, **replacement: object) -> Task:
        """Replace task fields with provided payload preserving identifiers."""
        existing = self._ensure_task_exists(task_id)
        payload = existing.model_dump(mode="python")
        payload.update(replacement)
        payload["id"] = task_id
        payload.setdefault("created", existing.created)
        payload.setdefault("prompts", existing.prompts)
        payload.setdefault("status_updates", existing.status_updates)
        payload.setdefault("deliverables", existing.deliverables)
        payload.setdefault("dependencies", existing.dependencies)
        payload.setdefault("external_links", existing.external_links)
        payload.setdefault("issues", existing.issues)
        payload.setdefault("branches", existing.branches)
        payload.setdefault("success_criteria", existing.success_criteria)
        payload.setdefault("phases", existing.phases)
        payload.setdefault("tags", existing.tags)
        task = Task.model_validate(payload)
        return self.storage.save_task(task)

    def update_task(self, task_id: str, **updates: object) -> Task:
        """Apply a partial update to a task."""
        existing = self._ensure_task_exists(task_id)
        payload = existing.model_dump(mode="python")
        payload.update(updates)
        payload["id"] = existing.id
        payload["created"] = existing.created
        task = Task.model_validate(payload)
        return self.storage.save_task(task)

    def delete_task(self, task_id: str) -> bool:
        """Delete task from storage."""
        return self.storage.delete_task(task_id)

    def archive_task(self, task_id: str, *, author: Optional[str] = None) -> Task:
        """Archive a task by setting its status and recording a status update."""
        task = self._ensure_task_exists(task_id)
        update_author = author or "system"
        archived = self.update_status(
            task_id=task_id,
            status=TaskStatus.ARCHIVED,
            author=update_author,
            summary="Task archived.",
            details=None,
        )
        return archived

    def update_status(
        self,
        task_id: str,
        *,
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
        """Get highest priority available task (READY status only)."""
        candidates = [
            task
            for task in self.storage.list_tasks()
            if task.status == TaskStatus.READY
        ]
        if priority is not None:
            candidates = [task for task in candidates if task.priority == priority]
        if not candidates:
            return None
        candidates.sort(
            key=lambda task: (
                task.priority_rank(),
                -task.updated.timestamp(),
            )
        )
        return candidates[0]

    def add_progress_update(
        self,
        task_id: str,
        *,
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

    def get_starter_prompt(self, task_id: str) -> str:
        """Return the starter prompt for a task."""
        task = self._ensure_task_exists(task_id)
        return task.prompts.starter

    def add_followup_prompt(
        self,
        task_id: str,
        *,
        author: str,
        content: str,
        context: str | None = None,
    ) -> Task:
        """Append a follow-up prompt entry to the task."""
        task = self._ensure_task_exists(task_id)
        prompt = Prompt(
            timestamp=datetime.now(tz=timezone.utc),
            author=author,
            content=content,
            context=context,
        )
        task.prompts.followups.append(prompt)
        return self.storage.save_task(task)

    def search_tasks(self, query: str) -> List[Task]:
        """Search tasks by query string."""
        return self.storage.search_tasks(query)
