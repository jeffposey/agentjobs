---
search:
  boost: 5.0
---

# Slot: acceptance 


_What "done" means. Replaces success_criteria._



<div data-search-exclude markdown="1">



URI: [aj:slot/acceptance](https://github.com/jeffposey/agentjobs/schema/v2/slot/acceptance)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [AcceptanceCriterion](../classes/AcceptanceCriterion.md) |
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
| self | aj:acceptance |
| native | aj:acceptance |




## LinkML Source

<details>
```yaml
name: acceptance
description: What "done" means. Replaces success_criteria.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: AcceptanceCriterion
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>