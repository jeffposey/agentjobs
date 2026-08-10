---
search:
  boost: 5.0
---

# Slot: links 


_External references. Renamed from external_links; URL now validated._



<div data-search-exclude markdown="1">



URI: [aj:slot/links](https://github.com/jeffposey/agentjobs/schema/v2/slot/links)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Link](../classes/Link.md) |
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
| self | aj:links |
| native | aj:links |




## LinkML Source

<details>
```yaml
name: links
description: External references. Renamed from external_links; URL now validated.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Link
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>