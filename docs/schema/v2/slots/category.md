---
search:
  boost: 5.0
---

# Slot: category 


_Validated against the project config vocabulary at save time, not enumerated in this schema -- taxonomy is project-local, semantics are not._



<div data-search-exclude markdown="1">



URI: [aj:slot/category](https://github.com/jeffposey/agentjobs/schema/v2/slot/category)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:category |
| native | aj:category |




## LinkML Source

<details>
```yaml
name: category
description: Validated against the project config vocabulary at save time, not enumerated
  in this schema -- taxonomy is project-local, semantics are not.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: string
required: true

```
</details></div>