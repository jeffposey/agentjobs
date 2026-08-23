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
      Nothing outside the recorded approved list may be closed, and a proposal
      recorded after a close is not a proposal.
run_task:
  title: Groom the {project} backlog
  category: meta
  priority: medium
  tags: [grooming, playbook]
  acceptance:
    - text: The closure proposal was recorded on this task before anything closed,
        one item per task with its id, its proposed outcome, its reason and its
        evidence.
    - text: Every executed close appears in the approved list, with the approved
        outcome (duplicate or superseded), and nothing else was closed.
    - text: No task file was deleted, no spec was rewritten, no priority was changed
        and no queue position was moved.
---

# Groom

**Prune the list.** `reorder` orders what survives — you do not do its job and it does
not do yours. You meet at the tail of the queue from different directions: reorder asks
"does anything down here belong near the head?", and you ask "is anything down here
dead?"

Your run is a task. Everything below happens on it, and it is the whole record of what
you did, so a reader who has only that task must be able to check your work against it.

---

## 1. What you are sweeping, and what you are not

The corpus is every **open** task in the project — `lifecycle` `draft`, `ready` or
`active`. Read it through the read surfaces (`tasks_list`, `task_get`, the REST
equivalents), never by parsing the YAML files yourself: the files are generated state
and a reader that goes around the model will eventually disagree with it.

**Read the closed tasks too.** You are not judging them, but a superseding task is very
often one that already shipped, and you cannot cite it if you never looked. The same
goes for git: `git log` is where "this was fixed another way" is proved.

Three kinds of open task are **outside the sweep**, and skipping them is not laziness:

- **Your own run task.** Obviously, and it is worth saying because the tally at the end
  is the one close you make that is not from the approved list.
- **A task an agent is working right now.** `lifecycle: active` with an owner — a
  session may be mid-flight on it, and closing work in flight is the one failure that
  costs somebody else's whole session. If you believe it is dead, say so in the proposal
  as *deferred, in flight*; do not propose it for closure.
- **A parent with open children.** Closing it orphans them: the children keep their
  `parent` pointer and lose the record that gives them their reason. If the whole
  subtree is dead, propose **every task in it, children first**, and say so explicitly
  in the proposal — one item per task, as always, not one item for the family.

Everything else is in scope, including `draft`, and including **a task whose `ball` is
`human`**. That last one looks like it should be excluded and must not be: the proposal
you are writing is addressed to that same person, so withholding an item because it is
already on their desk hands them a shorter list, not a safer one. Where a human is
holding a task, say so in the item — they may have context you cannot see, and it costs
one clause to tell them which items those are. A backlog's dead drafts collect precisely
in the pile nobody has triaged, and a sweep that skips that pile skips the job.

A draft nobody will ever promote is exactly the kind of thing that accumulates.

## 2. The three conditions

- **Duplicate** — two tasks that are the *same work*. Evidence: the counterpart's id,
  and a sentence saying why they are one job rather than two related ones.
- **Superseded** — a task whose work another task, or a shipped change, now covers.
  Evidence: the superseding task id, or the commit SHA and what in it does the covering.
- **Stale** — a task whose premise no longer holds: the feature it extends was
  rewritten, the problem it fixes was fixed another way, the decision it implements was
  reversed. Staleness is **surfaced, not closed** — see §6.

**Read both whole records before you propose either.** Not the titles. Two tasks with
near-identical titles are routinely different work — one is the mechanism and the other
is the surface — and a task whose title reads stale often has a live spec underneath it.
The title is how you *find* a candidate pair; the record is how you judge it.

Cluster first, then read: group the corpus by tag, category and subject noun, and read
the clusters. That is how a hundred open tasks become a tractable number of comparisons
without the sweep degenerating into a title scan.

### When two tasks are duplicates, say which one dies

This is the most contestable judgment you will make, and a proposal that names a pair
without naming the survivor is not actionable. State both ids and which one closes.

Prefer keeping, in this order:

1. the one that is **further along** — claimed, active, or with a branch;
2. the one with the **richer record** — the fuller spec, the decisions, the log;
3. the one **other tasks depend on**, by `needs` or by `parent`;
4. failing all of that, the **older id**, and say that is why.

If the two records each hold something the other does not, they are probably not
duplicates — or the merge of them is work you are not authorised to do. Raise it as a
question (§6) instead of proposing a close that silently loses half a spec.

## 3. Propose, never act first

Record a closure proposal on the run task as a `progress` log entry, **before** you hand
off and long before anything closes. One item per task, carrying exactly four things:

1. the **task id**;
2. the **proposed outcome** — `duplicate` or `superseded`, and nothing else;
3. the **reason**, in a sentence;
4. the **evidence** — the counterpart id, the superseding task, or the commit.

Then hand off `ball: human` / `ball_reason: review` with the proposal as the
`ball_prompt`, and **stop**.

Three rules about that handoff, each of which has a way of being got wrong:

- **The `ball_prompt` carries the whole list, not a pointer to it.** The prompt is what
  the human is shown when the task surfaces; "see the log entry above" makes them go
  find it, and a proposal nobody can read without digging is a proposal nobody will
  answer.
- **One proposal, one gate.** Propose the whole sweep at once. Drip-feeding items
  through separate handoffs multiplies the gate by the number of items and trains
  everyone to click through it.
- **A proposal recorded after a close is not a proposal.** The ordering *is* the
  guarantee: because the list is on the record before anything executes, checking the
  run is a set comparison rather than a judgment about intent. Break the order and there
  is nothing left to audit.

The proposal is the deliverable of the first half of the run. **A run that closed
something before this handoff has failed, whether or not the close was right.**

## 4. Execute only what was approved

Approval is recorded on the run task — the human's own entry, or the ball leaving
`human` with the approved list stated. Partial approval is expressed by striking items.
**What executes is the recorded approved list, nothing else.**

- **Re-read the record before you execute.** You may be a different session from the one
  that proposed, and you are certainly a later one. The approved list is what the task
  says now, not what you remember proposing.
- **Ambiguity is not approval.** If the approval does not say clearly whether an item is
  in, that item is out; ask again rather than interpreting. "Looks fine" over a list of
  nine is approval of nine; "looks fine, though I'm not sure about the last one" is
  approval of eight.
- **An approval that adds a task you did not propose is not executable either.** It is a
  perfectly reasonable thing for a human to say, and the answer is to propose that task
  properly — read it, state the outcome, the reason and the evidence — and let them
  approve the amended list. You never close on an id alone.

Execute each approved closure through the managed `close` verb with `outcome:
duplicate` or `outcome: superseded`, and **reference the run task by id in each close's
body**, so the closed record says where the decision was made and who made it. Where a
close is one half of a duplicate pair, name the survivor in the body too — the person
who finds that task in six months is looking for where the work went.

If a close is refused, **report it; never work around it.** A refusal is the system
telling you something you did not know — the task moved, someone claimed it, the
revision is stale. Log what was refused and why, finish the rest of the approved list,
and say so in the tally.

Then close the run task yourself with the tally: **proposed, approved, executed,
refused, deferred** — the five numbers and the ids behind each. That is the one close
you make that nobody approved, and it is legitimate because it is your own run.

**If approval never comes, leave the run task parked and stop.** A groom proposal is not
urgent and an unanswered gate is the gate working. Do not chase it, do not re-propose,
and do not close the run task to tidy it away.

## 5. The executable set

Exactly `{close-as-duplicate, close-as-superseded}`. Everything in this list is outside
it, and none of these is a rule you may trade against a good reason:

- **Never delete a task file.** The record of a closed task is why the close is
  auditable.
- **Never rewrite a spec.** Not to clarify it, not to merge two of them. That is
  `flesh-out`'s verb, on a task whose ball is parked for review.
- **Never change a priority.** A band change is a queue decision.
- **Never move a queue position.** Anything you notice about order goes to `reorder` as
  an observation you raise, not as a move you make.
- **Never close as `wont_do`** — or `cancelled`, which is how this schema spells it.
  See §6.

## 6. Stale, and the things you raise instead of doing

A stale task that is neither a duplicate nor superseded is **raised as a `question`**
on the run task, not proposed for closure. Cancelling a task is a judgment about
*intent* — whether the thing is still wanted — and intent belongs to the person whose
backlog it is. You can see that a premise has changed; you cannot see whether they still
want the outcome.

Write the question as a question, with what you found: the task id, what its premise
was, what changed, and what you would expect the answer to be. A question that says only
"is task-N still relevant?" hands the reading back to them and is worth less than not
asking.

Raise the same way, and never act on:

- **Order.** "task-N is dead last and looks urgent" is reorder's, and reorder is a run
  someone triggers.
- **Merges.** Two records that each hold something the other needs.
- **Anything that would make you edit a spec, a priority or a position** to be able to
  propose a clean close. If the close needs a change first, it is not yours.

## 7. Finding nothing is a result

A sweep that proposes nothing is a legitimate outcome and you should report it as one:
what you read, how you clustered it, which pairs you compared and rejected, and why
nothing qualified. Then hand off with that, and close the run task.

**Do not manufacture closures to have something to show.** A groom run's value is the
human's trust that an id on its list is dead; one bad close costs more of that than ten
runs of nothing gain. The backlog being healthy is the good outcome, not the boring one.

## 8. Why the gate is mandatory here

`reorder` writes and is audited afterwards. You propose and wait. The asymmetry is the
design, not caution:

**A wrong close is silent and compounding.** The closed task leaves every list and is
forgotten — which is the exact failure the queue program exists to end — and nobody ever
gets the prompt that would let them notice. A wrong queue move is loud and cheap: the
order is read every time anyone asks what is next, and the next run corrects it.

"These two tasks are the same" is also precisely the judgment a human will contest, and
contest usefully, because they hold the intent behind both records and you hold only the
text.

So: **propose then approve where errors hide** — which is here — and write then audit
where errors show themselves, which is `reorder`.
