---
search:
  boost: 5.0
---

# Slot: name 


_Git branch name associated with the task._



<div data-search-exclude markdown="1">



URI: [aj:slot/name](https://github.com/jeffposey/agentjobs/schema/v1/slot/name)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Branch](../classes/Branch.md) | Branch lifecycle metadata |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Branch](../classes/Branch.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
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
| self | aj:name |
| native | aj:name |




## LinkML Source

<details>
```yaml
name: name
description: Git branch name associated with the task.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Branch
domain_of:
- Branch
range: string
required: true

```
</details></div>