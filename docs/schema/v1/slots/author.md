---
search:
  boost: 5.0
---

# Slot: author 

<div data-search-exclude markdown="1">



URI: [aj:slot/author](https://github.com/jeffposey/agentjobs/schema/v1/slot/author)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompt](../classes/Prompt.md) | Individual prompt entry for a task |  no  |
| [StatusUpdate](../classes/StatusUpdate.md) | Chronological status update authored during task execution |  no  |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Prompt](../classes/Prompt.md), [StatusUpdate](../classes/StatusUpdate.md), [Comment](../classes/Comment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:author |
| native | aj:author |




## LinkML Source

<details>
```yaml
name: author
domain_of:
- Prompt
- StatusUpdate
- Comment
range: string

```
</details></div>