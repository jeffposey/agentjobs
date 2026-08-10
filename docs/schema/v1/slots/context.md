---
search:
  boost: 5.0
---

# Slot: context 


_Additional context regarding the prompt._



<div data-search-exclude markdown="1">



URI: [aj:slot/context](https://github.com/jeffposey/agentjobs/schema/v1/slot/context)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Prompt](../classes/Prompt.md) | Individual prompt entry for a task |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Prompt](../classes/Prompt.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Prompt](../classes/Prompt.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:context |
| native | aj:context |




## LinkML Source

<details>
```yaml
name: context
description: Additional context regarding the prompt.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Prompt
domain_of:
- Prompt
range: string

```
</details></div>