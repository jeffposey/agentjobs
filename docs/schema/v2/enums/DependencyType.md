---
search:
  boost: 2.0
---


# Enum: DependencyType 




_Relationship to another task. `depends_on` renamed to `needs`; the other two are unchanged._



<div data-search-exclude markdown="1">

URI: [aj:enum/DependencyType](https://github.com/jeffposey/agentjobs/schema/v2/enum/DependencyType)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| needs | None |  |
| blocks | None |  |
| related | None |  |




## Slots

| Name | Description |
| ---  | --- |
| [type](../slots/type.md) |  |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: DependencyType
description: Relationship to another task. `depends_on` renamed to `needs`; the other
  two are unchanged.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  needs:
    text: needs
  blocks:
    text: blocks
  related:
    text: related

```
</details>

</div>