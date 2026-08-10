---
search:
  boost: 5.0
---

# Slot: comments 


_Comments and feedback. Second append-only authored log; merged with status_updates into one typed log in v2._



<div data-search-exclude markdown="1">



URI: [aj:slot/comments](https://github.com/jeffposey/agentjobs/schema/v1/slot/comments)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Comment](../classes/Comment.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:comments |
| native | aj:comments |




## LinkML Source

<details>
```yaml
name: comments
description: Comments and feedback. Second append-only authored log; merged with status_updates
  into one typed log in v2.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Comment
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>