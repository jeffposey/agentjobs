---
search:
  boost: 5.0
---

# Slot: parent 


_Task id of an umbrella task. Tasks with open children are never claimable. Absorbs task-045's parent/child design._



<div data-search-exclude markdown="1">



URI: [aj:slot/parent](https://github.com/jeffposey/agentjobs/schema/v2/slot/parent)
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
| self | aj:parent |
| native | aj:parent |




## LinkML Source

<details>
```yaml
name: parent
description: Task id of an umbrella task. Tasks with open children are never claimable.
  Absorbs task-045's parent/child design.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>