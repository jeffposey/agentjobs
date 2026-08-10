---
search:
  boost: 5.0
---

# Slot: kind 


_What kind of party this is. Lives in config only -- never copied into a task file, which is the whole point of D4._



<div data-search-exclude markdown="1">



URI: [aj:slot/kind](https://github.com/jeffposey/agentjobs/schema/v2/slot/kind)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Actor](../classes/Actor.md) | A party that can act on tasks |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [ActorKind](../enums/ActorKind.md) |
| Domain Of | [Actor](../classes/Actor.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Actor](../classes/Actor.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:kind |
| native | aj:kind |




## LinkML Source

<details>
```yaml
name: kind
description: What kind of party this is. Lives in config only -- never copied into
  a task file, which is the whole point of D4.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Actor
domain_of:
- Actor
range: ActorKind
required: true

```
</details></div>