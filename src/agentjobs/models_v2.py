"""Schema v2 data models for AgentJobs.

Implements the design accepted in ``docs/schema-design.md``. Read that document for
*why* any of this is shaped the way it is; this module is the enforcement.

**These models are not yet wired into the application.** v1 (``models.py``) still runs
storage, the manager, the API and the GUI. Replacing it in place would break 26 modules,
roughly 180 field references and all 38 corpus files in a single commit, leaving main
red until task-052. Task-051 owns the migration and the switchover; this module lands
first so it can be reviewed on its own.

The machine-readable definition of the same schema lives in ``schema/agentjobs-v2.yaml``
and the two are checked against each other by loading
``schema/examples/task-048.v2.yaml`` -- a file that validates against the LinkML schema
-- in the test suite. If they drift, that test fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, model_validator

SCHEMA_VERSION = 2
"""The version stamp every v2 file carries (design doc D3, section 8)."""


# ---------------------------------------------------------------------------
# Vocabularies
#
# Closed and model-enforced, per tenet 5: semantics are enforced, taxonomy is
# configurable, prose is free. `category` and `tags` are deliberately plain strings
# here -- they are project taxonomy, validated against config by task-052, not by the
# model.
# ---------------------------------------------------------------------------


class Lifecycle(str, Enum):
    """Where a task is in its life (design doc section 3)."""

    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    CLOSED = "closed"


class Ball(str, Enum):
    """Who acts next. Required while a task is open; null only when closed."""

    AGENT = "agent"
    HUMAN = "human"
    EXTERNAL = "external"


class BallReason(str, Enum):
    """Why the ball holder holds it. Scoped to the holder -- see BALL_REASONS."""

    # ball: agent
    AVAILABLE = "available"
    WORK = "work"
    REVISE = "revise"
    # ball: human
    SPEC = "spec"
    REVIEW = "review"
    DECISION = "decision"
    APPROVAL = "approval"
    INPUT = "input"
    # ball: external
    DEPENDENCY = "dependency"
    SERVICE = "service"


BALL_REASONS: Dict[Ball, frozenset[BallReason]] = {
    Ball.AGENT: frozenset({BallReason.AVAILABLE, BallReason.WORK, BallReason.REVISE}),
    Ball.HUMAN: frozenset(
        {
            BallReason.SPEC,
            BallReason.REVIEW,
            BallReason.DECISION,
            BallReason.APPROVAL,
            BallReason.INPUT,
        }
    ),
    Ball.EXTERNAL: frozenset({BallReason.DEPENDENCY, BallReason.SERVICE}),
}
"""Which reasons belong to which ball holder (consistency rule 2).

A single flat enum with a scoping table, rather than three enums, so that
``ball_reason`` is one field with one type while still being unable to hold
``human/work`` or ``agent/review``.
"""


class Outcome(str, Enum):
    """How a task ended. Set if and only if lifecycle is closed."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"


class Priority(str, Enum):
    """Relative urgency."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AcceptanceStatus(str, Enum):
    """State of one acceptance criterion.

    Deliberately distinct from DeliverableStatus: a criterion is *verified* (met), a
    deliverable is *produced* (done). Collapsing them recreates the v1 problem of one
    word straining across meanings (design doc section 3).
    """

    PENDING = "pending"
    MET = "met"
    FAILED = "failed"
    DROPPED = "dropped"


class DeliverableStatus(str, Enum):
    """State of one deliverable."""

    PENDING = "pending"
    DONE = "done"
    DROPPED = "dropped"


class BranchStatus(str, Enum):
    """Git branch lifecycle. Unchanged from v1 -- genuinely distinct."""

    ACTIVE = "active"
    MERGED = "merged"
    ABANDONED = "abandoned"


class DependencyType(str, Enum):
    """Relationship to another task. v1's `depends_on` is renamed `needs`."""

    NEEDS = "needs"
    BLOCKS = "blocks"
    RELATED = "related"


class LinkRel(str, Enum):
    """What an external link points at."""

    PR = "pr"
    ISSUE = "issue"
    DOC = "doc"
    DESIGN = "design"
    BUILD = "build"
    OTHER = "other"


class LogEntryType(str, Enum):
    """Type of a log entry (design doc section 4)."""

    NOTE = "note"
    PROGRESS = "progress"
    TRANSITION = "transition"
    HANDOFF = "handoff"
    DECISION = "decision"
    QUESTION = "question"
    ANSWER = "answer"
    INSTRUCTION = "instruction"


# ---------------------------------------------------------------------------
# Nested value objects
#
# Eleven of the twelve v2 classes have no identifier -- they are value objects owned by
# one task, which is the measured argument in design doc section 7 for staying
# document-shaped rather than moving to a relational store.
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Base for every v2 model: unknown fields are an error.

    D2, strict everywhere. A tolerated unknown field is a silent no-op whichever writer
    produced it -- a migrator bug writing `pirority`, an API caller passing a field a
    rename retired, a GUI form posting a stale key. Under `extra="forbid"` every one of
    those fails immediately, by name. Strictness is the regression test for the
    migrator and the API.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContextPointer(StrictModel):
    """A curated "read this first" pointer, with the reason it matters."""

    path: str = Field(..., description="Repository-relative path.")
    why: str = Field(..., description="Why a reader should open this first.")


class Spec(StrictModel):
    """The specification, split along the questions agents actually ask.

    v1 had one `description` blob plus a `prompts.starter` that largely restated it.
    """

    summary: str = Field(
        ...,
        description="One or two sentences. The only summary, for every audience.",
    )
    intent: Optional[str] = Field(default=None, description="WHY this task exists (markdown).")
    description: Optional[str] = Field(
        default=None, description="WHAT to do (markdown) -- the working spec."
    )
    constraints: Optional[str] = Field(
        default=None, description="Hard requirements and prohibitions (markdown)."
    )
    out_of_scope: Optional[str] = Field(
        default=None, description="Explicit non-goals, so agents do not wander."
    )
    context: List[ContextPointer] = Field(
        default_factory=list, description="Read-this-first pointers with reasons."
    )


class Assignment(StrictModel):
    """Live ownership and authoring-time eligibility.

    v1's `assigned_to` conflated these: documented as "currently assigned" and used as
    a static label.
    """

    owner: Optional[str] = Field(
        default=None,
        description="Actor id holding the task. Set on claim, cleared on release/close.",
    )
    eligible: List[str] = Field(
        default_factory=list,
        description="Actor ids that may claim this task. Empty means anyone.",
    )


class AcceptanceCriterion(StrictModel):
    """One element of the definition of done."""

    id: str = Field(..., description="Identifier scoped to the task, e.g. ac-1.")
    text: str = Field(..., description="What must be true.")
    verify: Optional[str] = Field(
        default=None,
        description="Optional machine-checkable hint, e.g. a command to run.",
    )
    status: AcceptanceStatus = Field(default=AcceptanceStatus.PENDING)


class Deliverable(StrictModel):
    """An artifact this task produces."""

    path: str = Field(..., description="Repository-relative path.")
    note: Optional[str] = Field(default=None, description="What it is.")
    status: DeliverableStatus = Field(default=DeliverableStatus.PENDING)


class Dependency(StrictModel):
    """A typed relationship to another task."""

    task: str = Field(..., description="Referenced task id.")
    type: DependencyType = Field(default=DependencyType.NEEDS)
    note: Optional[str] = Field(default=None, description="Why the relationship exists.")


class Link(StrictModel):
    """An external resource."""

    url: HttpUrl = Field(..., description="Validated URL.")
    rel: LinkRel = Field(default=LinkRel.OTHER, description="What it points at.")
    title: Optional[str] = Field(default=None, description="Display title.")


class Branch(StrictModel):
    """Git branch lifecycle metadata."""

    name: str = Field(..., description="Branch name.")
    status: BranchStatus = Field(default=BranchStatus.ACTIVE)
    merged_at: Optional[datetime] = Field(default=None)


class LogEntry(StrictModel):
    """One entry in the unified append-only log (design doc section 4).

    Replaces v1's `status_updates`, `comments` and `prompts.followups` -- three
    append-only authored lists with an implied but unenforced role split.
    """

    id: int = Field(..., ge=1, description="Per-task integer, assigned by the manager.")
    ts: datetime = Field(..., description="When the entry was written.")
    actor: str = Field(
        ...,
        description="Actor id. A bare reference; kind is resolved from config (D4).",
    )
    type: LogEntryType = Field(..., description="Entry type.")
    body: Optional[str] = Field(default=None, description="Prose, markdown.")
    re: Optional[int] = Field(
        default=None, ge=1, description="Optional id of the entry this threads to."
    )
    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured payload, typed per entry type.",
    )


# ---------------------------------------------------------------------------
# The root aggregate
# ---------------------------------------------------------------------------


class Task(StrictModel):
    """A task, in schema v2.

    The five consistency rules from design doc section 3 are enforced below in
    ``_check_consistency``. They are what make limbo unrepresentable: an open task with
    nobody responsible, or a handoff with no stated ask, cannot be written down.
    """

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        alias="schema",
        description="Schema version stamp. Always 2 for this model (D3).",
    )

    id: str = Field(..., description="Unique task identifier.")
    title: str = Field(..., description="Task title.")
    created: datetime
    updated: datetime

    # ----- state: three orthogonal axes plus an outcome -----
    lifecycle: Lifecycle = Field(default=Lifecycle.DRAFT)
    ball: Optional[Ball] = Field(default=None, description="Who acts next. Required while open.")
    ball_reason: Optional[BallReason] = Field(
        default=None, description="Why they hold it, scoped to the holder."
    )
    ball_prompt: Optional[str] = Field(
        default=None,
        description="The ask, addressed to whoever holds the ball. Required when ball is set.",
    )
    outcome: Optional[Outcome] = Field(
        default=None, description="How it ended. Set only when closed."
    )
    archived: bool = Field(
        default=False,
        description="Visibility flag, orthogonal to how the task ended.",
    )

    priority: Priority = Field(default=Priority.MEDIUM)
    category: str = Field(..., description="Project taxonomy; validated against config.")
    tags: List[str] = Field(default_factory=list)
    effort: Optional[str] = Field(
        default=None, description="Free text. An estimate, not a contract."
    )

    assignment: Assignment = Field(default_factory=Assignment)
    parent: Optional[str] = Field(default=None, description="Task id of the umbrella task, if any.")

    spec: Spec
    acceptance: List[AcceptanceCriterion] = Field(default_factory=list)
    deliverables: List[Deliverable] = Field(default_factory=list)

    dependencies: List[Dependency] = Field(default_factory=list)
    links: List[Link] = Field(default_factory=list)
    branches: List[Branch] = Field(default_factory=list)

    log: List[LogEntry] = Field(default_factory=list)

    # ----- consistency rules (design doc section 3) -----

    @model_validator(mode="after")
    def _check_consistency(self) -> "Task":
        """Enforce the five rules that make the state model coherent.

        Note on null versus absent: they mean the same thing for `ball` and `outcome`,
        and both are accepted. Omission is the canonical form -- what the migrator and
        manager write -- but a human being explicit with `outcome: null` must not be
        punished for it. Pydantic's Optional[X] = None gives this for free; it is
        recorded here because the generated JSON Schema is *stricter* than this model
        and rejects an explicit null (design doc section 3).
        """
        closed = self.lifecycle is Lifecycle.CLOSED

        # 1. ball is absent-or-null if and only if the task is closed.
        if closed and self.ball is not None:
            raise ValueError("a closed task must not have a ball; it is over")
        if not closed and self.ball is None:
            raise ValueError(
                f"lifecycle '{self.lifecycle.value}' is open, so ball is required "
                "(agent, human or external) -- an open task nobody holds is limbo"
            )

        # 2. ball_reason must belong to the current holder's vocabulary.
        if self.ball is None:
            if self.ball_reason is not None:
                raise ValueError("ball_reason is set but no ball is; who holds it?")
        else:
            if self.ball_reason is None:
                raise ValueError(f"ball is '{self.ball.value}', so ball_reason is required")
            allowed = BALL_REASONS[self.ball]
            if self.ball_reason not in allowed:
                permitted = ", ".join(sorted(reason.value for reason in allowed))
                raise ValueError(
                    f"ball_reason '{self.ball_reason.value}' does not belong to "
                    f"ball '{self.ball.value}'; expected one of: {permitted}"
                )

        # 3. outcome is set if and only if the task is closed.
        if closed and self.outcome is None:
            raise ValueError(
                "a closed task needs an outcome (completed, cancelled, superseded "
                "or duplicate) -- how it ended is data, not a lifecycle fork"
            )
        if not closed and self.outcome is not None:
            raise ValueError(
                f"outcome '{self.outcome.value}' is set but lifecycle is "
                f"'{self.lifecycle.value}'; only closed tasks have an outcome"
            )

        # 4. ball_prompt is required whenever the ball is set. A default is permitted
        #    for agent/available, where the spec is itself the ask.
        #    ball_reason is non-None here: rule 2 above rejects a ball without one.
        #    Spelling that out rather than asserting it keeps the check honest if the
        #    rules are ever reordered.
        holder, reason = self.ball, self.ball_reason
        if holder is not None and reason is not None:
            if reason is not BallReason.AVAILABLE and not (self.ball_prompt or "").strip():
                raise ValueError(
                    f"ball is '{holder.value}/{reason.value}' so ball_prompt is "
                    "required: a handoff without its ask is a notification with no "
                    "payload"
                )

        # 5. owner tracks lifecycle: absent before a claim, present while active.
        if self.lifecycle in (Lifecycle.DRAFT, Lifecycle.READY) and self.assignment.owner:
            raise ValueError(
                f"lifecycle '{self.lifecycle.value}' is unclaimed, so assignment.owner "
                f"must be empty (got '{self.assignment.owner}')"
            )
        if self.lifecycle is Lifecycle.ACTIVE and not self.assignment.owner:
            raise ValueError("an active task is claimed, so assignment.owner is required")

        return self

    @model_validator(mode="after")
    def _check_log(self) -> "Task":
        """Log ids are unique, ordered, and `re:` points at an entry that exists."""
        seen: set[int] = set()
        previous = 0
        for entry in self.log:
            if entry.id in seen:
                raise ValueError(f"duplicate log entry id {entry.id}")
            if entry.id < previous:
                raise ValueError(
                    f"log entry {entry.id} follows {previous}; the log is append-only "
                    "and must be in ascending id order"
                )
            seen.add(entry.id)
            previous = entry.id
        for entry in self.log:
            if entry.re is not None and entry.re not in seen:
                raise ValueError(
                    f"log entry {entry.id} threads to {entry.re}, which does not exist"
                )
            if entry.re is not None and entry.re >= entry.id:
                raise ValueError(
                    f"log entry {entry.id} threads to {entry.re}, which is not earlier"
                )
        return self

    @model_validator(mode="after")
    def _check_parent(self) -> "Task":
        """A task cannot be its own parent. Deeper cycles need the store (task-045)."""
        if self.parent is not None and self.parent == self.id:
            raise ValueError(f"task {self.id} cannot be its own parent")
        return self

    # ----- derived -----

    @property
    def display_status(self) -> str:
        """One human-readable label, derived on read and never stored.

        Storing it was rejected in design doc section 3: a denormalized copy of three
        fields is a drift bug waiting for its moment, and the derivation is this.
        """
        if self.lifecycle is Lifecycle.CLOSED:
            label = (self.outcome or Outcome.COMPLETED).value.capitalize()
            return f"{label} (archived)" if self.archived else label
        if self.ball is Ball.HUMAN:
            human_labels: Dict[BallReason, str] = {
                BallReason.SPEC: "Needs spec",
                BallReason.REVIEW: "Needs review",
                BallReason.DECISION: "Needs decision",
                BallReason.APPROVAL: "Needs approval",
                BallReason.INPUT: "Needs input",
            }
            return human_labels.get(self.ball_reason or BallReason.REVIEW, "Waiting on human")
        if self.ball is Ball.EXTERNAL:
            if self.ball_reason is BallReason.DEPENDENCY:
                blockers = [d.task for d in self.dependencies if d.type is DependencyType.NEEDS]
                return f"Blocked on {blockers[0]}" if blockers else "Blocked"
            return "Blocked on a service"
        if self.ball is Ball.AGENT:
            if self.ball_reason is BallReason.AVAILABLE:
                return "Ready"
            owner = self.assignment.owner
            verb = "Revising" if self.ball_reason is BallReason.REVISE else "In progress"
            return f"{verb} ({owner})" if owner else verb
        # str() because mypy types Enum.value as Any, and this returns str.
        return str(self.lifecycle.value).capitalize()

    @property
    def is_open(self) -> bool:
        """True while the task is not closed."""
        return self.lifecycle is not Lifecycle.CLOSED

    def next_log_id(self) -> int:
        """The id the next appended log entry should carry."""
        return max((entry.id for entry in self.log), default=0) + 1

    def open_questions(self) -> List[LogEntry]:
        """Question entries with no answer threaded to them."""
        answered = {entry.re for entry in self.log if entry.type is LogEntryType.ANSWER}
        return [
            entry
            for entry in self.log
            if entry.type is LogEntryType.QUESTION and entry.id not in answered
        ]

    def priority_rank(self) -> int:
        """Sort key: critical first."""
        return {
            Priority.CRITICAL: 0,
            Priority.HIGH: 1,
            Priority.MEDIUM: 2,
            Priority.LOW: 3,
        }[self.priority]


class SchemaVersionError(ValueError):
    """Raised when a file's schema stamp is missing or not understood."""


def check_schema_version(data: Dict[str, Any], *, source: str = "task data") -> None:
    """Reject payloads that are not stamped as v2, loudly and with the fix.

    D3: a missing `schema` field means v1, and v1 files are converted by a one-shot
    migrator rather than silently coerced. The error names the command, because the
    reader hitting it is usually an agent that has never run it.
    """
    stamp = data.get("schema")
    if stamp is None:
        raise SchemaVersionError(
            f"{source} has no 'schema' field, so it is a v1 file. Convert the corpus "
            "with: agentjobs migrate-schema"
        )
    if stamp != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{source} declares schema {stamp!r}, but this build understands "
            f"schema {SCHEMA_VERSION}. Upgrade agentjobs, or convert with: "
            "agentjobs migrate-schema"
        )


def load_task(data: Dict[str, Any], *, source: str = "task data") -> Task:
    """Validate a mapping into a v2 Task, checking the version stamp first.

    Checking the stamp before validating matters: without it, a v1 file fails with a
    pile of unknown-field and missing-field errors that never mention the real problem.
    """
    check_schema_version(data, source=source)
    return Task.model_validate(data)


def utcnow() -> datetime:
    """Timezone-aware current time, the only clock these models should use."""
    return datetime.now(tz=timezone.utc)


TaskListAdapter: TypeAdapter[List[Task]] = TypeAdapter(List[Task])
"""Convenience adapter for validating a batch, used by the migrator's corpus pass."""
