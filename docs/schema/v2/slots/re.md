---
search:
  boost: 5.0
---

# Slot: re 


_Optional id of an earlier entry this one responds to. How an `answer` attaches to its `question`._



<div data-search-exclude markdown="1">



URI: [aj:slot/re](https://github.com/jeffposey/agentjobs/schema/v2/slot/re)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Integer](../types/Integer.md) |
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
| self | aj:re |
| native | aj:re |




## LinkML Source

<details>
```yaml
name: re
description: Optional id of an earlier entry this one responds to. How an `answer`
  attaches to its `question`.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: integer

```
</details></div>