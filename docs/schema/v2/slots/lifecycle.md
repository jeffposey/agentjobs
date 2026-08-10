---
search:
  boost: 5.0
---

# Slot: lifecycle 


_Where the task is in its life._



<div data-search-exclude markdown="1">



URI: [aj:slot/lifecycle](https://github.com/jeffposey/agentjobs/schema/v2/slot/lifecycle)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Lifecycle](../enums/Lifecycle.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| If Absent | `string(draft)` |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:lifecycle |
| native | aj:lifecycle |




## LinkML Source

<details>
```yaml
name: lifecycle
description: Where the task is in its life.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
ifabsent: string(draft)
owner: Task
domain_of:
- Task
range: Lifecycle
required: true

```
</details></div>