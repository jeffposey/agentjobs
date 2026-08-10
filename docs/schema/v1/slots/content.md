---
search:
  boost: 5.0
---

# Slot: content 

<div data-search-exclude markdown="1">



URI: [aj:slot/content](https://github.com/jeffposey/agentjobs/schema/v1/slot/content)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompt](../classes/Prompt.md) | Individual prompt entry for a task |  no  |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Prompt](../classes/Prompt.md), [Comment](../classes/Comment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:content |
| native | aj:content |




## LinkML Source

<details>
```yaml
name: content
domain_of:
- Prompt
- Comment
range: string

```
</details></div>