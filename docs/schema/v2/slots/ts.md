---
search:
  boost: 5.0
---

# Slot: ts 


_When the event happened._



<div data-search-exclude markdown="1">



URI: [aj:slot/ts](https://github.com/jeffposey/agentjobs/schema/v2/slot/ts)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Datetime](../types/Datetime.md) |
| Domain Of | [LogEntry](../classes/LogEntry.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [LogEntry](../classes/LogEntry.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:ts |
| native | aj:ts |




## LinkML Source

<details>
```yaml
name: ts
description: When the event happened.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: datetime
required: true

```
</details></div>