---
search:
  boost: 5.0
---

# Slot: label 


_Accessible label; alt text wherever it renders._



<div data-search-exclude markdown="1">



URI: [aj:slot/label](https://github.com/jeffposey/agentjobs/schema/v2/slot/label)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Attachment](../classes/Attachment.md) | One image stored beside the tasks, referenced from the log entry it illustrat... |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Attachment](../classes/Attachment.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Attachment](../classes/Attachment.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:label |
| native | aj:label |




## LinkML Source

<details>
```yaml
name: label
description: Accessible label; alt text wherever it renders.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Attachment
domain_of:
- Attachment
range: string
required: true

```
</details></div>