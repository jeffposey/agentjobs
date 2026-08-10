---
search:
  boost: 5.0
---

# Slot: active 


_Whether this webhook is active._



<div data-search-exclude markdown="1">



URI: [aj:slot/active](https://github.com/jeffposey/agentjobs/schema/v1/slot/active)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Boolean](../types/Boolean.md) |
| Domain Of | [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
### Slot Characteristics

| Property | Value |
| --- | --- |
| If Absent | `true` |
| Owner | [Webhook](../classes/Webhook.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:active |
| native | aj:active |




## LinkML Source

<details>
```yaml
name: active
description: Whether this webhook is active.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
ifabsent: 'true'
owner: Webhook
domain_of:
- Webhook
range: boolean

```
</details></div>