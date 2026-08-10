---
search:
  boost: 5.0
---

# Slot: dependencies 


_Task dependencies and relationships._



<div data-search-exclude markdown="1">



URI: [aj:slot/dependencies](https://github.com/jeffposey/agentjobs/schema/v1/slot/dependencies)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Dependency](../classes/Dependency.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:dependencies |
| native | aj:dependencies |




## LinkML Source

<details>
```yaml
name: dependencies
description: Task dependencies and relationships.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Dependency
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>