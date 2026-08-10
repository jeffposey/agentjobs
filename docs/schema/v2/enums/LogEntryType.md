---
search:
  boost: 2.0
---


# Enum: LogEntryType 




_What kind of event a log entry records (section 4). One typed log replaces v1's status_updates, comments and prompts.followups._



<div data-search-exclude markdown="1">

URI: [aj:enum/LogEntryType](https://github.com/jeffposey/agentjobs/schema/v2/enum/LogEntryType)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| note | None | Free-form remark |
| progress | None | Work narration -- what was done, what was verified |
| transition | None | Automatic record of a state-axis change; `data` carries the delta |
| handoff | None | The ball is moving; `body` is the ask, mirroring `ball_prompt` |
| decision | None | A choice, its reasoning, and the rejected alternative |
| question | None | An explicit open thread |
| answer | None | Resolves a `question`, via `re` |
| instruction | None | A directive to the working agent |













## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: LogEntryType
description: What kind of event a log entry records (section 4). One typed log replaces
  v1's status_updates, comments and prompts.followups.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  note:
    text: note
    description: Free-form remark. Anyone.
  progress:
    text: progress
    description: Work narration -- what was done, what was verified. Agent.
  transition:
    text: transition
    description: Automatic record of a state-axis change; `data` carries the delta.
      Written by the manager, never trusted to callers.
  handoff:
    text: handoff
    description: The ball is moving; `body` is the ask, mirroring `ball_prompt`.
  decision:
    text: decision
    description: A choice, its reasoning, and the rejected alternative. Binding.
  question:
    text: question
    description: An explicit open thread. Surfaceable in UIs until answered.
  answer:
    text: answer
    description: Resolves a `question`, via `re`.
  instruction:
    text: instruction
    description: A directive to the working agent. Replaces v1 followup prompts.

```
</details>

</div>