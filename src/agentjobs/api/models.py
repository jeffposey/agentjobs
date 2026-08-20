"""Request/response models for AgentJobs REST API (schema v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from agentjobs.manager import DependencyFacts, TaskManager
from agentjobs.models_v2 import (
    AcceptanceCriterion,
    Ball,
    BallReason,
    Branch,
    ContextPointer,
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


class TaskRead(Task):
    """Task plus server-computed dependency state for read surfaces."""

    unmet_needs: List[str] = Field(default_factory=list)
    actionable: bool = False
    needs_cycles: List[List[str]] = Field(default_factory=list)
    unblocks_count: int = 0
    open_children_count: int = 0

    @classmethod
    def from_tasks(cls, manager: TaskManager, tasks: List[Task]) -> List["TaskRead"]:
        """Enrich a list of tasks with dependency facts computed over the whole corpus.

        The facts are deliberately not computed from ``tasks``. Every read surface that
        returns rows -- the full list, a filtered list, a search -- goes through here,
        and a count scoped to whichever rows the filter returned would report 0 for a
        parent whose children the filter excluded. Inside a request's
        ``corpus_snapshot`` the corpus is already parsed, so this costs no extra reads.
        """
        facts = manager.dependency_facts()
        return [cls.from_task(task, facts[task.id]) for task in tasks]

    @classmethod
    def from_task(cls, task: Task, facts: DependencyFacts) -> "TaskRead":
        """Attach dependency facts without changing the persisted task schema."""
        return cls.model_validate(
            {
                **task.model_dump(exclude={"display_status"}),
                "unmet_needs": list(facts.unmet_needs),
                "actionable": facts.actionable,
                "needs_cycles": [list(cycle) for cycle in facts.needs_cycles],
                "unblocks_count": facts.unblocks_count,
                "open_children_count": facts.open_children_count,
            }
        )


class DependencyRelation(BaseModel):
    """Resolved dependency relation shown on a task detail page."""

    task_id: str
    title: Optional[str]
    exists: bool
    state: Literal["open", "done", "missing"]
    note: Optional[str]
    reason: str


class ScopedDependencyEdge(BaseModel):
    """A sequence arrow in one umbrella's contained task graph."""

    source: str
    target: str
    note: Optional[str]
    source_exists: bool
    target_exists: bool
    source_contained: bool
    target_contained: bool


class DashboardStats(BaseModel):
    """Counts rendered by the dashboard stat tiles."""

    total: int
    in_progress: int
    blocked: int
    waiting_for_human: int
    awaiting_input: int
    completed: int


class ProjectRevisionResponse(BaseModel):
    """Small file-derived change signal for one project's task collection."""

    revision: str
    task_count: int


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
    active_tasks: List[TaskRead]
    recent_updates: List[DashboardRecentUpdate]
    waiting_tasks: List[TaskRead]
    backlog_tasks: List[TaskRead]
    next_task: Optional[TaskRead]
    next_action: Literal["blocked", "backlog", "next_up", "nothing_claimable", "empty_project"]
    broken_files: List[BrokenTaskFile]


class ReviewIdentity(BaseModel):
    """Configured human identity used by review mutations, or why none is safe."""

    ok: bool
    user: Optional[str]
    problem: Optional[str]
    detail: str


class TaskDetailResponse(BaseModel):
    """Everything the React detail page needs to resume and review one task."""

    task: TaskRead
    parent_task: Optional[TaskRead]
    children: List[TaskRead]
    needs: List[DependencyRelation]
    blocks: List[DependencyRelation]
    related: List[DependencyRelation]
    child_dependency_edges: List[ScopedDependencyEdge]
    identity: ReviewIdentity


class HumanActionResponse(BaseModel):
    """A manager-backed human action returns the newly persisted task state."""

    task: Task


class SafeMutationRequest(BaseModel):
    """Fields every mutation may carry to make a retry safe.

    Both are optional, so existing callers are untouched and keep last-write-wins
    behaviour. A client that supplies them gets: a retry that replays instead of
    writing twice, and a refusal when it decided against a version of the task that
    has since moved on.
    """

    operation_id: Optional[str] = Field(
        default=None,
        description=(
            "Caller-generated UUID. Resending the same request with the same id "
            "replays the original result instead of writing again; reusing it for a "
            "different request is a conflict and writes nothing."
        ),
    )


class RevisionedRequest(SafeMutationRequest):
    """A mutation that acts on content the caller has already read."""

    expected_revision: Optional[datetime] = Field(
        default=None,
        description=(
            "The `updated` value from a prior read. When supplied, the request is "
            "refused if the task changed in the meantime, and the current task is "
            "returned so the caller can decide again."
        ),
    )


class AttachmentUpload(BaseModel):
    """One pasted image, on its way to a sidecar file.

    Base64 in the request body, never in the stored record: the transport needs the
    bytes inline and the YAML must stay readable, and those are different problems with
    different right answers. ``media_type`` is deliberately absent -- the server reads
    the type from the bytes, because a declared type is a claim and the magic number is
    the blob.
    """

    data_base64: str = Field(
        ...,
        min_length=1,
        description="Base64-encoded image bytes (PNG, JPEG or WebP).",
    )
    label: str = Field(
        default="",
        description="Accessible label used as alt text where the image renders.",
    )


class TaskCreateRequest(SafeMutationRequest):
    """Payload for creating a new task."""

    id: Optional[str] = Field(
        default=None,
        description="Optional explicit task identifier (e.g., task-042).",
    )
    actor: Optional[str] = Field(
        default=None,
        description=(
            "Configured actor id to record as the creator. Written to the creation "
            "log entry, so a task can say who filed it. Refused when the project does "
            "not define the id."
        ),
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
    context: List[ContextPointer] = Field(
        default_factory=list,
        description="Curated read-this-first paths and why each one matters (spec.context).",
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
    attachments: List[AttachmentUpload] = Field(
        default_factory=list,
        description="Images evidencing the report, stored as sidecar files.",
    )

    def manager_kwargs(self) -> Dict[str, Any]:
        """Reshape the flat request into TaskManager.create_task keyword arguments."""
        payload = self.model_dump(exclude_none=True, exclude={"eligible", "attachments"})
        spec = {
            key: payload.pop(key)
            for key in ("intent", "constraints", "out_of_scope", "context")
            if key in payload
        }
        if spec:
            payload["spec"] = spec
        if self.eligible:
            payload["assignment"] = {"eligible": self.eligible}
        return payload


class TaskUpdateRequest(RevisionedRequest):
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


class MutationResult(BaseModel):
    """What a mutation did, for callers that need more than the new task.

    Returned only when a request asks for it with `?envelope=true`, so the existing
    task-shaped responses stay exactly as they were. `replayed` is the field that
    cannot be derived any other way: a caller retrying after a timeout has no way to
    tell "I did that" from "you already had".
    """

    project_id: str = Field(description="Project the mutation addressed.")
    operation_id: Optional[str] = Field(
        default=None, description="The operation id the caller supplied, echoed back."
    )
    replayed: bool = Field(
        description=(
            "True when this operation had already been applied, so nothing was written "
            "and no log entry was added."
        )
    )
    task: TaskRead = Field(description="The task as persisted and reloaded.")
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Post-commit side effects that failed, such as webhook delivery. A warning "
            "never means the task write failed; that would be an error."
        ),
    )


class ErrorDetail(BaseModel):
    """One rejected input field."""

    path: str
    message: str


class ErrorBody(BaseModel):
    """The structured error a mutation returns instead of prose.

    An agent has to branch on failures, and pattern-matching an English sentence is
    not a contract. The code set is closed and matches the MCP error vocabulary
    exactly, so the two layers cannot drift into describing the same failure
    differently.
    """

    code: str = Field(description="Stable machine-readable failure code.")
    message: str = Field(description="Human-readable explanation.")
    retryable: bool = Field(description="Whether an identical retry could succeed.")
    detail: str = Field(description="Alias of message, for callers reading FastAPI's shape.")
    task_id: Optional[str] = None
    current_task: Optional[TaskRead] = Field(
        default=None,
        description="Present on a revision conflict: the state that made you stale.",
    )
    field_errors: List[ErrorDetail] = Field(default_factory=list)
    suggested_action: Optional[str] = None


class ClaimRequest(SafeMutationRequest):
    """An agent takes ownership of a ready task."""

    agent: str = Field(..., description="Actor id claiming the task.")


class HandoffRequest(RevisionedRequest):
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


class ReleaseRequest(SafeMutationRequest):
    """An agent bows out; the task returns to the pool."""

    actor: str = Field(..., description="Who is releasing the task.")
    body: Optional[str] = Field(default=None, description="Optional log entry body.")


class PromoteRequest(RevisionedRequest):
    """A draft's spec is finished; the task becomes claimable."""

    actor: str = Field(..., description="Who is promoting the task.")
    body: Optional[str] = Field(default=None, description="Optional log entry body.")


class CloseRequest(RevisionedRequest):
    """End the task with an outcome."""

    actor: str = Field(..., description="Who is closing the task.")
    outcome: Outcome = Field(..., description="How it ended.")
    body: Optional[str] = Field(default=None, description="Optional log entry body.")
    archive: bool = Field(default=False, description="Also hide the task from listings.")


class LogAppendRequest(SafeMutationRequest):
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


class ProgressUpdateRequest(SafeMutationRequest):
    """Progress update payload appended to the task log."""

    author: str
    summary: str
    details: Optional[str] = None


class DispatchRequestBody(BaseModel):
    """Ask AgentJobs to start an agent on this task.

    There is still no ``actor`` field, and that absence is still the design. The actor
    recorded on a dispatch is the author of the log entry that *caused* it, never
    whoever posted the request.

    ``user`` is not that field, and the distinction is the whole of task-188. It does
    not name the cause of the dispatch; it names the person whose authorising entry the
    server should **write** before dispatching. The entry is persisted, then re-read
    from storage, then put through the human-clocked check like any other -- so the
    evidence remains a row in the append-only log, and a request that tried to supply
    its own justification still gets nowhere. The identity claim itself is validated
    against the project's configured actors and refused unless it is ``kind: human``,
    exactly as ``POST /log`` and ``POST /approve`` have always validated theirs.

    Omit ``user`` and nothing changes: the causing entry is whatever the log already
    holds, which is what the CLI, MCP and auto-dispatch do.
    """

    caused_by: Optional[int] = Field(
        default=None,
        description=(
            "Log entry id authorising this dispatch. Defaults to the newest entry. Its "
            "actor must be a configured human."
        ),
    )
    group: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Runner group to choose from, overriding the project's. Names a group this "
            "machine already defines; it never creates one, and it cannot open a gate "
            "that is closed."
        ),
    )
    user: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "The signed-in human clicking Dispatch. Their authorising entry is written "
            "to the task before the run starts, and the dispatch is attributed to it. "
            "Must be an actor this project configures with 'kind: human'. Mutually "
            "exclusive with caused_by."
        ),
    )
    note: Optional[str] = Field(
        default=None,
        description=(
            "What the human typed, when the record could not brief an agent on its own. "
            "Becomes the body of the authorising entry. Only meaningful alongside 'user'."
        ),
    )

    @model_validator(mode="after")
    def _one_authorization(self) -> "DispatchRequestBody":
        """``caused_by`` cites an entry; ``user`` creates one. Never both.

        Refused here as well as in the guard layer, so the browser gets a 422 naming the
        field rather than a 409 naming a rule it did not mean to touch.
        """
        if self.user is not None and self.caused_by is not None:
            raise ValueError(
                "Send either 'caused_by' (cite an existing entry) or 'user' (write a new "
                "one), not both."
            )
        return self


class DispatchStarted(BaseModel):
    """What a successful dispatch reports back."""

    run_id: str = Field(..., description="AgentJobs' identifier for this run.")
    session_id: Optional[str] = Field(
        default=None,
        description="Session mode only, and assigned by the CLI rather than by us.",
    )
    mode: str = Field(..., description="session or batch.")
    posture: str = Field(..., description="What the run is permitted to do.")
    task_id: str = Field(..., description="The task the run is working.")
    caused_by: int = Field(..., description="The log entry this dispatch is attributed to.")
    runner: Optional[str] = Field(default=None, description="Runner that was selected and started.")
    group: Optional[str] = Field(
        default=None,
        description="Runner group it was selected from, when one participated.",
    )
