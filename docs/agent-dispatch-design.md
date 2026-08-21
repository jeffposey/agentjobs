# Agent Dispatch — Design Proposal

**Status: ACCEPTED — D1–D3 resolved with Jeff on 2026-08-10 (§11). Nothing here is
implemented; implementation tasks are derived in §13.**

**Amended 2026-08-11** after a read-only headless run against task-069 tested the
assumption this whole design rests on — that a zero-context agent can resume from the
task record alone. It could, and it found three defects in the design that dispatched
it: the prompt stub pointed at a dead document (§4), the human-clocked rule read
evidence it never required to be unforgeable (§2), and permission posture was never
specified (§4). Corrections are marked inline and dated. Nothing in D1–D4 changed.

Produced under task-060. This document is the deliverable of the dispatch design pass:
how a human decision recorded in AgentJobs turns into an agent actually running, what
keeps that safe, and what was rejected on the way. Implementation happens in separate
tasks derived from §13.

---

## 1. The gap

AgentJobs is a good **record** of agent work and a good **notifier** of humans. It does
not yet cause work to happen. The loop today:

```
agent works ──► ball: human ──► human sees it ──► human opens a chat window,
  ▲                                               re-states the context, starts an agent
  │                                                          │
  └──────────────── the human is the message bus ────────────┘
```

Task-046 addresses the left-to-right half and calls chat "a bootstrapping convenience
for now" without scheduling its removal. Nothing addressed the return path.

The return path is what separates this from a tracker with AI-flavoured fields. Jira
with an MCP server can already record that a human approved something. What it cannot do
is turn that approval into a running agent, in the right directory, with the right
context. **Dispatch is the feature that makes the schema load-bearing rather than
descriptive.**

Two things make it cheap to build correctly now, and they are the reason the design pass
was worth doing before the code:

- **`ball: agent` is already a dispatch queue.** Schema v2 made "who acts next" a
  required, queryable fact. A dispatcher does not need to infer intent; it reads it.
- **The resumption contract (schema-design §5) already guarantees a task record is
  sufficient to resume from.** So a dispatcher does not need to compose context. It
  needs to start a process pointed at a task id. That single observation removes most of
  the apparent difficulty — see §4.

---

## 2. The governing rule: the loop is human-clocked

Everything else in this document is subordinate to one rule.

> **A dispatch may only be caused by a log entry whose actor is a human.**
> An agent handoff never causes a dispatch, in any mode, ever.

!!! danger "Superseded 2026-08-11 by D5 — read §2a before implementing this"
    Jeff amended D4 the day after approving it. The rule above was right about the
    *property* it protected and too narrow about the mechanism. It is retained here
    because the reasoning below is still the reasoning; **§2a states what actually
    governs.** Where the two conflict, §2a wins.

!!! warning "The code enforced the superseded rule until 2026-08-20 (task-188)"
    Stated plainly, because the gap is the whole reason task-188 exists.
    `assert_human_clocked` was written against §2 as first drafted and never revisited
    after D5, so for nine days the dispatcher refused any task whose newest stored log
    entry was not a human's. Measured against this project's own backlog on 2026-08-20:
    **72 of 74 open tasks were refused**, 68 of them because the `transition` entry the
    manager writes when an agent files a task is attributed to that agent. Every
    agent-filed task failed the check from birth, and the remedy — a human writing a
    note by hand before every run — was ceremony, not safety.

    What changed is *where the entry comes from*, not what is checked. A caller that
    names the human clicking gets that human's authorising entry **written to the task
    record**, and the rule below is then evaluated against the stored entry exactly as
    it always was. Nothing in the forgeability section is relaxed; see
    [What is checked, and what is merely claimed](#what-is-checked-and-what-is-merely-claimed).

The feared failure mode is the circular one: agent finishes → something starts an agent →
it finishes → ... unbounded tokens, unbounded writes to a repository. The usual defence
is counters and cooldowns, which bound the blast radius of a loop that is still, in
principle, permitted to exist.

This rule makes the loop **structurally impossible** instead. The cycle physically
cannot advance without exactly one human act per turn. Counters and cooldowns remain
(§7), but demoted to what they should be: a backstop against a bug in the dispatcher
itself, not the primary defence.

The price is real and worth stating plainly: **"agent finishes, next agent picks up
automatically" is permanently off the table.** No chained autonomy, no overnight queue
that drains itself. Every turn of the wheel costs one click. That was accepted (D1)
as the correct trade for a system that spawns processes with commit access on a personal
machine.

A useful consequence: the rule is checkable in one line at spawn time — resolve the log
entry that caused this dispatch, look up its actor in the project's actor vocabulary,
refuse unless `kind == human`. It is not a policy that has to be maintained across the
codebase; it is a precondition on one function.

## 2a. What actually governs: bounded autonomy (D5, 2026-08-11)

> **No autonomous cycle runs unbounded.**
> A chain of dispatches may proceed without a human act per turn, provided a human
> authorized the chain in advance with a bound the loop can evaluate *itself*.

D4 guaranteed boundedness by requiring a human at every turn. That works, and it costs a
click per cycle for no safety gain once the real property is named. The property is
**boundedness**, not human causation. A human-set bound declared up front delivers it
just as completely and is strictly more useful.

An authorized chain must carry all three:

1. **A terminating condition the loop can evaluate.** Executable acceptance checks — a
   command per criterion whose exit code decides met/unmet. **A chain with no evaluable
   termination condition is refused, not capped.** This is the load-bearing clause: it
   is what makes the loop stop *because it is done* rather than because it ran out of
   allowance.
2. **A maximum iteration count**, set at authorization time.
3. **A ceiling** — wall-clock, and budget where the runner reports one.

Guardrails on top, all of which fail loudly to `ball: human` with the reason named:

- **Thrash detection.** If N consecutive iterations leave the check results unchanged,
  stop. A loop that is not converging is not working, and iteration count alone will not
  notice.
- **§7's caps now bind here, counting chains rather than iterations.** They were scoped
  to auto-dispatch (D3) because a human clicking is a decision; a loop is not clicking.
  *Amended 2026-08-18 by task-078 (decision L7), because as first written this clause
  made the feature inert:* the per-task-per-day cap is 3, so a chain a human authorized
  for 5 iterations would have been refused at iteration 4 by a limit meant for a
  different mechanism. So **per-day counts authorized chains**, the **lifetime cap keeps
  counting dispatches** — it is the backstop against a bug in the loop driver itself, and
  a backstop redefined to accommodate what it guards is not one — and the **cooldown does
  not apply within a chain**, since iteration *n+1* begins only after iteration *n* has
  reached a terminal state, which is the condition the cooldown exists to guarantee.
- **Regression guard.** A criterion that was `met` and becomes unmet stops the chain. An
  agent that breaks a passing check to make a failing one pass is going backwards.
- **The authorization is the human act.** §2's forgeability requirement (below) applies
  to it unchanged — the authorizing entry is resolved from the stored task, never from
  the request.

What this does *not* license: a standing queue that drains itself, or a chain whose
termination condition is prose a human has to read. Those remain refused. The
distinction that makes loops safe is the same one that makes them worth running — they
pay off exactly where a cheap objective oracle exists (tests, lint, typecheck) and not
where "good" requires taste.

Mechanism designed in **task-078** — see
[agent-loops-design.md](agent-loops-design.md), which specifies the
`acceptance[].check` schema change this rule depends on, answers where a check may run
and who may set one, and picks the numbers behind every bound above. Nothing here is
implemented.

### The rule is only as good as the evidence it reads (added 2026-08-11)

The check resolves a log entry and trusts its `actor` field. So the rule's integrity
rests entirely on **log entries being unforgeable**, and as originally written this
design never said they were.

The precedent is already in the codebase. `manager.add_log_entry` refuses to append a
`transition` entry (`manager.py:479`) on the grounds that a transition not accompanying
a real state change is a lie. `dispatch` entries have exactly the same property, with a
sharper consequence: a caller who can POST an arbitrary log entry can write one naming a
human as its actor, then cite it as `caused_by` — and the one rule this entire design
rests on is satisfied by a fabricated record. Every counter in §7 is downstream of the
same evidence.

**Therefore:**

- `add_log_entry` must refuse `dispatch` and `dispatch_result` for the same reason it
  refuses `transition`. Both are written by the dispatcher as a side effect of a real
  event, never by a caller. (task-069 defines the types; task-071 enforces this.)
- The `caused_by` entry must be resolved **from the stored task** at spawn time, never
  taken from the request body. A request that supplies its own justification is not
  evidence.
- The causing entry must be recent — a stale human approval from six months ago should
  not authorize a run today. Task-071 should pick a window and state it.

Raised as an open question by the read-only dispatch experiment on 2026-08-11, which
declined to decide it alone. Recorded here rather than left to task-071 to rediscover,
because it is a property of the *rule*, not of the endpoint that enforces it.


### What is checked, and what is merely claimed (added 2026-08-20, task-188)

The Dispatch button is one click. Pressing it on a task with a complete spec starts a
run; the server writes the authorising entry itself, attributed to the person signed in,
and only then dispatches. **This is not a relaxation of the rule above and it is very
easy to read it as one**, so the distinction is stated here rather than left in a
docstring.

Three things happen in this order, and the order is the design:

1. The request names an **identity** — who is clicking. This is a claim, and it is
   validated, not believed: the id must be an actor the project configures with
   `kind: human`, the same vocabulary `POST /log` and `POST /approve` have always
   validated theirs against. An unconfigured id is refused rather than assumed human.
2. That human's entry is **persisted** to the task file, as an ordinary `note` — exactly
   what a person writing the authorising note by hand produced before this existed.
3. The causing entry is then **resolved from the stored task** and put through
   `assert_human_clocked` like any other. It is re-read through the storage layer, not
   handed along from the write, so "resolved from the stored task at spawn time, never
   taken from the request body" is true in the literal sense the sentence means.

**The entry records an authorisation, not an outcome, and that wording is load-bearing.**
Step 2 happens inside the run lock and *before* the claim, because that ordering is what
makes the entry evidence rather than decoration — so step 3, the claim, and the spawn can
all still refuse after it has landed, and an append-only log cannot take it back. The
composed sentence is therefore "*Jeff Posey* authorised a dispatch of this task from the
task page", which stays true whether or not a run followed; whether one did is what the
`dispatch` entry beside it says. Writing "Dispatched by …" would have made a failed spawn
the one thing this feature could put into a record that was not so. Shrinking the window
by moving the write later was the rejected alternative: the window *is* the ordering that
makes the evidence real.

What a request still cannot do is supply its own *justification*. There is no field that
says "this run is authorised because ..."; there is a field that says who is asking, and
the consequence of setting it is a visible, permanent row in an append-only log under
that person's name. A caller who abuses it has not bypassed the record — they have
written to it, in public, and the run traces to them.

**An agent still cannot cause a dispatch**, and it survives for two independent reasons.
Structurally: agents do not have browsers. Mechanically: an agent id offered as the
authoriser is refused before anything is written (`authorizer_not_human`), and the entry
that *is* written still has to pass the human-clocked check, which reads the actor's
kind from config rather than from the request. Both are pinned by tests.

**Sufficiency is now asked separately, and directly.** The old check was answering two
unrelated questions at once — *who authorised this run* (safety) and *does the agent have
enough to work from* (sufficiency) — and answering the first by proxying through an
artifact of the second. It got both wrong in the same breath: a task with a complete spec
and a stale agent note was refused, while a task with an empty spec and a human note
saying "testing" was allowed. Sufficiency is now `spec.description`, and nothing else:

- **Not `ball_prompt`.** Empty on 69 of 69 `ready` tasks in this project, and correctly
  so — a `ready`/`agent`/`available` task is in the pool, not handed to anyone, so there
  is no current ask to state. A check keyed on it fires on 100% of the tasks you dispatch
  from, which teaches the reader to click through it without reading.
- **Not an empty `acceptance[]`.** Two of 74 open tasks have none and both have a full
  description; missing criteria are a grooming gap, not an authorisation one.

Measured 2026-08-20, that trigger fires on **zero** of this project's 74 open tasks. When
it does fire, the text the human types becomes the body of the authorising entry, so one
action serves both purposes.

**Where there is no signed-in user — the CLI, MCP, and a project with no human
configured — nothing changed.** The pre-existing rule applies: the newest stored entry
(or the one `--caused-by` names) must be a human's, and a task that fails it is refused
with `not_human_clocked`. The server deliberately does **not** substitute the project's
`default_user` to get past this. A run has to be signed for by whoever asked for it, and
a config value standing in for a person produces something that looks like evidence and
is not. The browser knows this before the click and disables the button rather than
offering one that can only refuse.

Built by task-188. The manual note control task-185 added stays: it is the right path
for deliberately writing an instruction onto a task, and it is what keeps the refusal
copy honest for callers with no button to press.

---

## 3. Anatomy of a dispatch

Four nouns, and where each lives. The split follows the precedent already set by
`projects.py`: *what the project is* is versioned with the project; *what this machine
will do about it* is machine-local and disposable.

| Noun | What it is | Where it lives | Versioned? |
|---|---|---|---|
| **Runner** | Named recipe for starting an agent: an argv template and optional env | `~/.agentjobs/dispatch.yaml` | No — machine-local |
| **Runner group** | Ordered list of runners that are interchangeable for one kind of work | `~/.agentjobs/dispatch.yaml` | No — machine-local |
| **Enablement** | Whether a given project may dispatch, and with which runner or group | `~/.agentjobs/dispatch.yaml` | No — machine-local |
| **Run** | One live or finished agent process: id, pid, status, output | `~/.agentjobs/runs/<run_id>/` | No — machine-local |
| **Dispatch record** | That a run happened, who authorized it, and how it ended | Task `log[]` | **Yes — git** |

The last row is the one that matters for the product: the durable, reviewable,
`git blame`-able fact that an agent was launched against this task lives in the task
file, alongside the work it produced. Everything else is scaffolding this machine
happens to need today.

### Machine-local configuration

```yaml
# ~/.agentjobs/dispatch.yaml — machine-local, never committed, never in a repo
version: 1
enabled: true                      # master switch; see also the DISPATCH_DISABLED sentinel

runners:
  claude:
    argv: ["claude", "-p", "{prompt}"]     # flags are the operator's business, not AgentJobs'
    env: {}                                 # additive over the server's environment
    # no `mode:` means batch. A session runner is `mode: session` AND `--bg
    # --remote-control` -- the two have to agree; see §4.
  codex:
    argv: ["codex", "exec", "{prompt}"]

# Optional (task-177). Absent, everything below behaves exactly as it did: a project
# names one runner and that runner runs. See §4 for selection and precedence.
runner_groups:
  standard:
    description: Ordinary work. What most dispatches should get.
    members:
      - runner: claude
      - runner: codex
        enabled: false             # written now, in play once it is set up
        note: Enable after codex is installed and signed in.
default_group: standard            # any project that names no group of its own

projects:
  agentjobs:
    enabled: true
    group: standard                # or `runner: claude` for exactly one
    require_clean_tree: true
    auto_dispatch: false           # §5; off until the manual path is boring

limits:
  max_concurrent_runs: 1           # machine-wide
  run_timeout_seconds: 1800        # batch runners only; terminates the run
  session_stale_seconds: 3600      # session runners; moves the ball, never kills (§9)
  auto:                            # applies ONLY to auto-dispatch (D3)
    per_task_per_day: 3
    per_task_lifetime: 10
    cooldown_seconds: 60
```

Placeholders (`{prompt}`, `{task_id}`, `{project_id}`, `{project_root}`, `{run_id}`,
`{agent}`, `{api_base}`) are substituted **per argv element, literally, with no shell**.
There is no `shell=True` anywhere in this design; see §10.

### Which address the agent is told

`{api_base}` and the address inside `{prompt}` are the same value, resolved once per run
in `dispatch/address.py`. They have to be: a fix applied to only one of them leaves a
second, wrong copy in every runner template that interpolates the other.

The value comes from the first of these that knows the answer.

1. **The socket the request arrived on.** A dispatch over HTTP passes
   `scope["server"]` — the listening socket's own name — down to the runner. It is
   preferred over the `Host` header because this dashboard is commonly published through
   a proxy, and the header then names an address that means nothing to a process
   starting on this machine. It also means the web path needs no configuration at all
   and cannot go stale.
2. **`AGENTJOBS_API_BASE`**, for a terminal that knows where the server is.
3. **`api_base:` in `~/.agentjobs/dispatch.yaml`**, the machine's standing answer. This
   is what `agentjobs dispatch run` uses, since a CLI invocation has no request to
   derive anything from.
4. **`http://localhost:8765`**, the CLI's serving default, as a last resort.

Before task-154 the parameter simply defaulted to (4) at every level and the HTTP
endpoint never passed anything, so every dispatched agent was told `:8765` whatever port
was serving. That is a worse failure than it sounds: an agent that cannot reach AgentJobs
cannot log the fact that it cannot reach AgentJobs, so the run's only symptom is silence.
`agentjobs dispatch run` now prints the address it resolved, for the same reason.

#### And then checks it answers

Resolving correctly is not the same as being right. Sources (2), (3) and (4) are claims
about a port — made by an environment, a file, or by this design standing in for a file
nobody wrote — and every one of them can be stale or absent while the resolver behaves
exactly as specified. Task-193 is that observed: on the machine this was built for, three
real dispatches each resolved cleanly to (4) and told the agent `:8765`, which nothing
there serves. They survived only because the agents read their task YAML off disk.

So a dispatch with **no observed address** — the CLI, and any library caller that passes
none — is gated on the address answering. `probe_api_base` asks `/api/version` with a two
second timeout, and `dispatch_task` refuses with `api_base_unreachable` if nothing
replies. It refuses rather than warns because the failure it prevents is silent by
construction: the run starts, the money is spent, and the only artifact is a task record
that stops changing.

Three outcomes, and they are deliberately not two:

| Probe result | Meaning | Dispatch |
| --- | --- | --- |
| answered as AgentJobs | `/api/version` returned this application's shape | proceeds |
| answered, but not as AgentJobs | something is listening; a reused port, or a version too old to serve `/api/version` | proceeds, and `dispatch config` warns |
| nothing answered | connection refused, or filtered until the timeout | **refused** |

The middle row proceeds because the check cannot tell a stranger on the port from an
older AgentJobs, and only one of those is broken. Refusing on a distinction the code
cannot actually draw would be a gate that blocks working setups.

An address that *was* observed is never probed. It arrived on the socket answering the
very request doing the dispatching, so the question is already answered — and a server
issuing a synchronous HTTP call to itself from inside a request handler is a deadlock
waiting for a worker count of one.

`agentjobs dispatch config` reports the same thing without dispatching anything: the
resolved address, the source that produced it, and what answered there. Before task-193
it reported the address nowhere at all, so the only place it was ever shown was
`dispatch run`, one line after the run started.

### The task-side record

Two new `LogEntryType` values — `dispatch` and `dispatch_result`. Adding them is a
schema change, which this project has decided to treat as cheap rather than something
to route around (schema-design §9).

```yaml
- id: 7
  ts: '2026-08-11T14:02:11Z'
  actor: Jeff Posey            # the human who authorized — never the agent
  type: dispatch
  body: Dispatched claude to work this task.
  data:
    run_id: run_a1b2c3d4
    agent: claude
    runner: claude
    trigger: manual            # manual | auto
    caused_by: 6               # log entry id whose actor gates the dispatch (§2)
    argv: ["claude", "-p", "You are the agent `claude` working ..."]
    cwd: C:/projects/agentjobs
    git_head: 4887b74
- id: 8
  ts: '2026-08-11T14:19:40Z'
  actor: claude
  type: dispatch_result
  re: 7
  data:
    run_id: run_a1b2c3d4
    outcome: completed         # see §9 for the full vocabulary
    exit_code: 0
    duration_seconds: 1049
    log_path: ~/.agentjobs/runs/run_a1b2c3d4/
```

When a runner group chose the runner, the entry also carries a `selection` block naming
the group, which precedence rung named it, and every candidate with its verdict (§4).
It is absent otherwise, so a flat configuration's entries are unchanged.

`argv` is recorded verbatim, which means **secrets must never appear in a runner's
argv** — put them in `env`, which is never logged. Stated here because the recording is
the safety feature and weakening it to hide a token would be the wrong fix.

On a successful run the `dispatch_result` body stays empty: the agent's own `progress`
and `handoff` entries carry the substance, and duplicating a transcript tail into git
would be noise. On any non-success outcome the last ~40 lines of combined output are
inlined into the body, so the git-tracked record still says something after the
machine-local logs are gone.

**Counting dispatches is derived, not stored.** The number of times a task has been
dispatched is `len([e for e in task.log if e.type is DISPATCH])`. No counter field, no
state to keep consistent, and the count survives in git alongside the evidence for it.

---

## 4. How an agent is actually invoked

### The prompt is a stub, not a composition

The strongest thing this design does is refuse to build a prompt.

Schema-design §5 already guarantees that a fresh agent resumes a task by reading `spec`,
the state axes plus `ball_prompt`, the newest-first `log`, and `acceptance`. That
guarantee is enforced by the schema — `ball_prompt` is required whenever the ball is
set. So the dispatch payload is not "the context an agent needs"; it is *a pointer to
where the context already is*:

```
You are the agent `claude` working task `task-060-agent-dispatch` in project
`agentjobs` (root: C:/projects/agentjobs). AgentJobs is serving at
http://localhost:8765. Read the task record and follow the resumption contract in
docs/agent-workflow.md. Dispatch run id: run_a1b2c3d4.
```

!!! warning "Amended 2026-08-11 — the stub pointed at a dead document"
    This stub originally pointed at `docs/agent-workflow.md`. That file is entirely
    v1-era (`mark_in_progress`, `TaskStatus`, `status_updates`) and contains no
    resumption contract, so **every dispatched agent would have been sent to a stale
    document as its first instruction.** Found by the read-only dispatch experiment on
    2026-08-11 — the first headless agent run under this design found the bug in the
    prompt that dispatched it.

    Task-046 rewrote `agent-workflow.md` for v2 and made it the operational guide; it
    links to `schema-design.md` section 5 as the canonical contract. Task-070 should
    retain the guide path and a test asserting that the referenced file exists and
    links to the contract.

Fixed text plus five substitutions. It never needs to change when the schema changes,
it cannot drift out of sync with the task record, and it is small enough to read in the
`dispatch` log entry. Composing a richer prompt would duplicate the resumption contract
in a second place and guarantee the two disagree eventually — rejected in §10.

### A parent task gets the other stub (task-164, 2026-08-21)

There are two stubs, and the record picks which one a run gets: **a task with an open
child is an epic, and the agent sent at an epic is told to supervise rather than to
work.** It starts one session per child, one at a time, and does not work a child itself.

That condition is the whole mechanism. It needs no new field, no label anyone has to
remember to set, and no judgement at spawn time — `manager.get_subtasks()` already
answers it, and `get_next_task()` already refuses to hand out a parent with open
children, so the two agree about what an epic is. Jeff's formulation was *"anything that
is starting with a new worktree should be in a new session"*; "has an open child" is that
sentence made checkable.

The second stub is a second string rather than an extra sentence on the first because it
**inverts** the first's load-bearing instruction. `PROMPT_STUB` opens by ordering a
worktree before anything else is written; a supervisor writes no code, needs no
isolation, and must not check anything out in the shared clone — doing so is the exact
collision the worktree rule prevents, and it would then commit the parent's task records
somewhere the dashboard cannot see them. By task-192's argument, an instruction that has
to precede reading the guide cannot be deferred to the guide; that applies to "do not
take a worktree" as much as it applied to "take one".

**A supervisor cannot dispatch its children, and that is D4 working, not a bug.** A
dispatch must be caused by a stored log entry written by a configured human (§2), which
is what makes agent-starts-agent structurally impossible rather than merely capped.
Nothing a supervisor writes satisfies it, so it starts its children with the runner CLI
directly, as its own subprocesses. The consequence is worth stating plainly rather than
discovering: **those children are not AgentJobs runs.** No run directory, no ledger row,
no `dispatch_result`, no reaping by the poller, and they do not count against
`max_concurrent_runs` — while the supervisor itself does hold a slot for the whole epic.
What supervises them instead is the task record, which is the signal the protocol tells a
supervisor to watch anyway. Whether the ledger should learn about agent-started children
is open, and is the kind of question §7's caps exist to make safe to defer.

The protocol the supervisor prompt points at — start a child, and what to do when one
finishes, parks, dies, or leaves the parent waiting — is in
[the workflow guide](agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them).
Driving that loop to completion *unattended*, including merging each clean child, is not
this: it is task-022, which depends on this and on the posture/merge decision in
task-021.

### The mechanism is a config template, and here is the argument

The obvious answer is `claude -p`. Three candidates were compared:

**(a) Hardcode Claude Code headless.** Shortest path. Rejected: the actor roster in
`.agentjobs/config.yaml` already names `codex` beside `claude`, so vendor-locking the
dispatcher contradicts a decision the project has already made and displays in its own
UI. It would also make "which agent worked this?" a lie the first time codex ran
anything.

**(b) The Claude Agent SDK, in-process.** Genuinely attractive — structured events,
real cancellation, no output parsing. Rejected on three grounds, in order of weight:
an agent crash or memory blow-up takes the AgentJobs server down with it, so the thing
supervising the run shares fate with the run; it forces AgentJobs to hold and manage API
credentials, which today it does not touch at all; and it is single-vendor by
construction, so (a)'s objection applies with a dependency attached. The isolation
argument alone is decisive: a supervisor that dies with its child cannot report on it.

**(c) A named runner with an argv template. → Chosen.** AgentJobs learns nothing about
any vendor. Adding a third agent is a config edit, not a code change. The child is a
separate OS process, so it can be killed, timed out, and outlived. The cost is that
AgentJobs sees only an exit code and a stream of text, which §9 shows is enough for
every outcome we need to distinguish.

Note what (c) *is not*: it is not a shell command string. Argv is a list, substitution
is per element, and nothing is interpolated into a shell. See §10.

### Dispatch is a session launcher, not a batch runner (decided 2026-08-18)

This design assumed the only headless option was `claude -p` — fire and forget, no way
in once it starts. Jeff asked whether a dispatched run could instead be a **remote-
controllable session** he can pick up interactively from another device. It can, and
**AgentJobs drives Claude Code's own dispatcher rather than reimplementing a worse one.**

The CLI's own word for these is "dispatched sessions". Claude Code already has a
dispatcher; this design predates knowing that.

!!! warning "The August table was written from `--help`, and two of its rows were wrong"
    Everything below was re-verified on **2.1.228** by running it, in throwaway repos,
    on 2026-08-18. The version this section originally cited (2.1.220) has moved on, and
    reading help text is not the same as running the command.

| Flag | What it gives us | Verified |
|---|---|---|
| `--remote-control [name]` | Session reachable and **steerable** from any device | Yes — and it composes with `--bg`, despite help text saying "interactive" |
| `--bg` / `--background` | Start as a background agent, return immediately | Yes |
| `claude agents --json [--all]` | Sessions as JSON, no TTY required; `--cwd` scopes to a root | Yes |
| `-w` / `--worktree [name]` | Session gets its own git worktree, git-locked — **and refuses every git operation aimed at the shared checkout, so dispatch cannot use it** (task-186, §8) | Yes |
| `--permission-mode <mode>` | Per-invocation posture | Yes — see the posture section below |
| `--session-id <uuid>` | ~~Caller assigns the id~~ | **No. `--bg` ignores it** and warns that it manages the id itself |
| `--max-budget-usd` | Per-run spend ceiling | **`--print` only** — a session has no spend ceiling |

Two of those corrections are load-bearing, and are stated plainly because a reader will
otherwise design against the old claims:

- **A run id cannot be a session id.** `--bg` prints a short id and owns the uuid. The
  dispatcher captures it from stdout, or correlates through `claude agents --json`.
- **Session mode has no money stop.** §7's runaway protection keeps its dispatch-count
  caps and loses its only hard ceiling. On a subscription this governs a usage window
  rather than a bill, which softens it without removing it.

**Verified end to end, 2026-08-18:** a session started with no TTY emitted a
`claude.ai/code/session_…` URL, and Jeff opened it on his phone and sent it a message,
which it answered. The claim that matters is not that a dispatched run is *visible* from
elsewhere but that it is *steerable* from elsewhere, and that is the form that was tested.

#### Two runner modes, split by purpose rather than by vendor

A runner declares which mode it is. This is the shape that satisfies the 2026-08-11
constraint that increased Claude dependence is acceptable, but that the abstraction must
not become a Claude-shaped hole no other CLI can fill.

| Mode | Invocation | For | Gets |
|---|---|---|---|
| `session` | `--bg --remote-control` | Work you might redirect: implementation, anything long, anything that may need a permission answered | Steerable from any device, park-and-ask |
| `batch` | `-p --output-format=stream-json --verbose --max-budget-usd N` | Bounded reports: review, triage, defect hunts | Spend ceiling, structured output, real exit code |

`batch` is **not** merely a fallback for a CLI without a session manager, though it
serves as one. It is the better mode for a whole class of dispatch, and it keeps the
argv-template runner of (c) above intact.

Two corrections to that table, both found by **running** the invocations rather than
reading them, on 2.1.235, 2026-08-19 — the same failure mode as the August flag table
above, and worth the same warning:

- **`--verbose` is not optional in the batch row.** `claude -p --output-format
  stream-json` exits with *"When using --print, --output-format=stream-json requires
  --verbose"* and starts nothing. A batch runner written from the original row does not
  run at all.
- **The worktree flag is nobody's — do not put `-w` in a runner template.** This bullet
  originally said the opposite: that `-w <task-slug>` was spliced in by `posture_flags`,
  so a template writing it too would hand one run two worktree flags. `posture_flags` has
  not written `-w` since 2026-08-19 (task-186), and the duplicate-flag hazard is replaced
  by a worse one: a template that writes `-w` produces a run that can do its work and then
  neither commit its task record nor merge it. §8 has the evidence. A dispatched agent
  takes its own worktree instead.

A third thing the same exercise settled: `--remote-control` takes an *optional* name, and
what stops it swallowing the prompt is that AgentJobs splices the posture flags in
immediately before the prompt element. A session template ending
`["claude", "--bg", "--remote-control", "{prompt}"]` is therefore correct, and the
composed argv is `claude --bg --remote-control --permission-mode acceptEdits
--settings <json> <prompt>`.

#### Rejected, with what rejecting them costs

- **Batch only — the original §9 model.** It keeps a hard spend ceiling, structured
  `stream-json` output, and exit-code-derived `failed`/`crashed` outcomes. All three are
  genuine, and all three are things session mode does not have. Rejected because it fails
  the point of the feature: a batch cannot be redirected mid-flight, so dispatch would
  replace the tracking and leave the conversation in a chat window. Retained as a mode
  precisely because its advantages are real.
- **Session only, deleting batch.** One code path, one UI story. Rejected: it discards
  the spend ceiling and the structured output entirely, and leaves nothing for a CLI with
  no session manager.

#### What this does not claim

Steering happens in **Claude's** surface, not in AgentJobs'. There is no CLI verb to send
a message into a running background session; you redirect it at `claude.ai/code` or with
`claude attach`. AgentJobs starts the run, records it, and links to it.

So dispatch does not remove the chat window — it **demotes** it. AgentJobs becomes where
work is tracked and decided; a chat becomes where one run is steered, hanging off a task
record rather than floating free. That is the honest version of §1's value argument, and
it is worth more than the overclaim it replaces.

One consequence to design around: the Remote Control URL appears **only in the
`claude logs` ANSI transcript**, not as a field in `claude agents --json`. Surfacing
"continue on your phone" in the UI means scraping a terminal rendering, or reconstructing
the URL from a session id. Neither is verified. Task-070 owns it.

D1–D4 are unaffected: who may cause a dispatch, and that approval is not dispatch, are
independent of what a dispatch starts.

### Permission posture: what a run may do (decided 2026-08-18)

Nothing above said **what a dispatched agent is allowed to do**, and that is the actual
risk boundary of the feature — not what may *start* a run, which §6 already gates four
times over. Mechanically it lives in the runner's argv, which makes it look like the
operator's business. It is not, and burying it in a config example means it gets chosen
by whoever copies the example first.

!!! warning "This section was written against a premise that turned out to be false"
    It assumed a permission prompt in headless mode "is not a prompt but a silent
    denial." That is true of `claude -p`. It is **false** of a `--bg` session, which
    **parks** at `status: waiting` / `state: blocked` and stays there indefinitely,
    reporting that state in the ledger. Verified 2026-08-18. `dontAsk` is the mode that
    silently denies.

That correction is what makes the posture below possible. The old framing — an agent that
cannot run `pytest` satisfies nothing, one that can run anything is an unattended shell —
was a real dilemma only because a batch run has no third move. A session has one: **ask.**

#### Four postures, chosen per project

Machine-local in `~/.agentjobs/dispatch.yaml`, like every other dispatch setting.

| Posture | Flags | For |
|---|---|---|
| `read_only` | `--tools "Read,Glob,Grep,WebFetch"` | Review, triage, plans, defect reports. Verified enforceable: the agent has no shell at all. |
| `auto` **(default)** | `--permission-mode auto` plus the project allow-list via `--settings` | Normal dispatched work. A classifier reviews each action, so the run keeps a gate and never needs a terminal. |
| `supervised` | `--permission-mode acceptEdits` plus the project allow-list via `--settings` | A run a human is actually watching and willing to answer. |
| `autonomous` | `--permission-mode bypassPermissions` | Per-project opt-in. Never the default. |

Every posture except `autonomous` also carries the dispatched project's own `.mcp.json`
server names in `--settings`, which is the only way a `--bg` run gets past the MCP
approval dialog. See [the project's own MCP servers travel with the
run](#the-projects-own-mcp-servers-travel-with-the-run).

!!! warning "`supervised` was the default until 2026-08-19, and could not finish work"
    The table below says `acceptEdits` + allow-list runs an arbitrary command with no
    prompt. That is true only of the nine allow-listed prefixes. **Everything else still
    parks** — and "everything else" includes `ls`, `cat`, `find`, `grep` and `sed`.

    Observed on the first two real dispatches ever run, both of task-107: run_a6deb292
    started cleanly, read its task, then parked asking permission to run
    `ls C:/projects/agentjobs/docs/` — the repository's own docs directory — and stayed
    parked until cancelled. A `--bg` session has no terminal, so nothing could answer.

    §4 as first written identified the gap precisely — *"there is no permission mode that
    permits `pytest` but not arbitrary commands"* — and concluded the allow-list had to
    carry that middle. It does not carry it: an allow-list enumerates what is permitted,
    and no list of prefixes anticipates what a task will need. `auto` mode is the middle
    that was missing. It was never evaluated in the table below, which is why it was not
    chosen at the time.

    This does not disturb the section's actual decisions. `bypassPermissions` is still
    rejected as a default, `dontAsk` is still rejected for producing untested work, and
    **no auto-escalation** still holds: `auto` gates every action through a classifier,
    which is a gate, not the absence of one.

**May a dispatched agent run shell commands unattended? Yes — only those matching its
project's allow-list.** Everything else parks, and AgentJobs turns a parked session into
ball → `human`/`input` with the pending command quoted in the `ball_prompt`, answerable
from a phone. The seed list is deliberately boring: `poetry run pytest:*`,
`poetry run ruff:*`, `poetry run black:*`, `poetry run mypy:*`, `npm run:*`,
`git status:*`, `git diff:*`, `git add:*`, `git commit:*` — and, since
2026-08-21, `git merge:*`.

That last one is the exception that proves the rule, and task-222 records why it was
made on Jeff's explicit authorisation rather than by a widening nobody reviewed.
`git merge` is not boring the way `git status` is: it writes to the working tree and
creates commits. What makes it acceptable is that the merge is the sanctioned end of
the documented lifecycle and is gated on a human approval recorded on the task —
the classifier was never the thing authorising it, only an unreliable obstacle in
front of it, and a run that does all of its work and then cannot land it is the most
expensive shape a failure takes. `git push` stays absent and must: pushing is a
separate act from merging here, and nothing authorises it.

The allow-list is still a maintenance surface that will be widened under pressure. What
changes is that widening it is a **visible act** — a prompt someone answered with "don't
ask again" — rather than a config edit nobody reviews.

The verified behaviour that determines all of this, every cell run as a `--bg` session:

| `--permission-mode` | Edits | Auto-classified safe reads | Arbitrary command |
|---|---|---|---|
| *(default)* = `manual` | prompt → **parks** | allowed | prompt → **parks** |
| `acceptEdits` | allowed | allowed | prompt → **parks** |
| `acceptEdits` + allow-list | allowed | allowed | **allow-listed: runs. Anything else: prompt → parks** |
| `auto` + allow-list | allowed | allowed | **classifier decides; no prompt, no terminal needed** |
| `dontAsk` | allowed | allowed | **silently denied, run continues** |
| `--tools "Read,Glob,Grep"` | no tool | no tool | no tool at all |

The `acceptEdits` + allow-list row read "runs, no prompt" until 2026-08-19. That was
measured with allow-listed commands only, and generalised to a column headed *arbitrary
command*, which is where the default came from. Corrected against run_a6deb292, which
parked on `ls`.

The `auto` row is why a fourth posture exists. Rather than enumerating permitted
prefixes, `auto` has a classifier evaluate each action, so it covers commands no list
anticipated **and** needs no human present — the combination none of the other rows
offer. Allow-list rules still take the form `Tool(prefix:*)` — the colon is not
optional, and omitting it silently matches nothing.

#### The project's own MCP servers travel with the run

A project that ships a `.mcp.json` could not be dispatched to at all until 2026-08-19,
and AgentJobs ships one so agents can reach the managed task tools. Claude Code prompts
the first time it finds a project-scoped MCP server:

```
New MCP server found in this project: agentjobs
1. Use this MCP server
2. Use this and all future MCP servers in this project
3. Continue without using this MCP server
```

A `--bg` session has no terminal, so nothing can answer. `claude agents --json` reports
`state: "blocked"` and the run burns its whole timeout doing nothing — run_08ddfa02, the
first real dispatch ever attempted, sat there ~913 seconds. There is no CLI verb that
approves a server non-interactively: `claude mcp` has add/remove/list/get/
reset-project-choices and nothing else ([#10447][mcp-approve] is the open request, and
[#72430][mcp-routines] is the same wall for cloud routines).

This is **not** the workspace-trust dialog. Trust for worktrees was fixed upstream on
2026-08-17 ([#23109][trust-fix]) and is keyed on the repository's main checkout; trust
only governs whether repo-committed approvals are honoured, and does not grant this one.

The fix uses `--settings`, which dispatch already composes and which is one of the three
approval sources that apply regardless of folder trust — the others being the user's
`~/.claude/settings.json` and managed settings, neither of which dispatch may write.
`posture_flags()` adds `enabledMcpjsonServers`, listing the names read out of the
**dispatched project's own** `.mcp.json`. Hardcoding `agentjobs` would fix one project;
`enableAllProjectMcpServers: true` was rejected as a much broader grant than dispatch
needs, since it approves any server in any project rather than the ones this project
declares.

Which postures carry it was measured, not reasoned about — probed on 2.1.235, in a
worktree declaring one server no settings file had ever heard of, so the machine-local
`enabledMcpjsonServers: ["agentjobs"]` workaround could not mask a result:

| Posture flags | `claude agents --json` | Dialog in transcript |
|---|---|---|
| `--permission-mode auto` | `blocked` | yes |
| `--tools Read,Glob,Grep,WebFetch` | `blocked` | yes |
| `--permission-mode bypassPermissions` | `done` | none |
| `--permission-mode auto` + `enabledMcpjsonServers` | `done` | none |

So `read_only` gains a `--settings` blob it never had, holding the approval and no
`permissions` key — an allow-list there would be a posture change. `autonomous` is left
exactly as it was: it never reaches the gate, and handing it settings it does not need
would imply a limit that is not there. If a future release makes `bypassPermissions`
honour the gate, that posture breaks and nothing in the suite will say so; the four
commands that re-measure it are in task-019's log.

A project with no `.mcp.json` yields no names, `enabledMcpjsonServers` is omitted rather
than emitted empty, and every posture's argv is byte-identical to what it was before.

[mcp-approve]: https://github.com/anthropics/claude-code/issues/10447
[mcp-routines]: https://github.com/anthropics/claude-code/issues/72430
[trust-fix]: https://github.com/anthropics/claude-code/issues/23109

#### Containment, and what it is not

!!! warning "Rewritten 2026-08-19 (task-186) — `-w` is no longer passed"
    This section argued that `-w` gave dispatched runs worktree containment for free:
    `<root>/.claude/worktrees/<name>` on branch `worktree-<name>`, git-locked with a lock
    reason naming the session and pid, `claude rm` refusing to discard one holding
    uncommitted changes, and **task-075's layer 2 therefore dropped for dispatched runs**
    because the CLI did it better than we would have. Every one of those statements about
    the CLI is still true. What the section did not know is that the same isolation
    refuses every git operation aimed at the shared checkout, which is where this project
    commits task records and runs its merge gate. §8 has the reproduction and the
    decision. The original text is preserved above rather than deleted, because the
    argument it makes is the one that has to be answered.

Containment is now the agent's own act — it takes a worktree before writing anything, and
§8 names the three things that make that hard to skip. Layer 1 of task-075, the convention
for interactive agents, was always this and is unaffected.

The containment argument was also load-bearing for a default that has since changed. It
was what made `acceptEdits` defensible: in the shared checkout an unattended agent commits
on top of a peer's in-flight work — the three 2026-08-11 failures, at machine speed — and
`read_only` would have been the only defensible default. `auto` has been the default since
task-020, and what gates it is a classifier evaluating every action, not the working tree.
So losing mechanical containment does not reopen the question this paragraph settled; the
default it defended is no longer the default.

**A worktree is not a sandbox.** An agent with shell access can `cd` anywhere on the
machine. Containment reduces the blast radius of accidents; it does not bound a confused
or adversarial agent. That was true when containment was mechanical and it is true now.
It is also why losing the mechanical form costs less than it appears to: it never bounded
anything, and it still does **not** justify making `autonomous` a default.

#### Rejected postures, with what rejecting them costs

- **Read-only only.** Cannot satisfy a single acceptance criterion in the derived tasks,
  all of which require running the suite. Kept as a posture because it is genuinely right
  for review and triage; rejected as the *only* mode because it makes dispatch a
  reporting feature rather than a work feature.
- **`dontAsk` as the default.** Never parks — it refuses and carries on, which is
  superficially the perfect unattended mode. Rejected on observed behaviour: the agent
  hits the denial, reports it, and stops trying, so it produces untested work and hands
  the task back anyway. That trades a visible stall for an invisible half-finished
  result. Available for a project that explicitly wants never-park semantics.
- **`bypassPermissions` as the default.** The only posture that never parks. Rejected as
  a *default* because it makes every dispatch an unattended shell, which §6's four gates
  exist to avoid granting casually.
- **A bespoke per-runner argv allow-list.** Rejected: `--settings permissions.allow`
  already does this, is enforced, and is settable per invocation. A second mechanism
  would need a translation layer to the one that actually works.

#### No auto-escalation, ever

A parked run must **not** be promoted to `autonomous` by a timeout, however long. That
would grant bypass permissions with no human present, which is what this entire section
exists to prevent, and it violates §2: every dispatch traces to a human act, and a
timeout is not one. If a human wants the remainder of a night's work to run unsupervised,
they say so — and *that message* is the human act.

Related, and settled: **credentials are not AgentJobs' problem.** The CLI uses whatever
the local install is authenticated with — a subscription login on this machine, with no
`ANTHROPIC_API_KEY` in the environment (verified 2026-08-11). The child inherits the
operator's own auth. This strengthens (b)'s rejection above: the SDK would have made
AgentJobs hold credentials that, under (c), it never sees. It also means §7's limits
govern a **usage window, not a per-token bill** — worth knowing when choosing the numbers.

### Model policy: task difficulty and dispatch profiles (decided 2026-08-18)

Raised because easy work and hard architecture work should not automatically consume the
same model. Decided by Jeff on `task-080-dispatch-model-profiles` after the two CLI
surfaces were read rather than assumed, and extended by `task-177-runner-groups`, which
built the candidate-list half of it. **`difficulty` and runner groups are built; the
difficulty → profile table is not** — see the reopen triggers at the end of this
section.

#### What the CLIs actually offer, since it changed the answer

Verified against the installed `claude` v2.1.228 and the `openai/codex` source:

| | Claude Code | Codex |
|---|---|---|
| Model | `--model` | `-m, --model` |
| Reasoning effort | `--effort`: 5 levels | `model_reasoning_effort`: 8 levels **plus `Custom(String)`** |
| Named profiles | **none** | `-p, --profile`, backed by a `ConfigProfile` |
| Unique to it | `--fallback-model` | `model_provider`, `service_tier`, `oss_provider` |

Two findings drive everything after this. **The effort vocabularies do not align** — a
shared AgentJobs enum is either a lowest common denominator that cannot express Codex's
`minimal` or `ultra`, or an unvalidatable pass-through string. And **Codex already has
profiles richer than the ones this design proposed**, so an AgentJobs profile layer would
be a second profile system racing `-p` to set the same keys, with no way to know which
won.

#### Task difficulty — building now

A task may declare `difficulty`: **`routine` | `standard` | `hard`**.

It answers a question no existing field does. `priority` is *how much does it matter that
this happens*; free-text `effort` is *how long will it take*; `difficulty` is *how much
capability does doing it well require*. Those come apart routinely — a one-line fix to a
race condition is critical, tiny, and genuinely hard.

**Absent is legal and means `standard`**, with the audit trail recording that it was
*defaulted* rather than *declared*. Requiring it would invalidate every existing task.

**It ships with no automated consumer, and that is not a defect.** Given the deferral
below, nothing routes on it. It earns its place on human orientation and filtering —
*"which of my ready tasks is hard enough that I should drive it myself rather than hand
it to an agent"*. Stated explicitly so a later reader does not mistake an unconsumed
field for a broken one.

*Rejected: five levels* — a five-level scale people honestly use three levels of is a
scale with two dead values. *Rejected: t-shirt sizes* — they read as *effort*, the one
field difficulty must not be confused with.

#### Runner groups and profiles are one mechanism (groups built 2026-08-19, task-177)

Two nouns, and it is worth being exact about which does what, because they were designed
eighteen days apart and the obvious mistake is to build them as two competing layers:

- a **group** is the *candidate list* — which runners are interchangeable for this kind
  of work;
- a **profile** is the *mapping* — which group or runner a given `difficulty` gets.

A group is an ordered list of runners plus a per-member on/off switch. A profile is a
table from difficulty to one of those groups. They share one resolver, one precedence
ladder, and one audit vocabulary. **Groups are built. The difficulty → profile table is
not**, and the ladder below has its rungs reserved rather than occupied.

```yaml
# ~/.agentjobs/dispatch.yaml — machine-local, exactly like runners and for the same reason
runner_groups:
  standard:
    description: Ordinary work. What most dispatches should get.
    members:
      - runner: claude-standard
      - runner: codex
        enabled: false
        note: Second option; enable once codex is installed and signed in.
  deep:
    description: Architecture, review, anything worth the slower model.
    members:
      - claude-deep
      - claude-standard        # fall back rather than fail

default_group: standard        # any project that names no group of its own
```

Each member names an ordinary runner whose argv already says what it says. **Argv remains
the only thing that launches.** AgentJobs never learns what a model is, never maintains
an effort vocabulary, and never fights Codex's `-p`.

`enabled: false` is a first-class state, not a comment with extra steps. Writing the
runner you have not configured yet and leaving it off is how someone records *this is the
second option once I set it up*; the `note` beside it is why. Enabling is a hand edit,
always, and a disabled member is never selected under any circumstance.

##### Selection: what the dispatcher can actually see

The motivation for a list rather than a single runner was "current session limits and
such". That was checked rather than assumed, on 2026-08-19, by running the installed CLIs
rather than reading their help — and the answer changed the design.

**No installed agent CLI reports remaining usage headroom in any scriptable form.**
`claude` 2.1.235 has no `usage` verb; `auth status --json` returns identity and plan tier
only; `agents --json` returns live sessions with no accounting. `/usage` is an
interactive built-in — run as `claude -p "/usage"` it is not executed at all, it reaches
the model as prompt text and comes back as a chat reply, having cost a model turn to
learn nothing. A `-p` run's own `--output-format json` reports what *that call* consumed,
after the money is spent.

The numbers do exist machine-locally, as a cache: `~/.claude.json` holds a private
`cachedUsageUtilization` with five-hour and seven-day percentages and reset timestamps.
**Reading it would be a bug.** On the machine this was designed on it was 8 days 21 hours
stale, reporting 98% of a five-hour window that had reset nine days earlier — a
dispatcher trusting it would have skipped the preferred runner every time, for a limit
that no longer existed. It is also undocumented private state with internal codename keys,
and it is account-wide, so it cannot distinguish one runner's headroom from another's,
which is the exact discrimination a group would need.

So selection is built on what is local, free, deterministic, and incapable of hanging:

1. **declared order** — the first member that can run, wins;
2. **`enabled`** — a disabled member is skipped;
3. **defined** — a member naming a runner absent from `runners:` is skipped;
4. **resolvable** — a member whose `argv[0]` is not on PATH is skipped.

Nothing is probed over the network and nothing is timed. *Rejected: probing the CLI for
headroom* — there is nothing to probe. *Rejected: reading `cachedUsageUtilization`* — it
was wrong by nine days on the machine it would have shipped from, and a stale answer here
does not degrade gracefully, it inverts. **Reopen when a first-party agent CLI ships a
documented command that prints remaining headroom as structured output.**

##### What a group refuses to do

**A group that applies and has no eligible member refuses the dispatch.** It does not fall
back to the project's plain runner. This is the one place the ladder's
fallback-and-say-so rule does not apply, and the distinction is worth stating precisely:

- *no group applies at all* → the plain `runner` is the last rung, reached normally;
- *a group applies and is exhausted* → refusal naming every candidate and why.

Substituting a runner from outside the group would run a model the requester did not ask
for, at a cost they did not choose, which is the failure the group layer exists to
prevent. The refusal names each member and its reason, so it is actionable rather than
merely correct.

##### Precedence, narrowest first

1. a **group named on this dispatch** — `POST /tasks/{id}/dispatch {"group": ...}`, or
   `agentjobs dispatch run --group`;
2. *(unbuilt)* a **profile named on this dispatch**, mapping `difficulty` to a group;
3. **`projects.<id>.group`**;
4. *(unbuilt)* a **machine default profile**;
5. **`default_group:`**, the machine-wide group;
6. **`projects.<id>.runner`** — today's behaviour, and the fallback.

Every level that participated is recorded, including which won. An unmatched difficulty,
or a hole in a profile table, **falls back to the plain runner and says so** rather than
refusing: a dispatch that dies because a config table has a gap is the worse failure. A
per-profile **`strict`** setting inverts that — refuse instead, naming the profile, the
difficulty and the missing rule. Strict is opt-in and off by default, because deliberate
spend is the whole point of the feature and someone who chose a conservative profile and
silently got a frontier model was failed quietly.

One consequence of rung 5 sitting above rung 6, stated so it is not rediscovered as a
bug: **adding `default_group` takes effect for every project that has not named a group
of its own**, including projects that name a plain `runner`. That is what a machine
default means. A project that wants its runner regardless should name its own group, or
the file should not have a `default_group`.

##### Labels, and what they are worth

A runner may additionally declare optional descriptive `model` and `effort` **labels**.
These are authored metadata for display and audit only: **never parsed out of argv, never
used to construct a command, never validated against a provider vocabulary.** They buy
back the explanation that runner-only selection gives up — `difficulty hard → group deep
→ runner claude-deep (labels: opus / high)` — at an accepted cost: *a label can drift
from the argv beside it.* That is a documentation defect, not a dispatch defect, and any
UI must present labels as the runner author's claim, not as something AgentJobs verified.

*Rejected: `{model}`/`{effort}` placeholders plus a difficulty → (model, effort) table.*
It needs a per-runner effort vocabulary anyway, because the two CLIs disagree — which is
per-provider tables wearing a portable name, plus a vocabulary to keep current as models
change. *Rejected: runner-only with no labels.* The saving is zero and it leaves the
audit trail unable to answer "why did this run cost that much" in the terms the question
is asked in.

*Rejected: a group literally named `default` being magic.* The first sketch had the group
called `default` be what a dispatch gets when it names nothing. An explicit
`default_group:` key does the same job without a reserved name, and nobody has to
discover that renaming a group changed the machine's behaviour.

##### The audit trail is the feature

Selection is deterministic given the same inputs, and the account of it lands in the
task's git-tracked `dispatch` entry — not only in a machine-local run directory, which is
disposable:

```yaml
data:
  runner: claude-standard        # the winner
  selection:
    group: standard
    source: project              # dispatch | project | machine
    candidates:
      - runner: codex
        eligible: false
        skipped_because: disabled
        detail: Second option; enable once codex is installed and signed in.
      - runner: claude-standard
        eligible: true
      - runner: claude-deep      # after the winner: considered, not reached
        eligible: true
```

`selection` is **absent** when no group participated. A machine with a flat `runners:`
map and `projects.<id>.runner` writes exactly the entry it always wrote, needs no
migration, and gets no warning. Someone who never wants a group should not learn from
their own task files that groups exist.

##### Where this sits relative to the gates

Group selection happens **inside** `assert_dispatch_permitted`, after all four gates have
opened — never around them. Naming a group is a request about cost and capability, never
about permission: there is no group name that makes a refused dispatch proceed, and one
naming a group this machine does not define is refused rather than quietly falling back.
Groups are machine-local for the same reason runners are (§6, gate 2): nothing in a
project repository may define or extend one.

##### Setting it up

`agentjobs dispatch example` prints a commented starting configuration with groups and
every option explained; `--write` writes it and refuses if anything is already there.
That is the only route by which AgentJobs will put a dispatch config on disk, and it
takes a human typing it. **AgentJobs never synthesises a `dispatch.yaml` and never adds
an entry nobody typed** — a file that appeared on its own would defeat the gate that
makes the file the record of what may execute here. What `--write` writes is switched off
at every level, so it cannot leave a machine able to dispatch that was not able to
before.

> **Open, not decided:** what a strict refusal does when the caller is *unattended* —
> auto-dispatch or a bounded loop. A refusal that only returns an error is a silent stall
> there; it likely has to move the ball to `human`/`decision`. Raised by claude
> 2026-08-18, and still open: task-177 built groups without a strict mode, so nothing
> forced an answer.

#### Why the profile table is still unbuilt

`difficulty` is cheap, useful immediately, and carries no risk. Groups earned their build
because a real machine had a real second runner to fall back to. The difficulty → group
table is the remaining piece, and it is a config-schema change plus a table plus CLI and
GUI surfaces whose value is proportional to how often dispatch is used with a mixed
backlog. The shape above is **accepted, not merely discussed**: it is recorded here so
the next session does not re-derive it.

**Reopen when any one of these is true:**

1. 20 real dispatches have run; or
2. a dispatch is observed consuming a materially wrong-cost model for the work, in either
   direction, *that naming a group on the dispatch would not have fixed*; or
3. auto-dispatch or bounded loops (`task-078-agent-loops`) begin dispatching unattended —
   an unattended loop picks its own group from nothing, which is exactly the case a
   difficulty mapping exists for.

---

## 5. What starts a dispatch

**Dispatch is a distinct action from approval (D1).** Approve means *I agree with this
work*. Dispatch means *spend money now*. Collapsing them makes every approval a purchase,
and makes it impossible to approve five tasks in a review session without starting five
agents.

The trigger is an explicit `POST /api/tasks/{id}/dispatch` — a button in the review UI
next to Approve, and `agentjobs dispatch run <task-id>` in the CLI. Nothing else starts
a run.

**The button is one click, and the two callers differ in one field (task-188).** The
browser sends `user`, naming the person clicking; the server writes their authorising
entry onto the task, then dispatches on it. So the ordinary case — a task with a
complete spec, filed by an agent — needs nothing written by hand first. It stops to ask
for text only when `spec.description` is empty, which is true of none of this project's
74 open tasks, and the text it asks for becomes that entry's body.

The CLI sends no such field, because a shell has nobody to name; it keeps the original
rule and is refused with `not_human_clocked` if the newest stored entry is an agent's.
Neither path takes its justification from the request — see
[What is checked, and what is merely claimed](#what-is-checked-and-what-is-merely-claimed)
for why writing an entry and trusting a field are not the same act.

**Auto-dispatch is designed here and built later.** A project may eventually set
`auto_dispatch: true`, which makes an approval that hands the ball to `agent`
immediately dispatch it. That is a one-line change on top of a correct manual path, and
it is gated behind everything in §6 and §7 — which is exactly why it should not ship in
the same breath as the machinery that protects it. Deferring it costs nothing; shipping
it early means no period during which the manual path was watched behaving.

*Built by task-074, in `src/agentjobs/dispatch/auto.py`, and still off everywhere.* The
paragraph above stands unchanged: the switch now exists, nothing has flipped it, and it
lives in machine-local `~/.agentjobs/dispatch.yaml`, which no browser can write. Two
human actions arm it — approving, and requesting changes, both of which hand the ball to
an agent with instructions attached. The generic `POST .../handoff` deliberately does
not: it is the agent-facing verb, so hooking it would put the trigger on the very
transition §2 forbids and leave safety resting on a filter — the same objection that
rejected the webhook trigger below.

Even with auto-dispatch on, §2 holds without exception: the approval is a human act, so
it may cause one dispatch. The handoff that ends the resulting run is an agent act, so
it causes nothing.

Two rejected triggers, both of which look natural given the existing code:

- **A webhook consumer on `task.handoff`.** The event already exists, HMAC-signed, and
  task-046 names it as the extension point. Rejected because it is exactly the wrong
  shape for this: webhook events fire on *agent* handoffs too, so the trigger surface
  would include the one transition §2 forbids, and safety would depend on filtering
  correctly rather than on never being asked. A dispatcher is not a notifier; it should
  not reuse the notifier's plumbing just because the plumbing is there.
- **A polling worker over `ball == agent`.** Turns the ball into an autonomous work
  queue, which is precisely the unbounded loop §2 exists to prevent. It also makes
  dispatch happen with no log entry to attribute it to, so "who authorized this?" has no
  answer.

---

## 6. Safety

Dispatch converts an unauthenticated localhost HTTP API into **remote code execution on
Jeff's machine**. That sentence is the whole reason this section exists, and it should
be read before every change to this subsystem.

### Four gates, each independently sufficient to stop a run

1. **The master switch.** `enabled: false` in `~/.agentjobs/dispatch.yaml`, absent file
   means off. A fresh `pip install agentjobs` can never dispatch anything.
2. **The runner must exist machine-locally.** A project cannot execute a command that
   was not written into `~/.agentjobs/dispatch.yaml` by hand, on this machine. **This is
   why runners are not in the versioned `.agentjobs/config.yaml`**: if they were,
   `git clone` of any repository would carry a "run this command on Jeff's machine"
   payload that the project's own config file legitimises. The one thing dispatch must
   never do is let a repository choose what executes. **Runner groups are covered by the
   same rule and for the same reason**: a group is a name over runners, nothing in a
   repository may define or extend one, and naming a group this machine does not define
   is a refusal rather than a fallback.
3. **Per-project enablement.** `projects.<id>.enabled`, off by default, set either by
   `agentjobs dispatch enable <project>` or by the GUI toggle (D2). The GUI may flip a
   project between enabled and disabled among runners *and groups* already configured on
   the machine; it may **not** define or edit a runner's argv, or create a group. So the browser-reachable surface can
   turn a known capability on and off, but cannot introduce a new command to execute —
   which keeps the RCE surface exactly as wide as the machine-local file says it is.
   Disabling is always available from the GUI without ceremony; a kill switch you cannot
   reach is not one.
4. **The sentinel file.** `~/.agentjobs/DISPATCH_DISABLED`, checked immediately before
   every spawn. Its presence refuses all new runs regardless of every other setting.
   File-based deliberately: it works when the server is wedged, it can be created by
   `touch`, by Explorer, or by an editor, and it needs no API to be reachable.

### The kill switch, at three scopes

- **One run:** `agentjobs dispatch cancel <run-id>`, or a Cancel button on the run.
- **Everything:** `agentjobs dispatch stop` writes the sentinel *and* cancels every live
  run. This is the panic button, and it is one command with no arguments.
- **The blunt one, and only for batch runners:** killing `agentjobs serve` terminates its
  batch runs too, by design. **It does not stop sessions** — a session outlives the
  AgentJobs server deliberately (§9), so the panic button for one is `claude stop <id>`,
  which works whether or not AgentJobs is running.

### Two preconditions checked at spawn time

- **The working tree must be clean** (`require_clean_tree`, default true). An autonomous
  agent committing on top of uncommitted human work entangles the two, and the resulting
  mess is hard to unpick precisely when you are least expecting it. `git_head` is
  recorded in the dispatch entry so the diff attributable to a run is always recoverable.
  The refusal names the offending paths, because otherwise `git status` and the refusal
  disagree and neither explains the other — see the exclusion below.

    **The project's tasks directory is excluded from this check** (task-182). AgentJobs
    writes into the very tree it is inspecting, twice per run: the claim writes the task
    YAML before the spawn, and the terminal `dispatch_result` entry is written after the
    run's last commit. For a project keeping its task records in the repository being
    dispatched — which is what `agentjobs init` sets up, and what this repository does —
    counting those meant dispatch refused on the strength of its own writes, every time,
    with the second failure guaranteed rather than merely likely.

    **What that costs, stated so it is not rediscovered as a bug:** a human's genuinely
    uncommitted *hand* edit to a task file no longer blocks a dispatch. That is a real
    loss and it was taken deliberately. There is no version of this that keeps the tasks
    directory meaningful here, because the leftover `dispatch_result` entry sits in that
    directory unconditionally after every completed run; a check that fires every time
    fires on nothing. Everything outside the tasks directory is inspected exactly as
    before, and that is where an agent's code commits land.
- **The causing actor must be human** (§2).

### The dispatcher commits what it writes

Every write to a task record has a committer except one. A human's goes through their own
git; an agent's goes through the task lifecycle it is required to follow. The
dispatcher's terminal `dispatch_result` has neither: `outcome` and `duration_seconds` are
only knowable once the run process has exited, which is *by definition* after the
session's last commit, so the entry lands in a working tree nobody is coming back to.
Observed twice in one evening on job-hunting task-016 (task-203) — every dispatched run
left the shared clone dirty, and the person who found it was always the human.

The write is correctly timed. What was missing is that whoever performs it commits it.
`dispatch/record_commit.py` does exactly that, at every point where the dispatcher writes
to a record outside a session's lifetime: the session and batch settles, a batch run that
never started, the ledger's sweep of an abandoned run, a parked session's handoff, and an
auto-dispatch cap refusal — that last one being the most orphaned of all, since no run
ever existed.

Three properties, and they matter more than the mechanism:

- **Only the one file.** `git commit --only -- <path>` commits that path from the working
  tree and ignores the index, so a colleague's `git add`-ed but uncommitted work in the
  same clone is still staged and still theirs afterwards. Never `-A`; never a bare
  `git commit` that would take whatever the index holds. The clone is worked by people
  and other agents at once, and a broad commit here would turn a dirty file into
  somebody's lost afternoon.
- **It never pushes.** A commit is local, reversible, and repairs exactly the problem in
  hand. A push publishes to a shared remote, can be rejected non-fast-forward, can want
  credentials a background process should not be taught to supply, and can start CI.
  AgentJobs also cannot know a remote is safe to push to — projects exist whose standing
  rule is that they must never acquire one. Handling a rejected push means fetching,
  rebasing or forcing, unattended, in a clone somebody else is working, which is how
  automation destroys work rather than tidying it. If unpushed dispatcher commits are
  ever seen piling up across days, the answer is a per-project opt-in, not a changed
  default.
- **It never raises.** This runs on the terminal path of a finished run. No repository,
  no git on PATH, a pre-commit hook that refuses, a contended `index.lock` — each is
  recorded as `record_commit` in the run's `meta.yaml` and leaves the run reported
  exactly as it would have been. A git problem must not turn a completed run into a
  crashed one.

The alternative — the session commits a placeholder and the dispatcher amends it — was
priced and rejected. Amending rewrites a commit the session may already have pushed,
which would then need a force-push; the amend races anything else that committed in the
clone after the session exited, so it would rewrite the wrong commit; it couples every
dispatched session's prompt to dispatcher internals; and it does nothing at all for the
sites where no session ever existed. A fresh commit is simpler and has no rewrite hazard.

The pre-spawn `dispatch` entry is deliberately **not** committed here. It is the one
dispatcher write that is reliably swept up, by the session it starts, and committing it
would move `HEAD` past the `git_head` that same entry just recorded — making the run's
own diff include the dispatcher's commit. The one case where no session follows is a
spawn that failed, and there the immediate `dispatch_result` commit carries both entries.

### Does dispatch ever run with no human present?

Two different questions, answered differently:

- **Initiation:** no, not under this design. Every run traces to a human act — a click
  now, an approval later under auto-dispatch. There is no schedule and no queue drain.
- **Duration:** yes. Runs take minutes and continue after you walk away. That is the
  point, and it is why the wall-clock timeout in §7 applies to manual runs too.

---

## 7. Runaway protection

With §2 in force, an unbounded loop requires a *bug* — a dispatcher that misattributes
an agent entry as human, or an auto-dispatch condition that re-fires. These limits exist
to make such a bug expensive-in-seconds rather than expensive-in-dollars.

**Budget limits — auto-dispatch only (D3).** Runaway needs an autonomous cycle; a human
clicking Dispatch repeatedly is a decision, not a malfunction, and refusing it would be
the tool second-guessing its owner about his own money.

| Limit | Default | On trip |
|---|---|---|
| Dispatches per task per 24h | 3 | Refuse; log; ball → human/decision |
| Dispatches per task, lifetime | 10 | Refuse; log; ball → human/decision |
| Cooldown between dispatches of one task | 60s | Refuse; log (no ball change — it is transient) |

**Safety limits — every run, manual included.** These are correctness, not spend.

| Limit | Default | On trip |
|---|---|---|
| Live runs per task | 1, always | Refuse the second immediately |
| Concurrent runs machine-wide | 1 as shipped; set per machine from a measurement | Refuse (do not queue — see below) |
| Wall-clock per run (**batch only**) | 1800s | Terminate the run; `dispatch_result: timeout`; ball → human |
| Staleness (**session only**) | 3600s idle with an unmoved ball | `finished_without_handoff`; ball → human. **The session is not killed** (§9) |

**Tripping a cap is never silent.** Three things happen together: the run is refused, a
`note` entry naming the specific limit and its value is appended to the task, and — for
the per-task caps — the ball moves to `human`/`decision` with a `ball_prompt` saying a
task has now been dispatched N times without reaching a conclusion. A task that burns
through its daily cap is telling you something is wrong with the task, and it should
land in the human inbox for that reason, not just stop quietly.

**Refuse rather than queue.** A concurrency limit that queues turns a click into a
promise to spend money later, at a moment you are not watching. "Busy, try again" is
worse UX and better behaviour.

Defaults were chosen conservative (D3) on the explicit understanding that they are cheap
to raise once auto-dispatch has been boring for a while, and expensive to discover you
needed after a bad night.

### Choosing the machine-wide ceiling (task-191, 2026-08-20)

**The ceiling is a machine-local number and it ships at 1 for a machine nobody has
measured.** `limits.max_concurrent_runs` lives in `~/.agentjobs/dispatch.yaml`, which is
never in a repository, so nothing here prescribes a value — it prescribes how to arrive
at one, and records what happened when this project's own machine was measured.

**Measure the gate, not the agents.** An agent spends most of its wall clock reading,
thinking and editing, none of which contends with anything. The moments that contend are
the ones where two of them run the project's full test gate at once. So the measurement
is: run the gate alone, then run N of them at once in separate checkouts, and compare
worst-case wall clock against the solo run. This is only meaningful once concurrent gates
are *possible* — see task-187, which is why every checkout now derives its own Playwright
and benchmark ports from its own filesystem path.

**What that produced here** (Ryzen 9 5900XT, 16 cores / 32 threads, 64 GB; 2026-08-20,
with one AgentJobs server generation resident and 22 hand-started Claude Code processes
already using 7.3 GB):

| Simultaneous gates | Worst-case wall clock | vs solo | Peak CPU |
|---|---|---|---|
| 1 | 355s | — | ~20% |
| 2 | 388s | +9% | ~25% |
| 4 | 411s | +16% | ~46% |
| 6 | 444s | +25% | brief 100% |

Every run at every level passed. The degradation is sublinear and there is no cliff
inside the range that was tried.

**Two things follow, and the second is the one that decides the number.**

*The gate is not the binding constraint on this machine.* The spec for task-191 predicted
it would be, and the earlier figure on task-187 — two gates taking roughly 25 minutes —
implied a cliff at two. That figure does not reproduce; it was taken before roughly 20
stale server generations were killed off this machine (task-196) and before a task-record
error that failed `test_agentjobs_context_paths_exist` on `main` was found. Whatever
eventually binds here is further out than four simultaneous gates.

*What binds instead is the human, and that is §2 doing its job rather than a shortfall.*
Every run that finishes is a review request, and the loop is human-clocked by design. A
ceiling above what one person can hold in their head does not produce more merged work;
it produces a queue of unreviewed branches, which is the queue this section refuses,
relocated into a person. The machine-wide ceiling is therefore set from **review
bandwidth bounded by a measurement**, not from the measurement alone.

**The ceiling counts dispatched runs and nothing else.** A session a human starts by hand
is invisible to it. On the machine above that is not a corner case — two or three are
normally open — so the value must leave the machine headroom for work the cap cannot see.
That is why the worst case worth measuring at a ceiling of N is N + the hand-started
sessions, which is what the six-gate row is.

**Set to 3 on this project's machine.** Six simultaneous gates is the honest worst case at
that ceiling, it costs 25%, and it passes. Rejected: *6 or higher*, which the hardware
would take and the reviewer would not, and which would spend the headroom the invisible
hand-started sessions need; *2*, which leaves measured headroom unused for no stated
reason; and *raising the shipped default in `config.py`*, because one 16-core machine's
numbers are not a laptop's and the setting is machine-local precisely so its owner
decides.

### Does a raised ceiling imply a queue? No — and the reason gets stronger, not weaker

Answered here so it stops being re-opened by whoever next finds the refusal annoying
(task-191, ac-5).

The recorded reason for refusing is that **a queue turns this click into a promise to
spend money later, when nobody is watching.** Raising the ceiling does not weaken that
argument. It sharpens it, in two ways:

- At a ceiling of 1, a queued dispatch would start within minutes, because the thing
  ahead of it is one run. At a ceiling of 3 the machine is only ever full when three
  agents are already working, which is exactly the moment a queued fourth would sit
  longest and start furthest from the click that authorised it. The gap between "I meant
  this" and "this ran" is widest precisely where a queue would be doing its work.
- §2's rule is that a run is attributed to a human log entry that caused it. A queue
  breaks the timing that rule depends on: the entry is written now, the run happens at an
  unpredictable later moment, and the authorisation the guard checks has meanwhile become
  a statement about a repository state that no longer exists. `require_clean_tree`,
  `claim_lost` and `owner_mismatch` are all judged at spawn time for this reason, and a
  queued run would fail them at a moment nobody is present to read the failure.

**And the refusal is now cheap to act on**, which was the other half of the complaint. It
names the runs holding the slots and the task each is working, rather than reporting a
count — so "busy, try again" comes with somewhere to go. A count was a dead end on the
task page specifically, because that page's run list shows only *this* task's runs and the
run occupying the machine is by definition on a different task.

What would reopen this: auto-dispatch becoming the normal way runs start. The refusal is
right for a click, because a person is there to read it. A condition that fires on its own
and is refused has nobody to tell, and that is a genuinely different problem — it wants a
retry policy rather than a queue, and it is not this section's answer to give.

---

## 8. Concurrency

Task-055 is **closed, completed** (merged 2026-08-10). It was named a hard prerequisite
for implementation and that condition is now satisfied. What it provides:

- `TaskStorage.mutate_task` holds a per-task advisory lock (`O_CREAT|O_EXCL` lockfile,
  works on Windows) across the whole read-modify-write, not just the write.
- Compare-and-swap: a write whose file changed since it was read fails with a typed
  conflict error.
- `manager.claim_task` checks claimability and sets the owner inside the lock; the loser
  of a race gets a reportable "already claimed by X".

Dispatch inherits all of that and adds one thing the storage lock cannot cover: **two
dispatch requests must not spawn two processes for the same task.** The file lock
protects a state transition lasting microseconds; a run lasts half an hour. So:

- **The dispatcher claims before it spawns.** If the task is `ready`, `claim_task` runs
  first, under 055's lock. If the claim loses, nothing was started and the cost of
  losing is a rejected HTTP request. Having the *child* claim itself after spawn would
  mean two processes start and one discovers, after paying for a model call, that it
  lost.
- **One live run per task**, enforced by a run lockfile at
  `~/.agentjobs/runs/.locks/<task_id>.lock` held for the run's lifetime — the same
  primitive 055 chose, for the same reason (atomic on Windows, no dependency, and no
  lease to renew).

  *Amended 2026-08-20 (task-190), after this design's original answer to a stale lock —
  "a timeout with an error naming the file" — turned out to make a leaked lock
  **permanent and silent**. Three corrections, and the primitive itself is unchanged:*

  - The lock is **the file's existence, not an open descriptor**. `acquire_run_lock`
    closes the handle before returning. Nothing ever read it, and keeping it open both
    blocked `unlink` on Windows — so the "delete the file" remedy failed for as long as
    the leaking process lived — and leaked a handle per run in a long-lived server.
  - The lock **names its run**, written by `RunLock.adopt` as soon as `runner.start`
    produces a run id. Before this every lock file on disk read `run=` empty, so nothing
    could ask whether the run holding it was over.
  - A lock is **reclaimed when it can be shown not to be held**: its named run has a
    terminal record, or (with no run record to consult) the process that took it is
    gone. Never on elapsed time — there is still no lease and no heartbeat, which is
    what the original choice of primitive was protecting. Being unable to tell refuses.
    `release_stale_locks` applies the same rule as a sweep inside §9's startup
    reconciliation, after it concludes orphaned runs, so a restart heals a leak instead
    of causing one.

  Releasing is correspondingly no longer the private business of the in-memory
  `RunHandle` the dispatch created. That object returns with the dispatch call, and the
  session poller — which is what actually concludes every session run — rebuilds its
  handle from disk. A rebuilt handle attaches a `RunLock` by path; the release refuses
  to delete a lock that has come to name a different run.
- **A run directory per run, no shared index.** `~/.agentjobs/runs/<run_id>/` is written
  only by its own supervisor, so listing runs is a directory scan and there is no
  contended index file to lock. Same reasoning as the task-per-file layout.

Re-dispatch of an already-`active` task (ball `agent`/`revise` after changes were
requested) does not claim — it verifies the existing owner matches the runner's agent
and that no live run holds the task lock.

### Working-tree isolation: cwd stays the project root, and the agent takes its own worktree

*Added 2026-08-18, resolving task-075's open cwd question. **Amended 2026-08-19
(task-186): AgentJobs no longer passes `-w`.** The cwd conclusion below is unchanged and
still correct; the paragraph that said isolation is supplied by AgentJobs is the part
that was wrong, and it is corrected in place rather than deleted, because the reasoning
that produced it was sound on the evidence available at the time.*

A run's `cwd` is the shared project root, exactly as the `dispatch` log entry above
records it. That looks like the failure task-075 exists to prevent — two dispatched runs
editing one working tree — and it is not, because **cwd and working tree are different
things here**. The runner is invoked *from* the repository and takes its own task-named
worktree before doing any work. cwd is where the CLI is launched; the worktree is where
it writes.

Setting cwd to a worktree instead would be actively wrong. The worktree does not exist
until the runner makes it, so AgentJobs would have to create one first — which is the
worktree pool this task considered and rejected (see task-075's decision entry). It also
breaks `claude agents --json --cwd <project-root>`, which is how §9's poller scopes the
session listing to one project.

#### The `-w` flag cannot be used, and here is the evidence

Until 2026-08-19, `posture_flags()` added `-w <task_id>` to every writing posture, on the
reasoning stated here originally: isolation *"is supplied by AgentJobs rather than left to
the agent to remember … so a dispatched run cannot fail to take a worktree the way a
human-driven agent can."* That reasoning was sound and its premise was false. The
isolation `-w` grants is enforced by a guard, shipped inside Claude Code, that refuses
every git operation a `-w` session aims at the shared checkout.

Probed directly on 2.1.235 in a throwaway repository with no AgentJobs configuration in
it, 2026-08-19:

```
claude -p --permission-mode auto -w probe1 "run: git -C <repo> status --porcelain"
```

> This session is isolated in the worktree …\.claude\worktrees\probe1, but this command
> redirects git to the shared checkout via -C. Refusing to run it — a worktree-isolated
> session's git operations must target its own worktree.

Four things about that refusal decide the design:

- **It is not a shallow `-C` check.** `cd <repo> && git status` is refused too, with a
  message naming the `cd`. There is no wrapping that gets past it, and building one would
  be circumventing a safety mechanism rather than fixing anything.
- **It is not configurable.** `--add-dir <repo root>` makes no difference — the guard is
  about git redirection, not filesystem access. `claude --help` exposes `-w/--worktree`
  and `--tmux` and nothing that softens either. It is reported as hook output but no user
  hook defines it; it ships in the CLI.
- **It forbids exactly the two things this project's process requires.** Every task
  record is committed to `main` in the shared clone
  ([ENGINEERING.md, "Task files live on `main`, always"](../ENGINEERING.md)), and the
  merge gate rebases and merges there. A `-w` run can do the work and then neither record
  nor merge it.
- **The cost was being paid, silently.** Every dispatched run to date ended with an
  uncommitted task record sitting in the shared clone waiting for a human to notice —
  observed on run_0c91653d, the first dispatched run to reach a closeout at all. Nothing
  looks broken, because the file is on disk and the dashboard reads it.

The trade is therefore explicit: **guaranteed containment that can never complete a task,
versus agent-taken containment that can.** A property that guarantees the run cannot
finish is not containment; it is a stall with good intentions. Task-186 chose the second.

#### Isolation comes from the agent, and where that is stated

The dispatched agent takes its own worktree, exactly as every other agent in this
repository is already required to
([ALLAGENTS.md, "Why you get your own worktree"](../ALLAGENTS.md)):
`git worktree add ../worktrees/aj-<nnn> -b <type>/task-<nnn>-<slug>`, before anything is
written.

Verified by running it rather than by reading about it, 2026-08-19: with `-w` omitted,
cwd at the repository root and `--permission-mode auto`, a session created a sibling
worktree, wrote a file into it, committed into it with `git -C`, and read the shared
clone's status — no refusal, no permission prompt, and **no `--add-dir`**. Writing outside
cwd was the one thing that could have made this cost a new flag; it does not.

The path in that probe was a direct sibling, `../aj-<nnn>`; the convention has since moved
one level deeper, to `../worktrees/<repo>-<nnn>`, so worktrees stop crowding the workspace
root. The probe still holds — both paths are outside cwd, which is the only property the
permission model distinguishes, and depth is not a second gate.

**The mechanism that keeps two dispatched runs out of one working tree** — the question
task-075 exists to answer, and the one this section must not leave vague — is now three
things, in order of when they act:

1. **The prompt stub says it, in the lines guaranteed to be read first.**
   `PROMPT_STUB` carries one imperative clause: the run is in the project's shared
   working tree, is not isolated, and must take a worktree before writing anything. This
   deliberately duplicates a line of the guide, against the stub's own
   pointer-not-a-composition rule, and the exception is earned: containment is the only
   instruction that must be obeyed *before* the agent reads anything, the guide included.
   A pointer cannot carry an instruction that has to precede following the pointer.

   **The clause gives the shell command and forbids the built-in tool (task-192).** As
   first shipped it said only "take your own git worktree", and a model satisfies that
   with Claude Code's `EnterWorktree` tool — the tool named for that sentence. That tool
   asks to relocate the session's permission root outside `.claude/worktrees/`, which
   `auto`'s classifier declines and a `--bg` session cannot answer, so the run parks
   indefinitely: task-020's failure through a different door, observed on run_6f1f0741
   on 2026-08-20, the first dispatch after this section was written. Neither posture nor
   a `--settings` pre-approval is the fix — an escalation gate is not an allow-rule, and
   `bypassPermissions` would drop every gate to clear one prompt. `git worktree add`
   needs no relocation at all, so the stub names it literally, and a test asserts both
   the command and the prohibition are in the rendered prompt.
2. **The guide states it in full**, at the top of
   [`docs/agent-workflow.md`](agent-workflow.md) rather than buried beside the claim, and
   as a general property of dispatch rather than a fact about this repository.
3. **`require_clean_tree` turns a violation into a refused spawn.** The precondition
   already refuses to start a run when the project root has uncommitted changes. If a
   dispatched run does write the shared tree, the *next* dispatch stops loudly instead of
   entangling two runs' work. It detects rather than prevents, and it is named here so the
   new arrangement is not mistaken for having no mechanism at all.

    This backstop was inert until task-182. `working_tree_clean` ran a bare
    `git status --porcelain` with no exclusion for the project's tasks directory, so
    dispatch's own writes to a task record tripped it — at both ends of a run, the claim
    before the spawn and the terminal `dispatch_result` entry after the agent's last
    commit. A check that refuses every dispatch is not a backstop, and in practice it was
    switched off to get any work done. The tasks directory is now excluded (§6), so the
    check fires on a dispatched run's stray writes and on nothing else.

    It still detects rather than prevents, and it is blind inside the tasks directory. A
    run that violates containment by writing *only* task YAML is not caught by mechanism 3;
    mechanisms 1 and 2 are what carry that case.

**What is genuinely lost, so it is not rediscovered as a bug.** An accident is no longer
automatically confined. Under `-w` a confused run wrote into a git-locked worktree nobody
else had checked out; now it writes wherever it is told to. This section already limited
what that was worth — *a worktree is not a sandbox* — and the argument it propped up has
been superseded besides: worktree containment was what made `acceptEdits` defensible as a
default, and `auto` has been the default since task-020, gated by a classifier evaluating
every action rather than by the working tree.

The limitation for other CLIs is unchanged in substance and simpler in form: **no runner
gets isolation from AgentJobs, whatever CLI it drives.** Revisit the first time two
dispatched runs are seen writing the same tree.

#### Reaping, and what it means now

**Removing a worktree is no longer part of the run's lifecycle, because a dispatched run
no longer has a worktree AgentJobs knows about.** `claude rm` still removes a finished
session's row and frees the pid it holds, and the ledger's `reap` is still the path that
calls it — verified 2026-08-19 that `claude rm` on a worktree-less background session
exits 0 and prints `removed <id>`, so this is a narrowing rather than a silent no-op.
Freeing a pid a finished session holds is real work and worth doing on its own.

What is gone is the *refusal*: `claude rm` declining to delete a worktree with
uncommitted changes was read here as a signal that a run had produced work nobody had
looked at. There is nothing for it to fire on now. Refusals are still surfaced and never
forced, because a session AgentJobs did not start can still own a worktree — and because a
refusal can also be a transient Windows file handle rather than unreviewed work (observed
2026-08-19; the retry seconds later succeeded).

The worktree a dispatched agent makes for itself is outside AgentJobs' knowledge
entirely. Removing it is the agent's own closing step and `git worktree list` is the
inventory. AgentJobs deliberately does not go hunting for directories it did not create in
order to delete them.

Reaping happens at server startup, as the poller settles each session, and on demand via
`agentjobs dispatch reap`.

---

## 9. Process lifecycle

**Rewritten 2026-08-18**, after §4 settled that dispatch drives Claude Code's session
manager. This section was written without knowing that manager existed, and most of it
described building a worse copy of it. What follows is split by runner mode, because
the two modes genuinely have different lifecycles — not because one is a degraded
version of the other.

The precedent to avoid still governs both, and is explicit in this repo:
`WebhookManager._dispatch` runs in a detached asyncio task, and a `NameError` inside it
was invisible for months (task-047). A detached coroutine whose exception nobody awaits
is a silence generator. **Do not repeat that shape.**

### Session mode

`--bg` returns immediately and the CLI owns the process. AgentJobs owns the *record*.

- **No supervisor thread.** A poller over `claude agents --json --cwd <project-root>`
  reads state. `--cwd` scopes the listing to one project, so an unrelated session
  elsewhere on the machine is never mistaken for a dispatched run.
- **No run directories, no stdout capture.** `claude logs <id>` owns the output, and it
  is an **ANSI pty scrape, not structured events**. AgentJobs should link to it, never
  parse it. This is a real loss against batch mode's `stream-json`, accepted knowingly.
- **The session id cannot be assigned.** `--bg` ignores `--session-id`. Capture the short
  id from stdout at spawn and store it on the run; correlate through the ledger after.
- **Sessions do not exit.** A finished session sits at `idle`/`done` holding its pid
  until `claude stop` or `claude rm`. **Reaping is AgentJobs' job**, and it is an
  obligation this mode *adds* rather than removes.

Cancellation delegates: `claude stop <id>` stops a session and keeps its conversation;
`claude rm <id>` removes the session's row and frees its pid. It would also delete a
worktree the session owned, and refuse while that worktree held uncommitted changes — but
a dispatched session owns none since task-186, so neither applies to a run AgentJobs
started. See §8's reaping note.

### Batch mode

The original model, retained in full and now scoped to runners that declare `batch`:
one dedicated **thread per run** doing a blocking `subprocess.Popen` + `wait()`. Not an
asyncio task, not a fire-and-forget coroutine. The thread body is wrapped so that a
terminal `dispatch_result` entry is written **on every path, including an unexpected
exception in the supervisor itself** — the `except` clause writes `outcome: crashed`
with the traceback rather than logging a warning and returning.

The ordering that makes this robust: the run directory and its `meta.yaml` (status
`starting`) are written to disk **before** `Popen`. A supervisor that dies between those
two points still leaves a row for reconciliation to find. A supervisor that dies before
writing anything never started a process.

Output goes to `~/.agentjobs/runs/<run_id>/stdout.log` and `stderr.log`, streamed to
disk, not buffered in memory — a long run can emit a lot, and holding it in RAM to write
at the end loses all of it when the interesting case (a crash) happens. Logs stay
machine-local and are never written into the repository: they are large, they are not
review material, and they may contain content the agent read from uncommitted files.

Spawn with `CREATE_NEW_PROCESS_GROUP` (Windows) / `start_new_session=True` (POSIX) so the
whole tree can be signalled — an agent that shelled out to `pytest` must not leave the
`pytest` behind. Cancel with `CTRL_BREAK_EVENT` / `SIGTERM` to the group, then a 30s
grace period, then `taskkill /T /F` / `SIGKILL`. The grace period exists so an agent can
finish a `git commit` rather than being killed mid-write. Windows is the primary
development platform, so it is the reference implementation, not the port.

### Outcomes

| Outcome | Condition | Ball afterwards |
|---|---|---|
| `completed` | Work finished **and** the ball moved during the run | Whatever the agent set |
| `finished_without_handoff` | Work finished but the ball is unchanged | Forced to human/decision |
| `failed` | Non-zero exit — **batch only** | Forced to human/decision |
| `timeout` | Batch wall-clock limit hit | Forced to human/decision |
| `cancelled` | Human cancelled (`claude stop` in session mode) | Forced to human/decision |
| `crashed` | Supervisor raised — **batch only** | Forced to human/decision |
| `interrupted` | Batch run found non-terminal at server startup | Forced to human/decision |

`finished_without_handoff` is the one worth arguing for. **A run that ends without moving
the ball is a failure**, even on a clean exit, because the agent did not complete the
contract in schema-design §5 — it stopped without stating what it needs. Treating a clean
exit as success regardless would reproduce, at the process level, exactly the limbo the
ball model was introduced to make unrepresentable.

!!! warning "Session mode cannot distinguish every outcome, and the gap is not cosmetic"
    `claude agents --json` reports `status`/`state` pairs — `busy`/`working`,
    `waiting`/`blocked`, `idle`/`done`, `idle`/`blocked`, `stopped` — and **no exit
    code**. So `completed` and `finished_without_handoff` are indistinguishable *in the
    ledger* (both `idle`/`done`) and must be told apart by asking whether the ball moved,
    which is where §5 always got it. But `failed` and `crashed` have **no representation
    at all**: a session that errors internally still reports `idle`/`done`. Batch mode
    gets both free from the process exit. This is the price of session mode, and it is
    why batch was retained rather than deleted. One error is now named anyway — an
    expired login, read from the session transcript rather than the ledger, because the
    ledger cannot carry it. See below.

Every forced ball move is logged with the dispatcher as actor, not the agent. This
requires a reserved `dispatcher` actor id, since `validate_actor` checks against the
project's configured vocabulary.

### A parked session is a handoff, not a stall

Unique to session mode, and the reason §4's `supervised` posture is implementable at all:
a run that meets a command outside its allow-list parks at `waiting`/`blocked` and waits
indefinitely. That is a **reportable state**, so the dispatcher turns it into a ball move
to `human`/`input` with the pending command quoted in the `ball_prompt`. The human
answers from any device, and the session resumes.

It follows that a parked run is never escalated by a timeout — see §4. A timeout is not
a human act, and §2 requires that every grant of autonomy trace to one.

### An expired login is the one session failure the ledger cannot see

A parked session is alive and waiting. An **expired login is the opposite**, and that
difference is why it went unnoticed for two days.

Claude Code does not refresh its own credential per session: a shared background daemon
owns the OAuth refresh for every `--bg` worker. On 2026-08-21 that refresh failed four
times over three minutes, after which the daemon discarded a token its own log line calls
`(token still valid)`, forty seconds before it would have expired anyway. Every
dispatched session on the machine died mid-turn. The desktop app, same account and same
machine, was unaffected throughout. task-224 has the timeline.

What a dying session does is **end the turn**: it emits one synthetic assistant message
saying `Login expired · Please run /login` and goes idle with nothing pending. So
`claude agents --json` reports `idle`/`done` — byte-identical to a session that finished
its work — and §9's `finished` path settles it. `run_a1e35ca5` is in the ledger as
`outcome: completed` after losing six minutes to a dead credential and needing a human to
notice and re-authenticate.

`dispatch.auth` closes that gap, and its scope is deliberately one thing: **make the
failure legible in the tracker**. The expiry itself is Claude Code's and the account's,
not AgentJobs'.

- **The signal is the session's own JSONL transcript**, where the failing turn carries a
  top-level `"error": "authentication_failed"`. Across every session log on this machine
  that field had three occurrences and all three were genuine. It is parsed as JSON and
  matched on the field, never grepped as a substring — any session that reads *about*
  this bug has the string in its own transcript.
- **A poll that finds one parks the run and hands the ball to `human`/`input`**, with a
  `ball_prompt` naming the only thing that fixes it: `claude auth login`, in a terminal,
  on that machine. Answering inside the session cannot work; the credential is already
  gone, so a message sent to a stalled session is retried against nothing and fails in
  milliseconds. Verified, 2026-08-21.
- **Parked, not finished, and not reaped.** Recovery is in place: after a re-auth the
  already-running session picks up where it stopped, with no restart and no re-dispatch.
  Reaping would destroy that and turn six lost minutes into a lost night. Holding the run
  lock is correct for the same reason — a fresh dispatch at the same task would die
  exactly as this one did.
- **It clears itself.** The dead line stays in the transcript forever; what makes it
  history is a real model reply underneath it. After that the run settles through the
  ordinary `finished` path.

Three things were considered and rejected, and are named here so they are not
re-proposed. A **token refresher or `ANTHROPIC_API_KEY` fallback** puts AgentJobs in the
credential business, which is task-066's territory, and `claude setup-token` strips the
claude.ai connectors this project's own MCP server runs on. A **retry loop** is verified
useless: the retry fails in nine milliseconds and burns the run's turns for nothing.
Reading **`~/.claude/daemon.log` or `daemon-auth-status.json` as a live gate** relies on
undocumented internals, and the status file is a latch nobody clears — it still read
`auth_required` three hours after a successful re-auth while six workers ran fine on the
new token.

### Restart, reconciliation, and the rule that reversed

**Batch: a run does not outlive its supervisor.** On graceful shutdown every live run is
cancelled; on startup any run directory in a non-terminal state is declared `interrupted`
and hands the ball to a human. Pid adoption was rejected — it is unreliable (pid reuse;
matching start times needs `psutil`), and it produces an orphaned autonomous agent
editing a repository with nothing supervising it and no working kill switch.

**Session: that rule is reversed, and the reversal is the point.**

!!! note "Why the original rule no longer applies"
    It was chosen to prevent an unsupervised orphan with no kill switch. For a
    remote-controlled session neither half of that premise holds: `claude stop <id>` is a
    working kill switch that does not involve AgentJobs at all, and the session is
    visible and reachable from any device. Killing live sessions because `agentjobs
    serve` restarted would destroy real work for no safety gain — including a session
    someone was mid-conversation with on their phone.

So startup reconciliation inverts. Rather than declaring survivors `interrupted`,
AgentJobs **re-attaches**: it reads `claude agents --json`, matches sessions against its
own ledger, and resumes tracking. A recorded session that is absent from the ledger is
genuinely gone and gets a terminal entry; one that is present is simply still running.

**And then it sweeps the run locks** (added 2026-08-20, task-190). Reconciliation
settles *runs*; until this, nothing settled the lock files those runs had taken, so the
restart this section is about stranded every one of them — permanently, because the only
release path needed an in-memory handle that had just died with the process. `reconcile`
now finishes by deleting every lock whose named run has a terminal record, or whose
holding process is gone when there is no run record to consult. The order is
load-bearing and not interchangeable: an orphaned batch run reads `running` on disk until
this section's rule declares it `interrupted`, so a sweep that ran first would find every
lock apparently live and leave the entire set behind.

This is the one place a restart is *supposed* to change locking state, and it is why the
lock can keep a primitive with no automatic release without a leak being permanent.

### Staleness replaces the wall-clock kill, for sessions

The 1800s wall-clock timeout stays for `batch` and is wrong for `session` — thirty
minutes is right for a batch and wrong for a session a human might pick up hours later.
Killing it at the deadline would destroy the property that made session mode worth
choosing.

**Replacement: a session sitting `idle`/`done` with an unmoved ball for 60 minutes is
treated as `finished_without_handoff` and the ball moves to a human. It is not killed**,
and it remains attachable afterwards. This preserves what the timeout was actually
protecting — a task quietly going nowhere — without conflating "nobody is looking at
this" with "this should be destroyed."

Sixty minutes was chosen over fifteen (too many false positives when an agent pauses
mid-work) and over four hours (catches only overnight stalls).

---

## 10. Rejected alternatives

Recorded so they are not relitigated. The invocation candidates (§4) and trigger
candidates (§5) are argued where they arise; these are the rest.

- **A shell command string instead of an argv list.** `shell=True` with `{task_id}` and
  `{prompt}` interpolated is one line shorter and turns every quoting bug into an
  injection. Task ids are validated slugs today, but `ball_prompt` is free markdown
  written by an agent, and the prompt is substituted into the command. Argv lists make
  the entire class impossible rather than currently-unreachable.

- **Runners in the versioned `.agentjobs/config.yaml`.** Would travel with the repo,
  which is a feature for categories and actors and a vulnerability for "what executable
  to run." Rejected in §6, gate 2. The general principle, already stated in
  `projects.py`: the registry is machine-local and disposable because a machine's
  capabilities are not a project's property.

- **A dispatch counter field on `Task`.** More schema surface, a value that can drift
  from the evidence, and a migration. Counting `dispatch` log entries gives the same
  number, in git, next to the reason for each increment.

- **Composing a rich prompt at dispatch time** (summary + last handoff + acceptance,
  templated). Rejected in §4: the resumption contract already promises the record is
  sufficient, so a composed prompt is a second copy of it that will drift, and it makes
  dispatch depend on schema shape. If the record is not sufficient to resume from, that
  is a bug in the record — fix the record, not the dispatcher.

- **A real job queue (Celery, RQ, or a database-backed queue).** Buys durable retries,
  distributed workers, and scheduling. All three are things this design deliberately
  does not want: retries and scheduling are how a human-clocked loop becomes an
  autonomous one, and distribution is out of scope by the task's own terms (one machine,
  one person). It would also add a broker to run for a system whose selling point is a
  directory of YAML files.

- **Reusing the webhook dispatcher's asyncio shape** for the run supervisor. Rejected in
  §9; it is the shape that hid task-047's bug for months.

- **Letting the GUI define runner commands** (rather than only toggling projects).
  Rejected in §6, gate 3 — it would make the browser-reachable surface able to widen the
  RCE surface, which is the one thing the machine-local split is protecting.

---

## 11. Decisions (resolved with Jeff, 2026-08-10)

**D1 — Dispatch is a separate action from approval; auto-dispatch is designed now and
built later.** Approve means "I agree"; Dispatch means "spend money now". Auto-dispatch
becomes an opt-in per project in a later task, gated behind the full safety layer.
*Rejected:* auto-on-approval from day one — ships the autonomous path and its protections
simultaneously, with no period of watching the manual path behave. *Rejected:* explicit
dispatch forever — forecloses the automation the project exists to reach.

**D2 — Enablement is available from both the CLI and the GUI, but runner definition is
CLI/file only.** Jeff asked for both surfaces. The concern raised was that dispatch makes
the unauthenticated localhost API into remote code execution, so the switch granting that
should not be browser-reachable. Resolved by splitting the capability rather than the
surface: the GUI may enable and disable a project against runners already defined
machine-locally; it may not define what command runs. Disable is always one click.

**D3 — Budget caps bind auto-dispatch only; safety limits bind every run.** Jeff's
reasoning, adopted: runaway requires an autonomous cycle, and a human clicking Dispatch
repeatedly is a decision, not a malfunction. Per-task counts and the cooldown therefore
apply only to auto-dispatch. One-live-run-per-task and the wall-clock timeout apply
always, because they are correctness rather than spend. Conservative starting numbers
(§7), to be raised once auto-dispatch is boring.

    Amended 2026-08-18: "the wall-clock timeout applies always" now reads *per mode*.
    Batch runs keep the terminating timeout. Sessions get the staleness rule instead,
    which moves the ball rather than killing the run — and session mode has **no spend
    ceiling at all**, since `--max-budget-usd` is `--print`-only. D3's reasoning is
    unchanged; the mechanism it relies on is not available in both modes.

**D4 (agent's call, recorded for objection) — the loop is human-clocked (§2).** Not put
to a vote because it is a consequence of D1 rather than an independent choice, but it is
the load-bearing rule and the one to revisit first if the design ever feels too
restrictive. **Superseded by D5 the following day** — which is what "revisit first" was
for.

**D5 — bounded autonomy replaces human clocking (§2a). Jeff, 2026-08-11.** In his words:
"change that D4 ruling so it doesn't need human clicks for no reason, just ensure there
are acceptance criteria that will stop it, plus safety guardrails." A chain may proceed
without a human act per turn if a human authorized it in advance with an evaluable
termination condition, an iteration cap, and a ceiling. *Rejected:* keeping D4 — it
bought no safety that a declared bound does not, and it foreclosed the agent-loop
workload that motivated dispatch in the first place. *Also rejected:* allowing chains
whose termination is prose — a condition only a human can evaluate is not a termination
condition for an unattended loop, so those stay refused rather than merely capped.

---

## 12. Relationship to other tasks

- **task-046 (agent → human).** The other half of the loop. 046 carries the notification
  direction and explicitly defers a pluggable notification service to a future task,
  naming `task.status_changed` as its extension point. Dispatch is the **return path 046
  never scheduled**, and it deliberately does *not* reuse that extension point (§5): a
  notifier fans out on every handoff, a dispatcher must fire on human acts only. They
  are siblings, not layers.
- **task-055 (write race).** Was named a hard prerequisite for implementation.
  **Closed/completed 2026-08-10** — implementation is unblocked. What it provides and
  what dispatch adds on top is §8.
- **task-052 (schema v2 manager API).** **Closed/completed.** `ball`, `claim_task`,
  `handoff`, and typed log entries all exist, which is why this design can read intent
  from `ball` instead of inferring it from v1 statuses.
- **task-053 (CLI).** The v2 verbs are not yet on the CLI (`agentjobs claim` does not
  exist today; this task's own claim went through the Python API). The `agentjobs
  dispatch` command group in §13 should land alongside or after that work rather than
  inventing its own conventions.

Both hard prerequisites are satisfied. Implementation can begin whenever the derived
tasks are scheduled.

---

## 13. Derived implementation tasks

Seven, each independently reviewable and each leaving the system in a working state.
The first four are the minimum for a usable manual dispatch; 5 and 6 make it safe to
live with; 7 is the automation, deliberately last.

1. **Machine-local dispatch configuration.** `~/.agentjobs/dispatch.yaml`, its model and
   loader, runner resolution and argv substitution (no shell), the master switch, and the
   `DISPATCH_DISABLED` sentinel. No spawning yet — this task is the config surface and
   its validation, testable in full without starting a process.

2. **Schema: dispatch log entries.** Add `dispatch` and `dispatch_result` to
   `LogEntryType`, define their `data` payloads, reserve the `dispatcher` actor id so
   `validate_actor` accepts it, and add the derived dispatch-count helper on `Task`.

3. **The runner and its supervisor.** Spawn with a process group, stream output to the
   run directory, one supervisor thread per run with a guaranteed terminal entry on every
   path, the outcome vocabulary from §9 including `finished_without_handoff`. Explicitly
   not the asyncio shape from task-047. Windows is the reference platform.

4. **The dispatch API and the guard layer.** `POST /api/tasks/{id}/dispatch`, the
   human-clocked precondition (§2), clean-tree and claim-before-spawn (§8), the safety
   limits, and the refusal paths with their log entries. This is where §2 becomes one
   checkable function.

5. **Run ledger, cancellation and reconciliation.** Run directory layout, the per-task
   run lock, `agentjobs dispatch cancel|stop|status`, cancel-on-shutdown, and startup
   reconciliation marking non-terminal runs `interrupted`.

6. **GUI: dispatch surface.** A Dispatch action beside Approve on a `ball: agent` task,
   live run status with a cancel button, a link to the run's output, and the
   per-project enable/disable toggle from D2 — enable/disable only, never runner
   definition.

7a. **Permission posture for a dispatched agent (task-076).** ~~Blocks task-070.~~
   **Decided 2026-08-18** — three postures, defaulting to `supervised`. See §4. What
   remains under this task is documentation, not a decision.

7b. **Session launcher vs batch runner (task-077).** ~~Open amendment.~~ **Decided
   2026-08-18** — dispatch drives `claude agents` in session mode, with `batch` retained
   as a second declared runner mode. See §4 and the rewritten §9. Items 3 and 5 above
   are rescoped by this rather than superseded: item 3 shrinks to spawn / capture-id /
   poll for sessions and keeps its supervisor for batch, and item 5 keeps its ledger,
   because `claude agents --json` has no run-to-task mapping and no history once a
   session is removed.

7c. **Runner groups (task-177).** ~~Part of the deferred profile layer.~~ **Built
   2026-08-19** — `runner_groups:` with per-member enable/disable, a group nameable on a
   dispatch, deterministic selection recorded in the task log, and
   `agentjobs dispatch example` as the setup route. See §4. The difficulty → profile
   table remains unbuilt, with its reopen triggers restated there.
   **Revised 2026-08-19** after review: the example's session runners carried `-p` and no
   `--remote-control`, so the config every new setup inherits could not have started a
   steerable session. Corrected, the §4 mode table corrected with it, and the example is
   now dispatched end to end by `tests/test_dispatch_example_config.py` rather than only
   parsed.

7. **Auto-dispatch (opt-in).** `auto_dispatch: true` per project, the budget caps from
   §7 with their ball-moving refusals, and the audit that a dispatch caused by an
   approval still satisfies §2. Last, and only once 1–6 have been boring for a while.

---

## Appendix: acceptance criteria coverage (task-060)

| Criterion | Where |
|---|---|
| sc-1 — decisions with rationale and rejected alternatives | Throughout; §10 and §11 |
| sc-2 — safety: opt-in, enable step, kill switch | §6 (four gates, three kill scopes, D2) |
| sc-3 — runaway protection, concretely | §2 (structural) and §7 (numbers and trip behaviour) |
| sc-4 — invocation chosen, alternatives recorded, not vendor-locked | §4 |
| sc-5 — process lifecycle, not the task-047 shape | §9 |
| sc-6 — relationship to task-046 and task-055 | §12 |
| sc-7 — derived implementation tasks | §13 |
