"""Convert parsed markdown tasks to AgentJobs YAML format (schema v2)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentjobs.models_v2 import (
    Ball,
    BallReason,
    Branch,
    Deliverable,
    Lifecycle,
    Priority,
    Spec,
    Task,
)

from agentjobs.queue import QUEUE_STEP

from .parser import ParsedTask


class TaskConverter:
    """Convert ParsedTask to an AgentJobs v2 Task model."""

    # Each imported status maps onto the v2 axes (design doc section 3). Imported
    # markdown carries no log to derive a ball_prompt from, so the mapped states use
    # generic asks.
    STATE_MAP: Dict[str, Dict[str, Any]] = {
        "complete": {"lifecycle": Lifecycle.CLOSED, "outcome": "completed"},
        "completed": {"lifecycle": Lifecycle.CLOSED, "outcome": "completed"},
        "done": {"lifecycle": Lifecycle.CLOSED, "outcome": "completed"},
        "in progress": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.AGENT,
            "ball_reason": BallReason.WORK,
            "ball_prompt": "Continue the work described in the spec.",
        },
        "in-progress": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.AGENT,
            "ball_reason": BallReason.WORK,
            "ball_prompt": "Continue the work described in the spec.",
        },
        "in_progress": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.AGENT,
            "ball_reason": BallReason.WORK,
            "ball_prompt": "Continue the work described in the spec.",
        },
        "active": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.AGENT,
            "ball_reason": BallReason.WORK,
            "ball_prompt": "Continue the work described in the spec.",
        },
        "blocked": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.EXTERNAL,
            "ball_reason": BallReason.DEPENDENCY,
            "ball_prompt": "NEEDS REVIEW: state what this task is blocked on.",
        },
        "on hold": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.EXTERNAL,
            "ball_reason": BallReason.DEPENDENCY,
            "ball_prompt": "NEEDS REVIEW: state what this task is blocked on.",
        },
        "paused": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.EXTERNAL,
            "ball_reason": BallReason.DEPENDENCY,
            "ball_prompt": "NEEDS REVIEW: state what this task is blocked on.",
        },
        "waiting": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "NEEDS REVIEW: state what this task is waiting on.",
        },
        "waiting for human": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "NEEDS REVIEW: state what this task is waiting on.",
        },
        "waiting_for_human": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "NEEDS REVIEW: state what this task is waiting on.",
        },
        "needs human": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "NEEDS REVIEW: state what this task is waiting on.",
        },
        "under review": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "Review the imported work and approve or request changes.",
        },
        "review": {
            "lifecycle": Lifecycle.ACTIVE,
            "ball": Ball.HUMAN,
            "ball_reason": BallReason.REVIEW,
            "ball_prompt": "Review the imported work and approve or request changes.",
        },
    }

    DRAFT_STATE: Dict[str, Any] = {
        "lifecycle": Lifecycle.DRAFT,
        "ball": Ball.HUMAN,
        "ball_reason": BallReason.SPEC,
        "ball_prompt": "Finish specifying this imported task.",
    }

    PRIORITY_MAP = {
        "critical": Priority.CRITICAL,
        "high": Priority.HIGH,
        "medium": Priority.MEDIUM,
        "normal": Priority.MEDIUM,
        "low": Priority.LOW,
    }

    def convert(
        self,
        parsed: ParsedTask,
        prompts_dir: Optional[Path] = None,
    ) -> Task:
        """Convert ParsedTask to a v2 Task model."""
        now = datetime.now(tz=timezone.utc)
        task_id = self._generate_task_id(parsed)
        state = dict(self._map_state(parsed.status))
        priority = self._map_priority(parsed.priority)

        deliverables = [
            Deliverable(
                path=self._derive_deliverable_path(deliverable),
                note=deliverable.get("description"),
                status="done" if deliverable.get("status") in ("completed", "done") else "pending",
            )
            for deliverable in parsed.deliverables
            if deliverable.get("description")
        ]

        description = self._build_description(parsed)
        summary = (parsed.human_summary or "").strip() or description[:200]

        branches = (
            [Branch(name=parsed.branch.strip())] if parsed.branch and parsed.branch.strip() else []
        )

        assignment: Dict[str, Any] = {}
        assigned_to = (parsed.assigned_to or "").strip().lower()
        if state["lifecycle"] is Lifecycle.ACTIVE:
            # Rule 5: an active task must name an owner. Imported markdown often does
            # not, and inventing an actor would be worse than admitting ignorance.
            assignment["owner"] = assigned_to or "unknown"
        elif assigned_to:
            assignment["eligible"] = [assigned_to]

        task = Task(
            id=task_id,
            title=parsed.title,
            created=now,
            updated=now,
            priority=priority,
            # Rule 6: an open task holds a place in its band. One conversion cannot
            # know the band -- that is a property of the corpus it is joining -- so it
            # produces a valid task at the top and `migrate_tasks` renumbers the batch
            # against the target directory before saving any of it.
            queue_position=None if state["lifecycle"] is Lifecycle.CLOSED else QUEUE_STEP,
            category=parsed.category or "general",
            effort=parsed.estimated_effort,
            spec=Spec(summary=summary, description=description),
            deliverables=deliverables,
            branches=branches,
            assignment=assignment,
            **state,
        )
        return task

    def _build_description(self, parsed: ParsedTask) -> str:
        """Compose description text including key sections."""
        segments: List[str] = []
        if parsed.description:
            segments.append(parsed.description.strip())

        if parsed.objectives:
            objective_lines = "\n".join(f"- {item}" for item in parsed.objectives)
            segments.append(f"## Objectives\n{objective_lines}")

        if parsed.issues:
            issue_lines = "\n".join(f"- {item}" for item in parsed.issues)
            segments.append(f"## Issues\n{issue_lines}")

        if parsed.phases:
            phase_lines = "\n".join(
                f"- **{phase.get('title', phase.get('id'))}** ({phase.get('status', 'planned')})"
                + (f": {phase['notes']}" if phase.get("notes") else "")
                for phase in parsed.phases
                if phase.get("id") or phase.get("title")
            )
            if phase_lines:
                segments.append(f"## Phases (imported)\n{phase_lines}")

        if parsed.notes:
            segments.append(parsed.notes.strip())

        description = "\n\n".join(segment for segment in segments if segment).strip()
        if not description:
            description = parsed.raw_content.strip()
        return description or "Imported task description unavailable."

    def _generate_task_id(self, parsed: ParsedTask) -> str:
        """Generate task ID from parsed data or filename."""
        if parsed.task_id:
            task_id = parsed.task_id.strip().lower()
            task_id = re.sub(r"^task[-\s]*", "", task_id)
            task_id = task_id.lstrip("#")
            task_id = task_id.replace(" ", "-")
            if task_id:
                return f"task-{task_id}"

        if parsed.source_file:
            stem = parsed.source_file.stem
            if stem.startswith("task-"):
                return stem.lower()

        slug = re.sub(r"[^a-z0-9\-]+", "-", parsed.title.strip().lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        slug = slug[:50] if slug else "imported-task"
        return f"task-{slug}"

    def _map_state(self, status_str: Optional[str]) -> Dict[str, Any]:
        """Map an imported status string onto the v2 state axes."""
        if not status_str:
            return self.DRAFT_STATE
        status_key = status_str.strip().lower()
        return self.STATE_MAP.get(status_key, self.DRAFT_STATE)

    def _map_priority(self, priority_str: Optional[str]) -> Priority:
        """Map priority string to Priority enum."""
        if not priority_str:
            return Priority.MEDIUM
        priority_key = priority_str.strip().lower()
        return self.PRIORITY_MAP.get(priority_key, Priority.MEDIUM)

    @staticmethod
    def _derive_deliverable_path(deliverable: Dict[str, str]) -> str:
        """Derive a filesystem-friendly path for a deliverable entry."""
        description = deliverable.get("description", "deliverable")
        slug = re.sub(r"[^a-zA-Z0-9/_\-.]+", "-", description.lower()).strip("-")
        if not slug:
            slug = "deliverable"
        return slug
