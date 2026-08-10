---
search:
  boost: 5.0
---

# Slot: url 

<div data-search-exclude markdown="1">



URI: [aj:slot/url](https://github.com/jeffposey/agentjobs/schema/v1/slot/url)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [ExternalLink](../classes/ExternalLink.md) | Reference to a relevant external resource |  no  |
| [Webhook](../classes/Webhook.md) | Webhook configuration for task event notifications |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [String](../types/String.md) |
| Domain Of | [ExternalLink](../classes/ExternalLink.md), [Webhook](../classes/Webhook.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |










## Identifier and Mapping Information






## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:url |
| native | aj:url |




## LinkML Source

<details>
```yaml
name: url
domain_of:
- ExternalLink
- Webhook
range: string

```
</details></div>