---
name: reorder
description: Re-order the open backlog band by band, with a recorded reason per move
  and human anchors respected.
target: project
difficulty: hard
verbs: [queue_move, log, handoff, close]
gates: []
run_task:
  title: Reorder the backlog
  category: meta
  priority: medium
  tags: [queue, playbook]
  acceptance:
    - text: Every move was made through the queue_move verb and its entry states the
        reason for the placement.
    - text: No strong anchor was moved, and every ordinary anchor that was overridden
        is named in the run's output with the grounds.
    - text: No task was closed, no spec was edited, and no priority band was changed.
---

# Reorder

Order what survives. `groom` prunes the list — you do not close anything, and it does
not move anything.

Human review of your output is **optional**, and that is deliberate: at this backlog
size the human is the less-informed party at ordering time, so requiring a click on
every order makes the order worse rather than more legitimate. What makes optional
review safe is the recorded reason on every move — a drifted order is audited after
the fact instead of having to be caught live. Write as if it will be audited, because
it will.

**No scoring.** There is no numeric ranking function over signals, and there is not
going to be one. Your output is an argument a person can read and disagree with; a
score is not.

## Graded effort by queue depth

Precision deep in the queue decays before it is consumed, so spend attention where
positions will actually be executed.

- **The head of each band — roughly the next 5.** Ordered exactly, with a reason per
  move.
- **The next 5 to 10.** Rough buckets. Get the grouping right; do not agonise over
  adjacent pairs.
- **The tail.** Left as it stands. It gets one fast triage scan whose single question
  is *does anything here belong near the head?* If something does, move it and say
  why. Everything else is left alone, and leaving it alone is the correct outcome.

## How you write

Every move goes through `queue move`, and the entry's body carries **the reason for the
placement** — not "reordered", but why this task sits ahead of the one it now sits
ahead of. That entry is the audit trail the optional review rests on.

You run **on a trigger** — new tasks arrived, or a human asked. Never on every dispatch
and never on a schedule. Continuous re-derivation is churn, and the queue has to stay
readable as a stable object between runs.

## Anchors: a human move is sticky

There is no pinned flag. `queue_move` is a managed verb and its log entry records its
actor, so *"was this position last set by a human?"* is derivable from the write
history itself. For every open task in the bands you touch, find the actor of its
latest `queue_move`. A human actor makes that position an **anchor**.

- Order **around** anchors by default.
- An **ordinary anchor** may be overridden only with an explicit statement in your
  output naming the anchor and the grounds. Never by silently re-deriving the band.
- A **strong anchor** — a position a human kept after reading the queue-move check's
  warning — is not moved at all. Raise a question about it if you disagree; do not
  override it. The human who read the warning and kept the move was the informed
  party.
- **Anchors expire on material change**: the task closes, its priority band changes,
  or a `needs` dependency resolves. At that point the position is yours again. An
  anchor with no expiry would slowly freeze the queue into whatever a human last
  touched.

## Out of scope

Never close a task, never edit a spec, never change a priority band, and never add or
remove a dependency to express an ordering preference — a false `needs` makes a task
unclaimable and lies to every later reader. Anything you think is dead goes to `groom`
as a raised observation.
