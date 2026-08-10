---
search:
  boost: 5.0
---

# Slot: status 

<div data-search-exclude markdown="1">



URI: [aj:slot/status](https://github.com/jeffposey/agentjobs/schema/v1/slot/status)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |
| [Phase](../classes/Phase.md) | Discrete phase within a task roadmap |  no  |
| [SuccessCriterion](../classes/SuccessCriterion.md) | Success criterion tracked per task |  no  |
| [StatusUpdate](../classes/StatusUpdate.md) | Chronological status update authored during task execution |  no  |
| [Deliverable](../classes/Deliverable.md) | Deliverable artifact tracked for task completion |  no  |
| [Dependency](../classes/Dependency.md) | Relationship metadata between tasks |  no  |
| [Issue](../classes/Issue.md) | Issue tracked against the task's lifecycle |  no  |
| [Branch](../classes/Branch.md) | Branch lifecycle metadata |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Phase](../classes/Phase.md), [SuccessCriterion](../classes/SuccessCriterion.md), [StatusUpdate](../classes/StatusUpdate.md), [Deliverable](../classes/Deliverable.md), [Dependency](../classes/Dependency.md), [Issue](../classes/Issue.md), [Branch](../classes/Branch.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:status |
| native | aj:status |




## LinkML Source

<details>
```yaml
name: status
domain_of:
- Task
- Phase
- SuccessCriterion
- StatusUpdate
- Deliverable
- Dependency
- Issue
- Branch
range: string

```
</details></div>