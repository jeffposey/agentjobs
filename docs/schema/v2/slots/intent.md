---
search:
  boost: 5.0
---

# Slot: intent 


_WHY this task exists. Markdown._



<div data-search-exclude markdown="1">



URI: [aj:slot/intent](https://github.com/jeffposey/agentjobs/schema/v2/slot/intent)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Spec](../classes/Spec.md) | The working specification, split along the questions agents actually ask |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Spec](../classes/Spec.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Spec](../classes/Spec.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:intent |
| native | aj:intent |




## LinkML Source

<details>
```yaml
name: intent
description: WHY this task exists. Markdown.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Spec
domain_of:
- Spec
range: string
required: true

```
</details></div>