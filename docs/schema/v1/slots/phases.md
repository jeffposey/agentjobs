---
search:
  boost: 5.0
---

# Slot: phases 


_Sub-units inside one task. Not claimable, no prompts, no comments. Overlaps with real sub-tasks; deleted in v2 (D1)._



<div data-search-exclude markdown="1">



URI: [aj:slot/phases](https://github.com/jeffposey/agentjobs/schema/v1/slot/phases)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Phase](../classes/Phase.md) |
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
| self | aj:phases |
| native | aj:phases |




## LinkML Source

<details>
```yaml
name: phases
description: Sub-units inside one task. Not claimable, no prompts, no comments. Overlaps
  with real sub-tasks; deleted in v2 (D1).
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Phase
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>