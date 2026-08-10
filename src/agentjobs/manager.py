"""Business logic for managing AgentJobs tasks (schema v2).

The manager owns the state axes. Every change to ``lifecycle``, ``ball``,
``ball_reason``, ``ball_prompt`` or ``outcome`` flows through a verb here --
claim, handoff, release, close -- and every verb appends a log entry, so the
record always shows who moved the ball and why (design doc section 3, rule 5).
Callers never write the axes directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .models_v2 import (
    Ball,
    BallReason,
    DeliverableStatus,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Task,
    utcnow,
)
from .storage import TaskLoadError, TaskStorage

if TYPE_CHECKING:
    from .webhooks import WebhookManager


class TaskNotFoundError(ValueError):
    """The addressed task does not exist.

    A subclass so API routes can map "no such task" to 404 while every other
    ValueError -- a refused claim, an invalid transition -- maps to 409.
    """


class TaskManager:
    """Core task management logic."""

    def __init__(self, storage: TaskStorage, webhook_manager: Optional["WebhookManager"] = None):
        self.storage = storage
        self.webhook_manager = webhook_manager

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _ensure_task_exists(self, task_id: str) -> Task:
        """Retrieve an existing task or raise a descriptive error."""
        task = self.storage.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return task

    def list_tasks(
        self,
        *,
        lifecycle: Optional[Lifecycle] = None,
        ball: Optional[Ball] = None,
        priority: Optional[Priority] = None,
    ) -> List[Task]:
        """Return all tasks optionally filtered along the state axes.

        ``ball=Ball.HUMAN`` is the human inbox -- the load-bearing query of the
        v2 design (section 5).
        """
        tasks = self.storage.list_tasks()
        if lifecycle is not None:
            tasks = [task for task in tasks if task.lifecycle == lifecycle]
        if ball is not None:
            tasks = [task for task in tasks if task.ball == ball]
        if priority is not None:
            tasks = [task for task in tasks if task.priority == priority]
        return tasks

    def load_errors(self) -> List[TaskLoadError]:
        """Files in the task directory that exist but cannot be read as tasks.

        Exposed so listing surfaces can show them. A broken file that is only logged is
        invisible to someone whose window into the data is a web page.
        """
        return self.storage.load_all().errors

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        return self.storage.load_task(task_id)

    def search_tasks(self, query: str) -> List[Task]:
        """Search tasks by query string."""
        return self.storage.search_tasks(query)

    def _needs_met(self, task: Task, closed_ids: Dict[str, bool]) -> bool:
        """Whether every `needs` dependency of a task is closed.

        A dependency on a task the store does not contain cannot be evaluated and does
        not block -- refusing forever on a dangling reference would strand the task.
        """
        for dep in task.dependencies:
            if dep.type.value != "needs":
                continue
            met = closed_ids.get(dep.task)
            if met is False:
                return False
        return True

    def get_next_task(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
    ) -> Optional[Task]:
        """Highest-priority claimable task: ready, eligible, no unmet needs."""
        tasks = self.storage.list_tasks()
        closed_ids = {task.id: task.lifecycle is Lifecycle.CLOSED for task in tasks}
        candidates = [
            task
            for task in tasks
            if task.lifecycle is Lifecycle.READY
            and (priority is None or task.priority == priority)
            and (agent is None or not task.assignment.eligible or agent in task.assignment.eligible)
            and self._needs_met(task, closed_ids)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda task: (
                task.priority_rank(),
                -task.updated.timestamp(),
            )
        )
        return candidates[0]

    # ------------------------------------------------------------------
    # Creation and generic edits
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        id: Optional[str] = None,
        title: str,
        description: str,
        summary: Optional[str] = None,
        priority: Priority = Priority.MEDIUM,
        category: str = "general",
        lifecycle: Lifecycle = Lifecycle.DRAFT,
        **kwargs: Any,
    ) -> Task:
        """Create a new task, generating an identifier when omitted.

        Tasks are born ``draft`` (ball: human/spec) or ``ready`` (ball:
        agent/available). Any other starting state would skip the transitions the log
        exists to record.
        """
        task_id = id or self.storage.generate_task_id()
        if self.storage.load_task(task_id):
            raise ValueError(f"Task '{task_id}' already exists.")

        lifecycle = Lifecycle(lifecycle)
        if lifecycle not in (Lifecycle.DRAFT, Lifecycle.READY):
            raise ValueError(
                f"A task cannot be created '{lifecycle.value}'; it starts draft or ready."
            )

        spec_payload = dict(kwargs.pop("spec", None) or {})
        spec_payload.setdefault("description", description)
        spec_payload.setdefault("summary", summary or title)

        now = utcnow()
        task_kwargs: Dict[str, object] = {
            "id": task_id,
            "title": title,
            "created": now,
            "updated": now,
            "lifecycle": lifecycle,
            "priority": priority,
            "category": category,
            "spec": spec_payload,
        }
        if lifecycle is Lifecycle.DRAFT:
            task_kwargs.update(
                ball=Ball.HUMAN,
                ball_reason=BallReason.SPEC,
                ball_prompt=kwargs.pop("ball_prompt", None) or "Finish specifying this task.",
            )
        else:
            # agent/available: the spec is itself the ask, no prompt required.
            task_kwargs.update(ball=Ball.AGENT, ball_reason=BallReason.AVAILABLE)
            kwargs.pop("ball_prompt", None)
        task_kwargs.update(kwargs)

        task = Task.model_validate(task_kwargs)
        return self.storage.save_task(task)

    def update_task(self, task_id: str, **updates: object) -> Task:
        """Apply a partial update to a task."""

        def apply(existing: Task) -> Task:
            payload = existing.model_dump(mode="python", by_alias=True, exclude={"display_status"})
            payload.update(updates)
            payload["id"] = existing.id
            payload["created"] = existing.created
            return Task.model_validate(payload)

        return self._mutate(task_id, apply)

    def delete_task(self, task_id: str) -> bool:
        """Delete task from storage."""
        return self.storage.delete_task(task_id)

    def mark_deliverable_complete(
        self,
        task_id: str,
        deliverable_path: str,
    ) -> Task:
        """Mark deliverable as done."""

        def apply(task: Task) -> Task:
            for deliverable in task.deliverables:
                if deliverable.path == deliverable_path:
                    deliverable.status = DeliverableStatus.DONE
                    break
            else:
                raise ValueError(
                    f"Deliverable '{deliverable_path}' not found for task '{task_id}'."
                )
            return task

        return self._mutate(task_id, apply)

    # ------------------------------------------------------------------
    # The state verbs (design doc section 5)
    #
    # Preconditions are checked *inside* the lock against a fresh read via
    # storage.mutate_task -- checking beforehand is exactly the double-claim race
    # task-055 closed. Two agents racing claim_task produce one winner and one
    # ValueError, never two owners.
    # ------------------------------------------------------------------

    def _mutate(self, task_id: str, mutator: Any) -> Task:
        """mutate_task, with a missing task reported as TaskNotFoundError."""
        if not self.storage._task_path(task_id).exists():
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return self.storage.mutate_task(task_id, mutator)

    @staticmethod
    def _append_entry(
        task: Task,
        *,
        actor: str,
        type: LogEntryType,
        body: Optional[str] = None,
        re: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> LogEntry:
        entry = LogEntry(
            id=task.next_log_id(),
            ts=utcnow(),
            actor=actor,
            type=type,
            body=body,
            re=re,
            data=data or {},
        )
        task.log.append(entry)
        return entry

    def claim_task(self, task_id: str, *, agent: str) -> Task:
        """Take ownership of a ready task, or refuse because someone else already did."""
        tasks = self.storage.list_tasks()
        closed_ids = {task.id: task.lifecycle is Lifecycle.CLOSED for task in tasks}

        def apply(task: Task) -> Task:
            if task.lifecycle is not Lifecycle.READY:
                owner = task.assignment.owner
                raise ValueError(
                    f"Task '{task_id}' is not available to claim "
                    f"(it is {task.display_status.lower()}"
                    + (f", owned by {owner}" if owner else "")
                    + ")"
                )
            if task.assignment.eligible and agent not in task.assignment.eligible:
                eligible = ", ".join(task.assignment.eligible)
                raise ValueError(
                    f"Task '{task_id}' is claimable only by: {eligible} (not '{agent}')"
                )
            if not self._needs_met(task, closed_ids):
                unmet = [
                    dep.task
                    for dep in task.dependencies
                    if dep.type.value == "needs" and closed_ids.get(dep.task) is False
                ]
                raise ValueError(f"Task '{task_id}' has unmet dependencies: {', '.join(unmet)}")
            task.lifecycle = Lifecycle.ACTIVE
            task.assignment.owner = agent
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.WORK
            task.ball_prompt = "Execute the spec; log progress and hand off when done."
            self._append_entry(
                task,
                actor=agent,
                type=LogEntryType.TRANSITION,
                body=f"Claimed by {agent}.",
                data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
            )
            return task

        return self._mutate(task_id, apply)

    def handoff(
        self,
        task_id: str,
        *,
        actor: str,
        ball: Ball,
        ball_reason: BallReason,
        ball_prompt: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Task:
        """Move the ball. The ask travels with it, by schema requirement."""

        def apply(task: Task) -> Task:
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is closed; the ball cannot move.")
            task.ball = Ball(ball)
            task.ball_reason = BallReason(ball_reason)
            task.ball_prompt = ball_prompt
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.HANDOFF,
                body=body or ball_prompt,
                data={"ball": task.ball.value, "ball_reason": task.ball_reason.value},
            )
            return task

        task = self._mutate(task_id, apply)
        self._fire(
            "task.handoff",
            task,
            {
                "triggered_by": actor,
                "ball": task.ball.value if task.ball else None,
                "ball_reason": task.ball_reason.value if task.ball_reason else None,
                "ball_prompt": task.ball_prompt,
            },
        )
        return task

    def release_task(self, task_id: str, *, actor: str, body: Optional[str] = None) -> Task:
        """Bow out cleanly: active goes back to ready, unclaimed and available."""

        def apply(task: Task) -> Task:
            if task.lifecycle is not Lifecycle.ACTIVE:
                raise ValueError(
                    f"Task '{task_id}' is not active (it is {task.display_status.lower()}); "
                    "only claimed work can be released."
                )
            task.lifecycle = Lifecycle.READY
            task.assignment.owner = None
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.AVAILABLE
            task.ball_prompt = None
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.TRANSITION,
                body=body or f"Released by {actor}; back in the pool.",
                data={"lifecycle": "ready", "ball": "agent", "ball_reason": "available"},
            )
            return task

        return self._mutate(task_id, apply)

    def close_task(
        self,
        task_id: str,
        *,
        actor: str,
        outcome: Outcome,
        body: Optional[str] = None,
        archive: bool = False,
    ) -> Task:
        """End the task with an outcome. How it ended is data, not a lifecycle fork."""

        def apply(task: Task) -> Task:
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is already closed.")
            task.lifecycle = Lifecycle.CLOSED
            task.outcome = Outcome(outcome)
            task.ball = None
            task.ball_reason = None
            task.ball_prompt = None
            task.assignment.owner = None
            if archive:
                task.archived = True
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.TRANSITION,
                body=body or f"Closed: {task.outcome.value}.",
                data={"lifecycle": "closed", "outcome": task.outcome.value},
            )
            return task

        task = self._mutate(task_id, apply)
        self._fire(
            "task.closed",
            task,
            {"triggered_by": actor, "outcome": task.outcome.value if task.outcome else None},
        )
        return task

    def archive_task(self, task_id: str, *, author: Optional[str] = None) -> Task:
        """Hide a task. An open task is closed as cancelled first; archived is a flag."""
        actor = author or "system"
        existing = self._ensure_task_exists(task_id)
        if existing.is_open:
            return self.close_task(
                task_id,
                actor=actor,
                outcome=Outcome.CANCELLED,
                body="Task archived.",
                archive=True,
            )

        def apply(task: Task) -> Task:
            task.archived = True
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.NOTE,
                body="Task archived.",
                data={"archived": True},
            )
            return task

        return self._mutate(task_id, apply)

    # ------------------------------------------------------------------
    # The log
    # ------------------------------------------------------------------

    def add_log_entry(
        self,
        task_id: str,
        *,
        actor: str,
        type: LogEntryType,
        body: Optional[str] = None,
        re: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Append one entry to the unified log."""
        entry_type = LogEntryType(type)
        if entry_type is LogEntryType.TRANSITION:
            raise ValueError(
                "transition entries are appended by the manager's state verbs, "
                "not written directly (design doc section 3, rule 5)"
            )

        def apply(task: Task) -> Task:
            self._append_entry(task, actor=actor, type=entry_type, body=body, re=re, data=data)
            return task

        task = self._mutate(task_id, apply)
        if entry_type is LogEntryType.QUESTION:
            self._fire("task.question", task, {"triggered_by": actor, "body": body})
        return task

    def add_progress_update(
        self,
        task_id: str,
        *,
        author: str,
        summary: str,
        details: Optional[str] = None,
    ) -> Task:
        """Append a progress entry to the log."""
        body = f"{summary}\n\n{details}" if details else summary
        return self.add_log_entry(task_id, actor=author, type=LogEntryType.PROGRESS, body=body)

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def _fire(self, event: str, task: Task, metadata: Dict[str, Any]) -> None:
        """Fire a webhook event when a webhook manager is attached."""
        if self.webhook_manager:
            self.webhook_manager.fire_event(event, task, metadata)
