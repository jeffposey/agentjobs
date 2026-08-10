---
search:
  boost: 5.0
---

# Slot: effort 


_Free text; renamed from estimated_effort. It is an estimate, not a contract, so it is deliberately unstructured._



<div data-search-exclude markdown="1">



URI: [aj:slot/effort](https://github.com/jeffposey/agentjobs/schema/v2/slot/effort)
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
| self | aj:effort |
| native | aj:effort |




## LinkML Source

<details>
```yaml
name: effort
description: Free text; renamed from estimated_effort. It is an estimate, not a contract,
  so it is deliberately unstructured.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>