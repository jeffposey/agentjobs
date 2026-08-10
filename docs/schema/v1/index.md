# AgentJobs Task Schema v1

The task schema as currently implemented in src/agentjobs/models.py, transcribed into LinkML so it can be rendered, browsed, and diffed against v2 by real schema tooling rather than hand-drawn diagrams.
This is a DESCRIPTIVE schema: it records v1 as it actually is, warts included, so the v1-to-v2 comparison is honest. Where v1 documents a vocabulary in prose but does not enforce it, that is called out in the slot description rather than quietly fixed here. Validated against the live corpus via `linkml-validate -s schema/agentjobs-v1.yaml tasks/agentjobs/*.yaml`.

URI: https://github.com/jeffposey/agentjobs/schema/v1

Name: agentjobs-v1



## Classes

| Class | Description |
| --- | --- |
| [Branch](classes/Branch.md) | Branch lifecycle metadata |
| [Comment](classes/Comment.md) | Comment on a task for human-agent communication |
| [Deliverable](classes/Deliverable.md) | Deliverable artifact tracked for task completion |
| [Dependency](classes/Dependency.md) | Relationship metadata between tasks |
| [ExternalLink](classes/ExternalLink.md) | Reference to a relevant external resource |
| [Issue](classes/Issue.md) | Issue tracked against the task's lifecycle |
| [Phase](classes/Phase.md) | Discrete phase within a task roadmap |
| [Prompt](classes/Prompt.md) | Individual prompt entry for a task |
| [Prompts](classes/Prompts.md) | Collection of prompt content for a task |
| [StatusUpdate](classes/StatusUpdate.md) | Chronological status update authored during task execution |
| [SuccessCriterion](classes/SuccessCriterion.md) | Success criterion tracked per task |
| [Task](classes/Task.md) | Primary task representation |
| [Webhook](classes/Webhook.md) | Webhook configuration for task event notifications |



## Slots

| Slot | Description |
| --- | --- |
| [active](slots/active.md) | Whether this webhook is active |
| [assigned_to](slots/assigned_to.md) | Documented as "currently assigned", used in practice as a static authoring-ti... |
| [author](slots/author.md) | Author of the prompt (agent or human name) |
| [branches](slots/branches.md) | Branch metadata associated with the task |
| [category](slots/category.md) | Task category for filtering |
| [comments](slots/comments.md) | Comments and feedback |
| [completed_at](slots/completed_at.md) | Timestamp when the phase reached completion |
| [content](slots/content.md) | Inline prompt content when not referencing a file |
| [context](slots/context.md) | Additional context regarding the prompt |
| [created](slots/created.md) | Creation timestamp |
| [deliverables](slots/deliverables.md) | Deliverables associated with task completion |
| [dependencies](slots/dependencies.md) | Task dependencies and relationships |
| [description](slots/description.md) | Markdown description |
| [details](slots/details.md) | Expanded detail for the status update |
| [estimated_effort](slots/estimated_effort.md) | Estimated effort (time or complexity) |
| [events](slots/events.md) | Events that trigger this webhook |
| [external_links](slots/external_links.md) | External references for the task |
| [followups](slots/followups.md) | Subsequent prompts appended during task progression |
| [human_summary](slots/human_summary.md) | Concise 1-2 sentence summary for human reviewers |
| [id](slots/id.md) | Unique task identifier (e |
| [issues](slots/issues.md) | Issues encountered while executing the task |
| [kind](slots/kind.md) | Documents comment | feedback | question in its description and enforces nothi... |
| [last_triggered](slots/last_triggered.md) | Last successful trigger |
| [merged_at](slots/merged_at.md) | When the branch was merged, if applicable |
| [name](slots/name.md) | Git branch name associated with the task |
| [note](slots/note.md) | Additional notes about the dependency |
| [notes](slots/notes.md) | Optional free-form notes about the phase |
| [path](slots/path.md) | Repository-relative path to the deliverable |
| [phases](slots/phases.md) | Sub-units inside one task |
| [priority](slots/priority.md) | Relative priority weighting |
| [prompt_file](slots/prompt_file.md) | Optional path reference to the prompt file |
| [prompts](slots/prompts.md) | Prompt collection used by collaborating agents |
| [reply_to](slots/reply_to.md) | Parent comment ID if this is a reply |
| [resolution](slots/resolution.md) | Resolution notes when an issue is closed |
| [secret](slots/secret.md) | Secret for HMAC signature verification |
| [starter](slots/starter.md) | Primary starter prompt content |
| [status](slots/status.md) | Current workflow status |
| [status_updates](slots/status_updates.md) | Chronological status updates |
| [success_criteria](slots/success_criteria.md) | Success criteria checklist |
| [summary](slots/summary.md) | Short summary of the update |
| [tags](slots/tags.md) | Tag metadata for filtering and search |
| [task_id](slots/task_id.md) | Task this comment belongs to |
| [timestamp](slots/timestamp.md) | Timestamp for when the prompt was issued |
| [title](slots/title.md) | Task title summarising the work |
| [type](slots/type.md) | Relationship type, validated against depends_on | blocks | related but typed ... |
| [updated](slots/updated.md) | Last update timestamp |
| [url](slots/url.md) | External resource URL |


## Enumerations

| Enumeration | Description |
| --- | --- |
| [Priority](enums/Priority.md) | Relative priority weighting |
| [TaskStatus](enums/TaskStatus.md) | High-level workflow status |


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
