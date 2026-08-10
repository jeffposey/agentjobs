---
search:
  boost: 5.0
---

# Slot: spec 


_The structured briefing. Replaces v1's single description blob and its duplicated starter prompt._



<div data-search-exclude markdown="1">



URI: [aj:slot/spec](https://github.com/jeffposey/agentjobs/schema/v2/slot/spec)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Spec](../classes/Spec.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:spec |
| native | aj:spec |




## LinkML Source

<details>
```yaml
name: spec
description: The structured briefing. Replaces v1's single description blob and its
  duplicated starter prompt.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Spec
required: true
inlined: true

```
</details></div>