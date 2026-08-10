---
search:
  boost: 5.0
---

# Slot: data 


_Optional structured payload, typed per entry type. For `transition` entries it carries the state delta, e.g. {lifecycle: active, ball: agent, ball_reason: work}. Deliberately unconstrained at the schema level; the per-type shape is validated by the manager that writes it._



<div data-search-exclude markdown="1">



URI: [aj:slot/data](https://github.com/jeffposey/agentjobs/schema/v2/slot/data)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [AnyValue](../classes/AnyValue.md) |
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
| self | aj:data |
| native | aj:data |




## LinkML Source

<details>
```yaml
name: data
description: 'Optional structured payload, typed per entry type. For `transition`
  entries it carries the state delta, e.g. {lifecycle: active, ball: agent, ball_reason:
  work}. Deliberately unconstrained at the schema level; the per-type shape is validated
  by the manager that writes it.'
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: AnyValue
inlined: true

```
</details></div>