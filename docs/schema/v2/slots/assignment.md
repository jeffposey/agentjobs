---
search:
  boost: 5.0
---

# Slot: assignment 


_Live ownership plus authoring-time eligibility._



<div data-search-exclude markdown="1">



URI: [aj:slot/assignment](https://github.com/jeffposey/agentjobs/schema/v2/slot/assignment)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Assignment](../classes/Assignment.md) |
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
| self | aj:assignment |
| native | aj:assignment |




## LinkML Source

<details>
```yaml
name: assignment
description: Live ownership plus authoring-time eligibility.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Assignment
inlined: true

```
</details></div>