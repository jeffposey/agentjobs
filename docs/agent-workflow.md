# Agent Workflow Guide

AgentJobs is a durable handoff protocol for agents, humans, and external dependencies.
The task record is the source of truth. It is a row in a database beside the server and
outside every checkout, which [the storage guide](storage-sqlite.md) covers. Chat can wake
a participant or make an interactive session convenient, but it is never required working
memory.

The canonical contract is [schema design section 5](schema-design.md#the-resumption-contract).
This guide shows how to apply it with the schema-v2 Python client.

## Before you write anything: take your own worktree

**One exception, and your prompt already told you if it applies:** an agent sent at a
task that has open children is a supervisor, writes no code, and takes no worktree. Skip
to [working a parent task](#working-a-parent-task-you-supervise-the-children-you-do-not-work-them).
Everyone else, read on.

**A dispatched run starts in the project's shared working tree, and nothing isolates it
for you.** Other agents may be working that same tree at the same moment, and none of you
can see the others. A shared checkout has one `HEAD` and one set of files, so a
`git checkout` replaces the files under whoever else is mid-task — without an error, for
either of you.

So this is your first act, before the claim and before anything is written to disk:

```bash
git worktree add ../worktrees/<repo>-<nnn> -b <type>/task-<nnn>-<slug>
```

That path is a `worktrees/` directory beside the project rather than a sibling of it, so
several live worktrees do not bury the projects in a listing of the workspace. `git
worktree add` creates the directory the first time.

`<repo>` is the project's directory name and `<nnn>` is the task's number, so an
AgentJobs worktree for task-045 is `../worktrees/agentjobs-045`. **That is the whole
convention, and it is stated here** — ENGINEERING.md and ALLAGENTS.md show it filled in
for their own project, and the prompt every dispatched agent receives is this same
literal command. Older worktrees on a machine that has been running a while may use an
abbreviated form such as `aj-045`; those are not wrong, they are just not what to write
next. What matters either way is that the directory names a task, so `git worktree list`
reads as an inventory.

Work there. Once your branch is merged, remove the worktree and then delete the branch
with `git branch -d` — in that order, because a branch checked out in a worktree cannot
be deleted, and with `-d` rather than `-D` so an unmerged branch is refused instead of
destroyed. `git worktree list` and `git branch --list` are the inventories; anything left
behind for a closed task is litter, and `agentjobs branches` names it.

**Run that command. Do not use a built-in worktree tool to get one.** Claude Code has an
`EnterWorktree` tool that looks like the right way to satisfy the paragraph above, and it
is not: it asks to relocate the session's permission root outside `.claude/worktrees/`,
which is an escalation the `auto` classifier declines and a background session has no
terminal to answer. The run then waits for an answer that cannot arrive — observed
2026-08-20 on the first dispatch after the `-w` change, which parked on that prompt
before it wrote a line. The same applies to anything else that moves where the session is
allowed to act. `git worktree add` is an ordinary shell command, needs no relocation, and
leaves you able to `git -C` the shared clone, which is where your merge happens.

**Your harness will tell you to do it the other way, and will say so in a sentence that
sounds like the last word.** A Claude Code background session opens with *"Before making
any code changes, use the EnterWorktree tool … This is enforced: file edits in the shared
checkout are rejected until you isolate."* Both halves are true of the harness and wrong
for this repository: the tool is the one thing you must not use, and the enforcement
would stop you writing in the clone where your merge has to land — and, on a files
project, where your task records have to be committed. So this
repository turns the enforcement off — `.claude/settings.json` carries
`"worktree": {"bgIsolation": "none"}`, which is the escape the refusal message itself
names. Verified on Claude Code 2.1.238, 2026-08-25, by writing into the shared checkout
from a `--bg` session with the key set and again with it removed.

Two things follow. **Take the worktree anyway** — nothing about that key changes why you
need one; it only stops the harness picking the wrong isolation for you. And **if a write
into the shared clone is refused regardless**, that is news: the key has stopped working,
and it belongs on the task record rather than in a workaround you keep to yourself.
Shell out through `Bash`, which was never guarded, and finish the work.

If a worktree for this task already exists from an earlier run, `git worktree add` will
refuse it. That is not a reason to reach for the tool — use the existing path, or take a
new one under a different name.

This used to be arranged for the agent. Dispatch passed Claude Code's `-w` flag, which
put the session in a worktree the CLI managed, and containment was mechanical. It cannot
any more: the isolation that flag grants is enforced by a guard that refuses **every**
git operation aimed at the shared checkout — by `-C` and by `cd` alike — and the merge
happens there. A run isolated that way could do the work and then be unable to land it. So the containment is unchanged in what
it protects; taking it is now your first act rather than the launcher's. The full argument, with the reproduction, is in
[the dispatch design](agent-dispatch-design.md).

## Then register, whatever started you

```bash
poetry run agentjobs run register --task task-<nnn> --project <project>
```

**Run it always.** A session AgentJobs dispatched recognises itself from
`AGENTJOBS_RUN_ID`, prints that it is already known, and writes nothing — so there is no
condition here for you to evaluate, and that is deliberate: a rule with an exception
attached is a rule some fraction of readers gets wrong.

What it buys, if you were hand-spawned rather than dispatched, is every stall protection
AgentJobs has. All of them are keyed on a run record: the poller iterates the run
directories, so a session with none is polled by nothing, and none of the permission
park, the expired-login handoff, the settle on a session that finished without handing
off, or the stall report can reach it. The task then reads `agent`/`work` throughout
while nothing happens to it, and a supervisor following *the signal is the task record*
waits on a process nobody is watching. That is what left task-217 dead for four hours on
2026-08-23.

Registering writes the run record a dispatch would have written, and from the next poll
you are followed by exactly the same code. It also takes the task's run lock, so nobody
can dispatch a second agent at the task you are working.

It refuses rather than guessing, and each refusal names its gate:

| Refusal | What it means |
| --- | --- |
| `session_unnamed` | Your runner does not publish its own session id. Find it with `<runner> agents` and pass `--session`. |
| `session_unknown` | No live session with that id under the project root. A session launched from inside a worktree is not listed there — the poller would look it up the same way and report it gone. |
| `session_interactive` | A session a person is sitting in is deliberately not adoptable. Claiming the task is what records that you are working it. |
| `live_run_exists` | Something is already following this task. |
| `dispatched_elsewhere` | You are a dispatched run for a *different* task. |

Nothing here restarts or stops anything, and registration is **not** a dispatch: no
run was authorised and none was started, so what lands on the task record is a note
rather than a `dispatch` entry. The design argument, including what was rejected, is in
[the dispatch design](agent-dispatch-design.md#every-protection-above-is-keyed-on-a-run-record-task-320-2026-08-27).

## When the work is done: does this run merge, or stop?

Both answers exist, and **your dispatch prompt is the only thing that tells you which one
you have.** It is the only channel that can: the answer comes from the project's posture
in machine-local `~/.agentjobs/dispatch.yaml`, which you cannot read and must not, and
every committed document in a repository has to be written for the default.

| Posture | The prompt says | What you do when the work is done |
|---|---|---|
| `read_only` | nothing about merging | you have no branch; your output is the record |
| `auto`, `supervised` | *"stops at the merge gate"* | hand off to `human`/`review` and **stop** |
| `autonomous` | *"releases the merge gate"* | record the evidence, then merge it yourself |

**No clause means you stop.** A prompt from an older dispatch, a prompt you are
reconstructing from memory, a session you are unsure about — all of them mean the gate
stands. Nothing here fails towards merging.

### Merging your own work, when the posture releases it

Record what you did on the task **first** — what you built, what you verified and how,
what you decided and what you rejected. This is not bookkeeping to do afterwards. It is
the only review this work will ever get, and a merge nobody can reconstruct is worse than
a merge nobody approved.

Then run the command the clause names:

```bash
poetry run agentjobs finish <task-id> --project <project> --posture-release
```

It rebases onto the base branch, runs the **full unqualified `scripts/check.py`** on the
rebased branch with that worktree's own interpreter, and merges `--no-ff` only if that is
green. Then it rebuilds, restarts, verifies the running service is serving the merge,
closes the task, removes your worktree and deletes your branch. Exit 0 means all of that
happened. Exit 1
means it stopped, and the task record says at which step and whether anything was merged.
Exit 2 means it declined and touched nothing — including when the posture does not
actually release the gate, which is checked there rather than taken on trust.

**Do not run `git merge` instead.** The whole reason an unreviewed merge is acceptable is
that an objective check ran on the exact commit being merged, and it ran somewhere other
than in your own account of your work. A hand merge is that check's absence.

**Two conditions, not one.** The gate is the floor; your judgement is the other half. If
you found something genuinely wrong — a design you are not confident in, a test you
disabled to get green, an acceptance criterion you could not meet — hand off for review
however green the gate is, and say why. "No serious issue detected by the agent" is you
grading your own homework, and it is trusted only as a veto, never as an authorisation.

**Never push** unless the prompt's push clause says the project permits it. That clause
is per project and defaults to off. A local merge is recoverable by anyone with a reflog;
a push is a publication, and it is exactly that recoverability that makes merging without
a reviewer defensible in the first place.

## Working a parent task: you supervise the children, you do not work them

A task with an open child is an epic. **Whoever holds it starts a separate session per
child and stays running as the supervisor.** You do not work a child in your own session,
however small it looks, and you do not work two at once. Several children may *run* at
once — see [how many children fly at once](#how-many-children-fly-at-once) — but none of
them runs in your session.

Your prompt says which of these you are, because dispatch reads it off the record: a task
with open children gets the supervisor prompt, and every other task gets the ordinary
one. There is no flag to set and no judgement call at spawn time.

### A refusal is not a wall — but three in a row is

Under `auto`, a refused tool call is **deny-and-continue**: you get an error and you keep
going. One refusal costs you nothing but that call.

**Three consecutive refusals arm a breaker**, and the next call after that becomes an
interactive prompt. A `--bg` run has nobody to answer it, so it stops there until a human
finds it — indefinitely.

The trap is the obvious reaction to a refusal: **rewording the same call and sending it
again.** That is how one refusal becomes two. On 2026-08-21 `run_d5ab5caf` was refused
while writing a child's brief, reworded it, was refused again, and parked — before it had
launched anything at all. A benign help command in between supplied the third.

So when a call is refused: **do something else, or say why you are stuck.** Do not re-send
it with softer wording. If the capability is genuinely required, stop and put the problem
on the record where a human can see it — that is what the ball is for.

Two things worth knowing about the refusals themselves. They are partly **stochastic**:
that same help command was approved twenty-five seconds before an identical one was
refused, so a refusal is not a stable property of a command. And they are about
**content** — an agent writing an instruction that tells another agent to skip human
review and merge to `main` gets declined, because the authorisation for that lives on the
task record, where the classifier cannot see it. A supervisor's log writes look exactly
like that, which is why dispatch pre-approves the project's own MCP servers for supervisor
runs, and only for those (task-220).

### Why a session, and where the line is

**The reason is context, and it says nothing about how many run at once.**

A session that works four children carries four children's worth of exploration by the
fourth, and the transcript a handoff was supposed to replace is exactly what the next
session cannot read. Every child worked in its own session ends with its findings in a
place the next reader can actually open: the task record. The supervisor's own context
stays small enough to still be a supervisor at the end of the epic.

**That argument is about isolation, not about ordering, and for a long time this section
was read as settling both.** It used to open "the reason is context, not parallelism —
the loop is still one child at a time", which put a claim about ordering in a subordinate
clause of an argument that does not reach it. Context cost is a property of how many
transcripts *one* session accumulates. Three concurrent child sessions leave the
supervisor's context exactly as small as three sequential ones, because the supervisor
reads task records and diffs and never a child's transcript — which is what the section
directly below insists on. So this justifies a session per child and is silent on how
many fly at once; [how many fly at once](#how-many-children-fly-at-once) is its own
question with its own answer (task-223).

**The threshold is: anything that takes a worktree gets a session.** Two reasons to
prefer it over a size estimate:

- It is checkable. "Is this big enough to be worth a session?" is a judgement made by the
  party with an interest in saying no; "does this write code?" is not.
- The worktree boundary is already a session boundary in everything but name. A worktree
  exists because a shared clone has one `HEAD`; one session moving between two of them is
  precisely the interleaving the isolation was for.

So: a child that edits files, runs the test gate, or produces a branch gets a session. A
child that is a decision to record, a question to answer, a task to file, or a record to
correct does not — that is task bookkeeping, it takes no worktree, and a spawned session
for it costs more than it saves.

### The supervisor is thin, deliberately

Every layer here is an agent that can be wrong, so a supervisor that re-derives each
child's context to double-check it is not a safeguard — it is a second agent doing the
work, with the context cost this rule exists to avoid.

**You read durable output, not transcripts.** A child's record, its acceptance statuses,
its branch and its diff are the evidence. If the record does not say what happened, the
answer is a handoff back to the child asking it to say so, not archaeology in its
scrollback. You are checking that the child reported and verified its work — not
re-verifying the work.

### How many children fly at once

**Every child whose dependencies are satisfied, up to this machine's run ceiling.** The
walk does this for you; what follows is the rule it implements and the reasons for it,
because you have to be able to read what it did and say whether it was right.

Until task-223 the answer was "one", and it was stated without a reason anywhere —
the only nearby justification was the section above, which argues for a session per child
and is silent on ordering. The cost of the unexamined answer was measurable: of task-269's
eight children, six declared no dependencies at all and were run strictly one after
another.

**Takeoff and landing are different resources, and only one of them is serial.** The work
parallelises, because each child has its own worktree and its own session. The merge does
not, because `main` is one branch — so each child queues for **this repository's merge
runway** inside its own `agentjobs finish`, held across rebase, gate and merge. That is
not fussiness about git, which handles concurrent merges perfectly well and says so when
it cannot. It is the one property the merge gate exists to guarantee: *the commit that
lands is the commit the gate verified*. Two finishers gating against a base the other is
moving cannot both have that.

The runway is the honest cost of this and it is much smaller than the flights: a scripted
finish on this machine's ledger averages about four minutes, against a run of about
thirty. A four-child epic spends roughly sixteen minutes queueing and parallelises the
rest.

#### A rolling frontier, never a wave

At every moment, every child whose `needs` are all satisfied and which is not already
running is started, up to the slot count. **A child completing recomputes eligibility
immediately**, not at the end of a round.

The obvious implementation is waves — find every eligible child, start them all, wait for
all of them, recompute — and it is wrong for the same reason the serial walk was wrong:
it prices a group at its slowest member. A wave of three where one takes an hour and two
take ten minutes leaves two slots idle for fifty minutes with eligible work sitting there.
There is no barrier anywhere in the loop.

Stated as the invariant, which is also what the tests assert: **at no point does an
eligible, unclaimed child exist while a slot is free.**

**"X unblocked me" is not the same as "I am eligible."** The scheduler asks *are all of
this child's needs satisfied?* — never *did the child that just finished name me?*. A
diamond is where those differ: a child with two unmet needs, one of which just closed,
is still blocked. An implementation that pushed newly-freed dependents onto a ready queue
on completion would start it against a prerequisite that has not landed.

#### Which child goes first when the frontier is wider than the slots

**The graph decides eligibility, the queue decides order, and out-degree breaks the ties
the queue does not.**

The queue holds a human's explicit decision about order, and `agentjobs queue move` has to
keep meaning something. Sorting the frontier by out-degree outright — prefer the child
with the most work waiting behind it — is a real scheduling heuristic and it would
quietly overrule every move anybody made, so it is the tie-break rather than the sort. In
practice `(band, queue_position)` is already a total order over open work, so the
tie-break rarely fires at all; that is the intended outcome, not a defect in it.

If you think a child that gates three others should go first, **move it in the queue**.
That is the same answer as everywhere else, and it survives the session.

#### The stop rule, sharpened rather than weakened

The reason a walk stops on a bad child is that a sibling depending on it would be building
on a gap with nobody awake to notice. That argument survives concurrency intact — but note
what it justifies. It justifies *stopping*, which is not the same as never having started.

- **No further child takes off.** Immediately, on the first child that does not close
  `completed`.
- **Children already in flight land.** They were started against a graph that was valid
  when they started, and none of them can depend on the failed child or claimability would
  not have offered them. Killing them throws work away for no safety gain.
- **Nothing that needed the failed child ever starts.** That is automatic rather than
  enforced: its needs are unmet and always will be, so it never enters the frontier.

What this gives up against the old rule is exactly the pessimism about siblings that
provably do not depend on the failure.

#### The ceiling is the machine's, and there is only one

`limits.max_concurrent_runs` in `~/.agentjobs/dispatch.yaml` is the real cap. A child has
been an ordinary dispatch since task-022, so it is counted against that ceiling like
anything else — the older claim that children are uncounted subprocesses stopped being
true then. `dispatch walk --max-concurrent N` can only narrow it.

**Your own supervising run holds one of those slots**, so an epic walk needs at least two
and gets `ceiling - 1` children in the air. Hitting the ceiling mid-walk is **backpressure,
not a refusal**: the walk waits and tries again, because something else on the machine
holding a slot is a normal condition and not a fact about this epic. A machine that stays
full for the whole per-child ceiling does stop the walk, and says which.

#### What it looks like when it is working

```bash
poetry run agentjobs dispatch walk <parent-id> --project <project> --dry-run
```

prints the bounds, the slot count, and exactly which children would take off now. A
finished walk reports how many were in flight at once, and

```bash
poetry run python scripts/run_report.py --epics
poetry run python scripts/run_report.py --epic <parent-id>
```

reports an epic's wall clock, the summed child work, the turnaround idle and the
parallelism actually achieved — `1.00x` being serial. A walk that reports one child in
flight either had a serial graph or never got a second slot, and those are worth telling
apart before concluding anything.

### Run the walk; do not drive the loop by hand

```bash
poetry run agentjobs dispatch walk <parent-id> --project <project>
```

That is the whole of your job between reading the parent and judging its criteria. The
walk keeps every eligible child flying — see [how many at once](#how-many-children-fly-at-once)
— starting each as a real dispatch, watching its task record to a terminal state, and
either topping the fleet back up or grounding it. It **blocks** until it is done, which is
deliberate: a supervisor that ends its turn saying it will check back periodically is not
supervising, and that is not a hypothetical (see below).

`--dry-run` prints the bounds and which children would start now, and starts nothing.
`--max-children N` stops after N, for a first run against a wide epic you want to watch;
`--max-concurrent N` narrows the fleet without narrowing the epic.

**Exit 0 means every open child is done. It does not mean the parent is done** — the walk
never closes a parent, because whether its acceptance criteria are met is the one
judgement in this loop that is not mechanical. Exit 1 means it stopped for cause and
handed the parent's ball to a human; the parent's record says which child and why. Exit 2
means it could not start.

**Children are real dispatches now, and inherit the epic's authorisation** (task-022).
Design §2's rule is unchanged — every run is caused by a stored log entry whose actor is a
configured human — and what the walk does is find *the human entry that authorised this
parent*, write an authorising entry on the child naming that person, that parent and that
entry, and dispatch on the stored row like anything else. The person clicked the epic;
the epic's children were named on its record when they did.

So each child has a run directory, a row in the run ledger, a `dispatch_result`, and the
poller settles and reaps it. It also counts against this machine's concurrency ceiling
alongside your own run, so **an epic walk needs at least two slots** — a machine set to
one refuses the first child, saying so — and that same ceiling is what decides how many
children fly at once.

**The child claims itself** through the dispatcher, as any dispatched task does. Do not
claim it on its behalf.

**Starting a child by hand is still possible and is the exception, not the loop:**

```bash
poetry run agentjobs dispatch child <child-id> --project <project>
```

Same authorisation, same budget, one child, and you watch it yourself.

### The bound, and what spends it

**Two runs per child, per human authorisation of the epic** — the first and one retry —
and it is enforced in the dispatcher rather than asked for in prose. The count comes off
the child's own log, so it survives the walk dying and being restarted, and a walk
somebody starts tomorrow inherits it.

**Only a run that *died* ever spends the retry.** A child that closed with a bad outcome,
or handed its ball to a person, has said something; retrying it would be ignoring it. A
session that vanished has said nothing, and the guide's own rule already covers that
case: a child that dies twice is dying for a reason you cannot see from here.

A human who wants a third attempt authorises the epic again. That resets the budget and
leaves a record that somebody chose to.

**In practice the retry fires less often than you would expect, and that is not a fault.**
A dispatched run that fails is handed to `human`/`decision` by its own run supervisor,
which writes what happened onto the child. The walk then reads that as *parked* -- somebody
has said this needs a person -- and stops without spending a retry. `DIED` is the residual
case: a run whose supervisor never got to write anything, which is what an expired login
or a killed process looks like. That asymmetry is the right way round. A child that said
something and got retried anyway would be a child being ignored.

**A walk that stops for cause also spends the epic's authorisation, deliberately.** It
hands the parent to a human, and that handoff is now the parent's newest entry -- so the
next `dispatch child` or `dispatch walk` is refused `parent_not_human_clocked` until a
person writes on the epic or dispatches it again. One human act buys one walk. A walk that
could restart itself after stopping for a person would not be stopping for a person.

### Supervision, in the four states a child can be in

**The walk does this for you.** What follows is the rule it implements, and it is here
because you have to be able to read what the walk did and say whether it was right — and
because when the walk hands the parent back, the state it stopped in is one of these.

**Watching is a mechanism, not an intention.** A supervisor that ends its turn saying it
will "check back periodically" is not supervising, it is asleep; on 2026-08-19 that is
exactly what happened, and the human found the parked child before the supervisor did.
That incident is why the loop is a blocking command rather than a paragraph asking you to
remember (task-022). If you ever find yourself hand-rolling a poll, you are rebuilding
something that exists.

**Poll the task record, not the process.** `idle`/`done` on a session is the wrong
signal: a child parked on review has a live process and is the one state that needs you.
`ball` is the signal.

| What you see on the child | What it means | What you do |
| --- | --- | --- |
| `ball: agent` | Working | Keep waiting |
| `ball: human` | Parked | Act now — see below |
| `lifecycle: closed` + `outcome` | Finished | Verify, then next child |
| `ball: agent`, process gone, no new log entries | Died | Recover — see below |
| The same, and `dispatch auth-check` exits 1 | Logged out | **Do not restart it** — see below |

**Child finished.** The child closed itself with an outcome — which, where a merge gate
applies, means its work was approved and merged by the session that did it. Verify from
the record and the repository: acceptance statuses filled in, branch marked `merged`, the
merge commit present. Then pick the next eligible child by `dependencies[]` and start it.
Do not re-run the child's verification; do check that it says it ran it.

**Child parked.** The ball is `human`, and which reason it carries decides whether it is
yours at all:

- `human/review` is the merge gate. It is not yours to release, whatever you think of the
  diff, and approving on the human's behalf is the one thing this whole protocol is built
  to prevent. Your job is to make sure the human knows it is waiting.
- `human/decision`, `input` or `spec` is a question. Answer it **only** if the parent's
  own spec already decides it — that is what a parent record is for — and record the
  answer as an `answer` entry on the child threaded to its question. Anything the parent
  does not decide is escalated, not guessed.

Either way, **stop starting children**: the next child may depend on the parked one, and
an unattended run that keeps going past a question is how a wrong answer gets built on.
The walk does exactly this — the first child that is not clean grounds every further
takeoff and none is ever skipped — and it hands the parent to `human`/`decision` with the
reason before it exits, so the parent record does not read `agent/work` while nothing is
happening to it. **Children already in flight are watched down rather than killed**: none
of them can depend on the parked one, or claimability would not have offered them, so
stopping them would throw away work for no safety gain.

**A parked child is also what an epic walk at posture `auto` or `supervised` is supposed
to produce.** The first child hands off for review, the walk stops, and a person
approves. That is the merge gate standing, not the walk failing. Only `autonomous` walks
an epic to the end unattended.

**The posture that decides this is the one the *parent* was dispatched at** (task-316).
A child inherits it, so an epic a person dispatched `autonomous` runs its children
`autonomous` even where the project's own default is narrower -- which is what makes the
sentence above about your own prompt true. It was not true until task-316: children fell
through to the project default, so an epic authorised `autonomous` stopped on its first
child for a review nobody had asked for. If you see that, check the child's `dispatch`
entry -- `posture_source: epic` is the fix working. The walk prints the envelope its
children will get before it starts any of them.

**Child died.** The session is gone, the child's ball is still `agent`, and nothing new
was written to its record. Before anything else, look at what survived: the child's branch
may have commits, and its worktree may have uncommitted work. Then, at most once per
child, start one fresh session with a `ball_prompt` naming what is already committed and
what is left. **One restart, then hand the child to a human** — a child that dies twice is
dying for a reason you cannot see from here, and a supervisor that keeps retrying spends
a night proving it. This is the rule the attempt budget above enforces, and the walk
spends its retry here and nowhere else.

Clean up only what is safe to clean: never force-remove a worktree holding uncommitted
work. Commit it to the child's own branch first so the next session can see it, or leave
it and say so in the handoff.

**Child logged out.** Before you spend that one restart, rule this out — because it is
indistinguishable from a death by looking, and restarting is the one response that cannot
help:

```bash
agentjobs dispatch auth-check <the child's session id>   # exits 1 when it is this
```

Claude Code refreshes its credential in a shared background daemon, not per session.
When that refresh fails the daemon discards the token and **every** `--bg` session on the
machine dies mid-turn, having emitted one line saying `Login expired · Please run /login`.
Two of them happened in two days of heavy use on 2026-08-21 (task-224). A child killed
this way has a live-looking record that simply stops, so a supervisor working from the
table above spends its one restart on a session that dies the same way within a second.

What to do instead, in order:

1. **Do not restart, and do not start the next child.** Every child you start now dies
   identically.
2. **Hand the parent off** to `human`/`input`, saying that a login expired and naming
   `claude auth login` as the fix. That is the whole recovery and the human cannot guess
   it — answering inside the session does not work, because the credential is already
   gone and anything sent to it is retried against nothing.
3. **Nothing is lost.** After the re-auth, a message to the stalled child wakes it and it
   resumes in place. Children that AgentJobs dispatched are handed back automatically by
   the poller; the ones you started yourself are yours to nudge.

A walk cannot tell this apart from an ordinary death either, so it will spend the child's
retry on it and then stop — with both attempts recorded on the child and the reason on the
parent. That is a bounded waste rather than a night of them, and `dispatch auth-check` on
the child's session id is the first thing to run when a walk stops on two deaths in a
row.

**Parent idle.** While a child runs, do nothing that costs context. That is not idleness
for its own sake — your context is the resource this rule protects, and spending it while
waiting is the same failure as working the children yourself, arrived at politely.

Permitted: the poll itself, `dispatch auth-check`, and writing progress to the parent
record. Not permitted: starting a second child, reading the running child's diff "to be
ready", or pre-loading the next child's context. Read the next child's record when it is
the next child.

### Closing the parent

When no unfinished child remains, the parent is not automatically done. Evaluate the
parent's own acceptance criteria against the children's durable evidence, do any
parent-level verification the record calls for, and close it only where that evidence
supports it. Children finishing is not the same as the parent's criteria being met.

**This is the step the walk deliberately leaves to you**, and the only one. Everything
before it — which child, start it, watch it, judge it, continue or stop — is mechanical
and is done by code. This one is a reading of evidence against criteria, so a walk that
did it would be an agent grading an epic on the strength of its children having stopped.

## Task records are readable generated state

Read a record whenever you want — `task_get` over MCP, `agentjobs show`, or the UI;
reviewing a task means opening it. But **do not edit them** directly. Every change goes through a managed interface — the
[MCP tools](mcp.md), the REST API, the CLI, or the web UI — which all reach the same
code path: strict validation, a per-task lock, and a log entry recording who moved what
and why. A direct edit skips all three and produces a record that looks right and is
not. That is not hypothetical: a task once written directly with `lifecycle: active`
and no `ball` logged no transition, failed no validator, and disappeared from every
listing as a broken file.

If a managed operation fails, diagnose the error — every one carries a code and a
suggested action. A failing tool is not permission to write to the store yourself.
Direct repair is an
emergency procedure for a maintainer, requires a stated reason, and is followed by
`agentjobs validate`.

Agents with MCP available should prefer it for every task read and write; the REST API
and CLI are the fallback when it is not.

## The Core Model

Schema v2 separates questions that v1 compressed into one `status` field:

| Field | Question | Examples |
| --- | --- | --- |
| `lifecycle` | Where is the task in its life? | `draft`, `ready`, `active`, `closed` |
| `ball` | Who acts next? | `agent`, `human`, `external` |
| `ball_reason` | Why do they hold it? | `work`, `review`, `decision`, `dependency` |
| `ball_prompt` | What must that holder do next? | A concrete, self-contained ask |
| `outcome` | How did a closed task end? | `completed`, `cancelled`, `superseded`, `duplicate` |

Every open task has a ball holder. Every non-available handoff has an ask. UI labels
such as "Needs review" or "Blocked" are computed from these fields; they are not stored
state.

## Resume Without Chat History

A fresh agent session resumes from the record alone, in the order defined by the
[resumption contract](schema-design.md#the-resumption-contract):

1. Read `spec`. `spec.summary` gives a one-or-two sentence orientation for a
   zero-context reader; `spec.description` is the detailed working specification.
2. Read the state axes and `ball_prompt` to learn who acts now and the immediate ask.
3. Read `log[]` newest-first: begin with the latest `handoff`, preserve every binding
   `decision`, and identify unanswered `question` entries.
4. Read `acceptance[]` to learn what done means and what has already been verified.

Also inspect `deliverables[]`, `dependencies[]`, `parent`, and `branches[]` when they
apply. Before ending a session, write every resumption-critical fact to the log and make
the `ball_prompt` current. A handoff is defective if the next participant needs the chat
transcript to discover what happened, why a decision was made, or what to do next.

## Work What the Queue Says Is Next

The backlog has a stored order — `queue_position`, an integer inside a priority band —
not a sort over timestamps. Selection is `(priority_rank, queue_position)` and nothing
else, so logging progress on a task no longer promotes it.

```python
from agentjobs import TaskClient

with TaskClient() as client:
    task = client.get_next_task(agent="my-agent")
    why = client.explain_next_task(agent="my-agent")
```

`explain_next_task()` — `agentjobs next --why`, or the `queue` field of the MCP
`task_next` result — returns the band and position the winner stands at, the empty bands
checked above it, and every open task ahead of it with the claimability rule that
excluded each. Read it before concluding the order is wrong: a task missing from the
answer is usually blocked, claimed, or holding open children rather than mis-placed.

**If you think something else should be first, move it**, so the next session inherits
the decision rather than re-deriving it:

```bash
agentjobs queue list                      # the reviewable order, band by band
agentjobs queue move task-045 --top       # or --before/--after <id>, or --bottom
```

```python
client.operations.queue_move("task-045", actor="my-agent", operation_id=str(uuid4()),
                             expected_revision=task.updated, top=True,
                             body="Blocks the release; the rest of high can wait.")
```

Over MCP that is `task_queue_move`, which takes an `actor` and an `operation_id` like
every other mutation and accepts a placement — a neighbour or an end of the band —
rather than a number. Every route appends a `queue_move` log entry, which is the only
record of *why* the order changed.

**A move answers back.** It always lands — nothing refuses one — but if the order you
just wrote cannot execute, the reply says so: the task you promoted is one the queue
will skip, it now stands ahead of something it needs, what it displaced is gating other
work, the band came out as it went in, or the band is corrupt. Deterministic, immediate,
and silent on an ordinary move. Read it rather than assuming a clean exit code means a
useful reorder.

Three things not to do instead:

- **Do not add a `needs` dependency to express order.** Dependencies are prerequisites.
  A false one makes the task unclaimable until the other closes, deadlocks the graph if
  it ever points both ways, and lies to every reader who takes it at face value.
- **Do not hand-edit `queue_position`.** There is no setter for it, for the same reason
  there is no `set_lifecycle`: the number is a consequence of a decision, and the record
  should show the decision. A hand-written number can also collide with another open
  task in the band, which is corruption selection refuses to answer over.
- **Do not rely on a chat instruction to reorder work.** Chat does not survive the
  session; the queue does, and it is what the next agent reads.

A broken queue is reported, never guessed past — but **what you catch depends on which
side of the wire you are on**, and this guide is the client side:

| Caller | What a broken queue does |
| --- | --- |
| `TaskClient.get_next_task()` | raises `TaskClientError` with `status_code == 409` |
| `TaskManager.get_next_task()`, in-process | raises `queue.QueueCorruptionError` |
| REST | `409 Conflict`, naming the offending ids and the repair command |
| MCP | the `queue_broken` error code |

`QueueCorruptionError` is **not** exported from the `agentjobs` package and is not what
the HTTP client raises, so `except QueueCorruptionError` around a `TaskClient` call is
code that cannot catch anything. Catch `TaskClientError` and read `status_code`:

```python
from agentjobs import TaskClient, TaskClientError

with TaskClient() as client:
    try:
        task = client.get_next_task(agent="my-agent")
    except TaskClientError as exc:
        if exc.status_code == 409:
            raise SystemExit("Queue is broken; run `agentjobs queue repair`") from exc
        raise
```

`agentjobs queue check` shows the whole picture and `agentjobs queue repair` fixes it,
stating everything it guessed.

## Canonical Agent Loop

```python
from agentjobs import Ball, BallReason, TaskClient

agent = "my-agent"

with TaskClient() as client:
    task = client.get_next_task(agent=agent)
    if task is None:
        raise SystemExit("No claimable task")

    task = client.claim_task(task.id, agent=agent)

    # Work in the task branch, record decisions, and verify the result.
    client.add_progress_update(
        task.id,
        agent=agent,
        summary="Implemented and verified the requested change",
        details="Changed src/feature.py. `poetry run pytest` passed.",
    )

    client.handoff_task(
        task.id,
        actor=agent,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Review branch feat/task-123-feature. Approve the merge or request "
            "specific changes. Tests: `poetry run pytest` passed."
        ),
        body=(
            "Implemented the feature and added regression coverage. The branch is "
            "complete; no merge has been performed."
        ),
    )
```

The claim is atomic: one eligible agent wins and other claimants receive an error.
`get_next_task()` returns only ready, eligible tasks with no unmet `needs` dependency
and no open child tasks.

The worktree and branch come before the claim — see [above](#before-you-write-anything-take-your-own-worktree).
A task write dirties nothing and is committed nowhere, so only code and documentation go
on the task branch. The older rule — task metadata committed on `main`, never on the
branch, because a handoff committed to a branch was invisible to the human it was
addressed to — described records kept as files, and
[the storage guide](storage-sqlite.md#what-a-record-still-is) says what replaced it.
Repository contributors must also follow `ALLAGENTS.md` and `ENGINEERING.md`.

## Resume an Existing Task

Do not assume an open task belongs to the current conversation. Fetch it and reconstruct
the state from the record:

```python
from agentjobs import Ball, TaskClient

with TaskClient() as client:
    task = client.get_task("task-123-feature")

    if task.ball is not Ball.AGENT:
        raise SystemExit(
            f"Do not work yet: {task.ball.value} holds the ball. "
            f"Current ask: {task.ball_prompt}"
        )

    latest_handoff = next(
        (entry for entry in reversed(task.log) if entry.type.value == "handoff"),
        None,
    )
```

Then follow the reading order above. A human approval or change request is itself a
handoff entry, so a new session does not need the conversation in which it was given.

## State Verbs and Handoffs

Use a state verb for every ownership change. Do not patch `lifecycle`, `ball`,
`ball_reason`, or `outcome` directly; manager verbs enforce consistency and append the
transition history.

### Human Review, Approval, Input, or Decision

At any human-decision point:

1. Record what changed, decisions made, verification performed, and remaining risk in
   the task log.
2. Call `handoff_task()` with `ball="human"`, the precise reason (`review`, `approval`,
   `decision`, `input`, or `spec`), and a self-contained `ball_prompt`.
3. Commit the task-record update where the project workflow requires it.
4. Notify through whatever interactive channel is available today: the chat reply and,
   when the host provides it, push notification. The notification is only a wake-up
   signal; all substance belongs in the task record.
5. Stop. Do not merge or make the decision on the human's behalf.

#### Name your links, and write them on their own lines

**An address in a `ball_prompt` goes on a line of its own, introduced by a name and a
colon, and the prose refers to it by that name** (task-363):

```
Three checks; the third needs a task page rather than the list.
  1. Open a task from the sidebar and confirm the list keeps its scroll position.
  2. Narrow the window to phone width; the panel should restack.
  3. Open the task page below and confirm the log expands.

Desktop shell: http://127.0.0.1:8910/app/
Tablet: http://127.0.0.1:8910/app/?w=1024
Task page for check 3: http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143
```

The review panel reads exactly that shape. A **link line** — an address alone on its
line, optionally `Name: ` in front of it, optionally bulleted — is lifted into a "Links
for this review" card above the questions and the verbs, titled with the name, and
**removed from the prose**, so the address is a target rather than something to retype
and is on screen once instead of twice. `links[]` entries whose `rel` is `pr` or
`build` join the card too, titled by their own `title`.

Two shapes the panel cannot help with, both of which `task_handoff` warns about:

- **An address inside a sentence** stays where you wrote it. Lifting it would leave a
  hole in the sentence and duplicating it puts a 60-character URL on a small screen
  twice, so neither happens — the fix is yours, at the moment you write the handoff.
- **A bare address on its own line** is lifted but cannot be titled, so the reviewer
  gets a row that does not say where it goes. Only you know that one of three
  identical-looking ports is the tablet one.

The first version of the card showed every address in both places. Reviewed on
2026-09-06: it "wastes too much space", and the state that read best was the one where
the links were only in the card. That is the rule this convention exists to satisfy.

**Ask in options, not in paragraphs** (task-017). A handoff may carry `questions[]`,
each a question with the answers you are offering and which one you recommend, written
in the same mutation as the handoff:

```python
client.handoff_task(
    task_id,
    actor="claude",
    ball="human",
    ball_reason="decision",
    ball_prompt="Four decisions before I can carry on; each recommendation is marked.",
    questions=[
        {
            "body": "How long before an idle session counts as stalled?",
            "placeholder": "a number of minutes",     # for a value rather than a choice
            "options": [
                {"label": "15 minutes", "description": "More false positives."},
                {"label": "4 hours", "description": "Only catches overnight stalls.",
                 "recommended": True},
            ],
        },
    ],
)
```

The GUI renders each as buttons with a free-text box always beside them, **on the task
page by default with nothing to click open**, and one Submit writes an `answer` entry
per question threaded by `re`. **This is the difference between
a decision answered in seconds on a phone and one that waits a day**, which is what the
same four questions cost on task-077 as prose. Two things follow from it:

* **Offer options wherever you honestly can**, including for a number — supply a
  `placeholder` and let the free-text box take it. An option list is never closed, so
  offering three is not a claim there is no fourth.
* **A question you never got an answer to stays open**, and stays in the human's form
  next time. If you carried on without it, close the thread yourself with an `answer`
  entry saying what you assumed — silence is the one outcome that helps nobody.

The React UI records what the human actually did, and each control writes the reason
that matches its label.

**Approval does one of two things, depending on the machine.** By default it hands the
ball back as `agent/work` with instructions to rebase, merge, update branch metadata and
close — the agent performs the merge. Where the project has `finish.enabled`, the same
click instead runs the scripted finish (task-241): rebase, the full gate, `--no-ff`
merge, rebuild, restart, verify, close, with no agent in the loop. A person approves per
task either way; what varies is who performs the merge afterwards. If you are woken after
an approval, **read the record before acting on memory** — it says whether `main` moved.
See [the dispatch design, §5a](agent-dispatch-design.md).

Approval also takes an **optional note**, which rides verbatim in `ball_prompt` and the log *in addition to* the merge
clearance, never instead of it. The other three send-back controls differ only in the
reason they record, and every one of them preserves its note in both `ball_prompt` and
the handoff log:

| control | writes | read it as |
|---|---|---|
| Request Changes | `agent/revise` | the work needs changing; come back for another review |
| Answer Questions | `agent/answer` | here is what you were waiting for; resume, prior work stands |
| New Instructions | `agent/redirect` | the direction changed; re-read the prompt, prior work stands |
| Hold | `agent/hold` | **stop.** Do not work this until the stated condition is met |

The panel offers only the controls that are true of the task in front of it: a task at
`human/decision` gets no Approve button, because there is nothing to merge. A held task
shows a Resume control instead of the review controls, and refuses a dispatch until it
is released.

### External Block

If claimed work cannot proceed, hand off to `external/dependency` for another task or
`external/service` for a third party, outage, or provisioning step. State the exact
unblocking event in `ball_prompt` and record what was tried. A ready task with an unmet
`needs` dependency stays ready and is simply not claimable; do not duplicate that fact as
stored blocked state.

**A handoff to a human must be actionable the moment they read it.** The dashboard
counts by ball, not by prose: a task at `human/decision` whose prompt opens "do this
after task-340 merges" calls the person over to find there is nothing they can do yet.
If the act waits on another task, give it a `needs` dependency on that task and hand it
to `external/dependency`; whoever closes the prerequisite hands it to the human, and only
then is there something to click. Task-347, 2026-09-05, is the incident.

### Release or Close

- `release_task()` returns active work to `ready` / `agent/available` and clears the
  owner. Use it when bowing out, not when waiting on a named participant.
- `close_task()` ends the lifecycle and records an outcome. A closed task has no ball.
  Closing as completed follows verification and, where required, explicit approval.

## Durable Logging

The unified `log[]` replaces v1's status updates, comments, and follow-up prompts.

```python
from agentjobs import TaskClient

with TaskClient() as client:
    client.add_log_entry(
        "task-123-feature",
        actor="my-agent",
        type="decision",
        body=(
            "Used the existing cache abstraction because it preserves invalidation "
            "semantics. Rejected a second cache client because it would split policy."
        ),
    )
    client.add_log_entry(
        "task-123-feature",
        actor="my-agent",
        type="question",
        body="Should failed imports be retried automatically?",
    )
```

Use `progress` for work and verification, `decision` for a choice plus reasoning and a
rejected alternative, `question` and `answer` with `re` for open threads, and
`instruction` for a durable directive. State changes create their own `transition` or
`handoff` entries; callers cannot forge transitions directly.

## Paraphrase a person, never quote them

A task record states what somebody **meant**, not the words they used, and never
reproduces the tone of a remark. The one-paragraph version of the rule is in
[ALLAGENTS.md](https://github.com/jeffposey/agentjobs/blob/main/ALLAGENTS.md#paraphrase-a-person-never-quote-them); this is the rest
of it -- why, what enforces it, what that enforcement cannot see, and how to fix a
record that already carries one.

**The reason is the remote.** Records here are public, they outlive the conversation
they came from, and an agent summarising what a person said reaches for the quotation
marks because a verbatim line makes the point faster. In a private notebook that is
harmless. In a public record it reads as a characterisation of a real person rather
than as engineering, and the substance -- which is all the next reader needs -- was
never the part that needed quoting.

Quote where the exact wording **is** the subject: an API name, a spec sentence being
disputed, a message being debugged, a docstring being argued with. Everywhere else,
say what was meant. "Rejected the section outright" carries everything.

### Three tiers, escalating with how durable the damage would be

| Where | What it does | What you see |
|---|---|---|
| `agentjobs.record_check` | Warns the author, on the write that added the text | A `quoted_remark` warning in the tool result |
| `tests/test_task_corpus.py` | Fails the gate over `tasks/` | The failure names the regions |
| `agentjobs.sqlstore.importer` | Refuses the import, having written nothing | `QuotationPolicyError`, naming the regions |

Each names a region -- `task-286: log[5].body at offset 218 (informality)` -- and never
the remark. A tool result, a CI log and an exception string are all places the quotation
would become durable again, which is the same mistake one layer out.

The importer **refuses rather than quarantining**, which is a deliberate departure from
how it handles a record it cannot parse. The quarantine table holds the whole file in
`raw_text`, so quarantining a record for its *content* would put the content in the
database in the same act that claimed to keep it out. `enforce_quotation_policy=False`
is the escape for an operator who has read the hits and judged them; the report then
says the policy was off and names everything it let through.

### What the detector catches, and what it does not

It fires on the intersection of a **quotation shape**, a **tone marker**, and -- for
everything but profanity -- an **attribution cue** near the quotation. Two signals
rather than one, because this corpus holds thousands of legitimately quoted technical
phrases and a detector that fired on quotation marks would be switched off within a day.
Measured on 2026-09-07 over 373 records: ten findings, all of them real.

It is a floor, not a definition, and these are the gaps:

- **Tone that uses no listed word.** A verbatim quotation in calm, plain language passes,
  and so does a sarcastic one built out of ordinary words.
- **A paraphrase that keeps the tone.** Nothing mechanical reads unquoted prose for
  register; that half of the rule is yours.
- **A quoted machine message.** An error string with a listed word in it, sitting after
  a reporting verb, is a false positive. Rare rather than impossible.
- **A two-word shout.** Two capitalised words in a row is a heading or a status label far
  more often than a voice, so the bar is three.

`src/agentjobs/quotation.py` is the authority on all of this and says the same thing at
more length.

### Fixing one

```bash
poetry run agentjobs quotations              # every region worth looking at; exit 1 if any
poetry run agentjobs redact task-286 \
    --field 'log[5].body' \
    --replacement-file paraphrase.md \
    --reason 'verbatim quotation of a person'
```

`redact` is **the only verb that reaches a log entry.** The log is append-only, which is
right for a record of what happened and has no answer at all for text that should never
have been written; before this the only route was to hand-splice the YAML, which the
managed-write guard exists to stop and which leaves the file non-canonical and the
removal unrecorded. It addresses `title`, `ball_prompt`, `spec.<name>` and
`log[<id>].body`, refuses anything else rather than guessing, appends a note saying what
was redacted and why and how many characters went -- never the text, and never a hash of
it, since a hash of a short phrase is not one-way in any useful sense -- and re-writes
the record canonically, whichever backend holds it.

**You supply the replacement, and it should say what the removed text meant.** There is
no black-bar mode on purpose: a redaction that loses why a task exists is a worse record,
not a safer one. Use `--replacement-file` for anything longer than a sentence.

## Querying the Queues

```python
from agentjobs import TaskClient

with TaskClient() as client:
    ready = client.list_tasks(lifecycle="ready")
    human_inbox = client.list_tasks(ball="human")
    externally_blocked = client.list_tasks(ball="external")
    high_priority = client.list_tasks(priority="high")
    task = client.get_task("task-123-feature")
    matches = client.search_tasks("cache invalidation")
```

The human inbox is `ball=human`, not a stored waiting status. The blocked list is
`ball=external`, not a stored blocked status.

## Creating a Self-Sufficient Task

```python
from agentjobs import TaskClient

with TaskClient() as client:
    task = client.create_task(
        title="Add bounded retry handling",
        summary=(
            "Import jobs currently fail permanently on transient upstream errors; "
            "add bounded retries while preserving non-retryable failures."
        ),
        description=(
            "Retry HTTP 429 and 5xx responses up to three times with capped backoff. "
            "Do not retry validation failures. Add deterministic tests."
        ),
        priority="high",
        category="infrastructure",
        lifecycle="ready",
        eligible=["my-agent"],
    )
```

`spec.summary` is not a role-specific "human field" and the description is not an
agent-only field. Both audiences use the same record: the summary provides orientation;
the description supplies detail.

## Notifications and Future Extension

AgentJobs currently relies on the active host's available channel--chat and, when
available, push notification--to alert a human after the durable handoff is written. It
does not yet provide a general email, SMS, mobile-push, desktop-toast, or accounts
service.

The intended extension point already exists in `src/agentjobs/webhooks.py`. Webhooks are
HMAC-signed, and schema v2 emits `task.handoff` with the ball holder and `ball_prompt`.
A future pluggable notification service can subscribe to handoffs where `ball=human` and
route them to configured channels. This is the schema-v2 replacement for the older
`task.status_changed` extension point; the receiver and account/channel model remain
explicitly out of scope here.

## Errors and Server Setup

```python
from agentjobs import TaskClient, TaskClientError

try:
    with TaskClient(base_url="http://localhost:8765", timeout=60) as client:
        task = client.get_task("task-123-feature")
except TaskClientError as exc:
    print(f"AgentJobs request failed: {exc}")
```

AgentJobs is not yet published to PyPI. Install it from a clone and open the primary
React application:

```bash
poetry install
npm --prefix frontend ci && npm --prefix frontend run build
poetry run agentjobs open
```

`agentjobs serve` is the foreground-server form. Both serve the packaged React app at
`/app/`; neither needs Node **at runtime**, which is not the same as not needing it from
a clone — the bundle is gitignored, so a clone builds it once with the `npm` line above
and a release wheel ships with it already built. `open` checks for it and refuses with
that command rather than opening a browser onto a 404.

See the [task schema reference](task-schema.md), [API reference](api-reference.md), and
[schema-v2 design](schema-design.md) for the complete field and endpoint contracts.
