---
search:
  boost: 5.0
---

# Slot: task 


_Referenced task identifier. Must exist in the store._



<div data-search-exclude markdown="1">



URI: [aj:slot/task](https://github.com/jeffposey/agentjobs/schema/v2/slot/task)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Dependency](../classes/Dependency.md) | A relationship to another task |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Dependency](../classes/Dependency.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Dependency](../classes/Dependency.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:task |
| native | aj:task |




## LinkML Source

<details>
```yaml
name: task
description: Referenced task identifier. Must exist in the store.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Dependency
domain_of:
- Dependency
range: string
required: true

```
</details></div>