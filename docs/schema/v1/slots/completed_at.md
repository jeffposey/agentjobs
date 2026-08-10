---
search:
  boost: 5.0
---

# Slot: completed_at 


_Timestamp when the phase reached completion._



<div data-search-exclude markdown="1">



URI: [aj:slot/completed_at](https://github.com/jeffposey/agentjobs/schema/v1/slot/completed_at)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Phase](../classes/Phase.md) | Discrete phase within a task roadmap |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Datetime](../types/Datetime.md) |
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
| self | aj:completed_at |
| native | aj:completed_at |




## LinkML Source

<details>
```yaml
name: completed_at
description: Timestamp when the phase reached completion.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Phase
domain_of:
- Phase
range: datetime

```
</details></div>