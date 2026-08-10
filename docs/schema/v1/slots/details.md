---
search:
  boost: 5.0
---

# Slot: details 


_Expanded detail for the status update._



<div data-search-exclude markdown="1">



URI: [aj:slot/details](https://github.com/jeffposey/agentjobs/schema/v1/slot/details)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [StatusUpdate](../classes/StatusUpdate.md) | Chronological status update authored during task execution |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [StatusUpdate](../classes/StatusUpdate.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [StatusUpdate](../classes/StatusUpdate.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:details |
| native | aj:details |




## LinkML Source

<details>
```yaml
name: details
description: Expanded detail for the status update.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: StatusUpdate
domain_of:
- StatusUpdate
range: string

```
</details></div>