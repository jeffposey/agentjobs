---
search:
  boost: 5.0
---

# Slot: ball_prompt 


_The ask, in prose, addressed to whoever holds the ball. Required whenever the ball is set (tenet 3): a handoff without its payload is rejected at the schema level. May default for agent/available, where the spec is the ask._



<div data-search-exclude markdown="1">



URI: [aj:slot/ball_prompt](https://github.com/jeffposey/agentjobs/schema/v2/slot/ball_prompt)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:ball_prompt |
| native | aj:ball_prompt |




## LinkML Source

<details>
```yaml
name: ball_prompt
description: 'The ask, in prose, addressed to whoever holds the ball. Required whenever
  the ball is set (tenet 3): a handoff without its payload is rejected at the schema
  level. May default for agent/available, where the spec is the ask.'
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>