---
search:
  boost: 2.0
---


# Enum: Ball 




_Who acts next. Required on every open task, null only when closed (tenet 2). An open task with nobody responsible is not representable._



<div data-search-exclude markdown="1">

URI: [aj:enum/Ball](https://github.com/jeffposey/agentjobs/schema/v2/enum/Ball)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| agent | None |  |
| human | None |  |
| external | None |  |




## Slots

| Name | Description |
| ---  | --- |
| [ball](../slots/ball.md) | Who acts next |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: Ball
description: Who acts next. Required on every open task, null only when closed (tenet
  2). An open task with nobody responsible is not representable.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  agent:
    text: agent
  human:
    text: human
  external:
    text: external

```
</details>

</div>