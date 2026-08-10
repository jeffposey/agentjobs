---
search:
  boost: 5.0
---

# Slot: type 


_Relationship type, validated against depends_on | blocks | related but typed as a bare str._



<div data-search-exclude markdown="1">



URI: [aj:slot/type](https://github.com/jeffposey/agentjobs/schema/v1/slot/type)
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
| If Absent | `string(depends_on)` |
| Owner | [Dependency](../classes/Dependency.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:type |
| native | aj:type |




## LinkML Source

<details>
```yaml
name: type
description: Relationship type, validated against depends_on | blocks | related but
  typed as a bare str.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
ifabsent: string(depends_on)
owner: Dependency
domain_of:
- Dependency
range: string

```
</details></div>