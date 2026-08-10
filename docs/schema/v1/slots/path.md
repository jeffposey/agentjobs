---
search:
  boost: 5.0
---

# Slot: path 


_Repository-relative path to the deliverable._



<div data-search-exclude markdown="1">



URI: [aj:slot/path](https://github.com/jeffposey/agentjobs/schema/v1/slot/path)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Deliverable](../classes/Deliverable.md) | Deliverable artifact tracked for task completion |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Deliverable](../classes/Deliverable.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Deliverable](../classes/Deliverable.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:path |
| native | aj:path |




## LinkML Source

<details>
```yaml
name: path
description: Repository-relative path to the deliverable.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Deliverable
domain_of:
- Deliverable
range: string
required: true

```
</details></div>