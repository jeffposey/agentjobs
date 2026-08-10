---
search:
  boost: 5.0
---

# Slot: ball 


_Who acts next. Required while open; absent or null when closed. This is the field that makes limbo unrepresentable._



<div data-search-exclude markdown="1">



URI: [aj:slot/ball](https://github.com/jeffposey/agentjobs/schema/v2/slot/ball)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Ball](../enums/Ball.md) |
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


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:ball |
| native | aj:ball |




## LinkML Source

<details>
```yaml
name: ball
description: Who acts next. Required while open; absent or null when closed. This
  is the field that makes limbo unrepresentable.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: Ball

```
</details></div>