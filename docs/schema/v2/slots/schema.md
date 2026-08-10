---
search:
  boost: 5.0
---

# Slot: schema 


_Schema version stamp. A missing `schema` field means v1, which the v2 loader refuses loudly rather than guessing. Breaking changes bump this integer and ship a converter; additive changes do not bump it. NOTE for task-050: as a Pydantic field name, `schema` shadows a deprecated BaseModel attribute and will emit a shadow warning -- needs an alias or a model_config allowance._



<div data-search-exclude markdown="1">



URI: [aj:slot/schema](https://github.com/jeffposey/agentjobs/schema/v2/slot/schema)
<!-- no inheritance hierarchy -->





## Applicable Classes

| Name | Description | Modifies Slot |
| --- | --- | --- |
| [Task](../classes/Task.md) | A unit of work |  no  |






## Properties

### Type and Range

| Property | Value |
| --- | --- |
| Range | [Integer](../types/Integer.md) |
| Domain Of | [Task](../classes/Task.md) |

### Cardinality and Requirements

| Property | Value |
| --- | --- |
| Required | Yes |
### Slot Characteristics

| Property | Value |
| --- | --- |
| Owner | [Task](../classes/Task.md) |


<details>
<summary>Additional Constraints</summary>
**Must Equal:** 2

</details>











## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:schema |
| native | aj:schema |




## LinkML Source

<details>
```yaml
name: schema
description: 'Schema version stamp. A missing `schema` field means v1, which the v2
  loader refuses loudly rather than guessing. Breaking changes bump this integer and
  ship a converter; additive changes do not bump it. NOTE for task-050: as a Pydantic
  field name, `schema` shadows a deprecated BaseModel attribute and will emit a shadow
  warning -- needs an alias or a model_config allowance.'
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
owner: Task
domain_of:
- Task
range: integer
required: true
equals_number: 2

```
</details></div>