# AgentJobs Task Schema v2

The proposed next iteration of the task schema, transcribed from docs/schema-design.md (ACCEPTED, D1-D3 resolved 2026-07-29) into LinkML so it can be rendered, browsed, validated and diffed against v1 before any Python is written.
This is a PRESCRIPTIVE schema: nothing here is implemented yet. It is the machine- readable companion to the design document, and the intended input to task-050's model implementation. Section references below point at docs/schema-design.md.
Three of v2's design tenets are enforced structurally rather than by convention: every open task names who acts next (`ball`, required while open), every handoff carries its ask (`ball_prompt`, required whenever the ball is set), and each concept has exactly one mechanism (phases, prompts, issues and Comment are gone).
NULL AND ABSENT ARE EQUIVALENT (decided 2026-07-29). `ball: null` and an omitted `ball` mean the same thing, as do `outcome: null` and an omitted `outcome`. This matters because hand-editing YAML in git is a first-class interface (D2): a human writing `outcome: null` is being explicit, not wrong.
LinkML cannot express that. It has no null type, so an optional enum slot compiles to a bare `$ref` and the generated JSON Schema rejects an explicit null. The consequence, recorded so it is not mistaken for a design position:

  * OMISSION is the canonical on-disk form -- what the migrator and the manager
    write, and the only form the generated JSON Schema accepts.
  * EXPLICIT NULL is equally valid input and must be accepted by the loader.
    Pydantic's `Optional[Ball] = None` does this natively, so task-050 gets it for
    free -- but the JSON Schema artifact in schema/generated/ is therefore
    STRICTER than the runtime, and must not be used as the sole validator for
    hand-edited files.
  * The `value_presence: ABSENT` conditions in the rules below should be read as
    "absent or null" throughout.

URI: https://github.com/jeffposey/agentjobs/schema/v2

Name: agentjobs-v2



## Classes

| Class | Description |
| --- | --- |
| [AcceptanceCriterion](classes/AcceptanceCriterion.md) | One verifiable condition for done |
| [Actor](classes/Actor.md) | A party that can act on tasks |
| [AnyValue](classes/AnyValue.md) | An untyped structured value |
| [Assignment](classes/Assignment.md) | Separates live ownership from authoring-time eligibility -- v1 conflated both... |
| [Branch](classes/Branch.md) | Git branch lifecycle |
| [ContextPointer](classes/ContextPointer.md) | A file worth reading before starting, and why it is worth reading |
| [Deliverable](classes/Deliverable.md) | An artifact the task produces |
| [Dependency](classes/Dependency.md) | A relationship to another task |
| [Link](classes/Link.md) | An external reference, with its kind made explicit |
| [LogEntry](classes/LogEntry.md) | One immutable event in the task's history (section 4) |
| [Spec](classes/Spec.md) | The working specification, split along the questions agents actually ask |
| [Task](classes/Task.md) | A unit of work |



## Slots

| Slot | Description |
| --- | --- |
| [acceptance](slots/acceptance.md) | What "done" means |
| [actor](slots/actor.md) | Who or what produced this entry, referenced by actor id (D4) |
| [archived](slots/archived.md) | Visibility flag, orthogonal to how the task ended |
| [assignment](slots/assignment.md) | Live ownership plus authoring-time eligibility |
| [ball](slots/ball.md) | Who acts next |
| [ball_prompt](slots/ball_prompt.md) | The ask, in prose, addressed to whoever holds the ball |
| [ball_reason](slots/ball_reason.md) | Why the ball holder holds it |
| [body](slots/body.md) | The human-readable content |
| [branches](slots/branches.md) | Git branches associated with the task |
| [category](slots/category.md) | Validated against the project config vocabulary at save time, not enumerated ... |
| [constraints](slots/constraints.md) | Hard requirements and prohibitions |
| [context](slots/context.md) | Curated read-this-first pointers, each with a reason |
| [created](slots/created.md) | Creation timestamp |
| [data](slots/data.md) | Optional structured payload, typed per entry type |
| [deliverables](slots/deliverables.md) | Artifacts the task is expected to produce |
| [dependencies](slots/dependencies.md) | Relationships to other tasks |
| [description](slots/description.md) | WHAT to do -- the working spec |
| [effort](slots/effort.md) | Free text; renamed from estimated_effort |
| [eligible](slots/eligible.md) | Who may claim this task |
| [id](slots/id.md) | Unique task identifier (e |
| [intent](slots/intent.md) | WHY this task exists |
| [kind](slots/kind.md) | What kind of party this is |
| [lifecycle](slots/lifecycle.md) | Where the task is in its life |
| [links](slots/links.md) | External references |
| [log](slots/log.md) | One append-only typed log (section 4) |
| [merged_at](slots/merged_at.md) | When the branch was merged, if it was |
| [name](slots/name.md) | Git branch name |
| [note](slots/note.md) | What it is |
| [out_of_scope](slots/out_of_scope.md) | Explicit non-goals, so agents do not wander |
| [outcome](slots/outcome.md) | How the task ended |
| [owner](slots/owner.md) | Current owner, referenced by actor id (D4) |
| [parent](slots/parent.md) | Task id of an umbrella task |
| [path](slots/path.md) | Repository-relative path |
| [priority](slots/priority.md) |  |
| [re](slots/re.md) | Optional id of an earlier entry this one responds to |
| [rel](slots/rel.md) | What this link is |
| [schema](slots/schema.md) | Schema version stamp |
| [spec](slots/spec.md) | The structured briefing |
| [status](slots/status.md) |  |
| [summary](slots/summary.md) | One to two sentences |
| [tags](slots/tags.md) | Also validated against the config vocabulary at save |
| [task](slots/task.md) | Referenced task identifier |
| [text](slots/text.md) | The condition, stated so it can be judged true or false |
| [title](slots/title.md) | Task title summarising the work |
| [ts](slots/ts.md) | When the event happened |
| [type](slots/type.md) |  |
| [updated](slots/updated.md) | Last update timestamp |
| [url](slots/url.md) | Target URL |
| [verify](slots/verify.md) | Optional machine-checkable hint -- a command that demonstrates the criterion |
| [why](slots/why.md) | What the reader will find there |


## Enumerations

| Enumeration | Description |
| --- | --- |
| [AcceptanceStatus](enums/AcceptanceStatus.md) | Whether a criterion is verified |
| [ActorKind](enums/ActorKind.md) | What kind of party acted |
| [Ball](enums/Ball.md) | Who acts next |
| [BallReason](enums/BallReason.md) | Why the ball holder holds it |
| [BranchStatus](enums/BranchStatus.md) | Branch lifecycle |
| [DeliverableStatus](enums/DeliverableStatus.md) | Whether a deliverable was produced |
| [DependencyType](enums/DependencyType.md) | Relationship to another task |
| [Lifecycle](enums/Lifecycle.md) | Where the task is in its life |
| [LinkRel](enums/LinkRel.md) | What an external link is |
| [LogEntryType](enums/LogEntryType.md) | What kind of event a log entry records (section 4) |
| [Outcome](enums/Outcome.md) | How the task ended |
| [Priority](enums/Priority.md) | Relative priority weighting |


## Types

| Type | Description |
| --- | --- |
| [Boolean](types/Boolean.md) | A binary (true or false) value |
| [Curie](types/Curie.md) | a compact URI |
| [Date](types/Date.md) | a date (year, month and day) in an idealized calendar |
| [DateOrDatetime](types/DateOrDatetime.md) | Either a date or a datetime |
| [Datetime](types/Datetime.md) | The combination of a date and time |
| [Decimal](types/Decimal.md) | A real number with arbitrary precision that conforms to the xsd:decimal speci... |
| [Double](types/Double.md) | A real number that conforms to the xsd:double specification |
| [Float](types/Float.md) | A real number that conforms to the xsd:float specification |
| [Integer](types/Integer.md) | An integer |
| [Jsonpath](types/Jsonpath.md) | A string encoding a JSON Path |
| [Jsonpointer](types/Jsonpointer.md) | A string encoding a JSON Pointer |
| [Ncname](types/Ncname.md) | Prefix part of CURIE |
| [Nodeidentifier](types/Nodeidentifier.md) | A URI, CURIE or BNODE that represents a node in a model |
| [Objectidentifier](types/Objectidentifier.md) | A URI or CURIE that represents an object in the model |
| [Sparqlpath](types/Sparqlpath.md) | A string encoding a SPARQL Property Path |
| [String](types/String.md) | A character string |
| [Time](types/Time.md) | A time object represents a (local) time of day, independent of any particular... |
| [Uri](types/Uri.md) | a complete URI |
| [Uriorcurie](types/Uriorcurie.md) | a URI or a CURIE |


## Subsets

| Subset | Description |
| --- | --- |
