---
search:
  boost: 5.0
---

# Slot: timestamp 

<div data-search-exclude markdown="1">



URI: [aj:slot/timestamp](https://github.com/jeffposey/agentjobs/schema/v1/slot/timestamp)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompt](../classes/Prompt.md) | Individual prompt entry for a task |  no  |
| [StatusUpdate](../classes/StatusUpdate.md) | Chronological status update authored during task execution |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Prompt](../classes/Prompt.md), [StatusUpdate](../classes/StatusUpdate.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:timestamp |
| native | aj:timestamp |




## LinkML Source

<details>
```yaml
name: timestamp
domain_of:
- Prompt
- StatusUpdate
range: string

```
</details></div>