---
search:
  boost: 5.0
---

# Slot: deliverables 


_Artifacts the task is expected to produce._



<div data-search-exclude markdown="1">



URI: [aj:slot/deliverables](https://github.com/jeffposey/agentjobs/schema/v2/slot/deliverables)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Deliverable](../classes/Deliverable.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
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
| self | aj:deliverables |
| native | aj:deliverables |




## LinkML Source

<details>
```yaml
name: deliverables
description: Artifacts the task is expected to produce.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Deliverable
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>