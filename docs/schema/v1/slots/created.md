---
search:
  boost: 5.0
---

# Slot: created 

<div data-search-exclude markdown="1">



URI: [aj:slot/created](https://github.com/jeffposey/agentjobs/schema/v1/slot/created)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Comment](../classes/Comment.md), [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:created |
| native | aj:created |




## LinkML Source

<details>
```yaml
name: created
domain_of:
- Task
- Comment
- Webhook
range: string

```
</details></div>