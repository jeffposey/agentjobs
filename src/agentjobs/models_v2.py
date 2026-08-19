"""Schema v2 data models for AgentJobs.

Implements the design accepted in ``docs/schema-design.md``. Read that document for
*why* any of this is shaped the way it is; this module is the enforcement.

This is **the** schema. Task-052 migrated the corpus and repointed storage, the manager,
the API, the GUI and the CLI at these models, and deleted v1 (``models.py``) outright.
A file without a ``schema: 2`` stamp is a v1 file and is refused by name.

The machine-readable definition of the same schema lives in ``schema/agentjobs-v2.yaml``
and the two are checked against each other by loading
``schema/examples/task-048.v2.yaml`` -- a file that validates against the LinkML schema
-- in the test suite. If they drift, that test fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    computed_field,
    model_validator,
)

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


class ValueEnum(str, Enum):
    """A string enum whose ``str()`` is its value, not ``ClassName.MEMBER``.

    Python 3.11 changed ``__str__`` on mixin enums: ``str(Lifecycle.READY)`` became
    ``"Lifecycle.READY"`` where it used to be ``"ready"``. Comparisons are unaffected --
    these subclass ``str``, so ``task.ball == "human"`` is still True -- which is exactly
    what makes the change dangerous. Every conditional keeps working while every
    *rendered* value silently becomes wrong.

    It escaped the test suite because tests assert on comparisons and on JSON, and
    Pydantic serialises by value regardless. It only shows up where a template
    interpolates an enum: `data-ball="Ball.HUMAN"` broke the task list's filters while
    the badge beside them, which compares, rendered correctly.

    Restoring the mixin's ``__str__`` and ``__format__`` fixes every such site at once,
    rather than appending ``.value`` at each one and waiting for the next template to
    forget.
    """

    __str__ = str.__str__

    def __format__(self, format_spec: str) -> str:
        """Format as the value too, so f-strings agree with str()."""
        return str.__format__(self, format_spec)


class Lifecycle(ValueEnum):
    """Where a task is in its life (design doc section 3)."""

    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    CLOSED = "closed"


class Ball(ValueEnum):
    """Who acts next. Required while a task is open; null only when closed."""

    AGENT = "agent"
    HUMAN = "human"
    EXTERNAL = "external"


class BallReason(ValueEnum):
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


class Outcome(ValueEnum):
    """How a task ended. Set if and only if lifecycle is closed."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"


class Priority(ValueEnum):
    """Relative urgency."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AcceptanceStatus(ValueEnum):
    """State of one acceptance criterion.

    Deliberately distinct from DeliverableStatus: a criterion is *verified* (met), a
    deliverable is *produced* (done). Collapsing them recreates the v1 problem of one
    word straining across meanings (design doc section 3).
    """

    PENDING = "pending"
    MET = "met"
    FAILED = "failed"
    DROPPED = "dropped"


class DeliverableStatus(ValueEnum):
    """State of one deliverable."""

    PENDING = "pending"
    DONE = "done"
    DROPPED = "dropped"


class BranchStatus(ValueEnum):
    """Git branch lifecycle. Unchanged from v1 -- genuinely distinct."""

    ACTIVE = "active"
    MERGED = "merged"
    ABANDONED = "abandoned"


class DependencyType(ValueEnum):
    """Relationship to another task. v1's `depends_on` is renamed `needs`."""

    NEEDS = "needs"
    BLOCKS = "blocks"
    RELATED = "related"


class LinkRel(ValueEnum):
    """What an external link points at."""

    PR = "pr"
    ISSUE = "issue"
    DOC = "doc"
    DESIGN = "design"
    BUILD = "build"
    OTHER = "other"


class LogEntryType(ValueEnum):
    """Type of a log entry (design doc section 4)."""

    NOTE = "note"
    PROGRESS = "progress"
    TRANSITION = "transition"
    HANDOFF = "handoff"
    DECISION = "decision"
    QUESTION = "question"
    ANSWER = "answer"
    INSTRUCTION = "instruction"
    DISPATCH = "dispatch"
    DISPATCH_RESULT = "dispatch_result"


MANAGER_WRITTEN_LOG_TYPES = frozenset(
    {LogEntryType.TRANSITION, LogEntryType.DISPATCH, LogEntryType.DISPATCH_RESULT}
)
"""Entry types only the manager may append (design doc section 3, rule 5).

An entry of one of these types asserts that something *happened* -- a state axis moved,
a process was started, a process ended. Letting a caller post one would put a claim in an
append-only record with no event behind it, which is a lie the log can never retract.
Every write path is expected to refuse these by consulting this set rather than by
listing types of its own, so a type added here cannot be forgotten at one of them.
"""


class DispatchTrigger(ValueEnum):
    """What caused a dispatch (design doc section 5)."""

    MANUAL = "manual"
    AUTO = "auto"


class DispatchMode(ValueEnum):
    """Which process lifecycle a run had (design doc section 4, task-077)."""

    SESSION = "session"
    BATCH = "batch"


class DispatchPosture(ValueEnum):
    """What the run was permitted to do (design doc section 4, task-076)."""

    READ_ONLY = "read_only"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class DispatchOutcome(ValueEnum):
    """How a run ended (design doc section 9).

    ``finished_without_handoff`` is the one that has to be argued for: a run that ends
    without moving the ball is a failure even on a clean exit, because the agent stopped
    without saying what it needs. Calling that success would reproduce, at the process
    level, exactly the limbo the ball model exists to make unrepresentable.

    ``failed`` and ``crashed`` are reachable only from batch runs. A session that errors
    internally still reports ``idle``/``done`` to ``claude agents --json``, which carries
    no exit code -- that gap is the price of session mode and the reason batch was kept.
    """

    COMPLETED = "completed"
    FINISHED_WITHOUT_HANDOFF = "finished_without_handoff"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CRASHED = "crashed"
    INTERRUPTED = "interrupted"


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
    description: str = Field(
        ...,
        description="WHAT to do (markdown) -- the working spec.",
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


class Attachment(StrictModel):
    """One image stored beside the tasks, referenced from the entry it illustrates.

    The blob lives in a sidecar file; only this metadata is in the YAML. That is the
    whole point of the storage decision: a task file stays something a person can read
    in a text editor and git can diff line by line, which a base64 blob would end.

    ``path`` is relative to the project's tasks directory, not to the repository root,
    because that is the directory storage already owns and resolves safely. ``sha256``
    is both the file's name and its integrity check: a read that does not hash to this
    is refused rather than rendered.
    """

    path: str = Field(
        ...,
        description="Sidecar path relative to the tasks directory.",
        examples=["attachments/task-042/6f4b...c1.png"],
    )
    media_type: str = Field(..., description="Image media type, derived from the bytes.")
    sha256: str = Field(..., min_length=64, max_length=64, description="Content hash.")
    size_bytes: int = Field(..., ge=1, description="Size of the stored file.")
    label: str = Field(..., description="Accessible label; alt text where it renders.")


class DispatchCandidateData(StrictModel):
    """One runner a group offered, and what the selector concluded about it.

    Every member of the group appears, winner included, in the order the file declares
    them. A candidate listed after the winner is marked eligible and carries no reason:
    "considered and not reached" is a different fact from "considered and rejected", and
    a reader three weeks later needs to be able to tell them apart.
    """

    runner: str = Field(..., description="Runner name from the group's member list.")
    eligible: bool = Field(..., description="Nothing disqualified it.")
    skipped_because: Optional[str] = Field(
        default=None,
        description=(
            "Why it was passed over: disabled, undefined_runner, or "
            "executable_not_found. Absent on an eligible candidate."
        ),
    )
    detail: Optional[str] = Field(
        default=None,
        description="The member's own note, or what specifically was missing.",
    )


class DispatchSelectionData(StrictModel):
    """How a runner group chose the runner that ran (task-177).

    Present only when a group participated. A machine with a flat ``runners:`` map
    resolves through ``projects.<id>.runner`` exactly as it always has and writes no
    selection at all, so nothing about a config that has never heard of groups changes
    shape in git.

    The winner is the ``runner`` field of the entry this hangs off, not a field here --
    one name for the thing that ran, in the place every existing reader already looks.
    """

    group: str = Field(..., description="Runner group the candidates came from.")
    source: str = Field(
        ...,
        description=(
            "Which rung of the precedence ladder named the group: dispatch, project, " "or machine."
        ),
    )
    candidates: List[DispatchCandidateData] = Field(
        ...,
        min_length=1,
        description="Every member of the group, in declared order, with its verdict.",
    )


class DispatchData(StrictModel):
    """Payload of a ``dispatch`` entry: what was started, by whose authority, against what.

    This is the durable, git-tracked half of a dispatch. Run directories under
    ``~/.agentjobs/runs/`` are machine-local and disposable; this entry is the part that
    survives, so it has to answer "what ran, against what" on its own.

    ``argv`` is recorded verbatim, which means **a runner must never put a secret in its
    argv** -- secrets go in the runner's ``env``, which is never logged. Stated here
    because the recording is the safety feature: weakening it to hide a token would be
    the wrong fix for the wrong problem.
    """

    run_id: str = Field(..., description="Machine-local run identifier.")
    agent: str = Field(..., description="Actor id the run acts as.")
    runner: str = Field(..., description="Runner name from ~/.agentjobs/dispatch.yaml.")
    mode: DispatchMode = Field(..., description="Session or batch (task-077).")
    posture: DispatchPosture = Field(..., description="What the run may do (task-076).")
    trigger: DispatchTrigger = Field(..., description="Manual click or auto-dispatch.")
    caused_by: int = Field(
        ...,
        ge=1,
        description=(
            "Id of the log entry whose actor authorises this dispatch. The loop is "
            "human-clocked (D4), and this is the evidence for that claim."
        ),
    )
    argv: List[str] = Field(..., min_length=1, description="Resolved argv, verbatim.")
    cwd: str = Field(..., description="Working directory the process was started in.")
    git_head: str = Field(
        ...,
        description="Commit the working tree was on, so a run's diff stays attributable.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Session mode only, and never assigned by us: `--bg` manages the id itself "
            "and the dispatcher captures it afterwards, so it can be absent."
        ),
    )
    selection: Optional[DispatchSelectionData] = Field(
        default=None,
        description=(
            "How a runner group chose `runner` (task-177). Absent when no group "
            "participated, which is every dispatch on a flat configuration."
        ),
    )


class DispatchResultData(StrictModel):
    """Payload of a ``dispatch_result`` entry: how a run ended.

    Written on every exit path, including the supervisor's own exception. A run with no
    terminal entry is indistinguishable from a run still going, which is the failure
    mode that hid a webhook bug for months (task-047).
    """

    run_id: str = Field(..., description="The run this concludes.")
    outcome: DispatchOutcome = Field(..., description="How it ended (design section 9).")
    exit_code: Optional[int] = Field(
        default=None,
        description="Batch only. A session reports no exit code, so this stays absent.",
    )
    duration_seconds: Optional[float] = Field(
        default=None, ge=0, description="Wall-clock duration of the run."
    )
    log_path: Optional[str] = Field(
        default=None, description="Machine-local run directory, while it still exists."
    )


DISPATCH_PAYLOADS: Dict[LogEntryType, type[StrictModel]] = {
    LogEntryType.DISPATCH: DispatchData,
    LogEntryType.DISPATCH_RESULT: DispatchResultData,
}
"""Typed ``data`` payloads, enforced on the entry rather than only at the write path.

v2's tenet is that semantics are enforced, not documented. A dispatch entry whose payload
cannot say what ran is worse than no entry: it looks like evidence.
"""


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
    attachments: Optional[List[Attachment]] = Field(
        default=None,
        description="Images evidencing this entry, stored as sidecar files.",
    )

    @model_validator(mode="after")
    def _check_typed_payload(self) -> "LogEntry":
        """Validate ``data`` against the payload model its type declares, where one exists.

        Only the dispatch types have one so far. The alternative -- validating in the
        manager method that writes them -- was rejected because it leaves a hand-edited
        or hand-migrated file free to carry a dispatch entry with nothing in it, and the
        one thing this entry exists to do is be trustworthy after the machine-local run
        directory is gone.
        """
        payload_model = DISPATCH_PAYLOADS.get(self.type)
        if payload_model is not None:
            # The idempotency marker rides in `data` on every manager-written entry. It
            # is infrastructure, not part of any entry's payload, so it is excluded here
            # rather than declared on each payload model.
            payload_model.model_validate(
                {key: value for key, value in self.data.items() if key != "operation"}
            )
        return self


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_status(self) -> str:
        """One human-readable label, derived on read and never stored.

        Storing it was rejected in design doc section 3: a denormalized copy of three
        fields is a drift bug waiting for its moment, and the derivation is this.
        A computed field rather than a plain property so API responses carry it;
        storage excludes it on write, and a file that contains it is rejected by
        name (extra="forbid") rather than silently round-tripped.
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

    @property
    def dispatch_count(self) -> int:
        """How many times this task has been dispatched, counted from the log.

        Derived rather than stored. A counter field would be a second copy of a fact the
        log already carries, so it could disagree with the evidence for it -- and it
        would need a migration on every existing file to introduce. Runaway protection
        (design section 7) reads this, so a count that can drift is a limit that can be
        wrong in the direction that costs money.

        Deliberately a plain property, not a ``computed_field``: unlike
        ``display_status`` it is not something an API response should carry by default,
        and it would then also have to be excluded on write.
        """
        return sum(1 for entry in self.log if entry.type is LogEntryType.DISPATCH)

    def dispatches_since(self, since: datetime) -> int:
        """Dispatches recorded at or after ``since``, for the per-day budget cap.

        Naive timestamps are read as UTC, matching how the rest of the model normalises
        them: a cap that silently counts nothing because two datetimes were not
        comparable is worse than one that is wrong out loud.
        """
        cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return sum(
            1
            for entry in self.log
            if entry.type is LogEntryType.DISPATCH
            and (entry.ts if entry.ts.tzinfo else entry.ts.replace(tzinfo=timezone.utc)) >= cutoff
        )

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
