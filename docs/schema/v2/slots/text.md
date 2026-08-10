---
search:
  boost: 5.0
---

# Slot: text 


_The condition, stated so it can be judged true or false._



<div data-search-exclude markdown="1">



URI: [aj:slot/text](https://github.com/jeffposey/agentjobs/schema/v2/slot/text)
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
| Required | Yes |
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
| self | aj:text |
| native | aj:text |




## LinkML Source

<details>
```yaml
name: text
description: The condition, stated so it can be judged true or false.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: AcceptanceCriterion
domain_of:
- AcceptanceCriterion
range: string
required: true

```
</details></div>