---
search:
  boost: 5.0
---

# Slot: owner 


_Current owner, referenced by actor id (D4). Set on claim, cleared on release or close. Absent or null in draft and ready, required while active (enforced in task-050, see note above)._



<div data-search-exclude markdown="1">



URI: [aj:slot/owner](https://github.com/jeffposey/agentjobs/schema/v2/slot/owner)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Assignment](../classes/Assignment.md) | Separates live ownership from authoring-time eligibility -- v1 conflated both... |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Actor](../classes/Actor.md) |
| Domain Of | [Assignment](../classes/Assignment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Assignment](../classes/Assignment.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:owner |
| native | aj:owner |




## LinkML Source

<details>
```yaml
name: owner
description: Current owner, referenced by actor id (D4). Set on claim, cleared on
  release or close. Absent or null in draft and ready, required while active (enforced
  in task-050, see note above).
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Assignment
domain_of:
- Assignment
range: Actor
inlined: false

```
</details></div>