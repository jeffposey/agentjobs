---
search:
  boost: 5.0
---

# Slot: estimated_effort 


_Estimated effort (time or complexity). Free text._



<div data-search-exclude markdown="1">



URI: [aj:slot/estimated_effort](https://github.com/jeffposey/agentjobs/schema/v1/slot/estimated_effort)
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
| self | aj:estimated_effort |
| native | aj:estimated_effort |




## LinkML Source

<details>
```yaml
name: estimated_effort
description: Estimated effort (time or complexity). Free text.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>