---
name: reorder
description: Order the open backlog band by band, with a recorded reason on every move
  and human anchors respected.
target: project
difficulty: hard
verbs: [queue_move, log, handoff, close]
gates: []
run_task:
  title: Reorder the {project} backlog
  category: meta
  priority: medium
  tags: [queue, playbook]
  acceptance:
    - text: Every move was made through the queue_move verb, and its entry says why
        this task now sits where it does rather than that it moved.
    - text: Every anchor in the bands touched was respected, or overridden with the
        anchor named and the grounds stated on this task. No strong anchor was moved.
    - text: Nothing was closed, no spec was edited, no priority band was changed, no
        dependency was added or removed, and no strong anchor was written.
    - text: This task records the trigger, what the run read, every move with its
        reason, every anchor decision, and the questions raised.
---

# Reorder

**Order what survives.** `groom` prunes the list — you close nothing, and it moves
nothing. You meet at the tail from opposite directions: it asks "is anything down here
dead?", you ask "does anything down here belong near the head?"

Your run is a task. Everything below is recorded on it, and it is the whole audit of
what you did, because nobody is required to approve your work before it lands.

---

## 1. Human review of your output is optional. That is what the record is for

You write directly. There is no propose-then-approve step, and that is deliberate: at
this backlog size the person whose backlog it is cannot hold every record in their head
at ordering time, so requiring their click on every order makes the order *worse*, not
more legitimate.

What makes optional review safe rather than nominal is that **every move carries its
reason**, so a drifted order is audited after the fact instead of having to be caught
live. Write as though it will be audited. Sooner or later it is.

The asymmetry with `groom` is the design and not caution. A wrong close is silent and
compounding — the task leaves every list and is forgotten. A wrong queue move is loud
and cheap — the order is read every time anyone asks what is next, and the next run
corrects it. **Propose-then-approve where errors hide; write-then-audit where errors
show themselves.**

## 2. No scoring

There is no numeric ranking function over signals — not over unblocking value, age,
centrality, effort, or any weighting of them — and there is not going to be one. Your
output is an argument a person can read and disagree with. A score is not: it launders
a judgment into a number and then hides the judgment inside the weights.

If you catch yourself wanting to rank by a formula, write the sentence instead. The
sentence is the deliverable.

## 3. Triggered, not continuous

You run because somebody asked, or because new tasks arrived and somebody decided that
warranted a pass. **Never on a schedule, and never as a step inside some other run.**

Continuous re-derivation is churn. The queue's value is that it is a stable object
between runs — something a person can read on Monday and still recognise on Tuesday —
and an order that regenerates itself constantly cannot be argued with, only observed.

Say what your trigger was, in your first entry on the run task. A run that cannot name
its trigger should not have started.

## 4. Graded effort by queue depth

Precision deep in a queue decays before it is ever consumed. Spend attention where
positions will actually be executed.

Per band:

- **The head — roughly the next 5.** Ordered exactly, one move at a time, with a reason
  per move. This is where the whole value of the run is.
- **The next 5 to 10.** Rough buckets. Get the grouping right — "these three are the
  unblocking work, those two are polish" — and do not agonise over adjacent pairs
  inside a bucket.
- **The tail.** Left exactly as it stands, apart from one fast triage scan whose single
  question is *does anything here belong near the head?* If something does, move it and
  say why. **Everything else stays put, and leaving it alone is the correct outcome,
  not the lazy one.**

The bands themselves are ordered by the queue, not by you: `critical` before `high`
before `medium` before `low`, always. You order *within* a band. Moving a task between
bands is a different decision — see §8.

An empty or one-task band is finished before you start it. Say so and move on.

## 5. Every move is a `queue move`, and its body is the reason

Move through the verb — `agentjobs queue move`, `task_queue_move` over MCP, or the REST
call under either. Never by editing `queue_position`, which no surface will let you do
anyway, and never by adding a `needs` dependency to force an order (§8).

**The body of the move is the reason for the placement.** It is the audit trail the
optional review rests on, so it has to answer the question a reader will actually ask,
which is *why is this ahead of that*:

- **Name what it is now ahead of, or what it displaced.** "Ahead of task-231 because
  231's spec is still a sketch and this one is ready to work."
- **Name what changed.** New information is the usual reason a position is wrong: a
  dependency closed, a task turned out to gate three others, the thing it was waiting
  for shipped.
- **Say it in a sentence a person can disagree with.** If your reason is true of every
  task in the band, it is not a reason for this one.

**Never write "reordered", "moved to top", or "queue maintenance".** The verb already
records that it moved and where; a body that repeats the mechanics has recorded nothing
and leaves the move unauditable, which is the one thing that makes writing without
approval defensible.

### Read the warnings your own move returns

Every move answers back. The queue-move check is deterministic and immediate, and it is
telling you something about the order *you just wrote*:

- **`above_prerequisite`** — you have put a task ahead of something it needs. The order
  you just wrote cannot execute in the order it reads. **Fix it**; this is a mistake,
  not a preference.
- **`queue_broken`** — **stop moving.** The band is not in a state to be reasoned about,
  so neither is anything you do next. Record it, say what you had done so far, and
  raise it. Do not repair it yourself: a repair guesses, and what it guesses is exactly
  what a person should look at.
- **`demoted_blocker`** — what you pushed down gates other open work, and it names
  which. Often the honest response is to undo the move. If you keep it, the reason must
  say you saw this and why the promotion still wins.
- **`promoted_unclaimable`** — the task you promoted cannot be claimed right now. This
  is frequently *fine*: ordering is about what should be next, not only about what can
  be started this minute. But say so in the reason, so a reader knows you saw it.
- **`no_op`** — the band came out as it went in. Your move did nothing; do not report
  it as if it did.

**Never call `queue keep`.** That verb records a strong anchor — a claim that a human
read a warning and defended a place anyway. Nothing in the code stops you writing one.
The entire worth of that record is that only a person has ever written one, and a run
that writes its own is immunising its own moves against the next run's judgment.

## 6. Anchors: a human move is sticky

There is no pinned flag, and there is not going to be one — it would need a clearing
rule or the queue eventually freezes into whatever was last touched. Stickiness comes
from the write history, because the write history cannot drift from the truth.

**Read it from the queue listing, not from the logs.** Every open task in
`agentjobs queue list` and `GET /api/projects/{project}/queue` carries `last_move`:

| Field | What it tells you |
|---|---|
| `actor` | Who ran the verb. |
| `kind` | `human` or `agent` per the project's `actors:` vocabulary; `null` when the id is not configured. |
| `at` | When, which is what lets you age an anchor (§6.2). |
| `body` | The reason recorded with the move. |
| `anchor` | `strong` when a human kept this place after reading the move's warnings; otherwise `null`. |

`last_move` is `null` for a task nobody has ever moved. That is most of them, and it
means the position is a default rather than a decision — yours to set freely.

### 6.1 What counts as an anchor

**`actor` is who executed the move. It is not reliably who decided it**, and this is
the rule most easily got wrong. In a product built so that agents carry out human
decisions, a decision a person made and an agent applied is recorded with the agent's
id — so reading the actor alone systematically under-counts human authority, in exact
proportion to how well the workflow is working. Measured on this repository's own
backlog: of 31 moved open tasks, 28 named an agent, and most of those were applying an
ordering a human had approved.

So judge the decision, using both fields:

- **`anchor: strong` → never move it.** Full stop. Raise a question about it if you
  disagree; do not override it. The person who read the warning and kept the place was
  the informed party, which is the exact condition that legitimises a human override.
  This is the only anchor strength you cannot argue with, and the only one no agent can
  manufacture.
- **`kind: human` → an anchor.**
- **`kind: agent`, and the body attributes the placement to a person → an anchor**, on
  the same terms as a human one. It qualifies when the body names a person and says
  they asked, approved or decided — *"applying the ordering approved by Jeff on
  2026-08-21"*, *"Jeff asked for this a second time"* — or cites a record where that
  decision is written down. If it cites a record and reading it is cheap, read it.
- **`kind: agent`, with the agent's own argument in the body → not an anchor.** That is
  a previous run's reasoning, and disagreeing with it is your job. Read it before you
  overrule it, and say what you know that it did not.
- **`kind: null` → treat it as an ordinary anchor, and say you did.** The id is not in
  the project's vocabulary, so you cannot tell what wrote it. The cost of wrongly
  anchoring is one position left alone for one run; the cost of wrongly overriding a
  person is the credibility of the whole order. Take the cheap mistake. Mention the
  unconfigured id in your output — somebody should fix the config.

An attribution in a body is a claim, not proof, and it is deliberately checkable rather
than mechanical: it lives in a sentence a reader can weigh, not a flag an agent could
set. Where you rely on one, quote it.

### 6.2 What you may do with an anchor

- **Order around it by default.** An anchored task keeps its place; you arrange the
  others relative to it. This is the ordinary case and it needs no commentary.
- **Overriding an ordinary anchor is permitted, and costs one explicit statement**
  naming the anchor, quoting what it said, and giving the grounds — on the run task,
  and in the body of the move itself. Never by silently re-deriving the band, which is
  the failure this rule exists to prevent: a decision reversed without comment looks
  deliberate and destroys trust in every other position.
- **An anchor expires when its task materially changes**, at which point the position
  is yours again, with no statement required:
  - the task **closed** — it is out of the queue entirely;
  - its **band changed** since the anchor was set;
  - a **`needs` dependency resolved** after `last_move.at` — check the prerequisite's
    closing entry against that timestamp.

  An anchor with no expiry would slowly freeze the queue into whatever a person last
  touched, which is the same loss of authorship in the other direction.

  Expiry is not a licence to forget. When you re-place an expired anchor, say in the
  reason that a person had set this place and what changed to make it yours.

## 7. What to record, and when

On the run task, through the log:

1. **Before you move anything** — a `progress` entry naming the trigger, what you read
   (open counts per band), and the anchors you found: how many, which are strong, and
   which ordinary ones you expect to argue with. This is the entry that lets a later
   reader check your run against the queue as it was.
2. **As you go** — the move bodies carry the reasons; you do not need to duplicate them.
   Log the *decisions* the moves do not carry: an anchor overridden and why, a warning
   you kept a move over, a bucket you drew and the principle behind it.
3. **At the end** — a `progress` entry with the tally: moves made per band, anchors
   respected, anchors overridden (each named), strong anchors left alone, questions
   raised, and what you deliberately did not touch.
4. **Then close the run task.** Your run is your own to close; no approval gates it.

Raise a `question` rather than acting whenever you meet: a strong anchor you think is
wrong, a task that looks like it belongs in another band, a task that looks dead, or an
ordering you cannot express without changing something that is not yours to change.

**Do not move your own run task.** It is an open task in a band like any other, and a
run that promotes itself is the one move nobody can audit fairly.

## 8. Out of scope, and none of it trades against a good reason

- **Never close a task.** Anything that looks dead is `groom`'s, raised as a question.
- **Never change a priority band.** A band change is a different decision — it says
  something about urgency, not about sequence — and it belongs to a person or to a run
  that was asked for it. If a task is plainly in the wrong band, raise it.
- **Never edit a spec, a title, or acceptance criteria.** That is `flesh-out`'s work,
  on a task parked for review.
- **Never add or remove a dependency to express an ordering preference.** A false
  `needs` is not a strong hint about order: it makes the task unclaimable until the
  other closes, deadlocks the graph if two ever point at each other, and lies to every
  later reader. The queue is where order lives. That is the whole point of it.
- **Never write a strong anchor** (§5).
- **Never repair or compact a broken queue mid-run** (§5).

## 9. Changing nothing is a result

A pass that finds the order already right is a legitimate outcome and a good one.
Report it as one: which bands you read, what you found at each head, which anchors held,
and why nothing needed to move.

**Do not manufacture moves to have something to show.** Churn is the failure mode this
playbook is bounded against, and a run that shuffles a band to look busy costs exactly
the stability the order exists to provide.
