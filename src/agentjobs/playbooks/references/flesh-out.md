---
name: flesh-out
description: Write a full spec onto a thin task, then park it at human review.
target: task
difficulty: hard
verbs: [log, update, promote, close, handoff]
gates:
  - before: handoff
    what: Either the spec was written onto the target through one `update_content` and
      the target promoted out of `draft`, or — where the target duplicated an existing
      task — the detail was written onto that counterpart and the target closed
      `duplicate` naming it. Both shapes end at one handoff parking a human at
      `review`, on the record that survived, with a prompt naming what was written,
      what the run was least sure of, and every question it raised. The run stops
      there — it does not dispatch, and it does not start the work it just specified.
---

# Flesh out

**Specify one task, then stop.** `groom` prunes the list and `reorder` orders what
survives; you do neither. You turn a record nobody could work from into one a session
with no other context could.

This run **is** the target task's own dispatch. There is no run task, because the
record belongs on the task being worked — a flesh-out of task-123 is ordinary work
*on* task-123, and a second task to say so would split one story across two records.
So everything below happens on the target, and the target is the whole account of what
you did.

---

## 1. First, find out whether this task already exists

Before you read the record closely and long before you write anything, **search the
backlog for the task you have been pointed at.** A thin record is thin because
somebody filed it in a hurry, and the commonest thing filed in a hurry is a thing
already on the list. A duplicate you specify carefully is worse than one you specify
badly: you will have written two good specs for one job, and the second reader has no
way to tell which one the work belongs to.

Search on more than the title. Four passes, and they find different things:

1. **The nouns in the title**, and their synonyms. "Top bar" and "header" are the same
   thing to a person and different strings to `grep`.
2. **The artefact the work would touch** — the file, route, endpoint or command. This
   is the pass that finds the counterpart nobody phrased the same way, because two
   people describing one defect will agree about the file long before they agree about
   the words.
3. **Closed tasks as well as open ones.** A closed counterpart is a different answer
   from an open one and §3 handles it; a search that skips them will tell you a
   shipped feature is unbuilt.
4. **The epic that owns the area.** If a parent task covers this surface, the answer
   is usually one of its children, and the children are where the scope lines are.

**Then read the candidates' `out_of_scope`, and read it before you read anything else
of theirs.** It is the field that decides this question fastest and in either
direction. A neighbouring task that explicitly excludes your target's work is not a
duplicate and has just proved it in writing — and it has also told you the exclusion
was deliberate, which is a fact your spec should carry. A neighbouring task that
silently assumes your target's work is the harder case, and §2 is about that one.

**The test for a duplicate is: if the counterpart were done, would this task have
nothing left to do?** Not "are these about the same area" — half a backlog is about
the same area. Same *work*. If the honest answer is "some of it", it is not a
duplicate; it is an overlap, and an overlap is a sentence in your spec and a
`question`, not a close.

Say what you searched and what you found, even when you found nothing. A run that
reports "no duplicate" without saying where it looked has told the reviewer to do the
search again.

## 2. If it is a duplicate: fold it in, then close the draft

Two records for one job is the failure. So you end the run with one, and the one you
keep is **the counterpart, not the target** — it is older, it already has whatever
history and dependencies accumulated on it, and other records may already point at it.

Three acts, in this order:

1. **Write what the target added onto the counterpart**, through one `update_content`,
   exactly as §5 and §6 describe. Usually that is a sentence or two: the target was
   filed because somebody hit the problem again, and what is new is *the way they hit
   it* — a second surface, a narrower repro, a case the counterpart's scope did not
   obviously cover. Add that, and nothing else. **You are not re-specifying the
   counterpart.** If the target genuinely adds nothing, write nothing and say so.
2. **Close the target** with `outcome: duplicate`, and name the counterpart in the
   body along with what you moved onto it. `duplicate` and not `cancelled`: the
   difference is whether the work is still wanted, and here it is.
3. **Hand off the counterpart** at `human`/`review`, per §8, with a prompt that names
   the close as the first thing it says.

**The close happens in the run and is not gated, and that is a deliberate departure
from `groom`.** Groom's closes are gated because a groom run closes records nobody is
then asked to look at, so a wrong close is silent — the task leaves every list and no
prompt ever arrives. Here the opposite is true by construction: the survivor is parked
at review in the same run, the prompt's first line says which task was closed into it,
and the reviewer is looking at the merge and the close together. A close a human is
holding in their hand is not the failure the gate exists to prevent. Reversing it is a
patch that reopens the task, and the target's own record still holds everything it
said.

**Two cases where you do not do this**, and both end with the draft still open:

- **The counterpart is `active` with an owner, or has a live run.** Writing a spec
  onto ground a session is standing on is the §3 rule and it does not stop applying
  because you found a duplicate. Log a `note` on the counterpart saying it was filed
  again and what the new detail is, leave the target open, and hand the *target* back
  at `human`/`decision` saying the two need merging once the run lands.
- **You are not sure.** A close you would have to argue for is an overlap. Write the
  overlap into the target's spec, name the counterpart in a `question`, and specify
  the target normally.

## 3. The other three ways the premise fails

You were pointed at this task by someone who believed it was thin. Check that belief,
because the remaining failures this playbook can cause all start with skipping this.

**Thin is not the same as short.** A three-sentence record that names a specific
defect, the file it lives in and how to reproduce it is not thin — it is brief, and
rewriting it into six paragraphs makes it longer and no clearer. The test is the
resumption contract in `ALLAGENTS.md`, and it is a question about a reader, not a word
count: *could a session with no memory of any conversation start this work from the
record alone?* If yes, the task does not need you.

- **The work is already done.** `git log` and the closed tasks are where that is
  proved, and a task filed weeks ago is exactly the kind that quietly shipped another
  way. This is §1's third search pass arriving at a different answer: the counterpart
  is closed `completed`, so there is nothing to fold. Raise it as a question, hand
  back, and leave the close to a human — a task that shipped another way is a
  judgement about whether it *fully* shipped, and that is not yours.
- **Somebody is working it right now.** `lifecycle: active` with an owner, or a live
  run. Writing a spec under a session that is mid-flight rewrites the ground it is
  standing on. Hand back and say so.
- **The record is too thin to specify without inventing requirements.** A title and
  nothing else, on work whose intent you cannot recover from the repository, the
  parent, or the log. **Do not invent a spec.** Say in a question what you would need
  to know, hand off `human`/`decision`, and stop.

The last one is the one that costs most to get wrong. A confident specification of
work nobody asked for is worse than an empty record, because the next session will
believe it — and unlike an empty record, it looks finished, so nobody re-reads the
title to check.

**Finding the task adequately specified is a result, and you report it as one.** What
you read, what you compared it against, and why it did not need this. Change nothing,
hand the ball back, and say that plainly. A run that pads a working record to have
something to show has made the backlog worse and left no trace of having done so.

That outcome is more likely than it sounds. Measured in this repository on 2026-08-23,
across 113 open and 174 closed tasks: the thinnest open record carried a 429-character
description and five acceptance criteria, the median was 2,387 characters, and not one
task in the corpus had an empty `spec.description`. A project whose tasks are filed by
agents does not accumulate thin ones — the thin ones come from a human filing from a
phone, which is exactly when a duplicate is most likely too. Those counts move daily
and that shape has not; take the measurement yourself before assuming the premise
holds here. `record_can_brief` in `src/agentjobs/dispatch/guards.py` is the machine's
version of the same question.

## 4. What you read before you write

In this order, because each one bounds the next:

1. **The record itself** — title, whatever spec fields exist, tags, category, and the
   **whole log**. The creation entry routinely carries the real intent in a sentence
   nobody moved into the spec, and that sentence is the closest thing you have to the
   author's actual ask.
2. **The parent, if there is one.** A child's scope is bounded by its parent's. A
   child that quietly redefines what the parent is for is the commonest way a
   flesh-out goes wrong, and it is invisible in review because the child reads well on
   its own.
3. **Its dependencies, both directions.** What it needs, and what needs it. A task
   three others are waiting on is a task whose spec has to answer their questions.
4. **The neighbours §1 turned up.** Not for the duplicate question, which is settled
   by now, but for the scope lines. A task whose neighbour has already excluded this
   work in `out_of_scope` inherits an argument it should quote rather than re-make.
5. **The repository.** The code the task names, the docs that govern it, the design
   documents whose decisions bind it, and the closed tasks that did adjacent work.

If you cannot say which of these you read, you are guessing, and a guess written into
`spec.description` is indistinguishable from knowledge once it is on the record.

## 5. What you write

Seven fields. Each has a job, and each has a way of being filled with words that do
that job no good.

- **`spec.summary`** — one or two sentences orienting a reader with zero context. It
  is a *different* text from the description, not its first line clipped. A reader
  scanning a list decides from this alone whether to open the task.
- **`spec.intent`** — why this is worth doing at all. This is the field that answers
  "do we still want this?" in six months, and it is the one nobody can reconstruct
  later from the code.
- **`spec.description`** — the working specification: what to build, concretely enough
  that somebody who has never discussed it can start. Not a design document, and not a
  restatement of the title at length.
- **`spec.constraints`** — what the implementation must respect, **and where each one
  came from**: a design decision by its id, a rule in `ENGINEERING.md`, a `decision`
  entry on another task. A constraint with no cited source is your opinion wearing a
  rule's clothes, and the session that reads it cannot tell the difference.
- **`spec.out_of_scope`** — what a reader would reasonably assume is included and is
  not. This is where the ambition you had to cut goes, so that cutting it is a
  recorded decision rather than an omission somebody re-adds next month. It is also
  the field the *next* flesh-out will read first (§1), so a boundary you draw here
  saves someone else the search you just did.
- **`spec.context[]`** — the files to read first, each with a `why`. A path with no
  `why` is a reading list; the `why` is what lets a reader skip the ones that do not
  bear on their part.
- **`acceptance[]`** — what "done" means, as statements someone who did not write them
  could check. Two rules: **do not write a criterion you cannot imagine failing**, and
  do not write one only the implementer can evaluate. "The code is clean" is neither
  checkable nor arguable; "`agentjobs next` returns task-N before task-M on a corpus
  where both are ready" is both.

**Leave a field empty rather than filling it.** An `out_of_scope` reading "nothing is
out of scope" and a `constraints` reading "follow the engineering standards" are worse
than blanks: they cost a reader the same attention as real content and return none,
and they teach the reader to skim the next task's fields too.

### You are specifying, not deciding

This is the line that matters most and the easiest one to cross without noticing.
Where the work needs a decision that is not yours to make — which of two designs, what
the default should be, whether a thing ships at all — **name the decision in the spec
and say it is open.** Do not pick one and write the pick into prose.

A spec that quietly settles an open question is the worst artefact this playbook can
produce, because the task then gets dispatched and a session implements a choice
nobody made, with the record showing a specification rather than a decision. Nothing
in the log says a call was taken. That is how a preference becomes a requirement in one
step, and there is no later moment at which anybody catches it.

Where you take a small call because the alternative is an unstartable spec, record it
as a `question` on the task and say in the `ball_prompt` that you took it. Then it is
a call someone can reverse instead of a fact they inherit.

## 6. Write through the verb, once

Write through the managed `update_content` verb — the MCP tool, or `PATCH
/api/projects/{project}/tasks/{task}`. **Never edit the YAML file.** The file is
generated state; a writer that goes around the model will eventually disagree with it,
and the update's own log entry is half of what makes your write reversible.

**One update, not six.** The log entry is the before/after boundary, so a single
update leaves one entry a reviewer can read as a diff, and six leave six entries and
no single answer to "what did this run change?". Compose the whole spec, then write it.
This holds for the duplicate shape too: one update on the counterpart, not one per
sentence you moved.

That entry, plus git, is the entire reversibility argument for writing before a human
has read anything. Do not weaken it by writing in pieces.

## 7. What you may move, and what you may not

You may write: `spec.summary`, `spec.intent`, `spec.description`, `spec.constraints`,
`spec.out_of_scope`, `spec.context[]`, and `acceptance[]`. Then `promote` **or**
`close`, per §8. Then one handoff. That is the whole of your authority.

Everything below is outside it, and none of these is a rule you may trade against a
good reason:

- **Never change `priority`.** A band change is a queue decision.
- **Never move `queue_position`.** Anything you notice about order is `reorder`'s, and
  `reorder` is a run somebody triggers.
- **Never touch `dependencies[]`.** Adding a `needs` edge to express "this should come
  first" makes a task unclaimable and lies to every reader who takes it at face value;
  adding a `parent` re-homes a task inside a story you were not asked to rewrite. The
  overlap you found in §1 goes in the spec and in a `question`, never in an edge.
- **Never change the title.** On a thin task the title is usually the only thing a
  human actually wrote, and rewriting it destroys the one piece of evidence a reviewer
  has for what was originally meant. If it is wrong, that is exactly the kind of thing
  to say in a question.
- **Never change `tags` or `category`.** They feed bands, filters and views, so they
  are queue decisions in the same sense a priority is.
- **Never close anything but the target, and only as `duplicate`.** `completed`,
  `cancelled` and `superseded` are all judgements about whether work is still wanted,
  which is `groom`'s question and a human's answer. You close one record, because you
  merged it into another one in the same run, and you say which.
- **Never `claim`, `release` or reopen.** Claiming would make you the owner of work
  you were sent to specify, and reopening a closed record is a judgement that the work
  is wanted again — both are somebody else's call, and `promote` is the *only* move
  along the lifecycle axis this playbook has.
- **Never start the work.** See §8.

Anything else you notice — this belongs in another band, this needs a task that does
not exist, this premise looks stale — is a `question` on the record. Raising it is the
whole of your authority over it, and a question that carries what you found is worth
ten that ask whether somebody has thought about it.

## 8. Promote, then park; the park is the gate

**Where the target was a `draft` and you specified it, `promote` it before you hand
off.** A draft is a record whose spec is unfinished, and you have just finished it, so
leaving it a draft would say something about the record that is no longer true — and
`handoff` deliberately leaves `lifecycle` alone, so nothing else in the run would ever
correct it. Where the target was already `ready`, promote is not available and nothing
needs it.

**Know what promotion costs, because it is not nothing.** `_skip_reason` in
`src/agentjobs/manager.py` decides claimability on `lifecycle` and never consults the
ball, and `dispatch_task` refuses only `agent/hold`. Measured against the real manager
on 2026-08-23: a task promoted and then parked at `human`/`review` **is still returned
by `get_next_task()`**, so a spec no human has read is reachable by `agentjobs next`
and by a dispatch. The park is a signal to a person, not a lock on the queue. So the
thing that actually keeps an unread spec from being worked is the review arriving
before anyone reaches for the task — which is a reason to make the prompt below
decidable in one reading, not a reason to skip the promote.

Then, on whichever record survived the run: hand off `ball: human` /
`ball_reason: review`, and **stop**.

`review` rather than `approval`, and the difference from `groom` is deliberate: groom
asks a human to *authorise a list of writes that have not happened*, which is what
`approval` means, and you are asking one to *read something you already wrote*, which
is the same ask a merge review makes. Design §5.3 and decision P7 both name `review`
here, and unlike groom's case the vocabulary fits the question being asked.

The `ball_prompt` carries five things:

1. **What you closed, if anything** — first, before everything else. "task-N was a
   duplicate of this one; I closed it and moved X onto here" is the sentence a
   reviewer most needs to see and most needs to be able to reverse.
2. **What you wrote** — which fields, and what each now says in a clause. Not the full
   text: unlike a groom proposal, your work is on the record directly beneath the
   prompt, and repeating it makes the prompt longer without making it more decidable.
3. **What you were least sure of**, named specifically. Not "please review" — *which*
   sentence you would delete first if you were wrong, and why you kept it. This single
   line is what makes the gate worth having; a reviewer who has to find the soft spot
   themselves is doing the run's job. If everything genuinely was obvious, say that,
   and say what made it obvious.
4. **What you did not write, and why** — the fields left empty, and the ambition you
   moved to `out_of_scope`.
5. **Every question you raised**, by subject, so the reviewer knows there is something
   below the fold addressed to them.

Then stop. Do not dispatch the task, do not claim it back, and **do not start the work
you just specified.** That last one is the live temptation: you now understand the task
better than anyone, the record says exactly what to do, and doing it would look like
initiative. It would also delete the gate — a spec nobody has read would have been
implemented on its own authority, which is the one outcome this whole shape exists to
make impossible.

If the surviving record's ball was already `human` when you arrived, you are adding to
somebody's pile rather than starting one. Say so in the prompt, and say what the ball
was before you moved it, so the reason you overwrote is recoverable.

**If review never comes, leave the task parked.** An unanswered gate is the gate
working. Do not re-write the spec to be more persuasive, and do not hand it back to an
agent to get it moving.

## 9. Why write first and review after

`groom` proposes and waits; you write and then wait. The asymmetry is the design, and
it turns on which direction the error runs.

**A wrong close is silent and compounding** — the task leaves every list and nobody
ever gets the prompt that would let them notice. **A wrong spec is loud.** It is the
first thing anyone opening the task reads, it is sitting where a reviewer is already
looking, and the review request is on the record. It is also cheap to reverse: the
prior text is in git and in the update's own log entry, and the target was by
definition mostly empty, so there was little there to destroy.

The one close you may make is the exception that proves the rule rather than a hole in
it: it is silent only if nobody is asked to look, and §2 requires that the survivor be
parked at review in the same run with the close named first in the prompt. Take that
requirement away and the close becomes groom's kind, which is why it is a requirement
and not advice.

The rejected shape is proposing a draft spec as a log entry for a human to transcribe
later. It reads as the safer option and is not one: it is a proposal formatted as a
chore, it puts the specification somewhere the schema does not read, and the person it
protects is the person who then has to retype it.

So: **propose then approve where errors hide, write then review where they show
themselves.** This is the second kind.

## 10. One run, one task

You were aimed at one task and you specify one task. Sweeping a band of thin records in
a single run is out of scope by construction — each target is its own dispatch, its own
gate and its own review, and batching them would produce one review request covering
several unrelated specs, which is the drip-feed failure in reverse.

The duplicate case does not breach this. You still end with one specified record; the
second one you touched, you touched to close it into the first.

If you find five more tasks that need this while reading, name them in a question. Five
named ids in a question is a useful morning; five specs written on one authorisation is
not.
