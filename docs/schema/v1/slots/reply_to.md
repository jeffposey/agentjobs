---
search:
  boost: 5.0
---

# Slot: reply_to 


_Parent comment ID if this is a reply._



<div data-search-exclude markdown="1">



URI: [aj:slot/reply_to](https://github.com/jeffposey/agentjobs/schema/v1/slot/reply_to)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Comment](../classes/Comment.md) | Comment on a task for human-agent communication |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Comment](../classes/Comment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Comment](../classes/Comment.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:reply_to |
| native | aj:reply_to |




## LinkML Source

<details>
```yaml
name: reply_to
description: Parent comment ID if this is a reply.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Comment
domain_of:
- Comment
range: string

```
</details></div>