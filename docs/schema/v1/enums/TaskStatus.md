---
search:
  boost: 2.0
---


# Enum: TaskStatus 




_High-level workflow status. This single 8-value vocabulary answers three different questions at once (where in its life / who acts next / why), which is the central problem v2's state axes exist to solve._



<div data-search-exclude markdown="1">

URI: [aj:enum/TaskStatus](https://github.com/jeffposey/agentjobs/schema/v1/enum/TaskStatus)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| draft | None | Being specified |
| ready | None | Spec complete, claimable |
| in_progress | None | Claimed and being worked |
| blocked | None | Cannot proceed |
| waiting_for_human | None | Needs a human |
| under_review | None | Work product awaiting review |
| completed | None | Finished successfully |
| archived | None | Hidden |




## Slots

| Name | Description |
| ---  | --- |
| [status](../slots/status.md) | Current workflow status |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1






## LinkML Source

<details>
```yaml
name: TaskStatus
description: High-level workflow status. This single 8-value vocabulary answers three
  different questions at once (where in its life / who acts next / why), which is
  the central problem v2's state axes exist to solve.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
permissible_values:
  draft:
    text: draft
    description: Being specified.
  ready:
    text: ready
    description: Spec complete, claimable.
  in_progress:
    text: in_progress
    description: Claimed and being worked.
  blocked:
    text: blocked
    description: Cannot proceed. Does not say blocked on what, or on whom.
  waiting_for_human:
    text: waiting_for_human
    description: Needs a human. Does not say what for.
  under_review:
    text: under_review
    description: Work product awaiting review. The tell that "why" leaked into this
      vocabulary -- it is really waiting_for_human because review.
  completed:
    text: completed
    description: Finished successfully.
  archived:
    text: archived
    description: Hidden. Conflates visibility with outcome, so an abandoned draft
      and an old finished task become indistinguishable.

```
</details>

</div>