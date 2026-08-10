---
search:
  boost: 5.0
---

# Slot: tags 


_Tag metadata for filtering and search. No vocabulary enforced._



<div data-search-exclude markdown="1">



URI: [aj:slot/tags](https://github.com/jeffposey/agentjobs/schema/v1/slot/tags)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
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
| self | aj:tags |
| native | aj:tags |




## LinkML Source

<details>
```yaml
name: tags
description: Tag metadata for filtering and search. No vocabulary enforced.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: string
multivalued: true

```
</details></div>