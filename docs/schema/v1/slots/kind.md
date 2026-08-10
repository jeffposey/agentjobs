---
search:
  boost: 5.0
---

# Slot: kind 


_Documents comment | feedback | question in its description and enforces nothing. One of v1's two unvalidated free-text vocabularies._



<div data-search-exclude markdown="1">



URI: [aj:slot/kind](https://github.com/jeffposey/agentjobs/schema/v1/slot/kind)
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
| If Absent | `string(comment)` |
| Owner | [Comment](../classes/Comment.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:kind |
| native | aj:kind |




## LinkML Source

<details>
```yaml
name: kind
description: Documents comment | feedback | question in its description and enforces
  nothing. One of v1's two unvalidated free-text vocabularies.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
ifabsent: string(comment)
owner: Comment
domain_of:
- Comment
range: string

```
</details></div>