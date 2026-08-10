---
search:
  boost: 5.0
---

# Slot: branches 


_Branch metadata associated with the task._



<div data-search-exclude markdown="1">



URI: [aj:slot/branches](https://github.com/jeffposey/agentjobs/schema/v1/slot/branches)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Branch](../classes/Branch.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:branches |
| native | aj:branches |




## LinkML Source

<details>
```yaml
name: branches
description: Branch metadata associated with the task.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Branch
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>