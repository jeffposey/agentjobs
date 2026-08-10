---
search:
  boost: 5.0
---

# Slot: secret 


_Secret for HMAC signature verification._



<div data-search-exclude markdown="1">



URI: [aj:slot/secret](https://github.com/jeffposey/agentjobs/schema/v1/slot/secret)
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
| self | aj:secret |
| native | aj:secret |




## LinkML Source

<details>
```yaml
name: secret
description: Secret for HMAC signature verification.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
rank: 1000
owner: Webhook
domain_of:
- Webhook
range: string
required: true

```
</details></div>