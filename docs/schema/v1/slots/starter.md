---
search:
  boost: 5.0
---

# Slot: starter 


_Primary starter prompt content._



<div data-search-exclude markdown="1">



URI: [aj:slot/starter](https://github.com/jeffposey/agentjobs/schema/v1/slot/starter)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompts](../classes/Prompts.md) | Collection of prompt content for a task |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Prompts](../classes/Prompts.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
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
| self | aj:starter |
| native | aj:starter |




## LinkML Source

<details>
```yaml
name: starter
description: Primary starter prompt content.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Prompts
domain_of:
- Prompts
range: string
required: true

```
</details></div>