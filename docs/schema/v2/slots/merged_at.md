---
search:
  boost: 5.0
---

# Slot: merged_at 


_When the branch was merged, if it was._



<div data-search-exclude markdown="1">



URI: [aj:slot/merged_at](https://github.com/jeffposey/agentjobs/schema/v2/slot/merged_at)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Branch](../classes/Branch.md) | Git branch lifecycle |  no  |






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


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:merged_at |
| native | aj:merged_at |




## LinkML Source

<details>
```yaml
name: merged_at
description: When the branch was merged, if it was.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Branch
domain_of:
- Branch
range: datetime

```
</details></div>