---
search:
  boost: 5.0
---

# Slot: merged_at 


_When the branch was merged, if applicable._



<div data-search-exclude markdown="1">



URI: [aj:slot/merged_at](https://github.com/jeffposey/agentjobs/schema/v1/slot/merged_at)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Branch](../classes/Branch.md) | Branch lifecycle metadata |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Datetime](../types/Datetime.md) |
| Domain Of | [Branch](../classes/Branch.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Branch](../classes/Branch.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:merged_at |
| native | aj:merged_at |




## LinkML Source

<details>
```yaml
name: merged_at
description: When the branch was merged, if applicable.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Branch
domain_of:
- Branch
range: datetime

```
</details></div>