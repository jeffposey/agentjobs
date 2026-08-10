---
search:
  boost: 5.0
---

# Slot: followups 


_Subsequent prompts appended during task progression. A third append-only authored list, alongside status_updates and comments._



<div data-search-exclude markdown="1">



URI: [aj:slot/followups](https://github.com/jeffposey/agentjobs/schema/v1/slot/followups)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompts](../classes/Prompts.md) | Collection of prompt content for a task |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Prompt](../classes/Prompt.md) |
| Domain Of | [Prompts](../classes/Prompts.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Prompts](../classes/Prompts.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:followups |
| native | aj:followups |




## LinkML Source

<details>
```yaml
name: followups
description: Subsequent prompts appended during task progression. A third append-only
  authored list, alongside status_updates and comments.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Prompts
domain_of:
- Prompts
range: Prompt
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>