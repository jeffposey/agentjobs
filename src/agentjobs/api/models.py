"""Request/response models for AgentJobs REST API (schema v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentjobs.manager import DependencyFacts, TaskManager
from agentjobs.models_v2 import (
    AcceptanceCriterion,
    Ball,
    BallReason,
    Branch,
    ContextPointer,
    Deliverable,
    Dependency,
    DispatchPosture,
    Lifecycle,
    Link,
    LogEntryType,
    Outcome,
    Priority,
    QuestionDraft,
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


class AttentionResponse(BaseModel):
    """How much of this project is stopped waiting on a person.

    Its own endpoint rather than a field of the dashboard, because the header renders
    it on every surface and the dashboard projection is 900KB of task records --
    measured against this repository's own corpus, 2026-09-05. A badge that cost that
    on the Tasks tab would not be worth having.
    """

    blocking: int


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


class QueueProblemRead(BaseModel):
    """One broken queue rule, named well enough to fix by hand."""

    kind: str
    band: str
    tasks: List[str] = Field(default_factory=list)
    position: Optional[int] = None
    message: str


class QueueBrokenRead(BaseModel):
    """The queue could not be read, what is wrong with it, and what repairs it.

    Null on a healthy corpus. Present, the dashboard renders a banner naming the
    offending tasks instead of a next action computed from an order that does not
    exist -- design section 8's React row.
    """

    problems: List[QueueProblemRead] = Field(default_factory=list)
    repair_command: str


class ReviewIdentity(BaseModel):
    """Configured human identity used by review mutations, or why none is safe."""

    ok: bool
    user: Optional[str]
    problem: Optional[str]
    detail: str


class DashboardResponse(BaseModel):
    """The complete Python-computed dashboard contract."""

    stats: DashboardStats
    active_tasks: List[TaskRead]
    recent_updates: List[DashboardRecentUpdate]
    waiting_tasks: List[TaskRead]
    backlog_tasks: List[TaskRead]
    next_task: Optional[TaskRead]
    queue_preview: List[TaskRead] = Field(
        default_factory=list,
        description=(
            "The head of the claimable queue, in the queue's own order. `next_task` is "
            "its first element. Sent as a list because the slot board offers a way to "
            "*start* each of them, one per free run slot -- so it is at least "
            "`QUEUE_PREVIEW_LIMIT` long and grows with this machine's "
            "`max_concurrent_runs`, which is what decides how many cells the board has."
        ),
    )
    next_action: Literal[
        "blocked", "backlog", "queue_broken", "next_up", "nothing_claimable", "empty_project"
    ]
    broken_files: List[BrokenTaskFile]
    queue_broken: Optional[QueueBrokenRead] = None
    identity: ReviewIdentity


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

    **Unknown fields are refused, not ignored** -- D2, the same rule every stored v2
    model already follows through ``StrictModel``. These request models had escaped it
    because they descend from ``BaseModel`` directly, so a mutation carrying a field
    the server does not know was accepted, dropped, and answered ``200``. The caller
    then has every reason to believe it worked. That is the failure the field
    allowlists exist to prevent -- ``queue_position`` in ``TaskUpdateRequest`` most of
    all, where a silently ignored patch would leave somebody certain they had moved a
    task that had not moved.
    """

    model_config = ConfigDict(extra="forbid")

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
    posture: Optional[DispatchPosture] = Field(
        default=None,
        description=(
            "What a run dispatched at this task may do. Content, not a state axis: it "
            "is a request bounded by the project's machine-local ceiling, never a "
            "grant, so it needs no verb of its own (task-308). Send null to clear it."
        ),
    )
    spec: Optional[Spec] = None
    acceptance: Optional[List[AcceptanceCriterion]] = None
    deliverables: Optional[List[Deliverable]] = None
    dependencies: Optional[List[Dependency]] = None
    links: Optional[List[Link]] = None
    branches: Optional[List[Branch]] = None


class QueueMutationRequest(RevisionedRequest):
    """A queue mutation: attributed, retry-safe, and neither field optional.

    ``operation_id`` is **required** here, unlike on the older verbs where it had to
    stay optional so callers written before it existed kept working. Nothing was ever
    written against these routes, so there is no such caller to protect, and a reorder
    a timeout can silently apply twice is exactly the failure the ledger exists to
    prevent -- a duplicated move puts a task somewhere nobody asked for and leaves two
    ``queue_move`` entries each claiming to be the decision.
    """

    actor: str = Field(..., min_length=1, description="Actor id performing the move.")
    operation_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Caller-generated UUID. Resending the same request with the same id "
            "replays the original result instead of moving the task twice."
        ),
    )
    body: Optional[str] = Field(
        default=None,
        description="Log body for the queue_move entry. Omit it and the manager writes its own.",
    )


class QueueMoveRequest(QueueMutationRequest):
    """Where in its band a task is being put. Exactly one placement, never two.

    "before task-063 and also at the top" is two answers to one question, and choosing
    one of them on the caller's behalf is how a reorder ends up somewhere nobody asked
    for. The manager refuses it; this refuses it a layer earlier, where the caller can
    still see which fields it sent.
    """

    before: Optional[str] = Field(default=None, description="Place it ahead of this task.")
    after: Optional[str] = Field(default=None, description="Place it behind this task.")
    top: bool = Field(default=False, description="Place it first in its band.")
    bottom: bool = Field(default=False, description="Place it last in its band.")
    with_children: bool = Field(
        default=False,
        description="Carry the task's open same-band descendants with it, contiguously.",
    )

    @model_validator(mode="after")
    def _exactly_one_placement(self) -> "QueueMoveRequest":
        """Refuse zero placements and refuse two."""
        chosen = [
            name
            for name, given in (
                ("before", self.before is not None),
                ("after", self.after is not None),
                ("top", self.top),
                ("bottom", self.bottom),
            )
            if given
        ]
        if len(chosen) != 1:
            given = ", ".join(chosen) if chosen else "none"
            raise ValueError(
                "exactly one placement is required -- before, after, top or bottom "
                f"(given: {given})"
            )
        return self


class QueueKeepRequest(RevisionedRequest):
    """Keep a queue position that was moved over a stated warning.

    The other half of the notice the queue-move check raises: `undo` is an ordinary
    move back, and this is what "keep" sends. It records the strong anchor -- a place a
    person defended against an objection they had read -- which `reorder` may not move.
    """

    actor: str = Field(..., min_length=1, description="Actor id keeping the position.")
    operation_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Caller-generated UUID. Resending the same request with the same id "
            "replays the original result instead of writing a second anchor."
        ),
    )
    body: Optional[str] = Field(
        default=None,
        description="Why it was kept. Omit it and the manager writes its own sentence.",
    )


class QueueMoveWarning(BaseModel):
    """One deterministic finding about a move that has already landed.

    Computed synchronously by `agentjobs.queue_check` from facts the move handler had
    already read -- no model, no dispatch, no delay. `kind` is the closed vocabulary a
    client branches on; `message` is the sentence every surface shows, written once in
    the check so the CLI, this response and the browser cannot describe one fact three
    ways.
    """

    kind: str = Field(
        description=(
            "One of: queue_broken, promoted_unclaimable, above_prerequisite, "
            "demoted_blocker, no_op."
        )
    )
    message: str = Field(description="The finding, as a sentence meant to be shown as-is.")
    tasks: List[str] = Field(
        default_factory=list,
        description="Every task this finding is about, even when the message names fewer.",
    )


class QueueMovePlacement(BaseModel):
    """A placement, in the shape the queue-move route accepts one.

    Returned as `queue_undo` so a client can offer one-click undo without deriving the
    inverse itself. It names a neighbour rather than a number, because a number can be
    rewritten by a rebalance while "behind task-063" cannot.
    """

    kind: str = Field(description="top, bottom, before or after.")
    target: Optional[str] = Field(default=None, description="The neighbour, for before and after.")


class ReprioritizeRequest(QueueMutationRequest):
    """Change a task's band, and optionally where it lands inside the new one.

    The default placement is the bottom of the target band. A band change already says
    everything about urgency; where inside the new band it goes is a separate question
    the caller may answer, and "bottom" is the answer that assumes least.
    """

    priority: Priority = Field(..., description="The band to move the task into.")
    before: Optional[str] = Field(default=None, description="Place it ahead of this task.")
    after: Optional[str] = Field(default=None, description="Place it behind this task.")
    top: bool = Field(default=False, description="Place it first in the target band.")

    @model_validator(mode="after")
    def _at_most_one_placement(self) -> "ReprioritizeRequest":
        """Zero is the documented default; two is still two answers to one question."""
        chosen = [
            name
            for name, given in (
                ("before", self.before is not None),
                ("after", self.after is not None),
                ("top", self.top),
            )
            if given
        ]
        if len(chosen) > 1:
            raise ValueError(
                "at most one placement may be given -- before, after or top "
                f"(given: {', '.join(chosen)})"
            )
        return self


class QueueMaintenanceRequest(BaseModel):
    """Repair or compaction: attributed, and required to say who asked.

    ``operation_id`` is required for the same reason it is on a move -- a caller that
    cannot name its attempt cannot be told apart from one retrying -- but it is not
    replayed from a ledger, because the ledger is per-task and these two operations act
    on a whole band or a whole corpus. They do not need one: both are idempotent by
    construction, since repairing a repaired queue finds nothing to repair and
    compacting a compacted band renumbers it to the numbers it already has. Running
    twice and running once leave the same corpus, which is a stronger property than
    replay rather than a weaker substitute for it.
    """

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(..., min_length=1, description="Actor id asking for the operation.")
    operation_id: str = Field(
        ..., min_length=1, description="Caller-generated UUID identifying this attempt."
    )


class QueueCompactRequest(QueueMaintenanceRequest):
    """Renumber one band back to 100, 200, 300..."""

    band: Priority = Field(..., description="The band to compact. One band per request.")


class QueueMoveProvenanceRead(BaseModel):
    """Where a task's place came from: the last ``queue_move`` that set it.

    The anchor evidence a ``reorder`` run reads (``playbooks/reorder.md``), so it does
    not have to fetch and scan every task's log to learn who put each task where.

    Every field is present on every record. ``kind`` and ``anchor`` are nullable rather
    than absent, so a reader always finds the key and only ever has to judge its value
    -- an absent ``kind`` and a ``kind`` of ``null`` would mean the same thing here and
    only one of them can be checked in a single expression.
    """

    actor: str = Field(..., description="Actor id that wrote the move. Who executed it.")
    kind: Optional[str] = Field(
        ...,
        description=(
            "'human' or 'agent' per the project's actors vocabulary. Null when config "
            "does not define the id -- unknown, and not to be read as either kind."
        ),
    )
    at: str = Field(..., description="When the move was written, ISO-8601.")
    body: str = Field(
        ...,
        description=(
            "The reason recorded with the move. Where an agent-executed human decision "
            "says whose decision it was, which the actor alone cannot tell you."
        ),
    )
    anchor: Optional[str] = Field(
        ...,
        description=(
            "'strong' when a human kept this place after reading the move's warnings. "
            "Null otherwise. A strong anchor is never moved by a reorder run."
        ),
    )


class QueueEntryRead(BaseModel):
    """One task's place in line, and whether it can be taken."""

    task: str
    title: str
    queue_position: Optional[int] = None
    lifecycle: str
    ball: Optional[str] = None
    claimable: bool
    reason: Optional[str] = Field(
        default=None, description="Why it is not claimable. Null when it is."
    )
    last_move: Optional[QueueMoveProvenanceRead] = Field(
        default=None, description="The move that set this place. Null if it never moved."
    )


class QueueBandRead(BaseModel):
    """One priority band, in queue order. Listed even when empty."""

    band: str
    entries: List[QueueEntryRead] = Field(default_factory=list)


class QueueResponse(BaseModel):
    """The whole ordered backlog. This is the list a human reviews.

    It renders a broken queue rather than refusing to: ``problems`` says what is wrong
    and ``repair_command`` says what to type. That is design section 8's deliberate
    exception -- you have to be able to see a broken queue in order to fix it.
    """

    bands: List[QueueBandRead] = Field(default_factory=list)
    problems: List[QueueProblemRead] = Field(default_factory=list)
    repair_command: str


class QueueAssignmentRead(BaseModel):
    """One position a repair or a compaction wrote."""

    task: str
    band: str
    position: int


class QueueRepairResponse(BaseModel):
    """What a repair did. Everything it guessed is named, which is the point."""

    assigned: List[QueueAssignmentRead] = Field(default_factory=list)
    rebalanced: List[str] = Field(default_factory=list)
    unrepairable: List[str] = Field(default_factory=list)
    changed: bool
    report: str = Field(description="The same result rendered for a terminal.")


class QueueCompactResponse(BaseModel):
    """The tasks a compaction renumbered, and what they were renumbered to."""

    band: str
    moved: List[QueueAssignmentRead] = Field(default_factory=list)


class SkippedTaskRead(BaseModel):
    """One open task the queue passed over, and the rule that did it."""

    task: str
    position: Optional[int] = None
    reason: str


class NextExplanationResponse(BaseModel):
    """Why this task is next -- the answer plus the work it stands in front of.

    ``task`` is null when nothing is claimable, in which case ``skipped`` lists every
    open task with the rule that excluded it. That is the listing a reader wants
    precisely when a tool has just told them there is nothing to do.
    """

    task: Optional[str] = None
    band: Optional[str] = None
    queue_position: Optional[int] = None
    empty_bands_above: List[str] = Field(default_factory=list)
    skipped: List[SkippedTaskRead] = Field(default_factory=list)


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
    queue_warnings: List[QueueMoveWarning] = Field(
        default_factory=list,
        description=(
            "What the queue-move check found about a reorder that has already landed. "
            "Empty for every other verb, and empty for most moves -- silence is the "
            "normal outcome. A finding here never means the move was refused."
        ),
    )
    queue_undo: Optional[QueueMovePlacement] = Field(
        default=None,
        description=(
            "The placement that puts the task back where the move took it from, "
            "offered only alongside a queue warning and only for a single-task move."
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
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "The claiming session's own id, when the claim comes from an agent session "
            "AgentJobs did not start (task-354). With it, the claim writes an interactive "
            "run record so the work shows as running; without it, a claim is exactly what "
            "it was. Claude Code's MCP server and the CLI fill it from "
            "CLAUDE_CODE_SESSION_ID."
        ),
    )
    session_cwd: Optional[str] = Field(
        default=None,
        description=(
            "The directory that session runs in. Where the driver keeps its transcript "
            "store, so the task page can show the session's log. Defaults to the project "
            "root."
        ),
    )


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
    questions: List[QuestionDraft] = Field(
        default_factory=list,
        description=(
            "Questions to pose alongside this handoff, each optionally offering "
            "options. Written in the same mutation, so the human never opens a "
            "half-populated form."
        ),
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


class RedactRequest(RevisionedRequest):
    """Replace the text of one prose region with a stated redaction (task-376).

    Reachable over HTTP because the CLI is a service client. It is the only verb that
    reaches into the append-only log, and it stays that: the request names one region,
    supplies the replacement, and states a reason, all of which the manager records.
    """

    actor: str = Field(..., description="Who is redacting.")
    field: str = Field(
        ...,
        description="The region: a task field name, or 'log[<id>].body'.",
        examples=["spec.description", "log[12].body"],
    )
    replacement: str = Field(..., description="What the region says instead.")
    reason: str = Field(..., description="Why the text was removed. Recorded verbatim.")


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
    posture: Optional[DispatchPosture] = Field(
        default=None,
        description=(
            "What this one run may do, overriding both the project default and any "
            "posture on the task record. Refused with 'posture_above_ceiling' when it "
            "exceeds the project's machine-local max_posture -- populate a chooser "
            "from the dispatch state view's 'offerable_postures' so the refusal is "
            "never reachable by clicking (task-308)."
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
