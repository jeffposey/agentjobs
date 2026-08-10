---
search:
  boost: 5.0
---

# Slot: title 

<div data-search-exclude markdown="1">



URI: [aj:slot/title](https://github.com/jeffposey/agentjobs/schema/v2/slot/title)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |
| [Link](../classes/Link.md) | An external reference, with its kind made explicit |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Link](../classes/Link.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:title |
| native | aj:title |




## LinkML Source

<details>
```yaml
name: title
domain_of:
- Task
- Link
range: string

```
</details></div>