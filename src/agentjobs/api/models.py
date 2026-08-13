"""Request/response models for AgentJobs REST API (schema v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from agentjobs.models_v2 import (
    AcceptanceCriterion,
    Ball,
    BallReason,
    Branch,
    Deliverable,
    Dependency,
    Lifecycle,
    Link,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)


class DashboardStats(BaseModel):
    """Counts rendered by the dashboard stat tiles."""

    total: int
    in_progress: int
    blocked: int
    waiting_for_human: int
    awaiting_input: int
    completed: int


class DashboardRecentUpdate(BaseModel):
    """A compact task-log record for the recent activity list."""

    task_id: str
    task_title: str
    timestamp: datetime
    summary: str
    author: str


class BrokenTaskFile(BaseModel):
    """An on-disk task record that could not be loaded."""

    task_id: str
    path: str
    filename: str
    reason: str


class DashboardResponse(BaseModel):
    """The complete Python-computed dashboard contract."""

    stats: DashboardStats
    active_tasks: List[Task]
    recent_updates: List[DashboardRecentUpdate]
    waiting_tasks: List[Task]
    backlog_tasks: List[Task]
    next_task: Optional[Task]
    next_action: Literal["blocked", "backlog", "next_up", "nothing_claimable", "empty_project"]
    broken_files: List[BrokenTaskFile]


class TaskCreateRequest(BaseModel):
    """Payload for creating a new task."""

    id: Optional[str] = Field(
        default=None,
        description="Optional explicit task identifier (e.g., task-042).",
    )
    title: str = Field(..., description="Task title summarising the work to be done.")
    description: str = Field(..., description="Markdown working spec (spec.description).")
    summary: Optional[str] = Field(
        default=None,
        description="One-or-two sentence spec.summary. Defaults to the title.",
    )
    intent: Optional[str] = Field(default=None, description="Why the task exists (spec.intent).")
    constraints: Optional[str] = Field(
        default=None, description="Hard requirements and prohibitions (spec.constraints)."
    )
    out_of_scope: Optional[str] = Field(
        default=None, description="Explicit non-goals (spec.out_of_scope)."
    )
    priority: Priority = Field(
        default=Priority.MEDIUM,
        description="Relative urgency for the new task.",
    )
    category: str = Field(
        default="general",
        description="Classification category used for filtering in the UI.",
    )
    lifecycle: Lifecycle = Field(
        default=Lifecycle.DRAFT,
        description="Initial lifecycle: draft (ball human/spec) or ready (agent/available).",
    )
    eligible: List[str] = Field(
        default_factory=list,
        description="Actor ids that may claim the task. Empty means anyone.",
    )
    effort: Optional[str] = Field(
        default=None, description="Estimated effort (time or complexity). Free text."
    )
    tags: List[str] = Field(default_factory=list, description="Arbitrary tag labels.")
    parent: Optional[str] = Field(default=None, description="Umbrella task id, if any.")
    acceptance: List[AcceptanceCriterion] = Field(
        default_factory=list,
        description="Checklist defining done.",
    )
    deliverables: List[Deliverable] = Field(
        default_factory=list,
        description="Deliverables to be produced for task completion.",
    )
    dependencies: List[Dependency] = Field(
        default_factory=list,
        description="Task dependencies tracked in the system.",
    )
    links: List[Link] = Field(
        default_factory=list,
        description="External references relevant to the task.",
    )
    branches: List[Branch] = Field(
        default_factory=list,
        description="Git branches associated with the task lifecycle.",
    )

    def manager_kwargs(self) -> Dict[str, Any]:
        """Reshape the flat request into TaskManager.create_task keyword arguments."""
        payload = self.model_dump(exclude_none=True, exclude={"eligible"})
        spec = {
            key: payload.pop(key)
            for key in ("intent", "constraints", "out_of_scope")
            if key in payload
        }
        if spec:
            payload["spec"] = spec
        if self.eligible:
            payload["assignment"] = {"eligible": self.eligible}
        return payload


class TaskUpdateRequest(BaseModel):
    """Payload for partially updating a task.

    The state axes are deliberately absent: lifecycle, ball and outcome move only
    through the claim/handoff/release/close verbs, which log their transitions.
    """

    title: Optional[str] = None
    priority: Optional[Priority] = None
    category: Optional[str] = None
    effort: Optional[str] = None
    tags: Optional[List[str]] = None
    parent: Optional[str] = None
    spec: Optional[Spec] = None
    acceptance: Optional[List[AcceptanceCriterion]] = None
    deliverables: Optional[List[Deliverable]] = None
    dependencies: Optional[List[Dependency]] = None
    links: Optional[List[Link]] = None
    branches: Optional[List[Branch]] = None


class ClaimRequest(BaseModel):
    """An agent takes ownership of a ready task."""

    agent: str = Field(..., description="Actor id claiming the task.")


class HandoffRequest(BaseModel):
    """The ball moves; the ask travels with it."""

    actor: str = Field(..., description="Who is handing the work over.")
    ball: Ball = Field(..., description="Who acts next.")
    ball_reason: BallReason = Field(..., description="Why they hold it.")
    ball_prompt: Optional[str] = Field(
        default=None,
        description="The ask, addressed to the new holder. Required except agent/available.",
    )
    body: Optional[str] = Field(
        default=None, description="Log entry body; defaults to the ball_prompt."
    )


class ReleaseRequest(BaseModel):
    """An agent bows out; the task returns to the pool."""

    actor: str = Field(..., description="Who is releasing the task.")
    body: Optional[str] = Field(default=None, description="Optional log entry body.")


class CloseRequest(BaseModel):
    """End the task with an outcome."""

    actor: str = Field(..., description="Who is closing the task.")
    outcome: Outcome = Field(..., description="How it ended.")
    body: Optional[str] = Field(default=None, description="Optional log entry body.")
    archive: bool = Field(default=False, description="Also hide the task from listings.")


class LogAppendRequest(BaseModel):
    """Append one entry to the unified log."""

    actor: str = Field(..., description="Actor id writing the entry.")
    type: LogEntryType = Field(
        default=LogEntryType.NOTE,
        description="Entry type. Transitions are manager-only and rejected here.",
    )
    body: Optional[str] = Field(default=None, description="Prose, markdown.")
    re: Optional[int] = Field(
        default=None, description="Optional id of the earlier entry this threads to."
    )
    data: Dict[str, Any] = Field(default_factory=dict, description="Optional structured payload.")


class ProgressUpdateRequest(BaseModel):
    """Progress update payload appended to the task log."""

    author: str
    summary: str
    details: Optional[str] = None
