---
search:
  boost: 5.0
---

# Slot: status 

<div data-search-exclude markdown="1">



URI: [aj:slot/status](https://github.com/jeffposey/agentjobs/schema/v2/slot/status)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [AcceptanceCriterion](../classes/AcceptanceCriterion.md) | One verifiable condition for done |  no  |
| [Deliverable](../classes/Deliverable.md) | An artifact the task produces |  no  |
| [Branch](../classes/Branch.md) | Git branch lifecycle |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [AcceptanceCriterion](../classes/AcceptanceCriterion.md), [Deliverable](../classes/Deliverable.md), [Branch](../classes/Branch.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:status |
| native | aj:status |




## LinkML Source

<details>
```yaml
name: status
domain_of:
- AcceptanceCriterion
- Deliverable
- Branch
range: string

```
</details></div>