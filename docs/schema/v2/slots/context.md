---
search:
  boost: 5.0
---

# Slot: context 


_Curated read-this-first pointers, each with a reason._



<div data-search-exclude markdown="1">



URI: [aj:slot/context](https://github.com/jeffposey/agentjobs/schema/v2/slot/context)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Spec](../classes/Spec.md) | The working specification, split along the questions agents actually ask |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [ContextPointer](../classes/ContextPointer.md) |
| Domain Of | [Spec](../classes/Spec.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Spec](../classes/Spec.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:context |
| native | aj:context |




## LinkML Source

<details>
```yaml
name: context
description: Curated read-this-first pointers, each with a reason.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Spec
domain_of:
- Spec
range: ContextPointer
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>