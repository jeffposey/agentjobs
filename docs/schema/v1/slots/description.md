---
search:
  boost: 5.0
---

# Slot: description 

<div data-search-exclude markdown="1">



URI: [aj:slot/description](https://github.com/jeffposey/agentjobs/schema/v1/slot/description)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |
| [SuccessCriterion](../classes/SuccessCriterion.md) | Success criterion tracked per task |  no  |
| [Deliverable](../classes/Deliverable.md) | Deliverable artifact tracked for task completion |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [SuccessCriterion](../classes/SuccessCriterion.md), [Deliverable](../classes/Deliverable.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:description |
| native | aj:description |




## LinkML Source

<details>
```yaml
name: description
domain_of:
- Task
- SuccessCriterion
- Deliverable
range: string

```
</details></div>