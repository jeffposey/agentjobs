---
search:
  boost: 5.0
---

# Slot: assigned_to 


_Documented as "currently assigned", used in practice as a static authoring-time label. Ownership and eligibility conflated._



<div data-search-exclude markdown="1">



URI: [aj:slot/assigned_to](https://github.com/jeffposey/agentjobs/schema/v1/slot/assigned_to)
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
| self | aj:assigned_to |
| native | aj:assigned_to |




## LinkML Source

<details>
```yaml
name: assigned_to
description: Documented as "currently assigned", used in practice as a static authoring-time
  label. Ownership and eligibility conflated.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>