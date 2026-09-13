---
name: roadmap
description: Read the owner's phases, read the whole backlog against them, place every
  task in a workstream under the phase it serves, write ROADMAP.md from that reading,
  and regenerate the listing beside it.
target: project
difficulty: standard
verbs: [update, log, handoff, close]
gates:
  - before: update
    what: Every proposed rewording is recorded on this task with the record it is
      aimed at and what is wrong with it, before any record is edited.
run_task:
  title: Refresh the {project} roadmap
  category: documentation
  priority: medium
  tags: [roadmap, playbook]
  acceptance:
    - text: The phases were taken from ROADMAP.md as the owner wrote them. None was
        added, removed, renamed or reordered; anything that fits no phase, and any phase
        the backlog leaves empty, is a question on this task rather than an edit.
    - text: The workstreams were derived by reading the whole open backlog in one pass,
        and this task names each one, the phase it serves, what it is for, and which
        tasks are in it.
    - text: Every hard placement is argued on this task -- the tasks that could sit in
        two phases, and why they went where they did.
    - text: Every record edited is named on this task with what was wrong with it and
        what it now says. No record was edited that is not on that list.
    - text: Only titles and spec summaries were touched. No task was closed, reordered,
        re-banded, promoted, or had its description, constraints or acceptance changed.
    - text: ROADMAP.md was read end to end as a stranger would read it, and this task
        says what that reading found.
    - text: The listing was regenerated, both files committed, and the ROADMAP.md audit
        passes.
---

# Roadmap

**A roadmap says what a project is doing and why. A listing says what is in the queue.
This playbook produces both, and they are not the same act.**

The listing is not yours to write. `scripts/export_roadmap.py` renders `docs/backlog.md`
from the store, deterministically, and a hand-edit to it survives exactly until the next
regeneration.

`ROADMAP.md` is written by two hands, and the split is the whole method.
**The phases are the owner's.** They say where the project is going, in what order,
and where it stops for now, and a run of this playbook never adds, removes, renames or
reorders one: a phase is a decision about the product, and the store holds no record
that could justify a run making it. Everything under a phase is yours — which of a
hundred-odd open tasks belong together, what each group is *for*, and which phase it
serves. That part is the reason this is a playbook rather than a script: a generator
can sort by band, and only a reader who has held the whole backlog in mind at once can
say that half of it is one problem and name the phase that problem stands in the way
of.

Two consequences follow, and both are findings rather than edits. A task that fits no
phase is a question for the owner, not a new phase. A phase the backlog leaves empty
stays on the page, says so in a sentence, and is raised as a question too — the plan is
allowed to be ahead of the backlog, and the page is where that gap is visible.

Run this before a release, when somebody points at the published roadmap and says it
reads badly, or after a `groom` and `reorder` pass has changed what the top of the
backlog is.

---

## 1. Run `groom` and `reorder` first, or say why you did not

A roadmap publishes an *order* over *live work*. If the backlog still holds tasks that
should have been closed, the roadmap advertises dead work; if the order has not been
looked at since the tasks were filed, the roadmap's central claim — that this is what
happens next — is not true.

So the standing sequence is `groom`, then `reorder`, then this. Each is its own run with
its own task, because each is a different decision and mixing them costs the audit trail
all three depend on. **Never close a task or move a queue position yourself.** Something
that looks dead is `groom`'s; something in the wrong place is `reorder`'s. Raise it as a
`question` on your run task and carry on.

Running without them is allowed — a roadmap of a slightly stale backlog still beats one
frozen months ago — but say on the record that you did, and why.

## 2. Read the whole backlog in one pass, and find the workstreams

**This is the work.** Read the phases on the current `ROADMAP.md` first, so that you
read the backlog against a destination rather than for its own shape. Then regenerate
the listing — `poetry run python scripts/export_roadmap.py docs/backlog.md` — and read
every entry in it, start to finish, before writing anything. Not the records: the
listing, because that is the half a stranger sees, and a task that makes no sense there
is the problem you are here to solve.

You are looking for the subject each task actually belongs to, and the phase that
subject serves. A workstream is a group of tasks that a reader would want explained
together and that a person would think about in one sitting — usually five to fifteen
of them. The signals are reliable:

- **An open umbrella task is a workstream already named.** Its children belong with it
  unless one of them has drifted into somebody else's subject.
- **A shared surface pulls tasks together across bands.** Six tasks about what happens
  when a run dies are one workstream whether they are `high` or `low`, and the band is
  exactly what was hiding that from a reader.
- **A shared decision does too.** Tasks that would all be settled, or all be wasted, by
  one architectural call belong in one group.

Two failure modes to avoid, both of which produce a page that looks organised and is not.
**Do not group by category or by band** — the listing already sorts by band, and a
workstream called "documentation" tells a reader nothing they could not see. And **do not
leave a miscellaneous bucket**. A task you cannot place is a signal: either its summary
does not say what it is about, which is step 5's work, or it is genuinely alone, in which
case say so in the workstream that is nearest and explain the oddity in one clause.

Write the grouping onto your run task before writing the page. It is the argument, and
the page is only its output.

## 3. Place each workstream in the owner's phase it serves, and argue the hard ones

A phase answers "why this group of work, and why now" for a reader who does not know the
project, and its order on the page is the order the owner wants the project to move in.
**Never add, remove, rename or reorder a phase.** If the backlog seems to want a phase
the page does not have, or a phase reads as though it has been overtaken, raise it as a
`question` on the run task with the tasks that made you think so, and place the work in
the nearest phase that exists until the owner answers.

**A phase is not a release and not a date.** It is the owner's statement of priority,
and the queue is expected to follow it: a task in a later phase sitting above one in an
earlier phase is a `reorder` finding, raised on the run task and never fixed here. Until
the queue has caught up it still decides what an agent works, and the page must say so.

Argue the hard placements on the run task. A task that could sit in two phases is where
the reading is doing real work, and the reason it went one way is the thing a later pass
will want and cannot recover from the diff. An umbrella's child may sit in a different
phase from its parent when it plainly serves a different one — say so in the clause.

An empty phase is a result, not an error. Leave it on the page with one paragraph saying
what it will hold once somebody has tried, and raise a `question` naming the work the
backlog is missing. Do not file that work yourself; which gaps become tasks is the
owner's call.

## 4. Write the page, and keep it honest by hand

`ROADMAP.md` is prose you write. There is no generator to run over it, which means every
property it has is one you gave it:

- **Open with what the project is**, in three or four sentences, for somebody who arrived
  from a search result. The listing cannot do this and neither can any record.
- **Say how the page and the queue relate, and that the page runs behind the listing.**
  The phases and their order are the owner's plan, the queue is expected to follow it,
  and until it has an agent still takes work from `agentjobs next` rather than from this
  page. All three will otherwise be assumed some other way, and a reader who trusts a
  hand-written page as though it were generated has been misled by you.
- **Keep every phase on the page, including an empty one.** A phase with no tasks under
  it is the owner's plan running ahead of the backlog; say so in a sentence, and say what
  the phase will hold.
- **Roster each task as a list item beginning with its backticked id**, followed by a
  short clause in your own words. That shape is what the audit reads: a rostered id is a
  claim that this is live planned work, and a mention inside a paragraph is not audited at
  all, so prose may refer to a task freely.
- **Link to [docs/backlog.md](../docs/backlog.md) for the detail** rather than restating a
  summary. The page earns its length by grouping and explaining, never by copying.

Then run the audit, which is the same gate stage as the listing:

```bash
poetry run python scripts/export_roadmap.py docs/backlog.md --audit ROADMAP.md
```

It fails on a rostered task that is no longer open, and reports — without failing — the
open tasks you have not placed. **A run of this playbook should leave that report empty.**
It is allowed to be non-empty afterwards because tasks get filed every day and the gate
must not go red for that, not because placing them is optional for you.

## 5. What you may change in the records: two fields, and nothing else

**Titles and `spec.summary`. That is the whole list**, because it is exactly what the
projection publishes.

The reason for the boundary is that these two fields have a second audience nothing else
in the record has. A description is read by the agent doing the work; a summary is read
by a stranger deciding whether this project is serious. Editing anything else under
cover of "improving the roadmap" is a spec change wearing a disguise, and it belongs to
`flesh-out` on a task parked for review.

Never edit `docs/backlog.md`. It is generated; your edit lives until the next run of the
generator and then vanishes without a trace. `ROADMAP.md` is the opposite and step 4 is
where you write it.

## 6. Read the listing as a stranger, not as its author

Read the regenerated `docs/backlog.md`, not the records. The projection is where the
problems become visible, because a summary that is fine beside its own description is
often unreadable alone — and a summary you could not place in step 2 is usually one of
these.

Five things to look for, in the order they matter:

- **A summary that only a member of this project can parse.** Unexpanded shorthand, a
  bare task id standing in for a concept, a reference to a conversation. The test is
  whether somebody who has read the README and nothing else knows what the task is.
- **A summary that is not a summary.** The commonest fault is a first line clipped off
  the description, so the entry starts mid-argument. The second commonest is a summary
  that repeats the title word for word, which spends a reader's attention and gives them
  nothing — `task-381` was exactly this on 2026-09-11.
- **Anything personal or machine-local.** A home directory, an email address, a private
  hostname. The generator refuses to write when it finds one of these, so a leak of that
  shape stops the run rather than reaching the file. It catches only the unarguable
  cases: a person's name where a role would do, a client, an employer, or a machine
  described so specifically that it identifies one, are all yours to catch.
- **A quotation.** This repository's records paraphrase people rather than quoting them
  ([ALLAGENTS.md](../ALLAGENTS.md)), and a quotation that survived into a title or a
  summary is now on a public page. `agentjobs quotations` finds them; `agentjobs redact`
  is how one is removed.
- **A claim that has stopped being true.** Titles written as assertions age badly — "X
  is broken" outlives the fix, "we should Y" outlives the decision not to. The record
  says what happened; the title should not disagree with it.

## 7. Propose before you edit

Record the full list on your run task before touching a record: the task, the field, what
is wrong with it, and the replacement. Then edit.

The order matters for one reason. An edit to a summary is a small, plausible-looking
change that is almost impossible to audit afterwards — the diff shows the new words and
nothing about why, and the record's own log shows an update with no argument in it. The
proposal is the only place the reasoning can live. Write it as though somebody will
disagree with an item, because they should be able to.

**Preserve the meaning.** You are rewriting for an audience, not re-deciding what the
task is. If you cannot say what a task is about without guessing, that is a `flesh-out`
case: raise it as a question and leave the record alone.

## 8. Say what the listing does not say

A projection leaves things out on purpose — drafts are counted rather than listed, and
everything but id, title, band, order, summary, unmet needs and parent stays in the
store. When a reading of the file makes one of those omissions look like a mistake, that
is a finding about the *projection*, not about a record: file a task against
`src/agentjobs/roadmap.py` and say what a reader could not learn. Do not work around it
by stuffing the missing fact into a summary, and do not work around it in `ROADMAP.md`
either — a fact smuggled into the page is one the listing still cannot show.

## 9. Finish by regenerating, and prove it

The last act of the run is the generator, then the stage that audits the page:

```bash
poetry run python scripts/export_roadmap.py docs/backlog.md
poetry run python scripts/check.py --only roadmap
```

Commit both. **This run is when the listing gets regenerated.** No gate checks it against
the store and no other branch regenerates it, so a run that edited records and skipped
this step leaves the listing behind until the next pass. A run that rewrote summaries and
did not revisit the page has left the two halves describing different backlogs.

Close your run task with the tally: the workstreams and phases you settled on and why,
records read, records edited (each named), questions raised, and what you deliberately
left alone.

## 10. Changing nothing is a result

A pass that finds every summary already publishable is a good outcome and a common one
once the backlog has been through this a few times. Report it as one — what you read,
what you checked for, what held — and regenerate anyway, because the store will have
moved on even if no wording needed your attention.

**Do not manufacture rewrites to have something to show.** A summary rewritten because
it could be phrased differently, rather than because a reader could not understand it,
costs the record's history and buys nothing.

The same holds for the grouping, and more strongly. **Workstreams are worth more the
longer they survive**, because a reader returning in three months recognises them and can
see what moved. Re-deriving them from scratch every pass produces churn that looks like
progress; keep the grouping that still fits, place the new tasks into it, and change a
boundary only when you can say on the record what stopped being true. The phases are
stronger still: they change when the owner changes them, and a pass that finds them
unchanged has found the plan holding, which is the outcome it exists to report.
