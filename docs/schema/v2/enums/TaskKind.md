---
search:
  boost: 2.0
---


# Enum: TaskKind 




_What a task is. Absent means implementation. Changes how a task is shown and how an approval is worded, never what an approval authorises (task-592)._



<div data-search-exclude markdown="1">

URI: [aj:enum/TaskKind](https://github.com/jeffposey/agentjobs/schema/v2/enum/TaskKind)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| design | None | A design pass -- its deliverable is a decision, a doc or a plan |
| implementation | None | Building something |




## Slots

| Name | Description |
| ---  | --- |
| [kind](../slots/kind.md) | Design pass or implementation; absent means implementation |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: TaskKind
description: What a task is. Absent means implementation. Changes how a task is shown
  and how an approval is worded, never what an approval authorises (task-592).
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  design:
    text: design
    description: A design pass -- its deliverable is a decision, a doc or a plan.
  implementation:
    text: implementation
    description: Building something. The default when kind is absent.

```
</details>

</div>