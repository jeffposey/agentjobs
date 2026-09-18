---
search:
  boost: 5.0
---

# Slot: attachments 


_Images referenced from this entry. The blob is stored in the database, content-addressed, and written beside the task file by an export; only the metadata is in the record, so an exported task file stays readable._



<div data-search-exclude markdown="1">



URI: [aj:slot/attachments](https://github.com/jeffposey/agentjobs/schema/v2/slot/attachments)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | One immutable event in the task's history (section 4) |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Attachment](../classes/Attachment.md) |
| Domain Of | [LogEntry](../classes/LogEntry.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [LogEntry](../classes/LogEntry.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:attachments |
| native | aj:attachments |




## LinkML Source

<details>
```yaml
name: attachments
description: Images referenced from this entry. The blob is stored in the database,
  content-addressed, and written beside the task file by an export; only the metadata
  is in the record, so an exported task file stays readable.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: LogEntry
domain_of:
- LogEntry
range: Attachment
multivalued: true
inlined: true
inlined_as_list: true

```
</details></div>