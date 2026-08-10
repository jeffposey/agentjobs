---
search:
  boost: 5.0
---

# Slot: resolution 


_Resolution notes when an issue is closed._



<div data-search-exclude markdown="1">



URI: [aj:slot/resolution](https://github.com/jeffposey/agentjobs/schema/v1/slot/resolution)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Issue](../classes/Issue.md) | Issue tracked against the task's lifecycle |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Issue](../classes/Issue.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Issue](../classes/Issue.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:resolution |
| native | aj:resolution |




## LinkML Source

<details>
```yaml
name: resolution
description: Resolution notes when an issue is closed.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Issue
domain_of:
- Issue
range: string

```
</details></div>