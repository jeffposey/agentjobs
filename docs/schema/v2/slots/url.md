---
search:
  boost: 5.0
---

# Slot: url 


_Target URL. Actually validated as a URI, unlike v1._



<div data-search-exclude markdown="1">



URI: [aj:slot/url](https://github.com/jeffposey/agentjobs/schema/v2/slot/url)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Link](../classes/Link.md) | An external reference, with its kind made explicit |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Uri](../types/Uri.md) |
| Domain Of | [Link](../classes/Link.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Link](../classes/Link.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:url |
| native | aj:url |




## LinkML Source

<details>
```yaml
name: url
description: Target URL. Actually validated as a URI, unlike v1.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Link
domain_of:
- Link
range: uri
required: true

```
</details></div>