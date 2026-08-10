---
search:
  boost: 2.0
---


# Enum: AcceptanceStatus 




_Whether a criterion is verified. Deliberately distinct from DeliverableStatus: a criterion is *verified*, a deliverable is *produced* (section 3)._



<div data-search-exclude markdown="1">

URI: [aj:enum/AcceptanceStatus](https://github.com/jeffposey/agentjobs/schema/v2/enum/AcceptanceStatus)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| pending | None |  |
| met | None |  |
| failed | None |  |
| dropped | None |  |




## Slots

| Name | Description |
| ---  | --- |
| [status](../slots/status.md) |  |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: AcceptanceStatus
description: 'Whether a criterion is verified. Deliberately distinct from DeliverableStatus:
  a criterion is *verified*, a deliverable is *produced* (section 3).'
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  pending:
    text: pending
  met:
    text: met
  failed:
    text: failed
  dropped:
    text: dropped

```
</details>

</div>