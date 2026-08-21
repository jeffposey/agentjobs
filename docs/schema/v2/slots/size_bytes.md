---
search:
  boost: 5.0
---

# Slot: size_bytes 


_Size of the stored file._



<div data-search-exclude markdown="1">



URI: [aj:slot/size_bytes](https://github.com/jeffposey/agentjobs/schema/v2/slot/size_bytes)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Attachment](../classes/Attachment.md) | One image stored beside the tasks, referenced from the log entry it illustrat... |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Integer](../types/Integer.md) |
| Domain Of | [Attachment](../classes/Attachment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Attachment](../classes/Attachment.md) |


### Value Constraints

| Property | Value |
| --- | --- |
| Minimum Value | 1 |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:size_bytes |
| native | aj:size_bytes |




## LinkML Source

<details>
```yaml
name: size_bytes
description: Size of the stored file.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Attachment
domain_of:
- Attachment
range: integer
required: true
minimum_value: 1

```
</details></div>