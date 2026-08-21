---
search:
  boost: 5.0
---

# Slot: queue_position 


_Explicit order within the priority band. Unique among open tasks of the same priority in one project. Present if and only if the task is open. Assigned in sparse steps of 100 so an insertion takes a midpoint and rewrites one file rather than a whole band._



<div data-search-exclude markdown="1">



URI: [aj:slot/queue_position](https://github.com/jeffposey/agentjobs/schema/v2/slot/queue_position)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Integer](../types/Integer.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Task](../classes/Task.md) |


### Value Constraints

| Property | Value |
| --- | --- |
| Minimum Value | 1 |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:queue_position |
| native | aj:queue_position |




## LinkML Source

<details>
```yaml
name: queue_position
description: Explicit order within the priority band. Unique among open tasks of the
  same priority in one project. Present if and only if the task is open. Assigned
  in sparse steps of 100 so an insertion takes a midpoint and rewrites one file rather
  than a whole band.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: integer
minimum_value: 1

```
</details></div>