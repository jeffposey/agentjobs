---
search:
  boost: 5.0
---

# Slot: log 


_One append-only typed log (section 4). Entries are immutable and ordered. Replaces status_updates + comments + prompts.followups._



<div data-search-exclude markdown="1">



URI: [aj:slot/log](https://github.com/jeffposey/agentjobs/schema/v2/slot/log)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [LogEntry](../classes/LogEntry.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
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
| self | aj:log |
| native | aj:log |




## LinkML Source

<details>
```yaml
name: log
description: One append-only typed log (section 4). Entries are immutable and ordered.
  Replaces status_updates + comments + prompts.followups.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: LogEntry
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>