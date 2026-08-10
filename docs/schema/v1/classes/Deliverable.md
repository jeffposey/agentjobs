---
search:
  boost: 10.0
---

# Class: Deliverable 


_Deliverable artifact tracked for task completion._



<div data-search-exclude markdown="1">



URI: [aj:class/Deliverable](https://github.com/jeffposey/agentjobs/schema/v1/class/Deliverable)





```mermaid
 classDiagram
    class Deliverable
    click Deliverable href "../../classes/Deliverable/"
      Deliverable : description
        
      Deliverable : path
        
      Deliverable : status
        
      
```




<!-- no inheritance hierarchy -->

## Slots

| Name | Cardinality and Range | Description | Inheritance |
| ---  | --- | --- | --- |
| [path](../slots/path.md) | 1 <br/> [String](../types/String.md) | Repository-relative path to the deliverable | direct |
| [status](../slots/status.md) | 0..1 <br/> [String](../types/String.md) | Completion state, validated against pending | in_progress | completed but typ... | direct |
| [description](../slots/description.md) | 0..1 <br/> [String](../types/String.md) | Human-readable description of the deliverable | direct |





## Usages

| used by | used in | type | used |
| ---  | --- | --- | --- |
| [Task](../classes/Task.md) | [deliverables](../slots/deliverables.md) | range | [Deliverable](../classes/Deliverable.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v1




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:Deliverable |
| native | aj:Deliverable |






## LinkML Source

<!-- TODO: investigate https://stackoverflow.com/questions/37606292/how-to-create-tabbed-code-blocks-in-mkdocs-or-sphinx -->

### Direct

<details>
```yaml
name: Deliverable
description: Deliverable artifact tracked for task completion.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
attributes:
  path:
    name: path
    description: Repository-relative path to the deliverable.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    rank: 1000
    domain_of:
    - Deliverable
    required: true
  status:
    name: status
    description: Completion state, validated against pending | in_progress | completed
      but typed as a bare str.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    ifabsent: string(pending)
    domain_of:
    - Task
    - Phase
    - SuccessCriterion
    - StatusUpdate
    - Deliverable
    - Dependency
    - Issue
    - Branch
  description:
    name: description
    description: Human-readable description of the deliverable.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    domain_of:
    - Task
    - SuccessCriterion
    - Deliverable

```
</details>

### Induced

<details>
```yaml
name: Deliverable
description: Deliverable artifact tracked for task completion.
from_schema: https://github.com/jeffposey/agentjobs/schema/v1
attributes:
  path:
    name: path
    description: Repository-relative path to the deliverable.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    rank: 1000
    owner: Deliverable
    domain_of:
    - Deliverable
    range: string
    required: true
  status:
    name: status
    description: Completion state, validated against pending | in_progress | completed
      but typed as a bare str.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    ifabsent: string(pending)
    owner: Deliverable
    domain_of:
    - Task
    - Phase
    - SuccessCriterion
    - StatusUpdate
    - Deliverable
    - Dependency
    - Issue
    - Branch
    range: string
  description:
    name: description
    description: Human-readable description of the deliverable.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v1
    owner: Deliverable
    domain_of:
    - Task
    - SuccessCriterion
    - Deliverable
    range: string

```
</details></div>