---
search:
  boost: 5.0
---

# Slot: note 

<div data-search-exclude markdown="1">



URI: [aj:slot/note](https://github.com/jeffposey/agentjobs/schema/v2/slot/note)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Deliverable](../classes/Deliverable.md) | An artifact the task produces |  no  |
| [Dependency](../classes/Dependency.md) | A relationship to another task |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Deliverable](../classes/Deliverable.md), [Dependency](../classes/Dependency.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:note |
| native | aj:note |




## LinkML Source

<details>
```yaml
name: note
domain_of:
- Deliverable
- Dependency
range: string

```
</details></div>