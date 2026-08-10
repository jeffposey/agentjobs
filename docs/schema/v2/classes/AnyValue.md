---
search:
  boost: 10.0
---

# Class: AnyValue 


_An untyped structured value. LinkML's escape hatch, used only for LogEntry.data where the payload shape depends on the entry type._



<div data-search-exclude markdown="1">



URI: [linkml:Any](https://w3id.org/linkml/Any)





```mermaid
 classDiagram
    class AnyValue
    click AnyValue href "../../classes/AnyValue/"
      
```




<!-- no inheritance hierarchy -->

## Class Properties

| Property | Value |
| --- | --- |
| Class URI | [linkml:Any](https://w3id.org/linkml/Any) |


## Slots

| Name | Cardinality and Range | Description | Inheritance |
| ---  | --- | --- | --- |





## Usages

| used by | used in | type | used |
| ---  | --- | --- | --- |
| [LogEntry](../classes/LogEntry.md) | [data](../slots/data.md) | range | [AnyValue](../classes/AnyValue.md) |












## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2




## Mappings

| Mapping Type | Mapped Value |
| ---  | ---  |
| self | linkml:Any |
| native | aj:AnyValue |






## LinkML Source

<!-- TODO: investigate https://stackoverflow.com/questions/37606292/how-to-create-tabbed-code-blocks-in-mkdocs-or-sphinx -->

### Direct

<details>
```yaml
name: AnyValue
description: An untyped structured value. LinkML's escape hatch, used only for LogEntry.data
  where the payload shape depends on the entry type.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
class_uri: linkml:Any

```
</details>

### Induced

<details>
```yaml
name: AnyValue
description: An untyped structured value. LinkML's escape hatch, used only for LogEntry.data
  where the payload shape depends on the entry type.
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
class_uri: linkml:Any

```
</details></div>