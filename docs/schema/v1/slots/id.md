---
search:
  boost: 5.0
---

# Slot: id 

<div data-search-exclude markdown="1">



URI: [aj:slot/id](https://github.com/jeffposey/agentjobs/schema/v1/slot/id)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |
| [Phase](../classes/Phase.md) | Discrete phase within a task roadmap |  no  |
| [SuccessCriterion](../classes/SuccessCriterion.md) | Success criterion tracked per task |  no  |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |
| [Issue](../classes/Issue.md) | Issue tracked against the task's lifecycle |  no  |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Phase](../classes/Phase.md), [SuccessCriterion](../classes/SuccessCriterion.md), [Comment](../classes/Comment.md), [Issue](../classes/Issue.md), [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:id |
| native | aj:id |




## LinkML Source

<details>
```yaml
name: id
domain_of:
- Task
- Phase
- SuccessCriterion
- Comment
- Issue
- Webhook
range: string

```
</details></div>