---
search:
  boost: 5.0
---

# Slot: ball_reason 


_Why the ball holder holds it. Must belong to that holder's vocabulary -- see the rules on this class._



<div data-search-exclude markdown="1">



URI: [aj:slot/ball_reason](https://github.com/jeffposey/agentjobs/schema/v2/slot/ball_reason)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [BallReason](../enums/BallReason.md) |
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
| self | aj:ball_reason |
| native | aj:ball_reason |




## LinkML Source

<details>
```yaml
name: ball_reason
description: Why the ball holder holds it. Must belong to that holder's vocabulary
  -- see the rules on this class.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: BallReason

```
</details></div>