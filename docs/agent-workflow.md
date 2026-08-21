# Agent Workflow Guide

AgentJobs is a durable handoff protocol for agents, humans, and external dependencies.
The task YAML is the source of truth. Chat can wake a participant or make an interactive
session convenient, but it is never required working memory.

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

Work there. Remove it once your branch is merged; `git worktree list` is the inventory,
and one left behind for a closed task is litter.

**Run that command. Do not use a built-in worktree tool to get one.** Claude Code has an
`EnterWorktree` tool that looks like the right way to satisfy the paragraph above, and it
is not: it asks to relocate the session's permission root outside `.claude/worktrees/`,
which is an escalation the `auto` classifier declines and a background session has no
terminal to answer. The run then waits for an answer that cannot arrive — observed
2026-08-20 on the first dispatch after the `-w` change, which parked on that prompt
before it wrote a line. The same applies to anything else that moves where the session is
allowed to act. `git worktree add` is an ordinary shell command, needs no relocation, and
leaves you able to `git -C` the shared clone, which is where your task records go.

If a worktree for this task already exists from an earlier run, `git worktree add` will
refuse it. That is not a reason to reach for the tool — use the existing path, or take a
new one under a different name.

This used to be arranged for the agent. Dispatch passed Claude Code's `-w` flag, which
put the session in a worktree the CLI managed, and containment was mechanical. It cannot
any more: the isolation that flag grants is enforced by a guard that refuses **every**
git operation aimed at the shared checkout — by `-C` and by `cd` alike — and task records
are committed there. A run isolated that way could do the work and then be unable to
record it. So the containment is unchanged in what it protects; taking it is now your
first act rather than the launcher's. The full argument, with the reproduction, is in
[the dispatch design](agent-dispatch-design.md).

## Working a parent task: you supervise the children, you do not work them

A task with an open child is an epic. **Whoever holds it starts a separate session per
child and stays running as the supervisor.** You do not work a child in your own session,
however small it looks, and you do not work two at once.

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

The reason is context, not parallelism — the loop is still one child at a time.

A session that works four children carries four children's worth of exploration by the
fourth, and the transcript a handoff was supposed to replace is exactly what the next
session cannot read. Every child worked in its own session ends with its findings in a
place the next reader can actually open: the task record. The supervisor's own context
stays small enough to still be a supervisor at the end of the epic.

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

### Starting a child

Start the child the way you were started: your runner's CLI, backgrounded, carrying the
ordinary worker prompt with the child's id in it — the one at the top of this guide, not
the supervisor prompt you were given. With Claude Code that is:

```bash
claude --bg --remote-control --permission-mode auto "<the worker prompt for that child>"
```

**The child claims itself**, as the ordinary lifecycle says — worktree, branch, then
claim. Do not claim it on its behalf: you would take the ownership its own session then
cannot, and the first thing it did would be to fail a claim on a task it is already
working.

**You cannot use AgentJobs dispatch to start it, and that is by design.** A dispatch must
be caused by a stored log entry a human wrote (design §2, D4) — it is what makes
agent-starts-agent impossible rather than merely capped — and nothing you write satisfies
that. The consequence is worth knowing rather than discovering: **the children you start
are your own subprocesses, not AgentJobs runs.** They have no run directory, no entry in
the run ledger, no `dispatch_result`, and the poller will not settle or reap them. What
holds instead is the task record, which is what you should be watching anyway.

### Supervision, in the four states a child can be in

**Watching is a mechanism, not an intention.** A supervisor that ends its turn saying it
will "check back periodically" is not supervising, it is asleep; on 2026-08-19 that is
exactly what happened, and the human found the parked child before the supervisor did.
Poll in a backgrounded wait that exits on the condition.

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
If your own turn is ending while the child is parked, hand the parent off first —
`external/dependency`, naming the child — so the parent record does not read `agent/work`
while nothing is happening to it.

**Child died.** The session is gone, the child's ball is still `agent`, and nothing new
was written to its record. Before anything else, look at what survived: the child's branch
may have commits, and its worktree may have uncommitted work. Then, at most once per
child, start one fresh session with a `ball_prompt` naming what is already committed and
what is left. **One restart, then hand the child to a human** — a child that dies twice is
dying for a reason you cannot see from here, and a supervisor that keeps retrying spends
a night proving it.

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

## Task YAML is readable generated state

Read the task files whenever you want; reviewing a task means opening it. But **do not
edit them**. Every change goes through a managed interface — the
[MCP tools](mcp.md), the REST API, the CLI, or the web UI — which all reach the same
code path: strict validation, a per-task lock, and a log entry recording who moved what
and why. A direct edit skips all three and produces a record that looks right and is
not. That is not hypothetical: a task once written directly with `lifecycle: active`
and no `ball` logged no transition, failed no validator, and disappeared from every
listing as a broken file.

If a managed operation fails, diagnose the error — every one carries a code and a
suggested action. A failing tool is not permission to edit YAML. Direct repair is an
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

A broken queue is reported, never guessed past: `get_next_task()` raises
`QueueCorruptionError`, REST answers `409`, and MCP returns the `queue_broken` code.
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
For AgentJobs repository work specifically, task metadata is updated and committed on
`main` while code and documentation stay on the task branch, and the commit that records
a handoff must land on `main` or the human it is addressed to cannot see it. Repository
contributors must also follow `ALLAGENTS.md` and `ENGINEERING.md`.

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

The React UI records what the human actually did, and each control writes the reason
that matches its label. Approval hands the ball back as `agent/work` with instructions
to rebase, merge, update branch metadata, and close — and it now takes an **optional
note**, which rides verbatim in `ball_prompt` and the log *in addition to* the merge
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
poetry run agentjobs open
```

`agentjobs serve` is the foreground-server form. Both serve the packaged React app at
`/app/`; neither needs Node at runtime.

See the [task schema reference](task-schema.md), [API reference](api-reference.md), and
[schema-v2 design](schema-design.md) for the complete field and endpoint contracts.
