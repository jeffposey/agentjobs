---
search:
  boost: 5.0
---

# Slot: type 

<div data-search-exclude markdown="1">



URI: [aj:slot/type](https://github.com/jeffposey/agentjobs/schema/v2/slot/type)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Dependency](../classes/Dependency.md) | A relationship to another task |  no  |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Dependency](../classes/Dependency.md), [LogEntry](../classes/LogEntry.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:type |
| native | aj:type |




## LinkML Source

<details>
```yaml
name: type
domain_of:
- Dependency
- LogEntry
range: string

```
</details></div>