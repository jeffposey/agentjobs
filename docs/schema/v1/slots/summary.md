---
search:
  boost: 5.0
---

# Slot: summary 


_Short summary of the update._



<div data-search-exclude markdown="1">



URI: [aj:slot/summary](https://github.com/jeffposey/agentjobs/schema/v1/slot/summary)
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
| Required | Yes |
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
| self | aj:summary |
| native | aj:summary |




## LinkML Source

<details>
```yaml
name: summary
description: Short summary of the update.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: StatusUpdate
domain_of:
- StatusUpdate
range: string
required: true

```
</details></div>