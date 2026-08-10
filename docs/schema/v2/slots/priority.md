---
search:
  boost: 5.0
---

# Slot: priority 

<div data-search-exclude markdown="1">



URI: [aj:slot/priority](https://github.com/jeffposey/agentjobs/schema/v2/slot/priority)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Priority](../enums/Priority.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| If Absent | `string(medium)` |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:priority |
| native | aj:priority |




## LinkML Source

<details>
```yaml
name: priority
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
ifabsent: string(medium)
owner: Task
domain_of:
- Task
range: Priority

```
</details></div>