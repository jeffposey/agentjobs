"""Convert parsed markdown tasks to AgentJobs YAML format."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from agentjobs.models import (
    Branch,
    Deliverable,
    Phase,
    Priority,
    Prompts,
    Task,
    TaskStatus,
)

from .parser import ParsedTask


class TaskConverter:
    """Convert ParsedTask to AgentJobs Task model."""

    STATUS_MAP = {
        "complete": TaskStatus.COMPLETED,
        "completed": TaskStatus.COMPLETED,
        "done": TaskStatus.COMPLETED,
        "in progress": TaskStatus.IN_PROGRESS,
        "in-progress": TaskStatus.IN_PROGRESS,
        "in_progress": TaskStatus.IN_PROGRESS,
        "active": TaskStatus.IN_PROGRESS,
        "blocked": TaskStatus.BLOCKED,
        "on hold": TaskStatus.BLOCKED,
        "paused": TaskStatus.BLOCKED,
        "planned": TaskStatus.PLANNED,
        "pending": TaskStatus.PLANNED,
        "not started": TaskStatus.PLANNED,
        "waiting": TaskStatus.WAITING_FOR_HUMAN,
        "waiting for human": TaskStatus.WAITING_FOR_HUMAN,
        "waiting_for_human": TaskStatus.WAITING_FOR_HUMAN,
        "needs human": TaskStatus.WAITING_FOR_HUMAN,
        "under review": TaskStatus.UNDER_REVIEW,
        "review": TaskStatus.UNDER_REVIEW,
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
        """Convert ParsedTask to Task model."""
        now = datetime.now(tz=timezone.utc)
        task_id = self._generate_task_id(parsed)
        status = self._map_status(parsed.status)
        priority = self._map_priority(parsed.priority)

        phases = [
            Phase(
                id=phase["id"],
                title=phase["title"],
                status=self._map_status(phase.get("status")),
                notes=phase.get("notes"),
            )
            for phase in parsed.phases
            if "id" in phase and "title" in phase
        ]

        deliverables = [
            Deliverable(
                path=self._derive_deliverable_path(deliverable),
                description=deliverable.get("description"),
                status=deliverable.get("status", "pending"),
            )
            for deliverable in parsed.deliverables
            if deliverable.get("description")
        ]

        description = self._build_description(parsed)
        prompts = self._find_prompts(parsed, prompts_dir, fallback=description)

        branches = (
            [Branch(name=parsed.branch.strip())]
            if parsed.branch and parsed.branch.strip()
            else []
        )

        task = Task(
            id=task_id,
            title=parsed.title,
            description=description,
            human_summary=parsed.human_summary or description[:200],
            status=status,
            priority=priority,
            category=parsed.category or "general",
            assigned_to=parsed.assigned_to,
            estimated_effort=parsed.estimated_effort,
            phases=phases,
            deliverables=deliverables,
            prompts=prompts,
            branches=branches,
            created=now,
            updated=now,
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

    def _map_status(self, status_str: Optional[str]) -> TaskStatus:
        """Map status string to TaskStatus enum."""
        if not status_str:
            return TaskStatus.PLANNED
        status_key = status_str.strip().lower()
        return self.STATUS_MAP.get(status_key, TaskStatus.PLANNED)

    def _map_priority(self, priority_str: Optional[str]) -> Priority:
        """Map priority string to Priority enum."""
        if not priority_str:
            return Priority.MEDIUM
        priority_key = priority_str.strip().lower()
        return self.PRIORITY_MAP.get(priority_key, Priority.MEDIUM)

    def _find_prompts(
        self,
        parsed: ParsedTask,
        prompts_dir: Optional[Path],
        *,
        fallback: str,
    ) -> Prompts:
        """Find and link related prompt files."""
        if prompts_dir is None:
            return Prompts(starter=fallback or parsed.raw_content[:500])

        task_number = self._extract_task_number(parsed)
        if task_number is None:
            return Prompts(starter=fallback or parsed.raw_content[:500])

        pattern = f"task-{task_number}-*.md"
        prompt_files = sorted(prompts_dir.glob(pattern))
        if not prompt_files:
            return Prompts(starter=fallback or parsed.raw_content[:500])

        starter_file = prompt_files[0]
        try:
            starter_content = starter_file.read_text(encoding="utf-8")
        except OSError:
            starter_content = fallback or parsed.raw_content[:500]

        return Prompts(
            starter=starter_content,
            followups=[],
        )

    def _extract_task_number(self, parsed: ParsedTask) -> Optional[str]:
        """Extract numeric part from the parsed task ID or filename."""
        candidates: List[str] = []
        if parsed.task_id:
            candidates.append(parsed.task_id)
        if parsed.source_file:
            candidates.append(parsed.source_file.stem)

        for candidate in candidates:
            match = re.search(r"(\d+)", candidate)
            if match:
                return match.group(1).zfill(3)
        return None

    @staticmethod
    def _derive_deliverable_path(deliverable: Dict[str, str]) -> str:
        """Derive a filesystem-friendly path for a deliverable entry."""
        description = deliverable.get("description", "deliverable")
        slug = re.sub(r"[^a-zA-Z0-9/_\-.]+", "-", description.lower()).strip("-")
        if not slug:
            slug = "deliverable"
        return slug
