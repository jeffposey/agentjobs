---
search:
  boost: 10.0
---

# Class: AcceptanceCriterion 


_One verifiable condition for done. Replaces SuccessCriterion, and carries two optional fields describing how it is verified -- which are not the same kind of thing. `verify` is prose for a person and nothing executes it; `check` is an argv list this machine runs, and its exit code decides the criterion._



<div data-search-exclude markdown="1">



URI: [aj:class/AcceptanceCriterion](https://github.com/jeffposey/agentjobs/schema/v2/class/AcceptanceCriterion)





```mermaid
 classDiagram
    class AcceptanceCriterion
    click AcceptanceCriterion href "../../classes/AcceptanceCriterion/"
      AcceptanceCriterion : check
        
      AcceptanceCriterion : id
        
      AcceptanceCriterion : status
        
          
    
        
        
        AcceptanceCriterion --> "0..1" AcceptanceStatus : status
        click AcceptanceStatus href "../../enums/AcceptanceStatus/"
    

        
      AcceptanceCriterion : text
        
      AcceptanceCriterion : verify
        
      
```




<!-- no inheritance hierarchy -->

## Slots

| Name | Cardinality and Range | Description | Inheritance |
| ---  | --- | --- | --- |
| [id](../slots/id.md) | 1 <br/> [String](../types/String.md) | Criterion identifier, scoped to the task (e | direct |
| [text](../slots/text.md) | 1 <br/> [String](../types/String.md) | The condition, stated so it can be judged true or false | direct |
| [verify](../slots/verify.md) | 0..1 <br/> [String](../types/String.md) | Optional prose for a person: how somebody would satisfy themselves this crite... | direct |
| [check](../slots/check.md) | * <br/> [String](../types/String.md) | Optional argv list whose exit code decides this criterion: 0 is met, anything... | direct |
| [status](../slots/status.md) | 0..1 <br/> [AcceptanceStatus](../enums/AcceptanceStatus.md) |  | direct |





## Usages

| used by | used in | type | used |
| ---  | --- | --- | --- |
| [Task](../classes/Task.md) | [acceptance](../slots/acceptance.md) | range | [AcceptanceCriterion](../classes/AcceptanceCriterion.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:AcceptanceCriterion |
| native | aj:AcceptanceCriterion |






## LinkML Source

<!-- TODO: investigate https://stackoverflow.com/questions/37606292/how-to-create-tabbed-code-blocks-in-mkdocs-or-sphinx -->

### Direct

<details>
```yaml
name: AcceptanceCriterion
description: One verifiable condition for done. Replaces SuccessCriterion, and carries
  two optional fields describing how it is verified -- which are not the same kind
  of thing. `verify` is prose for a person and nothing executes it; `check` is an
  argv list this machine runs, and its exit code decides the criterion.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
attributes:
  id:
    name: id
    description: Criterion identifier, scoped to the task (e.g. ac-1).
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    domain_of:
    - Task
    - Actor
    - AcceptanceCriterion
    - LogEntry
    required: true
  text:
    name: text
    description: The condition, stated so it can be judged true or false.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    domain_of:
    - AcceptanceCriterion
    required: true
  verify:
    name: verify
    description: 'Optional prose for a person: how somebody would satisfy themselves
      this criterion holds. Never executed -- an executable check is `check`.'
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    domain_of:
    - AcceptanceCriterion
  check:
    name: check
    description: 'Optional argv list whose exit code decides this criterion: 0 is
      met, anything else is failed, including a timeout and a command that cannot
      be started (task-147). A list, never a string -- nothing splits it and no shell
      sees it. Changing it resets `status` to pending on every write path, because
      a status is a claim about a check having been run.'
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    domain_of:
    - AcceptanceCriterion
    range: string
    multivalued: true
  status:
    name: status
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    ifabsent: string(pending)
    domain_of:
    - AcceptanceCriterion
    - Deliverable
    - Branch
    range: AcceptanceStatus

```
</details>

### Induced

<details>
```yaml
name: AcceptanceCriterion
description: One verifiable condition for done. Replaces SuccessCriterion, and carries
  two optional fields describing how it is verified -- which are not the same kind
  of thing. `verify` is prose for a person and nothing executes it; `check` is an
  argv list this machine runs, and its exit code decides the criterion.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
attributes:
  id:
    name: id
    description: Criterion identifier, scoped to the task (e.g. ac-1).
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    owner: AcceptanceCriterion
    domain_of:
    - Task
    - Actor
    - AcceptanceCriterion
    - LogEntry
    range: string
    required: true
  text:
    name: text
    description: The condition, stated so it can be judged true or false.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    owner: AcceptanceCriterion
    domain_of:
    - AcceptanceCriterion
    range: string
    required: true
  verify:
    name: verify
    description: 'Optional prose for a person: how somebody would satisfy themselves
      this criterion holds. Never executed -- an executable check is `check`.'
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    owner: AcceptanceCriterion
    domain_of:
    - AcceptanceCriterion
    range: string
  check:
    name: check
    description: 'Optional argv list whose exit code decides this criterion: 0 is
      met, anything else is failed, including a timeout and a command that cannot
      be started (task-147). A list, never a string -- nothing splits it and no shell
      sees it. Changing it resets `status` to pending on every write path, because
      a status is a claim about a check having been run.'
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    owner: AcceptanceCriterion
    domain_of:
    - AcceptanceCriterion
    range: string
    multivalued: true
  status:
    name: status
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    ifabsent: string(pending)
    owner: AcceptanceCriterion
    domain_of:
    - AcceptanceCriterion
    - Deliverable
    - Branch
    range: AcceptanceStatus

```
</details></div>