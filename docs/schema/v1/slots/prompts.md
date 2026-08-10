---
search:
  boost: 5.0
---

# Slot: prompts 


_Prompt collection used by collaborating agents. Deleted in v2 (D1) -- the starter almost always restates the description._



<div data-search-exclude markdown="1">



URI: [aj:slot/prompts](https://github.com/jeffposey/agentjobs/schema/v1/slot/prompts)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Prompts](../classes/Prompts.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
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
| self | aj:prompts |
| native | aj:prompts |




## LinkML Source

<details>
```yaml
name: prompts
description: Prompt collection used by collaborating agents. Deleted in v2 (D1) --
  the starter almost always restates the description.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: Prompts
inlined: true

```
</details></div>