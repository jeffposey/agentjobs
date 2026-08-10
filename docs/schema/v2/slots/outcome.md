---
search:
  boost: 5.0
---

# Slot: outcome 


_How the task ended. Set if and only if lifecycle is closed; absent or null while open._



<div data-search-exclude markdown="1">



URI: [aj:slot/outcome](https://github.com/jeffposey/agentjobs/schema/v2/slot/outcome)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Outcome](../enums/Outcome.md) |
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
| self | aj:outcome |
| native | aj:outcome |




## LinkML Source

<details>
```yaml
name: outcome
description: How the task ended. Set if and only if lifecycle is closed; absent or
  null while open.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Outcome

```
</details></div>