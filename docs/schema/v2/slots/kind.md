---
search:
  boost: 5.0
---

# Slot: kind 

<div data-search-exclude markdown="1">



URI: [aj:slot/kind](https://github.com/jeffposey/agentjobs/schema/v2/slot/kind)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work, stored as one record in the project's database and exported a... |  no  |
| [Actor](../classes/Actor.md) | A party that can act on tasks |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Actor](../classes/Actor.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:kind |
| native | aj:kind |




## LinkML Source

<details>
```yaml
name: kind
domain_of:
- Task
- Actor
range: string

```
</details></div>