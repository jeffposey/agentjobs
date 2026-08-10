---
search:
  boost: 5.0
---

# Slot: why 


_What the reader will find there. Required -- a pointer without a reason is the kind of context that decays into noise._



<div data-search-exclude markdown="1">



URI: [aj:slot/why](https://github.com/jeffposey/agentjobs/schema/v2/slot/why)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [ContextPointer](../classes/ContextPointer.md) | A file worth reading before starting, and why it is worth reading |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [ContextPointer](../classes/ContextPointer.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [ContextPointer](../classes/ContextPointer.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:why |
| native | aj:why |




## LinkML Source

<details>
```yaml
name: why
description: What the reader will find there. Required -- a pointer without a reason
  is the kind of context that decays into noise.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: ContextPointer
domain_of:
- ContextPointer
range: string
required: true

```
</details></div>