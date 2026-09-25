---
search:
  boost: 5.0
---

# Slot: check 


_Optional argv list whose exit code decides this criterion: 0 is met, anything else is failed, including a timeout and a command that cannot be started (task-147). A list, never a string -- nothing splits it and no shell sees it. Changing it resets `status` to pending on every write path, because a status is a claim about a check having been run._



<div data-search-exclude markdown="1">



URI: [aj:slot/check](https://github.com/jeffposey/agentjobs/schema/v2/slot/check)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [AcceptanceCriterion](../classes/AcceptanceCriterion.md) | One verifiable condition for done |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [AcceptanceCriterion](../classes/AcceptanceCriterion.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [AcceptanceCriterion](../classes/AcceptanceCriterion.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:check |
| native | aj:check |




## LinkML Source

<details>
```yaml
name: check
description: 'Optional argv list whose exit code decides this criterion: 0 is met,
  anything else is failed, including a timeout and a command that cannot be started
  (task-147). A list, never a string -- nothing splits it and no shell sees it. Changing
  it resets `status` to pending on every write path, because a status is a claim about
  a check having been run.'
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: AcceptanceCriterion
domain_of:
- AcceptanceCriterion
range: string
multivalued: true

```
</details></div>