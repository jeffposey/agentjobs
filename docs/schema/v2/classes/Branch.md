---
search:
  boost: 10.0
---

# Class: Branch 


_Git branch lifecycle. Carried over from v1 unchanged._



<div data-search-exclude markdown="1">



URI: [aj:class/Branch](https://github.com/jeffposey/agentjobs/schema/v2/class/Branch)





```mermaid
 classDiagram
    class Branch
    click Branch href "../../classes/Branch/"
      Branch : merged_at
        
      Branch : name
        
      Branch : status
        
          
    
        
        
        Branch --> "0..1" BranchStatus : status
        click BranchStatus href "../../enums/BranchStatus/"
    

        
      
```




<!-- no inheritance hierarchy -->

## Slots

| Name | Cardinality and Range | Description | Inheritance |
| ---  | --- | --- | --- |
| [name](../slots/name.md) | 1 <br/> [String](../types/String.md) | Git branch name | direct |
| [status](../slots/status.md) | 0..1 <br/> [BranchStatus](../enums/BranchStatus.md) |  | direct |
| [merged_at](../slots/merged_at.md) | 0..1 <br/> [Datetime](../types/Datetime.md) | When the branch was merged, if it was | direct |





## Usages

| used by | used in | type | used |
| ---  | --- | --- | --- |
| [Task](../classes/Task.md) | [branches](../slots/branches.md) | range | [Branch](../classes/Branch.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | aj:Branch |
| native | aj:Branch |






## LinkML Source

<!-- TODO: investigate https://stackoverflow.com/questions/37606292/how-to-create-tabbed-code-blocks-in-mkdocs-or-sphinx -->

### Direct

<details>
```yaml
name: Branch
description: Git branch lifecycle. Carried over from v1 unchanged.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
attributes:
  name:
    name: name
    description: Git branch name.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    domain_of:
    - Branch
    required: true
  status:
    name: status
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    ifabsent: string(active)
    domain_of:
    - AcceptanceCriterion
    - Deliverable
    - Branch
    range: BranchStatus
  merged_at:
    name: merged_at
    description: When the branch was merged, if it was.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    domain_of:
    - Branch
    range: datetime

```
</details>

### Induced

<details>
```yaml
name: Branch
description: Git branch lifecycle. Carried over from v1 unchanged.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
attributes:
  name:
    name: name
    description: Git branch name.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    owner: Branch
    domain_of:
    - Branch
    range: string
    required: true
  status:
    name: status
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    ifabsent: string(active)
    owner: Branch
    domain_of:
    - AcceptanceCriterion
    - Deliverable
    - Branch
    range: BranchStatus
  merged_at:
    name: merged_at
    description: When the branch was merged, if it was.
    from_schema: https://github.com/jeffposey/agentjobs/schema/v2
    rank: 1000
    owner: Branch
    domain_of:
    - Branch
    range: datetime

```
</details></div>