---
search:
  boost: 5.0
---

# Slot: eligible 


_Who may claim this task. An empty list means anyone. Authoring-time intent, never mutated by claiming._



<div data-search-exclude markdown="1">



URI: [aj:slot/eligible](https://github.com/jeffposey/agentjobs/schema/v2/slot/eligible)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Assignment](../classes/Assignment.md) | Separates live ownership from authoring-time eligibility -- v1 conflated both... |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Assignment](../classes/Assignment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Assignment](../classes/Assignment.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:eligible |
| native | aj:eligible |




## LinkML Source

<details>
```yaml
name: eligible
description: Who may claim this task. An empty list means anyone. Authoring-time intent,
  never mutated by claiming.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Assignment
domain_of:
- Assignment
range: string
multivalued: true

```
</details></div>