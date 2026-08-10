---
search:
  boost: 5.0
---

# Slot: note 


_Additional notes about the dependency._



<div data-search-exclude markdown="1">



URI: [aj:slot/note](https://github.com/jeffposey/agentjobs/schema/v1/slot/note)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Dependency](../classes/Dependency.md) | Relationship metadata between tasks |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Dependency](../classes/Dependency.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Dependency](../classes/Dependency.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:note |
| native | aj:note |




## LinkML Source

<details>
```yaml
name: note
description: Additional notes about the dependency.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Dependency
domain_of:
- Dependency
range: string

```
</details></div>