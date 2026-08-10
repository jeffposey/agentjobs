---
search:
  boost: 5.0
---

# Slot: status_updates 


_Chronological status updates. Append-only, timestamped, authored -- the same shape as comments, with an implied but unenforced role split._



<div data-search-exclude markdown="1">



URI: [aj:slot/status_updates](https://github.com/jeffposey/agentjobs/schema/v1/slot/status_updates)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [StatusUpdate](../classes/StatusUpdate.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:status_updates |
| native | aj:status_updates |




## LinkML Source

<details>
```yaml
name: status_updates
description: Chronological status updates. Append-only, timestamped, authored -- the
  same shape as comments, with an implied but unenforced role split.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: StatusUpdate
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>