---
search:
  boost: 2.0
---


# Enum: ActorKind 




_What kind of party acted._



<div data-search-exclude markdown="1">

URI: [aj:enum/ActorKind](https://github.com/jeffposey/agentjobs/schema/v2/enum/ActorKind)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| agent | None |  |
| human | None |  |
| system | None | The manager itself, for automatically appended transitions |




## Slots

| Name | Description |
| ---  | --- |
| [kind](../slots/kind.md) | What kind of party this is |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: ActorKind
description: What kind of party acted.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  agent:
    text: agent
  human:
    text: human
  system:
    text: system
    description: The manager itself, for automatically appended transitions.

```
</details>

</div>