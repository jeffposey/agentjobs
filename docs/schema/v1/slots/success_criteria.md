---
search:
  boost: 5.0
---

# Slot: success_criteria 


_Success criteria checklist._



<div data-search-exclude markdown="1">



URI: [aj:slot/success_criteria](https://github.com/jeffposey/agentjobs/schema/v1/slot/success_criteria)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [SuccessCriterion](../classes/SuccessCriterion.md) |
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
| self | aj:success_criteria |
| native | aj:success_criteria |




## LinkML Source

<details>
```yaml
name: success_criteria
description: Success criteria checklist.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: SuccessCriterion
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>