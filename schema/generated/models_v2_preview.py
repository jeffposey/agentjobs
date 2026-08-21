from __future__ import annotations

import re
import sys
from datetime import (
    date,
    datetime,
    time
)
from decimal import Decimal
from enum import Enum
from typing import (
    Any,
    ClassVar,
    Literal,
    Optional,
    Union
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer
)


metamodel_version = "1.11.0"
version = "2.0.0"


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        serialize_by_alias = True,
        validate_by_name = True,
        validate_assignment = True,
        validate_default = True,
        extra = "forbid",
        arbitrary_types_allowed = True,
        use_enum_values = True,
        strict = False,
    )





class LinkMLMeta(RootModel):
    root: dict[str, Any] = {}
    model_config = ConfigDict(frozen=True)

    def __getattr__(self, key:str):
        return getattr(self.root, key)

    def __getitem__(self, key:str):
        return self.root[key]

    def __setitem__(self, key:str, value):
        self.root[key] = value

    def __contains__(self, key:str) -> bool:
        return key in self.root


linkml_meta = LinkMLMeta({'default_prefix': 'aj',
     'default_range': 'string',
     'description': 'The proposed next iteration of the task schema, transcribed '
                    'from docs/schema-design.md (ACCEPTED, D1-D3 resolved '
                    '2026-07-29) into LinkML so it can be rendered, browsed, '
                    'validated and diffed against v1 before any Python is '
                    'written.\n'
                    'This is a PRESCRIPTIVE schema: nothing here is implemented '
                    'yet. It is the machine- readable companion to the design '
                    "document, and the intended input to task-050's model "
                    'implementation. Section references below point at '
                    'docs/schema-design.md.\n'
                    "Three of v2's design tenets are enforced structurally rather "
                    'than by convention: every open task names who acts next '
                    '(`ball`, required while open), every handoff carries its ask '
                    '(`ball_prompt`, required whenever the ball is set), and each '
                    'concept has exactly one mechanism (phases, prompts, issues '
                    'and Comment are gone).\n'
                    'NULL AND ABSENT ARE EQUIVALENT (decided 2026-07-29). `ball: '
                    'null` and an omitted `ball` mean the same thing, as do '
                    '`outcome: null` and an omitted `outcome`. This matters '
                    'because hand-editing YAML in git is a first-class interface '
                    '(D2): a human writing `outcome: null` is being explicit, not '
                    'wrong.\n'
                    'LinkML cannot express that. It has no null type, so an '
                    'optional enum slot compiles to a bare `$ref` and the '
                    'generated JSON Schema rejects an explicit null. The '
                    'consequence, recorded so it is not mistaken for a design '
                    'position:\n'
                    '\n'
                    '  * OMISSION is the canonical on-disk form -- what the '
                    'migrator and the manager\n'
                    '    write, and the only form the generated JSON Schema '
                    'accepts.\n'
                    '  * EXPLICIT NULL is equally valid input and must be accepted '
                    'by the loader.\n'
                    "    Pydantic's `Optional[Ball] = None` does this natively, so "
                    'task-050 gets it for\n'
                    '    free -- but the JSON Schema artifact in schema/generated/ '
                    'is therefore\n'
                    '    STRICTER than the runtime, and must not be used as the '
                    'sole validator for\n'
                    '    hand-edited files.\n'
                    '  * The `value_presence: ABSENT` conditions in the rules '
                    'below should be read as\n'
                    '    "absent or null" throughout.',
     'id': 'https://github.com/jeffposey/agentjobs/schema/v2',
     'imports': ['linkml:types'],
     'license': 'MIT',
     'name': 'agentjobs-v2',
     'prefixes': {'aj': {'prefix_prefix': 'aj',
                         'prefix_reference': 'https://github.com/jeffposey/agentjobs/schema/v2/'},
                  'linkml': {'prefix_prefix': 'linkml',
                             'prefix_reference': 'https://w3id.org/linkml/'}},
     'source_file': 'schema/agentjobs-v2.yaml',
     'title': 'AgentJobs Task Schema v2'} )

class Lifecycle(str, Enum):
    """
    Where the task is in its life. Strictly ordered and closed (section 3). Answers only "where in its life" -- not who acts next, and not why.
    """
    draft = "draft"
    """
    Being specified. Not claimable.
    """
    ready = "ready"
    """
    Spec complete, claimable by any eligible agent. A ready task with unmet `needs` dependencies stays ready -- its blockedness is derived from the store, not restated as state -- but is excluded from /next and refuses claim.
    """
    active = "active"
    """
    Claimed, work underway, in whoever's court `ball` says.
    """
    closed = "closed"
    """
    Over. Carries an `outcome`. Visibility is the separate `archived` flag.
    """


class Ball(str, Enum):
    """
    Who acts next. Required on every open task, null only when closed (tenet 2). An open task with nobody responsible is not representable.
    """
    agent = "agent"
    human = "human"
    external = "external"


class BallReason(str, Enum):
    """
    Why the ball holder holds it. Closed vocabulary, scoped to the holder -- the scoping is enforced by the class-level rules on Task (section 3).
    """
    available = "available"
    """
    agent: ready and unclaimed -- any eligible agent may take it.
    """
    work = "work"
    """
    agent: claimed and executing.
    """
    revise = "revise"
    """
    agent: review came back with changes requested.
    """
    spec = "spec"
    """
    human: the spec needs human completion or refinement.
    """
    review = "review"
    """
    human: work product needs review (v1's under_review).
    """
    decision = "decision"
    """
    human: a choice is blocking progress.
    """
    approval = "approval"
    """
    human: a gate -- merge, spend, publish. Yes/no, not critique.
    """
    input = "input"
    """
    human: missing information only a human has.
    """
    dependency = "dependency"
    """
    external: a claimed task blocked on another task (v1's blocked).
    """
    service = "service"
    """
    external: blocked on a third party, outage, or provisioning.
    """


class Outcome(str, Enum):
    """
    How the task ended. Set if and only if lifecycle is closed.
    """
    completed = "completed"
    cancelled = "cancelled"
    superseded = "superseded"
    duplicate = "duplicate"


class Priority(str, Enum):
    """
    Relative priority weighting. Unchanged from v1.
    """
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ActorKind(str, Enum):
    """
    What kind of party acted.
    """
    agent = "agent"
    human = "human"
    system = "system"
    """
    The manager itself, for automatically appended transitions.
    """


class AcceptanceStatus(str, Enum):
    """
    Whether a criterion is verified. Deliberately distinct from DeliverableStatus: a criterion is *verified*, a deliverable is *produced* (section 3).
    """
    pending = "pending"
    met = "met"
    failed = "failed"
    dropped = "dropped"


class DeliverableStatus(str, Enum):
    """
    Whether a deliverable was produced.
    """
    pending = "pending"
    done = "done"
    dropped = "dropped"


class BranchStatus(str, Enum):
    """
    Branch lifecycle. Carried over from v1 unchanged -- genuinely distinct.
    """
    active = "active"
    merged = "merged"
    abandoned = "abandoned"


class DependencyType(str, Enum):
    """
    Relationship to another task. `depends_on` renamed to `needs`; the other two are unchanged.
    """
    needs = "needs"
    blocks = "blocks"
    related = "related"


class LinkRel(str, Enum):
    """
    What an external link is.
    """
    pr = "pr"
    issue = "issue"
    doc = "doc"
    design = "design"
    build = "build"
    other = "other"


class LogEntryType(str, Enum):
    """
    What kind of event a log entry records (section 4). One typed log replaces v1's status_updates, comments and prompts.followups.
    """
    note = "note"
    """
    Free-form remark. Anyone.
    """
    progress = "progress"
    """
    Work narration -- what was done, what was verified. Agent.
    """
    transition = "transition"
    """
    Automatic record of a state-axis change; `data` carries the delta. Written by the manager, never trusted to callers.
    """
    handoff = "handoff"
    """
    The ball is moving; `body` is the ask, mirroring `ball_prompt`.
    """
    decision = "decision"
    """
    A choice, its reasoning, and the rejected alternative. Binding.
    """
    question = "question"
    """
    An explicit open thread. Surfaceable in UIs until answered.
    """
    answer = "answer"
    """
    Resolves a `question`, via `re`.
    """
    instruction = "instruction"
    """
    A directive to the working agent. Replaces v1 followup prompts.
    """
    dispatch = "dispatch"
    """
    A run was started against this task. `data` carries run_id, agent, runner, mode, posture, trigger, caused_by, argv, cwd and git_head -- enough to answer "what ran, against what" once the machine-local run directory is gone. Written by the dispatcher, never trusted to callers.
    """
    dispatch_result = "dispatch_result"
    """
    How a run ended. `re` threads it back to its `dispatch` entry; `data` carries run_id and outcome, plus exit_code, duration_seconds and log_path where the runner mode has them. Written by the dispatcher, never trusted to callers.
    """



class Task(ConfiguredBaseModel):
    """
    A unit of work. One YAML file per task, in git -- diffable, reviewable, blame-able, mergeable. Hand-editing remains a first-class interface (D2).
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2',
         'rules': [{'description': 'Rule 1 and 3: ball is absent-or-null if and only '
                                   'if lifecycle is closed, and outcome is set if and '
                                   'only if lifecycle is closed. ABSENT below means '
                                   '"absent or null" -- see the null-and-absent note '
                                   'in the schema description.',
                    'postconditions': {'slot_conditions': {'ball': {'name': 'ball',
                                                                    'value_presence': 'ABSENT'},
                                                           'outcome': {'name': 'outcome',
                                                                       'value_presence': 'PRESENT'}}},
                    'preconditions': {'slot_conditions': {'lifecycle': {'equals_string': 'closed',
                                                                        'name': 'lifecycle'}}},
                    'title': 'closed_has_outcome_and_no_ball'},
                   {'description': 'Any open task must name who acts next and state '
                                   'the ask, and must not carry an outcome.',
                    'postconditions': {'slot_conditions': {'ball': {'name': 'ball',
                                                                    'value_presence': 'PRESENT'},
                                                           'outcome': {'name': 'outcome',
                                                                       'value_presence': 'ABSENT'}}},
                    'preconditions': {'slot_conditions': {'lifecycle': {'any_of': [{'equals_string': 'draft'},
                                                                                   {'equals_string': 'ready'},
                                                                                   {'equals_string': 'active'}],
                                                                        'name': 'lifecycle'}}},
                    'title': 'open_names_who_acts_next_and_the_ask'},
                   {'description': 'Rule 4 (tenet 3): a handoff without its payload is '
                                   'a notification with no content. Split from the '
                                   'rule above so the one documented exception can be '
                                   'expressed -- agent/available may omit it, because '
                                   "an unclaimed ready task's ask is its spec.",
                    'postconditions': {'slot_conditions': {'ball_prompt': {'name': 'ball_prompt',
                                                                           'value_presence': 'PRESENT'}}},
                    'preconditions': {'none_of': [{'slot_conditions': {'ball_reason': {'equals_string': 'available',
                                                                                       'name': 'ball_reason'}}}],
                                      'slot_conditions': {'lifecycle': {'any_of': [{'equals_string': 'draft'},
                                                                                   {'equals_string': 'ready'},
                                                                                   {'equals_string': 'active'}],
                                                                        'name': 'lifecycle'}}},
                    'title': 'open_tasks_state_their_ask'},
                   {'description': 'Rule 2, agent side: available | work | revise.',
                    'postconditions': {'slot_conditions': {'ball_reason': {'any_of': [{'equals_string': 'available'},
                                                                                      {'equals_string': 'work'},
                                                                                      {'equals_string': 'revise'}],
                                                                           'name': 'ball_reason'}}},
                    'preconditions': {'slot_conditions': {'ball': {'equals_string': 'agent',
                                                                   'name': 'ball'}}},
                    'title': 'agent_ball_reason_vocabulary'},
                   {'description': 'Rule 2, human side: spec | review | decision | '
                                   'approval | input.',
                    'postconditions': {'slot_conditions': {'ball_reason': {'any_of': [{'equals_string': 'spec'},
                                                                                      {'equals_string': 'review'},
                                                                                      {'equals_string': 'decision'},
                                                                                      {'equals_string': 'approval'},
                                                                                      {'equals_string': 'input'}],
                                                                           'name': 'ball_reason'}}},
                    'preconditions': {'slot_conditions': {'ball': {'equals_string': 'human',
                                                                   'name': 'ball'}}},
                    'title': 'human_ball_reason_vocabulary'},
                   {'description': 'Rule 2, external side: dependency | service.',
                    'postconditions': {'slot_conditions': {'ball_reason': {'any_of': [{'equals_string': 'dependency'},
                                                                                      {'equals_string': 'service'}],
                                                                           'name': 'ball_reason'}}},
                    'preconditions': {'slot_conditions': {'ball': {'equals_string': 'external',
                                                                   'name': 'ball'}}},
                    'title': 'external_ball_reason_vocabulary'}],
         'tree_root': True})

    schema: int = Field(default=..., description="""Schema version stamp. A missing `schema` field means v1, which the v2 loader refuses loudly rather than guessing. Breaking changes bump this integer and ship a converter; additive changes do not bump it. NOTE for task-050: as a Pydantic field name, `schema` shadows a deprecated BaseModel attribute and will emit a shadow warning -- needs an alias or a model_config allowance."""    , le=2, ge=2, json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    id: str = Field(default=..., description="""Unique task identifier (e.g. task-050-schema-v2-models).""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Actor', 'AcceptanceCriterion', 'LogEntry']} })
    title: str = Field(default=..., description="""Task title summarising the work.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Link']} })
    created: datetime  = Field(default=..., description="""Creation timestamp.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    updated: datetime  = Field(default=..., description="""Last update timestamp.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    lifecycle: Lifecycle = Field(default=Lifecycle.draft, description="""Where the task is in its life.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task'], 'ifabsent': 'string(draft)'} })
    ball: Optional[Ball] = Field(default=None, description="""Who acts next. Required while open; absent or null when closed. This is the field that makes limbo unrepresentable.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    ball_reason: Optional[BallReason] = Field(default=None, description="""Why the ball holder holds it. Must belong to that holder's vocabulary -- see the rules on this class.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    ball_prompt: Optional[str] = Field(default=None, description="""The ask, in prose, addressed to whoever holds the ball. Required whenever the ball is set (tenet 3): a handoff without its payload is rejected at the schema level. May default for agent/available, where the spec is the ask.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    outcome: Optional[Outcome] = Field(default=None, description="""How the task ended. Set if and only if lifecycle is closed; absent or null while open.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    archived: Optional[bool] = Field(default=False, description="""Visibility flag, orthogonal to how the task ended. Lets an old completed task and an abandoned draft both be hidden without destroying what they were.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task'], 'ifabsent': 'false'} })
    priority: Optional[Priority] = Field(default=Priority.medium, json_schema_extra = { "linkml_meta": {'domain_of': ['Task'], 'ifabsent': 'string(medium)'} })
    queue_position: Optional[int] = Field(default=None, description="""Explicit order within the priority band. Unique among open tasks of the same priority in one project. Present if and only if the task is open. Assigned in sparse steps of 100 so an insertion takes a midpoint and rewrites one file rather than a whole band.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    category: str = Field(default=..., description="""Validated against the project config vocabulary at save time, not enumerated in this schema -- taxonomy is project-local, semantics are not.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    tags: Optional[list[str]] = Field(default=None, description="""Also validated against the config vocabulary at save.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    effort: Optional[str] = Field(default=None, description="""Free text; renamed from estimated_effort. It is an estimate, not a contract, so it is deliberately unstructured.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    assignment: Optional[Assignment] = Field(default=None, description="""Live ownership plus authoring-time eligibility.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    parent: Optional[str] = Field(default=None, description="""Task id of an umbrella task. Tasks with open children are never claimable. Absorbs task-045's parent/child design.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    spec: Spec = Field(default=..., description="""The structured briefing. Replaces v1's single description blob and its duplicated starter prompt.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    acceptance: Optional[list[AcceptanceCriterion]] = Field(default=None, description="""What \"done\" means. Replaces success_criteria.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    deliverables: Optional[list[Deliverable]] = Field(default=None, description="""Artifacts the task is expected to produce.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    dependencies: Optional[list[Dependency]] = Field(default=None, description="""Relationships to other tasks. Validated against the store at save.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    links: Optional[list[Link]] = Field(default=None, description="""External references. Renamed from external_links; URL now validated.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    branches: Optional[list[Branch]] = Field(default=None, description="""Git branches associated with the task.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })
    log: Optional[list[LogEntry]] = Field(default=None, description="""One append-only typed log (section 4). Entries are immutable and ordered. Replaces status_updates + comments + prompts.followups.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task']} })


class Actor(ConfiguredBaseModel):
    """
    A party that can act on tasks. Replaces v1's free-text author string, so \"was this an agent or a human\" becomes queryable.
    Actors are PROJECT-LEVEL entities defined in config, not per-task data (D4). A task file references an actor by bare id and `kind` is resolved from config, so it cannot drift. This class documents the config entity; inside a task file an actor appears only as its id string.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    id: str = Field(default=..., description="""Actor identifier, e.g. claude or jeff. Unique within the project.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Actor', 'AcceptanceCriterion', 'LogEntry']} })
    kind: ActorKind = Field(default=..., description="""What kind of party this is. Lives in config only -- never copied into a task file, which is the whole point of D4.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Actor']} })


class Assignment(ConfiguredBaseModel):
    """
    Separates live ownership from authoring-time eligibility -- v1 conflated both into assigned_to. Absorbs task-045's assigned_to/supported_agents split.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    owner: Optional[str] = Field(default=None, description="""Current owner, referenced by actor id (D4). Set on claim, cleared on release or close. Absent or null in draft and ready, required while active (enforced in task-050, see note above).""", json_schema_extra = { "linkml_meta": {'domain_of': ['Assignment']} })
    eligible: Optional[list[str]] = Field(default=None, description="""Who may claim this task. An empty list means anyone. Authoring-time intent, never mutated by claiming.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Assignment']} })


class Spec(ConfiguredBaseModel):
    """
    The working specification, split along the questions agents actually ask. Read in order, this is the first half of the resumption contract (section 5).
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    summary: str = Field(default=..., description="""One to two sentences. The only summary, for every audience -- v1's human_summary split by length rather than by content.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })
    intent: Optional[str] = Field(default=None, description="""WHY this task exists. Markdown. Optional, decided during the v1 migration (task-051): the 31 tasks in the corpus predate the split and have no separable intent, and a required field satisfied by a placeholder is worse than an empty one -- it reads as answered when it is not. New tasks should fill it, but the schema does not force an invention.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })
    description: str = Field(default=..., description="""WHAT to do -- the working spec. Markdown.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })
    constraints: Optional[str] = Field(default=None, description="""Hard requirements and prohibitions. Markdown.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })
    out_of_scope: Optional[str] = Field(default=None, description="""Explicit non-goals, so agents do not wander. Markdown. Note: this is the field that would have prevented task-048's own scope drift.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })
    context: Optional[list[ContextPointer]] = Field(default=None, description="""Curated read-this-first pointers, each with a reason.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Spec']} })


class ContextPointer(ConfiguredBaseModel):
    """
    A file worth reading before starting, and why it is worth reading.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    path: str = Field(default=..., description="""Repository-relative path.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ContextPointer', 'Deliverable', 'Attachment']} })
    why: str = Field(default=..., description="""What the reader will find there. Required -- a pointer without a reason is the kind of context that decays into noise.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ContextPointer']} })


class AcceptanceCriterion(ConfiguredBaseModel):
    """
    One verifiable condition for done. Replaces SuccessCriterion; adds an optional machine-checkable hint.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    id: str = Field(default=..., description="""Criterion identifier, scoped to the task (e.g. ac-1).""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Actor', 'AcceptanceCriterion', 'LogEntry']} })
    text: str = Field(default=..., description="""The condition, stated so it can be judged true or false.""", json_schema_extra = { "linkml_meta": {'domain_of': ['AcceptanceCriterion']} })
    verify: Optional[str] = Field(default=None, description="""Optional machine-checkable hint -- a command that demonstrates the criterion. Advisory, not executed automatically.""", json_schema_extra = { "linkml_meta": {'domain_of': ['AcceptanceCriterion']} })
    status: Optional[AcceptanceStatus] = Field(default=AcceptanceStatus.pending, json_schema_extra = { "linkml_meta": {'domain_of': ['AcceptanceCriterion', 'Deliverable', 'Branch'],
         'ifabsent': 'string(pending)'} })


class Deliverable(ConfiguredBaseModel):
    """
    An artifact the task produces.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    path: str = Field(default=..., description="""Repository-relative path to the deliverable.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ContextPointer', 'Deliverable', 'Attachment']} })
    note: Optional[str] = Field(default=None, description="""What it is. Renamed from v1's description, to free that word up.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Deliverable', 'Dependency']} })
    status: Optional[DeliverableStatus] = Field(default=DeliverableStatus.pending, json_schema_extra = { "linkml_meta": {'domain_of': ['AcceptanceCriterion', 'Deliverable', 'Branch'],
         'ifabsent': 'string(pending)'} })


class Dependency(ConfiguredBaseModel):
    """
    A relationship to another task. Renamed from task_id to task, and now actually validated against the store at save. v1's purposeless `status` field is gone.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    task: str = Field(default=..., description="""Referenced task identifier. Must exist in the store.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Dependency']} })
    type: Optional[DependencyType] = Field(default=DependencyType.needs, json_schema_extra = { "linkml_meta": {'domain_of': ['Dependency', 'LogEntry'], 'ifabsent': 'string(needs)'} })
    note: Optional[str] = Field(default=None, description="""Why the relationship exists.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Deliverable', 'Dependency']} })


class Link(ConfiguredBaseModel):
    """
    An external reference, with its kind made explicit.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    url: str = Field(default=..., description="""Target URL. Actually validated as a URI, unlike v1.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Link']} })
    rel: Optional[LinkRel] = Field(default=LinkRel.other, description="""What this link is.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Link'], 'ifabsent': 'string(other)'} })
    title: Optional[str] = Field(default=None, description="""Display title.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Link']} })


class Branch(ConfiguredBaseModel):
    """
    Git branch lifecycle. Carried over from v1 unchanged.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    name: str = Field(default=..., description="""Git branch name.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Branch']} })
    status: Optional[BranchStatus] = Field(default=BranchStatus.active, json_schema_extra = { "linkml_meta": {'domain_of': ['AcceptanceCriterion', 'Deliverable', 'Branch'],
         'ifabsent': 'string(active)'} })
    merged_at: Optional[datetime ] = Field(default=None, description="""When the branch was merged, if it was.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Branch']} })


class LogEntry(ConfiguredBaseModel):
    """
    One immutable event in the task's history (section 4). Provenance lives at this layer: every entry carries a typed actor, and every state change flows through a logged transition. Field-level provenance was rejected as weight without readers.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    id: int = Field(default=..., description="""Per-task integer, assigned by the manager. Defines order.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Task', 'Actor', 'AcceptanceCriterion', 'LogEntry']} })
    ts: datetime  = Field(default=..., description="""When the event happened.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })
    actor: str = Field(default=..., description="""Who or what produced this entry, referenced by actor id (D4). `kind` is resolved from config and is never stored here.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })
    type: LogEntryType = Field(default=..., json_schema_extra = { "linkml_meta": {'domain_of': ['Dependency', 'LogEntry']} })
    re: Optional[int] = Field(default=None, description="""Optional id of an earlier entry this one responds to. How an `answer` attaches to its `question`.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })
    body: Optional[str] = Field(default=None, description="""The human-readable content. Markdown. For `handoff` entries this is the ask, mirroring ball_prompt; for `decision` entries it must include the rejected alternative.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })
    data: Optional[Any] = Field(default=None, description="""Optional structured payload, typed per entry type. For `transition` entries it carries the state delta, e.g. {lifecycle: active, ball: agent, ball_reason: work}. Deliberately unconstrained at the schema level; the per-type shape is validated by the manager that writes it.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })
    attachments: Optional[list[Attachment]] = Field(default=None, description="""Images stored beside the tasks and referenced from this entry. The blob lives in a sidecar file; only the metadata is in the YAML, so a task file stays readable in a text editor and diffable line by line.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LogEntry']} })


class Attachment(ConfiguredBaseModel):
    """
    One image stored beside the tasks, referenced from the log entry it illustrates. `sha256` is both the sidecar's filename and its integrity check: a read that does not hash to this is refused rather than rendered.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://github.com/jeffposey/agentjobs/schema/v2'})

    path: str = Field(default=..., description="""Sidecar path, relative to the tasks directory rather than the repo root.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ContextPointer', 'Deliverable', 'Attachment']} })
    media_type: str = Field(default=..., description="""Image media type, derived from the bytes rather than the filename.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Attachment']} })
    sha256: str = Field(default=..., description="""Content hash; also the sidecar's filename.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Attachment']} })
    size_bytes: int = Field(default=..., description="""Size of the stored file.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Attachment']} })
    label: str = Field(default=..., description="""Accessible label; alt text wherever it renders.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Attachment']} })


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
Task.model_rebuild()
Actor.model_rebuild()
Assignment.model_rebuild()
Spec.model_rebuild()
ContextPointer.model_rebuild()
AcceptanceCriterion.model_rebuild()
Deliverable.model_rebuild()
Dependency.model_rebuild()
Link.model_rebuild()
Branch.model_rebuild()
LogEntry.model_rebuild()
Attachment.model_rebuild()
