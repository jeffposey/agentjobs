"""Request/response models for AgentJobs REST API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from agentjobs.models import (
    Branch,
    Deliverable,
    Dependency,
    ExternalLink,
    Issue,
    Phase,
    Priority,
    Prompts,
    SuccessCriterion,
    TaskStatus,
)


class TaskCreateRequest(BaseModel):
    """Payload for creating a new task."""

    id: Optional[str] = Field(
        default=None,
        description="Optional explicit task identifier (e.g., task-042).",
    )
    title: str = Field(..., description="Task title summarising the work to be done.")
    description: str = Field(..., description="Markdown-formatted task description.")
    priority: Priority = Field(
        default=Priority.MEDIUM,
        description="Relative urgency for the new task.",
    )
    category: str = Field(
        default="general",
        description="Classification category used for filtering in the UI.",
    )
    status: TaskStatus = Field(
        default=TaskStatus.PLANNED,
        description="Initial workflow status assigned to the task.",
    )
    assigned_to: Optional[str] = Field(
        default=None, description="Agent or teammate assigned at creation time."
    )
    estimated_effort: Optional[str] = Field(
        default=None, description="Estimated effort (time or complexity)."
    )
    tags: List[str] = Field(default_factory=list, description="Arbitrary tag labels.")
    phases: List[Phase] = Field(
        default_factory=list,
        description="Optional project phases associated with the task.",
    )
    success_criteria: List[SuccessCriterion] = Field(
        default_factory=list,
        description="Checklist of success criteria for the task.",
    )
    deliverables: List[Deliverable] = Field(
        default_factory=list,
        description="Deliverables to be produced for task completion.",
    )
    dependencies: List[Dependency] = Field(
        default_factory=list,
        description="Task dependencies tracked in the system.",
    )
    external_links: List[ExternalLink] = Field(
        default_factory=list,
        description="External references relevant to the task.",
    )
    issues: List[Issue] = Field(
        default_factory=list,
        description="Issues linked to the execution of the task.",
    )
    branches: List[Branch] = Field(
        default_factory=list,
        description="Git branches associated with the task lifecycle.",
    )
    prompts: Optional[Prompts] = Field(
        default=None,
        description="Optional pre-populated prompts structure for the task.",
    )


class TaskUpdateRequest(BaseModel):
    """Payload for partially updating a task."""

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[Priority] = None
    category: Optional[str] = None
    assigned_to: Optional[str] = None
    estimated_effort: Optional[str] = None
    tags: Optional[List[str]] = None
    phases: Optional[List[Phase]] = None
    success_criteria: Optional[List[SuccessCriterion]] = None
    deliverables: Optional[List[Deliverable]] = None
    dependencies: Optional[List[Dependency]] = None
    external_links: Optional[List[ExternalLink]] = None
    issues: Optional[List[Issue]] = None
    branches: Optional[List[Branch]] = None
    prompts: Optional[Prompts] = None


class StatusUpdateRequest(BaseModel):
    """Status transition payload."""

    status: TaskStatus
    author: str
    summary: str
    details: Optional[str] = None


class ProgressUpdateRequest(BaseModel):
    """Progress update payload appended to task history."""

    author: str
    summary: str
    details: Optional[str] = None


class PromptAddRequest(BaseModel):
    """Request body for adding a follow-up prompt."""

    author: str
    content: str
    context: Optional[str] = None
