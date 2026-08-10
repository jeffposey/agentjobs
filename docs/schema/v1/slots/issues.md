---
search:
  boost: 5.0
---

# Slot: issues 


_Issues encountered while executing the task. Empty in every file in the corpus; deleted in v2 (D1)._



<div data-search-exclude markdown="1">



URI: [aj:slot/issues](https://github.com/jeffposey/agentjobs/schema/v1/slot/issues)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Issue](../classes/Issue.md) |
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
| self | aj:issues |
| native | aj:issues |




## LinkML Source

<details>
```yaml
name: issues
description: Issues encountered while executing the task. Empty in every file in the
  corpus; deleted in v2 (D1).
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Issue
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>