---
name: roadmap
description: Make the backlog fit to publish, then regenerate the tracked ROADMAP.md
  the public repository reads.
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
    - text: Every record edited is named on this task with what was wrong with it and
        what it now says. No record was edited that is not on that list.
    - text: Only titles and spec summaries were touched. No task was closed, reordered,
        re-banded, promoted, or had its description, constraints or acceptance changed.
    - text: The regenerated ROADMAP.md was read end to end as a stranger would read it,
        and this task says what that reading found.
    - text: The generator was run, its output committed, and the roadmap stage of
        scripts/check.py passes against the working tree.
---

# Roadmap

**The roadmap is not a document you write. It is the backlog, seen from outside.**

`scripts/export_roadmap.py` does the writing, deterministically, and you must never edit
`ROADMAP.md` by hand — the next regeneration would silently discard it. Your work is one
layer down: the titles and one-sentence summaries the projection publishes were written
by people talking to each other inside a private backlog, and a stranger is about to
read them. Making those fit to publish is judgment, which is why this is a playbook and
not just a script.

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

## 2. What you may change: two fields, and nothing else

**Titles and `spec.summary`. That is the whole list**, because it is exactly what the
projection publishes.

The reason for the boundary is that these two fields have a second audience nothing else
in the record has. A description is read by the agent doing the work; a summary is read
by a stranger deciding whether this project is serious. Editing anything else under
cover of "improving the roadmap" is a spec change wearing a disguise, and it belongs to
`flesh-out` on a task parked for review.

Never edit `ROADMAP.md`. It is generated; your edit lives until the next run of the
generator and then vanishes without a trace.

## 3. Read the generated file as a stranger, not as its author

Generate it first — `poetry run python scripts/export_roadmap.py ROADMAP.md` — and read
the result, not the records. The projection is where the problems become visible,
because a summary that is fine beside its own description is often unreadable alone.

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

## 4. Propose before you edit

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

## 5. Say what the roadmap does not say

A projection leaves things out on purpose — drafts are counted rather than listed, and
everything but id, title, band, order, summary, unmet needs and parent stays in the
store. When a reading of the file makes one of those omissions look like a mistake, that
is a finding about the *projection*, not about a record: file a task against
`src/agentjobs/roadmap.py` and say what a reader could not learn. Do not work around it
by stuffing the missing fact into a summary.

## 6. Finish by regenerating, and prove it

The last act of the run is the generator, then the gate stage:

```bash
poetry run python scripts/export_roadmap.py ROADMAP.md
poetry run python scripts/check.py --only roadmap
```

Commit the result. A run that edited records and did not regenerate has left the public
file disagreeing with the store it is a projection of, which is the one failure this
whole arrangement exists to prevent.

Close your run task with the tally: records read, records edited (each named), questions
raised, and what you deliberately left alone.

## 7. Changing nothing is a result

A pass that finds every summary already publishable is a good outcome and a common one
once the backlog has been through this a few times. Report it as one — what you read,
what you checked for, what held — and regenerate anyway, because the store will have
moved on even if no wording needed your attention.

**Do not manufacture rewrites to have something to show.** A summary rewritten because
it could be phrased differently, rather than because a reader could not understand it,
costs the record's history and buys nothing.
