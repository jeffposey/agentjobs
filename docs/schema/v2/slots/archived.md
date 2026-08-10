---
search:
  boost: 5.0
---

# Slot: archived 


_Visibility flag, orthogonal to how the task ended. Lets an old completed task and an abandoned draft both be hidden without destroying what they were._



<div data-search-exclude markdown="1">



URI: [aj:slot/archived](https://github.com/jeffposey/agentjobs/schema/v2/slot/archived)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Boolean](../types/Boolean.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| If Absent | `false` |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:archived |
| native | aj:archived |




## LinkML Source

<details>
```yaml
name: archived
description: Visibility flag, orthogonal to how the task ended. Lets an old completed
  task and an abandoned draft both be hidden without destroying what they were.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
ifabsent: 'false'
owner: Task
domain_of:
- Task
range: boolean

```
</details></div>