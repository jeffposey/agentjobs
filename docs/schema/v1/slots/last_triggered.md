---
search:
  boost: 5.0
---

# Slot: last_triggered 


_Last successful trigger. Written by record_trigger(), which raises NameError for the same missing timezone import (see task-047)._



<div data-search-exclude markdown="1">



URI: [aj:slot/last_triggered](https://github.com/jeffposey/agentjobs/schema/v1/slot/last_triggered)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Datetime](../types/Datetime.md) |
| Domain Of | [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
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
| self | aj:last_triggered |
| native | aj:last_triggered |




## LinkML Source

<details>
```yaml
name: last_triggered
description: Last successful trigger. Written by record_trigger(), which raises NameError
  for the same missing timezone import (see task-047).
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Webhook
domain_of:
- Webhook
range: datetime

```
</details></div>