---
search:
  boost: 5.0
---

# Slot: rel 


_What this link is._



<div data-search-exclude markdown="1">



URI: [aj:slot/rel](https://github.com/jeffposey/agentjobs/schema/v2/slot/rel)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Link](../classes/Link.md) | An external reference, with its kind made explicit |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [LinkRel](../enums/LinkRel.md) |
| Domain Of | [Link](../classes/Link.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| If Absent | `string(other)` |
| Owner | [Link](../classes/Link.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:rel |
| native | aj:rel |




## LinkML Source

<details>
```yaml
name: rel
description: What this link is.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
ifabsent: string(other)
owner: Link
domain_of:
- Link
range: LinkRel

```
</details></div>