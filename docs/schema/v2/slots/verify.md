---
search:
  boost: 5.0
---

# Slot: verify 


_Optional machine-checkable hint -- a command that demonstrates the criterion. Advisory, not executed automatically._



<div data-search-exclude markdown="1">



URI: [aj:slot/verify](https://github.com/jeffposey/agentjobs/schema/v2/slot/verify)
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
| self | aj:verify |
| native | aj:verify |




## LinkML Source

<details>
```yaml
name: verify
description: Optional machine-checkable hint -- a command that demonstrates the criterion.
  Advisory, not executed automatically.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: AcceptanceCriterion
domain_of:
- AcceptanceCriterion
range: string

```
</details></div>