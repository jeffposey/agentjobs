---
search:
  boost: 5.0
---

# Slot: task_id 

<div data-search-exclude markdown="1">



URI: [aj:slot/task_id](https://github.com/jeffposey/agentjobs/schema/v1/slot/task_id)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |
| [Dependency](../classes/Dependency.md) | Relationship metadata between tasks |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Comment](../classes/Comment.md), [Dependency](../classes/Dependency.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:task_id |
| native | aj:task_id |




## LinkML Source

<details>
```yaml
name: task_id
domain_of:
- Comment
- Dependency
range: string

```
</details></div>