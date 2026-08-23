---
name: groom
description: Find duplicate, superseded and stale tasks; propose closures; close only
  what a human approved.
target: project
difficulty: hard
verbs: [log, handoff, close]
gates:
  - before: close
    what: The full closure list, one item per task with its proposed outcome, its
      reason and its evidence, approved by a human as recorded on the run task.
      Nothing outside the recorded approved list may be closed.
run_task:
  title: Groom the backlog
  category: meta
  priority: medium
  tags: [grooming, playbook]
  acceptance:
    - text: The closure proposal was recorded on this task before anything closed.
    - text: Every executed close appears in the approved list, with the approved
        outcome (duplicate or superseded), and nothing else was closed.
    - text: No task file was deleted and no spec was rewritten.
---

# Groom

Prune the list. `reorder` orders what survives — you do not do its job, and it does
not do yours. You meet at the tail of the queue from different directions: reorder
asks "does anything down here belong near the head?", and you ask "is anything down
here dead?"

## What to look for

Sweep the open corpus for three conditions.

- **Duplicates.** Two tasks that are the same work. The evidence is the counterpart's
  id, and a sentence saying why they are one job rather than two related ones.
- **Superseded.** A task whose work another task, or a shipped change, now covers. The
  evidence is the superseding task id or the commit.
- **Stale.** A task whose premise no longer holds — the feature it extends was
  rewritten, the problem it fixes was fixed another way. Staleness is **surfaced, not
  closed**: see the executable set below.

Read the whole record before judging, not the title. Two tasks with similar titles are
routinely different work, and a task whose title reads stale often has a live spec.

## Propose, never act first

Record a closure proposal on the run task: one item per task, carrying

1. the task id,
2. the proposed outcome — `duplicate` or `superseded`,
3. the reason, in a sentence,
4. the evidence: the counterpart id, the superseding task, or the commit.

Then hand off `ball: human` / `ball_reason: review` with the proposal as the
`ball_prompt`, and **stop**. The proposal is the deliverable of the first half of the
run; a run that closed something before this handoff has failed regardless of whether
the close was right.

## Execute only what was approved

Approval is recorded on the run task — a human's own entry, or the ball leaving
`human` with the approved list stated. Partial approval is expressed by striking
items, and **what executes is the recorded approved list, nothing else.** If the
approval is ambiguous about an item, that item is not approved; ask again rather than
interpreting.

Execute each approved closure through the managed `close` verb with `outcome:
duplicate` or `outcome: superseded`, and reference the run task by id in each close's
body so the record says where the decision was made. Then close the run task with the
tally: proposed, approved, executed.

## The executable set, and what is outside it

The set is exactly `{close-as-duplicate, close-as-superseded}`.

- A stale task that is neither is **raised as a question**, not proposed for closure.
  `wont_do` is a human's own judgment about intent and is never yours to make.
- Never delete a task file.
- Never rewrite a spec.
- Never change a priority, and never move a queue position. Anything you notice about
  order goes to `reorder` as a raised observation, not as a move.

## Why the gate is mandatory here

A wrong close is silent and compounding: the closed task leaves every list and is
forgotten, which is the exact failure the queue program exists to end. "These two
tasks are the same" is also precisely the judgment a human will contest. So: propose
then approve where errors hide — which is here — and write then audit where errors
show themselves, which is `reorder`.
