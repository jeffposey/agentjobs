---
name: flesh-out
description: Write a full spec onto a thin task, then park it at human review.
target: task
difficulty: hard
verbs: [update, log, handoff]
gates:
  - before: handoff
    what: The spec was written onto the target task through update_content, and the
      handoff parks the target at human/review naming what was written and what the
      run was least sure of. The run stops there.
---

# Flesh out

The target task is thin: a title, maybe a sentence, and not enough for a session with
no other context to work from. Turn it into a specification that satisfies the
resumption contract.

This run **is** the target task's own dispatch — there is no separate run task. You
read the thin record and the repository, and you write.

## Write, then park for review

Write directly onto the task through the managed `update_content` verb:

- `spec.summary` — one or two sentences that orient a reader with zero context. Not a
  clipped first line of the description.
- `spec.intent` — why this is worth doing at all.
- `spec.description` — the working specification: what to build, in enough detail that
  someone who has never discussed it can start.
- `spec.constraints` — what the implementation must respect, including binding
  decisions recorded elsewhere. Name where each came from.
- `spec.out_of_scope` — what a reader would reasonably assume is included and is not.
- `acceptance[]` — what "done" means, as checkable statements rather than intentions.
- `spec.context[]` — the files a session should read first, each with a `why`.

Then hand off `ball: human` / `ball_reason: review` with a `ball_prompt` that names
what you wrote and **what you were least sure of**. Stop there.

Write-then-review rather than propose-then-write is deliberate. The write is cheap to
reverse — the prior text is in git and in the update's own log entry — the target is by
definition mostly empty so there is little to destroy, and a draft posted as a log
entry for a human to transcribe later is a proposal formatted as a chore. What makes
it safe is the parked ball: a wrong spec cannot be dispatched against, because the task
sits in review until a human releases it.

## Scope rules

You write spec fields and acceptance criteria. You never change lifecycle, priority,
queue position, or dependencies, and you never claim or close the task.

Anything else you notice — this looks like a duplicate, this belongs in another band,
this needs a task that does not exist — is a `question` on the record. Raising it is
the whole of your authority over it.

## When the task should not be specified

If the premise does not hold — the work is already done, the task is a duplicate, or
the record is too thin to guess at without inventing requirements — **do not invent a
spec.** Say so in a question, hand off to `human`/`decision`, and stop. A confident
specification of work nobody wants is worse than an empty one, because the next
session will believe it.
