"""Core data models for AgentJobs."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskStatus(str, Enum):
    """High-level workflow status for tasks."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    WAITING_FOR_HUMAN = "waiting_for_human"
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class Priority(str, Enum):
    """Priority levels applied to tasks."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Phase(BaseModel):
    """Discrete phase within a task roadmap."""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    id: str = Field(..., description="Phase identifier (e.g., phase-1)")
    title: str = Field(..., description="Human-readable phase title.")
    status: TaskStatus = Field(
        default=TaskStatus.PLANNED,
        description="Phase status leveraging core task workflow states.",
    )
    notes: Optional[str] = Field(
        default=None, description="Optional free-form notes about the phase."
    )
    completed_at: Optional[datetime] = Field(
        default=None, description="Timestamp when the phase reached completion."
    )


class SuccessCriterion(BaseModel):
    """Success criteria tracked per task."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Unique identifier for the success criterion.")
    description: str = Field(..., description="Description of the success criterion.")
    status: str = Field(
        default="pending",
        description="Completion state (pending | in_progress | completed | failed).",
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Ensure success criterion status is one of the supported values."""
        allowed = {"pending", "in_progress", "completed", "failed"}
        if value not in allowed:
            msg = f"status must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class Prompt(BaseModel):
    """Individual prompt entry for a task."""

    model_config = ConfigDict(populate_by_name=True)

    timestamp: datetime = Field(
        ..., description="Timestamp for when the prompt was issued."
    )
    author: str = Field(..., description="Author of the prompt (agent or human name).")
    prompt_file: Optional[str] = Field(
        default=None, description="Optional path reference to the prompt file."
    )
    content: Optional[str] = Field(
        default=None, description="Inline prompt content when not referencing a file."
    )
    context: Optional[str] = Field(
        default=None, description="Additional context regarding the prompt."
    )


class Prompts(BaseModel):
    """Collection of prompt content for a task."""

    model_config = ConfigDict(populate_by_name=True)

    starter: str = Field(..., description="Primary starter prompt content.")
    followups: List[Prompt] = Field(
        default_factory=list,
        description="Subsequent prompts appended during task progression.",
    )


class StatusUpdate(BaseModel):
    """Chronological status update authored during task execution."""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    timestamp: datetime = Field(
        ..., description="Timestamp when the status update was recorded."
    )
    author: str = Field(..., description="Author of the update (agent or collaborator).")
    status: TaskStatus = Field(
        ..., description="Workflow status the task transitioned to."
    )
    summary: str = Field(..., description="Short summary of the update.")
    details: Optional[str] = Field(
        default=None, description="Expanded detail for the status update."
    )


class Deliverable(BaseModel):
    """Deliverable artifact tracked for task completion."""

    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(..., description="Repository-relative path to the deliverable.")
    status: str = Field(
        default="pending",
        description="Completion state (pending | in_progress | completed).",
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable description of the deliverable."
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate deliverable status values."""
        allowed = {"pending", "in_progress", "completed"}
        if value not in allowed:
            msg = f"status must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class Dependency(BaseModel):
    """Relationship metadata between tasks."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(..., description="Referenced task identifier.")
    type: str = Field(
        default="depends_on",
        description="Relationship type (depends_on | blocks | related).",
    )
    status: Optional[str] = Field(
        default=None, description="Status of the dependency relationship."
    )
    note: Optional[str] = Field(
        default=None, description="Additional notes about the dependency."
    )

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        """Ensure dependency type is supported."""
        allowed = {"depends_on", "blocks", "related"}
        if value not in allowed:
            msg = f"type must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class ExternalLink(BaseModel):
    """Reference to relevant external resources."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., description="External resource URL.")
    title: str = Field(..., description="Display title for the external resource.")


class Issue(BaseModel):
    """Issue tracked against the task's lifecycle."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Issue identifier scoped to the task.")
    title: str = Field(..., description="Concise issue summary.")
    status: str = Field(
        default="open",
        description="Issue status (open | in_progress | resolved | wont_fix).",
    )
    resolution: Optional[str] = Field(
        default=None, description="Resolution notes when an issue is closed."
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate issue status values."""
        allowed = {"open", "in_progress", "resolved", "wont_fix"}
        if value not in allowed:
            msg = f"status must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class Branch(BaseModel):
    """Branch lifecycle metadata."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Git branch name associated with the task.")
    status: str = Field(
        default="active",
        description="Branch status (active | merged | abandoned).",
    )
    merged_at: Optional[datetime] = Field(
        default=None, description="When the branch was merged, if applicable."
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate branch status values."""
        allowed = {"active", "merged", "abandoned"}
        if value not in allowed:
            msg = f"status must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return value


class Task(BaseModel):
    """Primary task representation tracked by AgentJobs."""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    # Core metadata
    id: str = Field(..., description="Unique task identifier (e.g., task-001).")
    title: str = Field(..., description="Task title summarising the work.")
    created: datetime = Field(..., description="Creation timestamp.")
    updated: datetime = Field(..., description="Last update timestamp.")

    # Workflow
    status: TaskStatus = Field(
        default=TaskStatus.PLANNED, description="Current workflow status."
    )
    priority: Priority = Field(
        default=Priority.MEDIUM, description="Relative priority weighting."
    )
    category: str = Field(..., description="Task category for filtering.")
    assigned_to: Optional[str] = Field(
        default=None, description="Agent or teammate currently assigned."
    )
    estimated_effort: Optional[str] = Field(
        default=None, description="Estimated effort (time or complexity)."
    )

    # Content
    human_summary: Optional[str] = Field(
        default=None,
        description="Concise 1-2 sentence summary for human reviewers.",
    )
    description: str = Field(..., description="Markdown description of the task.")
    phases: List[Phase] = Field(
        default_factory=list, description="Phases tracked for this task."
    )
    success_criteria: List[SuccessCriterion] = Field(
        default_factory=list, description="Success criteria checklist."
    )

    # Agent interaction
    prompts: Prompts = Field(
        default_factory=lambda: Prompts(starter="", followups=[]),
        description="Prompt collection used by collaborating agents.",
    )
    status_updates: List[StatusUpdate] = Field(
        default_factory=list,
        description="Chronological status updates recorded for the task.",
    )
    deliverables: List[Deliverable] = Field(
        default_factory=list,
        description="Deliverables associated with task completion.",
    )

    # Relationships
    dependencies: List[Dependency] = Field(
        default_factory=list, description="Task dependencies and relationships."
    )
    external_links: List[ExternalLink] = Field(
        default_factory=list, description="External references for the task."
    )

    # Tracking
    issues: List[Issue] = Field(
        default_factory=list, description="Issues encountered while executing the task."
    )
    tags: List[str] = Field(
        default_factory=list, description="Tag metadata for filtering and search."
    )
    branches: List[Branch] = Field(
        default_factory=list, description="Branch metadata associated with the task."
    )

    def is_completed(self) -> bool:
        """Return True if the task is marked as completed."""
        return self.status == TaskStatus.COMPLETED

    def is_blocked(self) -> bool:
        """Return True if the task is currently blocked."""
        return self.status == TaskStatus.BLOCKED

    def is_active(self) -> bool:
        """Return True for statuses representing in-progress work."""
        return self.status in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_FOR_HUMAN,
            TaskStatus.UNDER_REVIEW,
        }

    def priority_rank(self) -> int:
        """Provide numeric ordering for priority comparisons."""
        order = {
            Priority.CRITICAL: 0,
            Priority.HIGH: 1,
            Priority.MEDIUM: 2,
            Priority.LOW: 3,
        }
        return order[self.priority]
