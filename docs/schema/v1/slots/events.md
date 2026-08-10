---
search:
  boost: 5.0
---

# Slot: events 


_Events that trigger this webhook. Free-text strings against no event vocabulary._



<div data-search-exclude markdown="1">



URI: [aj:slot/events](https://github.com/jeffposey/agentjobs/schema/v1/slot/events)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
| Multivalued | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Webhook](../classes/Webhook.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:events |
| native | aj:events |




## LinkML Source

<details>
```yaml
name: events
description: Events that trigger this webhook. Free-text strings against no event
  vocabulary.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Webhook
domain_of:
- Webhook
range: string
required: true
multivalued: true

```
</details></div>