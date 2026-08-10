---
search:
  boost: 5.0
---

# Slot: category 


_Task category for filtering. Free text -- no vocabulary, no validation against config._



<div data-search-exclude markdown="1">



URI: [aj:slot/category](https://github.com/jeffposey/agentjobs/schema/v1/slot/category)
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
| Required | Yes |
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
| self | aj:category |
| native | aj:category |




## LinkML Source

<details>
```yaml
name: category
description: Task category for filtering. Free text -- no vocabulary, no validation
  against config.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: string
required: true

```
</details></div>