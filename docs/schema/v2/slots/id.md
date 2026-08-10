---
search:
  boost: 5.0
---

# Slot: id 

<div data-search-exclude markdown="1">



URI: [aj:slot/id](https://github.com/jeffposey/agentjobs/schema/v2/slot/id)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |
| [Actor](../classes/Actor.md) | A party that can act on tasks |  no  |
| [AcceptanceCriterion](../classes/AcceptanceCriterion.md) | One verifiable condition for done |  no  |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Actor](../classes/Actor.md), [AcceptanceCriterion](../classes/AcceptanceCriterion.md), [LogEntry](../classes/LogEntry.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:id |
| native | aj:id |




## LinkML Source

<details>
```yaml
name: id
domain_of:
- Task
- Actor
- AcceptanceCriterion
- LogEntry
range: string

```
</details></div>