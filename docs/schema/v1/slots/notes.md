---
search:
  boost: 5.0
---

# Slot: notes 


_Optional free-form notes about the phase._



<div data-search-exclude markdown="1">



URI: [aj:slot/notes](https://github.com/jeffposey/agentjobs/schema/v1/slot/notes)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Phase](../classes/Phase.md) | Discrete phase within a task roadmap |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Phase](../classes/Phase.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Phase](../classes/Phase.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:notes |
| native | aj:notes |




## LinkML Source

<details>
```yaml
name: notes
description: Optional free-form notes about the phase.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Phase
domain_of:
- Phase
range: string

```
</details></div>