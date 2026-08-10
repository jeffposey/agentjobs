---
search:
  boost: 5.0
---

# Slot: external_links 


_External references for the task._



<div data-search-exclude markdown="1">



URI: [aj:slot/external_links](https://github.com/jeffposey/agentjobs/schema/v1/slot/external_links)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [ExternalLink](../classes/ExternalLink.md) |
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
| self | aj:external_links |
| native | aj:external_links |




## LinkML Source

<details>
```yaml
name: external_links
description: External references for the task.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: ExternalLink
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>