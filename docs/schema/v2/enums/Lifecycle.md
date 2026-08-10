---
search:
  boost: 2.0
---


# Enum: Lifecycle 




_Where the task is in its life. Strictly ordered and closed (section 3). Answers only "where in its life" -- not who acts next, and not why._



<div data-search-exclude markdown="1">

URI: [aj:enum/Lifecycle](https://github.com/jeffposey/agentjobs/schema/v2/enum/Lifecycle)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| draft | None | Being specified |
| ready | None | Spec complete, claimable by any eligible agent |
| active | None | Claimed, work underway, in whoever's court `ball` says |
| closed | None | Over |




## Slots

| Name | Description |
| ---  | --- |
| [lifecycle](../slots/lifecycle.md) | Where the task is in its life |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: Lifecycle
description: Where the task is in its life. Strictly ordered and closed (section 3).
  Answers only "where in its life" -- not who acts next, and not why.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  draft:
    text: draft
    description: Being specified. Not claimable.
  ready:
    text: ready
    description: Spec complete, claimable by any eligible agent. A ready task with
      unmet `needs` dependencies stays ready -- its blockedness is derived from the
      store, not restated as state -- but is excluded from /next and refuses claim.
  active:
    text: active
    description: Claimed, work underway, in whoever's court `ball` says.
  closed:
    text: closed
    description: Over. Carries an `outcome`. Visibility is the separate `archived`
      flag.

```
</details>

</div>