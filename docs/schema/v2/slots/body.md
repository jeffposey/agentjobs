---
search:
  boost: 5.0
---

# Slot: body 


_The human-readable content. Markdown. For `handoff` entries this is the ask, mirroring ball_prompt; for `decision` entries it must include the rejected alternative._



<div data-search-exclude markdown="1">



URI: [aj:slot/body](https://github.com/jeffposey/agentjobs/schema/v2/slot/body)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [LogEntry](../classes/LogEntry.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
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
| self | aj:body |
| native | aj:body |




## LinkML Source

<details>
```yaml
name: body
description: The human-readable content. Markdown. For `handoff` entries this is the
  ask, mirroring ball_prompt; for `decision` entries it must include the rejected
  alternative.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: string

```
</details></div>