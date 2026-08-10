---
search:
  boost: 5.0
---

# Slot: title 

<div data-search-exclude markdown="1">



URI: [aj:slot/title](https://github.com/jeffposey/agentjobs/schema/v1/slot/title)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | Primary task representation |  no  |
| [Phase](../classes/Phase.md) | Discrete phase within a task roadmap |  no  |
| [ExternalLink](../classes/ExternalLink.md) | Reference to a relevant external resource |  no  |
| [Issue](../classes/Issue.md) | Issue tracked against the task's lifecycle |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Task](../classes/Task.md), [Phase](../classes/Phase.md), [ExternalLink](../classes/ExternalLink.md), [Issue](../classes/Issue.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:title |
| native | aj:title |




## LinkML Source

<details>
```yaml
name: title
domain_of:
- Task
- Phase
- ExternalLink
- Issue
range: string

```
</details></div>