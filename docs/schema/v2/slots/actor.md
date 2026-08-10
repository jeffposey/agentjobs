---
search:
  boost: 5.0
---

# Slot: actor 


_Who or what produced this entry, referenced by actor id (D4). `kind` is resolved from config and is never stored here._



<div data-search-exclude markdown="1">



URI: [aj:slot/actor](https://github.com/jeffposey/agentjobs/schema/v2/slot/actor)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Actor](../classes/Actor.md) |
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
| self | aj:actor |
| native | aj:actor |




## LinkML Source

<details>
```yaml
name: actor
description: Who or what produced this entry, referenced by actor id (D4). `kind`
  is resolved from config and is never stored here.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: Actor
required: true
inlined: false

```
</details></div>