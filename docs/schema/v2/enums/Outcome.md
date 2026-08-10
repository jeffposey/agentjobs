---
search:
  boost: 2.0
---


# Enum: Outcome 




_How the task ended. Set if and only if lifecycle is closed._



<div data-search-exclude markdown="1">

URI: [aj:enum/Outcome](https://github.com/jeffposey/agentjobs/schema/v2/enum/Outcome)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| completed | None |  |
| cancelled | None |  |
| superseded | None |  |
| duplicate | None |  |




## Slots

| Name | Description |
| ---  | --- |
| [outcome](../slots/outcome.md) | How the task ended |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: Outcome
description: How the task ended. Set if and only if lifecycle is closed.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  completed:
    text: completed
  cancelled:
    text: cancelled
  superseded:
    text: superseded
  duplicate:
    text: duplicate

```
</details>

</div>