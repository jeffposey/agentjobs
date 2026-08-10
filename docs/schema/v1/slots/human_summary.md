---
search:
  boost: 5.0
---

# Slot: human_summary 


_Concise 1-2 sentence summary for human reviewers. Splits the audience by length rather than by content, so it duplicates the description's opening._



<div data-search-exclude markdown="1">



URI: [aj:slot/human_summary](https://github.com/jeffposey/agentjobs/schema/v1/slot/human_summary)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
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
| self | aj:human_summary |
| native | aj:human_summary |




## LinkML Source

<details>
```yaml
name: human_summary
description: Concise 1-2 sentence summary for human reviewers. Splits the audience
  by length rather than by content, so it duplicates the description's opening.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Task
domain_of:
- Task
range: string

```
</details></div>