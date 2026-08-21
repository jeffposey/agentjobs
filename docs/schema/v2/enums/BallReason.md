---
search:
  boost: 2.0
---


# Enum: BallReason 




_Why the ball holder holds it. Closed vocabulary, scoped to the holder -- the scoping is enforced by the class-level rules on Task (section 3)._



<div data-search-exclude markdown="1">

URI: [aj:enum/BallReason](https://github.com/jeffposey/agentjobs/schema/v2/enum/BallReason)

## Permissible Values
| Value | Meaning | Description |
| --- | --- | --- |
| available | None | agent: ready and unclaimed -- any eligible agent may take it |
| work | None | agent: claimed and executing |
| revise | None | agent: review came back with changes requested |
| answer | None | agent: the human supplied what the agent was waiting for -- an answer, a deci... |
| redirect | None | agent: the instructions changed |
| hold | None | agent: stopped by a human, with the release condition stated in ball_prompt |
| spec | None | human: the spec needs human completion or refinement |
| review | None | human: work product needs review (v1's under_review) |
| decision | None | human: a choice is blocking progress |
| approval | None | human: a gate -- merge, spend, publish |
| input | None | human: missing information only a human has |
| dependency | None | external: a claimed task blocked on another task (v1's blocked) |
| service | None | external: blocked on a third party, outage, or provisioning |




## Slots

| Name | Description |
| ---  | --- |
| [ball_reason](../slots/ball_reason.md) | Why the ball holder holds it |










## Identifier and Mapping Information





### Schema Source


* from schema: https://github.com/jeffposey/agentjobs/schema/v2






## LinkML Source

<details>
```yaml
name: BallReason
description: Why the ball holder holds it. Closed vocabulary, scoped to the holder
  -- the scoping is enforced by the class-level rules on Task (section 3).
from_schema: https://github.com/jeffposey/agentjobs/schema/v2
rank: 1000
permissible_values:
  available:
    text: available
    description: 'agent: ready and unclaimed -- any eligible agent may take it.'
  work:
    text: work
    description: 'agent: claimed and executing.'
  revise:
    text: revise
    description: 'agent: review came back with changes requested.'
  answer:
    text: answer
    description: 'agent: the human supplied what the agent was waiting for -- an answer,
      a decision, a permission, a cleared blocker. Prior work stands; resume.'
  redirect:
    text: redirect
    description: 'agent: the instructions changed. Prior work stands but the direction
      does not; re-read the prompt before continuing.'
  hold:
    text: hold
    description: 'agent: stopped by a human, with the release condition stated in
      ball_prompt. The only agent-side reason that is not workable -- auto-dispatch
      skips it.'
  spec:
    text: spec
    description: 'human: the spec needs human completion or refinement.'
  review:
    text: review
    description: 'human: work product needs review (v1''s under_review).'
  decision:
    text: decision
    description: 'human: a choice is blocking progress.'
  approval:
    text: approval
    description: 'human: a gate -- merge, spend, publish. Yes/no, not critique.'
  input:
    text: input
    description: 'human: missing information only a human has.'
  dependency:
    text: dependency
    description: 'external: a claimed task blocked on another task (v1''s blocked).'
  service:
    text: service
    description: 'external: blocked on a third party, outage, or provisioning.'

```
</details>

</div>