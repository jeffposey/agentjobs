"""Request/response models for AgentJobs REST API (schema v2)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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
    QueuedDispatchState,
    SelfClearingWait,
    Spec,
    Task,
    TaskSummary,
    queued_display_status,
    self_clearing_wait,
    summary_of,
)

from .queued_dispatch import queued_dispatch_for


class TaskRead(Task):
    """Task plus server-computed dependency state for read surfaces."""

    unmet_needs: List[str] = Field(default_factory=list)
    actionable: bool = False
    needs_cycles: List[List[str]] = Field(default_factory=list)
    unblocks_count: int = 0
    open_children_count: int = 0
    self_clearing_wait: Optional[SelfClearingWait] = None
    """Set when this `external`/`service` park is a quota wait that needs nobody.

    Unlike the dependency facts above, it is not passed in: `_fill_self_clearing_wait`
    derives it from the task's own log on every construction, so no caller can build a
    `TaskRead` that forgets it. Clients need the structure and not only
    `display_status`'s label -- the task list filters on it, and matching on the prose of
    a label is what ENGINEERING.md's rendered-value rule exists to prevent.
    """

    queued_dispatch: Optional[QueuedDispatchState] = None
    """Set when a dispatch of this task is waiting for a free slot on this machine.

    Derived, never stored, and for the same reason as the field above: `dispatch.queue`
    deliberately does not claim the task, because every dispatch gate is judged when a
    slot frees rather than at enqueue (task-459). So `lifecycle`/`ball`/`ball_reason`
    genuinely read `ready`/`agent`/`available` while a start has already been promised,
    and a stored flag would be a second copy of a fact the queue owns.

    Unlike `self_clearing_wait` it cannot be derived from the record at all -- the fact
    lives in the machine's execution store -- so `_fill_queued_dispatch` reads the
    request-scoped binding `api.queued_dispatch` installs. Outside a request there is no
    binding and the field stays `None`.

    Structure rather than only `display_status`'s prose: a client offering to cancel the
    entry needs its `queue_id`, and matching on the words of a label is what
    ENGINEERING.md's rendered-value rule exists to prevent.
    """

    @model_validator(mode="after")
    def _fill_self_clearing_wait(self) -> "TaskRead":
        """Derive the wait from this record, overwriting anything passed for it.

        Here rather than at each construction site, because there are two of them and a
        third would not know to do it. The derivation itself stays in `models_v2`, which
        is also where `display_status` reads it, so the label and the field cannot
        disagree.
        """
        self.self_clearing_wait = self_clearing_wait(self)
        return self

    @model_validator(mode="after")
    def _fill_queued_dispatch(self) -> "TaskRead":
        """Ask this request's dispatch queue about the task, overwriting what was passed.

        Here for the reason above and one more: there are eight construction sites across
        five route modules, and a site that forgot would be a surface telling a person
        nothing is happening to a task the machine has already promised to start.
        """
        self.queued_dispatch = queued_dispatch_for(self.id)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_status(self) -> str:
        """The record's label, with a waiting dispatch named where there is one.

        Overridden here rather than on `Task`, which cannot see the machine's queue. The
        derivation itself stays in `models_v2` beside the one it falls back to, so the
        label and `queued_dispatch` cannot disagree about what is happening.
        """
        return queued_display_status(self, self.queued_dispatch)

    @classmethod
    def from_tasks(cls, manager: TaskManager, tasks: List[Task]) -> List["TaskRead"]:
        """Enrich a list of tasks with dependency facts computed over the whole corpus.

        The facts are deliberately not computed from ``tasks``. Every read surface that
        returns rows -- the full list, a filtered list, a search -- goes through here,
        and a count scoped to whichever rows the filter returned would report 0 for a
        parent whose children the filter excluded. Inside a request's
        :func:`agentjobs.corpus.corpus_scope` the corpus is already loaded, so this
        costs no extra reads.
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


class TaskSummaryRead(TaskSummary):
    """A listing row: the task without its prose, its criteria or its log.

    What ``GET /tasks`` returns. It carries the same server-computed dependency state
    ``TaskRead`` does, because a list is where the claim gate is drawn -- a row is greyed
    out by ``actionable`` and explained by ``unmet_needs``, and a client that had to work
    those out for itself would need the corpus the projection exists to avoid sending.

    Whole records are still on offer, at ``GET /tasks/full``; see the route.
    """

    unmet_needs: List[str] = Field(default_factory=list)
    actionable: bool = False
    needs_cycles: List[List[str]] = Field(default_factory=list)
    unblocks_count: int = 0
    open_children_count: int = 0
    queued_dispatch: Optional[QueuedDispatchState] = None
    """Set when a dispatch of this task is waiting for a free slot on this machine.

    Filled by the same request-scoped binding ``TaskRead`` reads, and for the same
    reason: the fact lives in the machine's execution store rather than in the record,
    so no amount of the record would carry it.
    """

    @model_validator(mode="after")
    def _fill_queued_dispatch(self) -> "TaskSummaryRead":
        """Ask this request's dispatch queue about the task, overwriting what was passed."""
        self.queued_dispatch = queued_dispatch_for(self.id)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_status(self) -> str:
        """The record's label, with a waiting dispatch named where there is one."""
        return queued_display_status(self, self.queued_dispatch)

    @classmethod
    def from_summaries(
        cls, facts: Dict[str, DependencyFacts], summaries: List[TaskSummary]
    ) -> List[Self]:
        """Attach dependency facts to each row.

        The facts are passed in rather than fetched, unlike ``TaskRead.from_tasks``,
        because the caller has already had to list the corpus to produce ``summaries``
        and computing them from that one listing is what stops this route loading the
        project four times over (task-484).
        """
        return [cls.from_summary(summary, facts[summary.id]) for summary in summaries]

    @classmethod
    def from_summary(cls, row: TaskSummary, facts: DependencyFacts, **extra: Any) -> Self:
        """One row, with its dependency facts attached.

        ``extra`` is for a subclass that carries a field the projection does not, so the
        fact-attachment is written once. :class:`TaskCardRead` is the only user, and it
        is why the parameter is ``row`` rather than ``summary``: the field it adds is
        called ``summary``, and the two names collide.
        """
        return cls.model_validate(
            {
                **row.model_dump(mode="python", by_alias=True, exclude={"display_status"}),
                "unmet_needs": list(facts.unmet_needs),
                "actionable": facts.actionable,
                "needs_cycles": [list(cycle) for cycle in facts.needs_cycles],
                "unblocks_count": facts.unblocks_count,
                "open_children_count": facts.open_children_count,
                **extra,
            }
        )


class TaskCardRead(TaskSummaryRead):
    """A dashboard card: a listing row, plus the one line a card prints.

    The dashboard is a page of cards rather than a table, and every one of them --
    ``TaskCard`` on the panels, the free cells of the slot board -- draws the task's
    one-sentence summary under its title. That is the single field the listing row does
    not carry, so it is the single field added here.

    **Why a fourth read model rather than putting ``summary`` on ``TaskSummary``.** That
    would have put it on ``GET /tasks`` as well, where nothing draws it: the summary
    averages 293 bytes on this repository's own backlog, which is a third again on top
    of a row that task-484 had just cut to 785, spent on a field no consumer reads. A
    surface gets the fields it draws, which is the whole of task-483's argument, and
    "one more field, everywhere" is how a projection grows back into a record.

    Built from whole records rather than from rows, because the dashboard has them in
    hand: its recent-updates panel is the ten newest log entries in the project, so the
    snapshot behind this reads logs whatever the cards need. See
    :func:`agentjobs.dashboard.build_dashboard_snapshot`.
    """

    summary: str
    """The task's one-sentence orientation, as :attr:`Spec.summary` holds it.

    Required rather than defaulted, so a card built without one is a validation error
    instead of a page of blank lines under the titles.
    """

    can_brief: bool
    """Whether this record, on its own, could brief an agent that has never seen it.

    One bit rather than the field it is read from. The slot board's free cells offer a
    Dispatch button and have to know whether pressing it would stop to ask a person for
    text, and the answer is keyed on ``spec.description`` -- which is the largest field
    on a record and one no card draws.

    Computed by :func:`agentjobs.dispatch.guards.record_can_brief`, the same function the
    dispatch gate itself calls. The client used to compute it from the description it was
    being sent, with a comment noting that drift between its expression and the server's
    would cost a link instead of a button; there is now one expression and it is the
    gate's own. Required for the reason ``summary`` above is: a card built without it
    would disable a button rather than draw a wrong one, which is the kind of default
    nobody notices.
    """

    @classmethod
    def from_tasks(
        cls, facts: Dict[str, DependencyFacts], tasks: List[Task]
    ) -> List["TaskCardRead"]:
        """Project whole records into cards, attaching each one's dependency facts."""
        return [cls.from_task(task, facts[task.id]) for task in tasks]

    @classmethod
    def from_task(cls, task: Task, facts: DependencyFacts) -> "TaskCardRead":
        """One card. ``summary_of`` is the same projection the store's rows are."""
        # Imported here rather than at module scope: `dispatch.guards` reaches the
        # dispatch config and the execution store, and nothing else in this module needs
        # any of it.
        from agentjobs.dispatch.guards import record_can_brief

        return cls.from_summary(
            summary_of(task),
            facts,
            summary=task.spec.summary,
            can_brief=record_can_brief(task),
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


class AttentionEpisodeView(BaseModel):
    """The current attention episode, as much of it as a client needs to act.

    ``id`` is what a client compares against the last episode it notified for, so one
    desktop notification is drawn per episode however often the page polls. ``tasks``
    is the membership in inbox order; ``lead_task_id`` and ``lead_task_title`` are its
    head, repeated as fields so a notification can name the task without a client
    re-deriving "first" from an array it did not sort.
    """

    id: str
    started_at: datetime
    acknowledged: bool
    tasks: List[str]
    lead_task_id: Optional[str] = None
    lead_task_title: Optional[str] = None
    lead_ask: str = ""
    """What the lead task wants, in the two or three words a lock screen has room for.

    A notification that names the task says *which* work is stopped and never *what is
    being asked*, which is the only thing that decides whether the person has to go and
    find a computer: "Needs review" and "Needs a decision" are acted on differently, and
    a task title distinguishes neither. Rendered here rather than in each client for the
    same reason as ``deep_link`` -- three callers in two languages -- and empty where the
    task carries no reason, which is the one case a client must fall back from rather
    than print a bare dash.
    """
    deep_link: str = ""
    """Where a notification for this episode should land, acknowledgment marker and all.

    Added by task-423 because the rule now has three callers in two languages -- the
    React notifier, the push payload, and the service worker rendering a push that
    arrived while no page was running. Published by the server so the other two read it
    instead of each re-deriving it; ``waitingPath`` in ``episode.ts`` stays as the
    fallback for a bundle talking to a server that predates this field.
    """


class AttentionResponse(BaseModel):
    """How much of this project is stopped waiting on a person, and whether they know.

    Its own endpoint rather than a field of the dashboard, because the header renders
    it on every surface and the dashboard projection is 900KB of task records --
    measured against this repository's own corpus, 2026-09-05. A badge that cost that
    on the Tasks tab would not be worth having. The episode added on task-422 keeps
    that property: it is one small object, never the records.

    ``blocking`` is unchanged and still the badge number. ``episode`` is ``null``
    exactly when nothing is waiting.
    """

    blocking: int
    episode: Optional[AttentionEpisodeView] = None


class PushSubscriptionKeys(BaseModel):
    """The two values a browser hands out with a subscription, base64url as it gives them."""

    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    """A device asking to be woken.

    Exactly the shape ``PushSubscription.toJSON()`` produces, plus two fields of ours,
    so the client can pass the browser's object through without reassembling it.

    ``label`` is for the person -- "Pixel 9", "iPad" -- and is the only way to tell two
    rows apart in the UI, because the endpoint is never shown. ``detail`` is the
    lock-screen privacy setting for this device; it defaults to the quiet one.
    """

    endpoint: str
    keys: PushSubscriptionKeys
    label: str = ""
    detail: str = "count"


class PushUnsubscribeRequest(BaseModel):
    """A device asking to be forgotten, by whichever handle the caller has.

    The page has the id; a service worker reacting to ``pushsubscriptionchange`` has
    only the endpoint it is losing. Either identifies a row, and neither is an error
    when it matches nothing -- a device is routinely removed from both ends at once.
    """

    subscription_id: Optional[str] = None
    endpoint: Optional[str] = None


class PushTestRequest(BaseModel):
    """Which device to prove delivery to. Omitted means every registered device."""

    subscription_id: Optional[str] = None


class PushDeviceView(BaseModel):
    """One registered device, with its endpoint deliberately absent.

    A push endpoint is a URL anybody holding it can send a notification to, so it is
    treated as a secret: the API answers with the push service's host, a label the
    person chose, and what happened to the last attempt. ``healthy`` is false once a
    device has failed enough times in a row to be worth mentioning -- it is a report,
    not a removal.
    """

    id: str
    service: str
    label: str
    detail: str
    created_at: datetime
    last_attempt_at: Optional[datetime] = None
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    healthy: bool = True


class PushStatusResponse(BaseModel):
    """What the notifications panel needs to render itself.

    ``vapid_public_key`` is the application-server key a browser must subscribe
    against. It is public by construction -- the private half never leaves this
    process -- and reading it is what creates the machine's keypair on first use.
    """

    vapid_public_key: str
    contact: str
    watching: bool
    poll_seconds: float
    devices: List[PushDeviceView] = Field(default_factory=list)


class PushSendResult(BaseModel):
    """What one deliberate test push did.

    ``outcome`` rather than the status alone: ``gone`` means the device was forgotten
    as a result, which a status code on its own would not tell the page.
    """

    subscription_id: str
    outcome: str
    status: Optional[int] = None
    error: Optional[str] = None


class PushTestResponse(BaseModel):
    """One result per device the test was aimed at."""

    results: List[PushSendResult] = Field(default_factory=list)


class AttentionAckRequest(BaseModel):
    """The episode a person has just deliberately acted on.

    The id is required rather than implied by "the current one", so a click made
    against a screen one poll out of date acknowledges the episode the person actually
    saw, or nothing at all -- never whichever episode happens to be open when the
    request lands.
    """

    episode_id: str


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
    """The complete Python-computed dashboard contract.

    Every task list here is a :class:`TaskCardRead`, not a whole record. It answered
    with whole ``TaskRead`` records until task-495 -- spec prose, acceptance criteria and
    the complete log of every task on the page -- which measured 10,888 bytes per record
    and 5.2 MB in total against a generated corpus of 480, to draw cards showing a title,
    a summary line, a priority chip and a dependency badge.
    """

    stats: DashboardStats
    active_tasks: List[TaskCardRead]
    recent_updates: List[DashboardRecentUpdate]
    waiting_tasks: List[TaskCardRead]
    backlog_tasks: List[TaskCardRead]
    next_task: Optional[TaskCardRead]
    queue_preview: List[TaskCardRead] = Field(
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
    runner: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Specific machine-local runner for this run, overriding group and project "
            "defaults. Mutually exclusive with group."
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
    if_full: Literal["refuse", "queue"] = Field(
        default="refuse",
        description=(
            "What to do when every machine slot is taken. 'refuse' is the historical "
            "behaviour and the default, so a caller that predates task-459 is unchanged: "
            "409 'concurrency_limit'. 'queue' accepts the dispatch into the machine's "
            "dispatch queue instead -- 202 with 'queued': true and a 'queue_id' -- and "
            "the server starts it when a slot frees, with every dispatch gate judged at "
            "that moment rather than this one."
        ),
    )

    over_ceiling: bool = Field(
        default=False,
        description=(
            "Start this run although every machine slot is taken (task-461). A "
            "deliberate overage of limits.max_concurrent_runs for this one dispatch, "
            "and nothing else: every other gate binds as it always did, including the "
            "hourly cap, because an overage is a dispatch. Honoured only for a human "
            "principal -- a run credential sending it is refused 403 "
            "'capability_denied' under 'dispatch.over_ceiling'. Mutually exclusive with "
            "if_full=queue: asking to wait for a slot and asking to start without one "
            "are opposite answers to the same question."
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
        if self.runner is not None and self.group is not None:
            raise ValueError("Send either 'runner' or 'group', not both.")
        if self.over_ceiling and self.if_full == "queue":
            raise ValueError(
                "Send either 'over_ceiling' (start now, above the ceiling) or "
                "'if_full: queue' (wait for a slot), not both. They are the two "
                "different answers to a full machine."
            )
        return self


class DispatchStarted(BaseModel):
    """What a successful dispatch reports back -- or, since task-459, a queued one.

    **Read ``queued`` before anything else.** Both answers are 202, because both mean
    "accepted, and how it ends arrives later on the task". They are not the same event,
    and a client that renders "Dispatched" over a queued one has told somebody a run is
    working when nothing has started.
    """

    run_id: str = Field(
        ...,
        description=(
            "AgentJobs' identifier for this run. On a queued dispatch this is the queue "
            "entry's id, which is what the cancel route takes while it waits."
        ),
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Session mode only, and assigned by the CLI rather than by us.",
    )
    mode: str = Field(..., description="session or batch. Empty while a dispatch is queued.")
    posture: str = Field(
        ...,
        description=(
            "What the run is permitted to do. Empty while a dispatch is queued: the "
            "posture is resolved by the gates when it starts, not when it was queued."
        ),
    )
    task_id: str = Field(..., description="The task the run is working.")
    caused_by: int = Field(
        ...,
        description=(
            "The log entry this dispatch is attributed to. 0 while a dispatch is queued "
            "-- the authorising entry is written when it starts, exactly as for a click."
        ),
    )
    runner: Optional[str] = Field(default=None, description="Runner that was selected and started.")
    group: Optional[str] = Field(
        default=None,
        description="Runner group it was selected from, when one participated.",
    )
    queued: bool = Field(
        default=False,
        description=(
            "True when the machine was full and the caller sent if_full=queue: nothing "
            "has started, and the entry waits in the machine's dispatch queue."
        ),
    )
    queue_position: int = Field(
        default=0,
        description="1-based place in line while queued; 0 for a dispatch that started.",
    )
    queued_at: Optional[str] = Field(
        default=None, description="When the entry joined the queue, UTC."
    )
    over_ceiling: bool = Field(
        default=False,
        description=(
            "True when this run was started above limits.max_concurrent_runs because a "
            "person chose to (task-461). The machine is over its ceiling until it ends, "
            "and the slot board says so rather than clipping the count."
        ),
    )


# ---------------------------------------------------------------------------
# Analytics (docs/analytics-design.md section 7.3)
#
# Named models rather than inline dicts, and that is a hard requirement rather than a
# style preference: `npm run generate:api-client` regenerates
# `frontend/src/api/generated/` from `openapi.json`, and the `api` stage of
# `scripts/check.py` compares both against the working tree. An inline `dict` here
# generates anonymous TypeScript the page cannot name.
# ---------------------------------------------------------------------------


class FinishRecordWrite(BaseModel):
    """The ``finish`` row a scripted finish indexes itself as (task-472).

    Derived from the finish's ``meta.yaml`` by ``agentjobs.history``; the field names are
    the column names. ``source`` says who is writing: ``native`` upserts, ``imported``
    never overwrites a row that exists.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    started_at: str
    finished_at: Optional[str] = None
    seconds: Optional[float] = None
    outcome: Literal["finished", "escalated", "declined", "interrupted", "running"]
    reason: Optional[str] = None
    stopped_at: Optional[str] = None
    merged: bool = False
    merge_commit: Optional[str] = None
    run_id: Optional[str] = None
    dispatched_run_id: Optional[str] = None
    authority: Optional[str] = None
    source: Literal["native", "imported"] = "native"


class FinishStepWrite(BaseModel):
    """One ``finish_step`` row: a step of the sequence and what it cost."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(..., ge=1)
    step: str
    ok: bool
    skipped: bool = False
    seconds: float = 0.0
    detail: Optional[str] = None
    ts: str


class FinishHistoryWrite(BaseModel):
    """Body of ``PUT /history/finishes/{finish_id}``: the row and every step so far."""

    model_config = ConfigDict(extra="forbid")

    record: FinishRecordWrite
    steps: List[FinishStepWrite] = Field(default_factory=list)


class GateRunWrite(BaseModel):
    """The ``gate_run`` row a gate indexes itself as (task-472).

    ``origin`` says whose gate it was: the finisher's, a dispatched run's before its
    handoff, or somebody's at a shell. ``scope`` is the gate's own vocabulary with one
    rename -- the gate calls a reduced ``--since-gate`` run ``necessity``, the store
    calls it ``since_gate`` -- and a partial run is a row, not an omitted one.
    """

    model_config = ConfigDict(extra="forbid")

    origin: Literal["finish", "run", "manual"]
    finish_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    scope: Literal["full", "partial", "since_gate", "concurrent"]
    tree: Optional[str] = None
    checkout: Optional[str] = None
    branch: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    seconds: Optional[float] = None
    passed: Optional[bool] = None
    failed_stage: Optional[str] = None
    stages_run: Optional[int] = None
    stages_total: Optional[int] = None
    source: Literal["native", "imported"] = "native"


class GateStageWrite(BaseModel):
    """One ``gate_stage`` row. ``seconds`` is null for the stage that failed."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(..., ge=1)
    stage: str
    seconds: Optional[float] = None
    passed: Optional[bool] = None
    started_at: str
    finished_at: Optional[str] = None


class GateHistoryWrite(BaseModel):
    """Body of ``PUT /history/gates/{gate_id}``: the row and every stage so far."""

    model_config = ConfigDict(extra="forbid")

    record: GateRunWrite
    stages: List[GateStageWrite] = Field(default_factory=list)


class HistoryWriteResult(BaseModel):
    """What a history write did.

    ``written`` false is an answer, not an error: ``exists`` means an imported write met
    a row that was already there, and ``unknown_task`` means a finish named a task this
    project does not have and was refused rather than inserted with the foreign key
    off.
    """

    written: bool
    reason: Optional[Literal["exists", "unknown_task"]] = None


class AnalyticsRange(BaseModel):
    """The window every panel in one response was computed over.

    ``bucket`` and ``throughput_bucket`` are two grains rather than one, per section 8.3
    and section 8.4: the backlog level is read a day at a time while throughput is read
    a week or a month at a time, and a client that had to infer the second from the
    dates would be holding a copy of a rule that lives here.
    """

    key: Literal["30d", "90d", "12m", "all"]
    start: datetime = Field(..., description="Inclusive, UTC. Clipped to the coverage baseline.")
    end: datetime = Field(..., description="Exclusive, UTC.")
    bucket: Literal["day", "week", "month"] = Field(
        ..., description="Grain of the backlog and holder spines."
    )
    throughput_bucket: Literal["day", "week", "month"] = Field(
        ..., description="Grain of the throughput and cycle-time series."
    )
    timezone: str = Field(..., description="IANA zone name the buckets were computed in.")


class AnalyticsCoverage(BaseModel):
    """What the store is prepared to claim about its own history (section 3.6).

    Part of the payload rather than something the page infers, so no panel has to guess
    how far back it is allowed to draw. ``baseline_at`` is null for exactly one state --
    a project with no events at all -- which is what section 9.1 renders as a sentence
    rather than as an empty axis.
    """

    baseline_at: Optional[datetime] = Field(
        default=None, description="Null means the store makes no claim at all."
    )
    baseline_kind: Literal["native", "reconstructed", "backfilled", "unknown"] = "unknown"
    native_from: Optional[datetime] = Field(
        default=None, description="First event written by a verb at the moment of the change."
    )
    reconstructed_before: Optional[datetime] = Field(
        default=None, description="Draw the hatch left of this instant."
    )
    events: Dict[str, int] = Field(default_factory=dict, description="Event counts by source.")
    complete: bool = Field(
        default=False, description="True when the whole window is covered by native events."
    )
    note: Optional[str] = Field(
        default=None, description="One sentence, rendered as the coverage caption."
    )


class AnalyticsTotals(BaseModel):
    """Identical, field for field, to ``build_dashboard_snapshot()["stats"]``.

    Plus ``open``, which is the number the backlog series ends on. The two are computed
    by different code -- SQL here, Python there -- and
    ``tests/test_analytics_api.py`` asserts they agree, which is the whole point:
    a page whose headline counts disagreed with the Dashboard's would discredit both.
    """

    total: int
    in_progress: int
    blocked: int
    waiting_for_human: int
    awaiting_input: int
    completed: int
    open: int


class BacklogPoint(BaseModel):
    """One bucket of the backlog level and both flows, on a filled calendar spine."""

    day: date
    open_count: int
    opened: int = Field(..., description="Arrivals in this bucket.")
    closed: int = Field(..., description="Departures in this bucket.")
    estimated: bool = Field(
        ..., description="This bucket contains a reconstructed or backfilled event."
    )


class HolderPoint(BaseModel):
    """Who held the open work, per bucket, on the backlog's spine."""

    day: date
    agent: int
    human: int
    external: int


class ThroughputPoint(BaseModel):
    """Completions, cancellations and reopenings for one throughput bucket (T1).

    ``tasks_completed`` and ``completion_events`` differ whenever a task was reopened
    and closed again (section 3.3). The chart plots the first; the second is returned so
    a reader whose arithmetic does not work out has an answer.

    **The cycle-time percentiles are gone**, per section 21.2 and section 19.2: they
    measured ``created_at`` to ``closed_at``, which is mostly queue wait, and section
    18.1's segment series answers the question they stood in for. They were removed
    rather than deprecated in place -- a client cannot misread a field that is absent,
    and the generated client is regenerated by the same task that removed them.
    """

    bucket: date = Field(..., description="First day of the bucket, in the reporting zone.")
    tasks_completed: int = Field(..., description="COUNT(DISTINCT task_id).")
    completion_events: int = Field(..., description="COUNT(*).")
    cancelled: int = Field(..., description="Closed with any outcome other than completed.")
    reopened: int = Field(default=0, description="Tasks reopened in this bucket (T1).")
    estimated: bool = Field(
        default=False, description="This bucket contains a reconstructed close or reopen."
    )


class AgeBucket(BaseModel):
    """One band of the age distribution, emitted whether or not it holds anything."""

    label: Literal["0-6d", "7-29d", "30-89d", "90d+"]
    tasks: int
    mean_age_days: float


class AgingTask(BaseModel):
    """One of the ten oldest open, unarchived tasks."""

    task_id: str
    title: str
    priority: str
    ball: Optional[str] = None
    ball_reason: Optional[str] = None
    age_days: float


class StuckGroup(BaseModel):
    """Open work grouped by who holds it and why, with how long they have held it."""

    ball: str
    ball_reason: str
    tasks: int
    mean_days_held: float
    max_days_held: float
    oldest_task_id: str


# Analytics, second set (docs/analytics-design.md sections 17, 18 and 21)
#
# The same rule -- every model named -- and one new convention: every series carries its
# own `SeriesCoverage`, because the sources now have five different baselines (task
# events, finishes, gates, runs, the execution journal) and one page-level coverage
# cannot describe them. Units are in the field names: hours for task-scale durations,
# minutes for finishes and gates, seconds for latencies (section 21.2).
#
# No field here is keyed by runner or agent (section 16, decision 12);
# `tests/test_analytics_api.py` greps these models for either name and fails on it.
# ---------------------------------------------------------------------------


class SeriesCoverage(BaseModel):
    """What one series can honestly claim, independent of the page-level coverage.

    A series whose source is younger than the range starts where the source starts and
    says so here, rather than drawing zero over history nobody recorded (section 21.1).
    ``bucket`` is the grain the series is aggregated at, so a client never infers it.
    """

    recorded_from: Optional[datetime] = Field(
        default=None, description="First row of any source. Null means no rows at all."
    )
    native_from: Optional[datetime] = Field(
        default=None, description="First row written at the moment it happened."
    )
    complete: bool = Field(
        default=False, description="True when the window starts at or after native_from."
    )
    bucket: Literal["day", "week", "month"] = Field(
        default="week", description="The grain this series is aggregated at."
    )
    note: Optional[str] = Field(
        default=None, description="One sentence for the caption, or null when none is needed."
    )


class SegmentAmong(BaseModel):
    """Per segment: how many of the bucket's tasks had any time in it, and their median."""

    tasks: int
    p50_hours: Optional[float] = None


class SegmentPoint(BaseModel):
    """Where a completed task's time went, per week of close (sections 17 and 18.1).

    Five segments -- queue, work, waiting, review, finish -- partition each task's open
    life exactly, so ``queue + work + waiting + review + finish = total`` per task to
    the second. The percentiles here are per segment over the bucket's tasks, so the
    stack's height is a sum of medians and not the median total; ``total_p50_hours``
    is the latter. ``first_review_*`` is the owner's dispatch-to-handoff (S3): first
    claim to first review entry, bucketed by the review entry.
    """

    bucket: date = Field(..., description="First day of the week, in the reporting zone.")
    sample: int = Field(..., description="Completed tasks closed in this bucket, in the sample.")
    excluded: int = Field(..., description="Closed by an import row: close time unknown.")
    unreviewed: int = Field(..., description="Tasks with no review handoff (section 17.3).")
    queue_p50_hours: Optional[float] = None
    queue_p90_hours: Optional[float] = None
    work_p50_hours: Optional[float] = None
    work_p90_hours: Optional[float] = None
    waiting_p50_hours: Optional[float] = None
    waiting_p90_hours: Optional[float] = None
    review_p50_hours: Optional[float] = None
    review_p90_hours: Optional[float] = None
    finish_p50_hours: Optional[float] = None
    finish_p90_hours: Optional[float] = None
    total_p50_hours: Optional[float] = None
    total_p90_hours: Optional[float] = None
    first_review_p50_hours: Optional[float] = None
    first_review_p90_hours: Optional[float] = None
    first_review_sample: int = 0
    among: Dict[str, SegmentAmong] = Field(
        default_factory=dict, description="Per segment, the tasks with a nonzero value."
    )
    estimated: bool = Field(
        ..., description="A task in this bucket has a reconstructed boundary row."
    )


class CostPerTaskPoint(BaseModel):
    """What a completed task cost the machine: runs, finishes and gate minutes (S4, F5, G4).

    Bucketed by the task's close. ``without_gate`` counts the tasks with no full gate
    at all -- hand closes and recorded decisions -- so a low median is not read as a
    fast gate.
    """

    bucket: date
    sample: int = Field(..., description="Completed tasks closed in this bucket.")
    runs_mean: Optional[float] = None
    runs_mode: Optional[int] = None
    finishes_mean: Optional[float] = None
    gate_minutes_p50: Optional[float] = None
    gate_minutes_p90: Optional[float] = None
    without_gate: int = 0
    estimated: bool = False


class FinishPoint(BaseModel):
    """The scripted finish, per week of start (F1 to F4).

    Duration percentiles are over ``finished`` rows only: a finish that stopped at the
    gate is measuring the gate. ``steps_p50_s`` lists the steps with a nonzero median,
    and ``runway_waited`` is how many finishes queued behind another for the merge
    runway (task-223).
    """

    bucket: date
    finished: int = 0
    escalated: int = 0
    declined: int = 0
    interrupted: int = 0
    reasons: Dict[str, int] = Field(default_factory=dict, description="Escalation reasons.")
    duration_p50_min: Optional[float] = None
    duration_p90_min: Optional[float] = None
    sample: int = 0
    steps_p50_s: Dict[str, float] = Field(default_factory=dict)
    runway_waited: int = 0
    runway_p90_s: Optional[float] = None
    estimated: bool = False


class GatePoint(BaseModel):
    """Full gates, per week of start (G1 to G3).

    Duration percentiles are over green full gates: a red gate stops early and says
    nothing about cost. ``failed_stages`` is where the red ones stopped and
    ``origins`` says whose gates they were: the finisher's, a run's, or somebody's at a
    shell.
    """

    bucket: date
    full: int = 0
    passed: int = 0
    failed_stages: Dict[str, int] = Field(default_factory=dict)
    duration_p50_min: Optional[float] = None
    duration_p90_min: Optional[float] = None
    sample: int = 0
    stages_p50_s: Dict[str, float] = Field(default_factory=dict)
    origins: Dict[str, int] = Field(default_factory=dict)
    estimated: bool = False


class RunPoint(BaseModel):
    """Dispatched runs per bucket of start, at the spine grain (R-1 to R-4).

    A run is attributed whole to the bucket it started in. ``in_flight`` is the runs
    with no end yet, which are in ``runs`` and in no outcome.
    """

    bucket: date
    runs: int = 0
    triggers: Dict[str, int] = Field(default_factory=dict)
    agent_hours: float = 0.0
    outcomes: Dict[str, int] = Field(default_factory=dict)
    in_flight: int = 0
    duration_p50_min: Optional[float] = None
    duration_p90_min: Optional[float] = None
    sample: int = 0
    estimated: bool = False


class MachinePoint(BaseModel):
    """What the execution journal says about this project, per week (R-5, R-6).

    Its own series because its source is another database with its own baseline.
    ``start_latency`` is admission to launch; ``queue_wait`` is the time a queued
    dispatch waited for a slot; ``paused_run_hours`` is run-hours lost to usage-limit
    pauses, not wall-clock -- three runs stalled for one reset count three times.
    """

    bucket: date
    admitted: int = 0
    start_latency_p50_s: Optional[float] = None
    start_latency_p90_s: Optional[float] = None
    queued: int = 0
    queue_wait_p50_s: Optional[float] = None
    queue_wait_p90_s: Optional[float] = None
    paused_run_hours: float = 0.0
    paused_waiters: int = 0


class ReviewPoint(BaseModel):
    """How long a person took, per week (R2, R3, Q-2).

    ``exits`` is every departure from review in the bucket, approval or not, because a
    revise request is also the owner answering; ``wait_*`` is measured over all of them.
    An approval is an exit to agent/work or a close from review (section 17.2), and a
    first-time approval is one on the task's first review round. Questions are
    bucketed by when they were asked.
    """

    bucket: date
    exits: int = 0
    approvals: int = 0
    wait_p50_hours: Optional[float] = None
    wait_p90_hours: Optional[float] = None
    first_time_approvals: int = 0
    questions: int = 0
    answered: int = 0
    answer_p50_hours: Optional[float] = None
    answer_p90_hours: Optional[float] = None
    estimated: bool = False


class InReview(BaseModel):
    """One open task waiting on review, with how long the ball has sat there (R1)."""

    task_id: str
    title: str
    hours_waiting: float


class OpenQuestion(BaseModel):
    """One question on an open task with no threaded answer (Q-1)."""

    task_id: str
    entry_id: int
    hours_open: float


class AnalyticsResponse(BaseModel):
    """One request for the whole analytics page (section 7.1).

    Not seventeen endpoints: the panels share a range, a timezone and a coverage
    statement that must be identical across all of them, and the storage cost of the
    whole set was measured at 6.3 ms -- so splitting it would buy nothing and cost
    seventeen chances to render half a page.

    The second set (section 21) adds the process series. Each carries its own
    ``SeriesCoverage``; the page-level ``coverage`` still describes the task history.
    """

    range: AnalyticsRange
    coverage: AnalyticsCoverage
    totals: AnalyticsTotals
    backlog: List[BacklogPoint] = Field(
        default_factory=list, description="One entry per bucket in range, gaps filled."
    )
    holders: List[HolderPoint] = Field(default_factory=list, description="The same spine.")
    throughput: List[ThroughputPoint] = Field(default_factory=list)
    aging: List[AgeBucket] = Field(default_factory=list)
    oldest: List[AgingTask] = Field(default_factory=list, description="Ten, open and not archived.")
    stuck: List[StuckGroup] = Field(default_factory=list)
    segments: List[SegmentPoint] = Field(default_factory=list)
    segments_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    cost_per_task: List[CostPerTaskPoint] = Field(default_factory=list)
    cost_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    finishes: List[FinishPoint] = Field(default_factory=list)
    finishes_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    gates: List[GatePoint] = Field(default_factory=list)
    gates_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    runs: List[RunPoint] = Field(default_factory=list)
    runs_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    machine: List[MachinePoint] = Field(default_factory=list)
    machine_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    review: List[ReviewPoint] = Field(default_factory=list)
    review_coverage: SeriesCoverage = Field(default_factory=SeriesCoverage)
    in_review: List[InReview] = Field(default_factory=list)
    open_questions: List[OpenQuestion] = Field(default_factory=list)


# ----- drafting a spec with a model (task-175) --------------------------------


class ModelStatusResponse(BaseModel):
    """Whether this machine can draft a task spec right now, and why not when it cannot.

    **There is no field here that a credential could occupy**, which is the design's
    rule expressed as a shape rather than as a promise (`docs/model-access-design.md`
    §4): the status route answers a boolean, never the key, a prefix of it, or its
    length. `model` is a configured identifier and is shown because a person who asked
    for a draft is entitled to know what drafted it.

    `reason` is one of the closed set in `agentjobs.modelaccess.config.REASONS`, and
    `detail` is a sentence written in this repository. Neither is ever built from a
    provider's response.
    """

    available: bool = Field(..., description="Whether a drafting call can be made now.")
    reason: Optional[str] = Field(
        None, description="Why not, as a stable code. Null when available."
    )
    detail: Optional[str] = Field(None, description="One sentence a person can act on.")
    model: Optional[str] = Field(None, description="The configured model id, when there is one.")
    calls_per_hour: Optional[int] = Field(None, description="This machine's hourly cap.")
    calls_used: Optional[int] = Field(None, description="Calls made inside the rolling hour.")


class SpecDraftRequest(BaseModel):
    """What the human typed, and nothing about where the task should land.

    There is deliberately no field for lifecycle, ball, priority, parent, dependencies
    or actor -- not because the server would refuse them, but so that no caller can form
    the request that would ask a model to choose one.
    """

    title: str = Field("", description="The title the human typed, possibly empty.")
    description: str = Field("", description="The rough description the human typed.")


class SpecDraftResponse(BaseModel):
    """One draft, for a form to fill its fields from -- or a refusal saying why not.

    Six spec fields, matching `agentjobs.modelaccess.draft.SpecDraft`. A reply that
    named a priority or a parent produced a draft in which those values do not exist;
    there is nowhere in this model to put one.

    **A refusal is a 200 with `drafted: false`**, carrying the same `reason`/`detail`
    pair as the status route. That is a decision rather than laziness: the person is
    standing in a form they are about to file by hand, and "no draft, here is why" is
    not the same event as "your task could not be filed". Answering with an error status
    would route it through the form's filing-failed path. It also puts the refusal in
    the OpenAPI document, so the generated client is typed for it -- which the 409
    refusals elsewhere in this API are not.
    """

    drafted: bool = Field(True, description="False when no draft was produced.")
    summary: str = ""
    intent: str = ""
    description: str = ""
    constraints: str = ""
    out_of_scope: str = ""
    acceptance: List[str] = Field(default_factory=list)
    model: Optional[str] = Field(None, description="Which model produced this draft.")
    reason: Optional[str] = Field(None, description="Why there is no draft, as a stable code.")
    detail: Optional[str] = Field(None, description="One sentence a person can act on.")
