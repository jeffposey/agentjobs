---
search:
  boost: 5.0
---

# Slot: tags 


_Also validated against the config vocabulary at save._



<div data-search-exclude markdown="1">



URI: [aj:slot/tags](https://github.com/jeffposey/agentjobs/schema/v2/slot/tags)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Task](../classes/Task.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:tags |
| native | aj:tags |




## LinkML Source

<details>
```yaml
name: tags
description: Also validated against the config vocabulary at save.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: string
multivalued: true

```
</details></div>