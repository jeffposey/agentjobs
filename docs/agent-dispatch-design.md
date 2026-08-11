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

---

## 3. Anatomy of a dispatch

Four nouns, and where each lives. The split follows the precedent already set by
`projects.py`: *what the project is* is versioned with the project; *what this machine
will do about it* is machine-local and disposable.

| Noun | What it is | Where it lives | Versioned? |
|---|---|---|---|
| **Runner** | Named recipe for starting an agent: an argv template and optional env | `~/.agentjobs/dispatch.yaml` | No — machine-local |
| **Enablement** | Whether a given project may dispatch, and with which runner | `~/.agentjobs/dispatch.yaml` | No — machine-local |
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
  codex:
    argv: ["codex", "exec", "{prompt}"]

projects:
  agentjobs:
    enabled: true
    runner: claude
    require_clean_tree: true
    auto_dispatch: false           # §5; off until the manual path is boring

limits:
  max_concurrent_runs: 1           # machine-wide
  run_timeout_seconds: 1800        # applies to every run, manual or auto
  auto:                            # applies ONLY to auto-dispatch (D3)
    per_task_per_day: 3
    per_task_lifetime: 10
    cooldown_seconds: 60
```

Placeholders (`{prompt}`, `{task_id}`, `{project_id}`, `{project_root}`, `{run_id}`,
`{agent}`, `{api_base}`) are substituted **per argv element, literally, with no shell**.
There is no `shell=True` anywhere in this design; see §10.

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
docs/schema-design.md section 5. Dispatch run id: run_a1b2c3d4.
```

!!! warning "Amended 2026-08-11 — the stub pointed at a dead document"
    This stub originally pointed at `docs/agent-workflow.md`. That file is entirely
    v1-era (`mark_in_progress`, `TaskStatus`, `status_updates`) and contains no
    resumption contract, so **every dispatched agent would have been sent to a stale
    document as its first instruction.** Found by the read-only dispatch experiment on
    2026-08-11 — the first headless agent run under this design found the bug in the
    prompt that dispatched it.

    Task-070 must point the stub at whatever is *current* at implementation time. If
    task-046 finishes rewriting `agent-workflow.md` for v2 first, point there; until
    then, `schema-design.md` section 5 is the contract. A hardcoded doc path in a
    prompt is a maintenance hazard either way, and the implementing task should add a
    test asserting the referenced file exists and mentions the contract.

Fixed text plus five substitutions. It never needs to change when the schema changes,
it cannot drift out of sync with the task record, and it is small enough to read in the
`dispatch` log entry. Composing a richer prompt would duplicate the resumption contract
in a second place and guarantee the two disagree eventually — rejected in §10.

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

### Open amendment: dispatch may be a *session launcher*, not a batch runner (2026-08-11)

**Not yet decided. Recorded because it may reshape §9 and three derived tasks.**

This design assumed the only headless option was `claude -p` — fire and forget, no way
in once it starts. Jeff asked whether a dispatched run could instead be a **remote-
controllable session** he can pick up interactively from another device. Inspecting the
installed CLI (2.1.220), the answer appears to be yes, and more of this design already
exists upstream than was known when it was written. Verified from `claude --help` and
`claude agents --help`:

| Flag | What it gives us | What it displaces here |
|---|---|---|
| `--remote-control [name]` | Interactive session with Remote Control enabled | The premise that a dispatched run is unattendable |
| `--bg` / `--background` | Start as a background agent, return immediately | §9's supervisor thread per run |
| `claude agents --json` | Active + completed sessions as JSON, **no TTY required** | §8/§9's run ledger and directory layout |
| `-w` / `--worktree [name]` | Session gets its own git worktree | Most of task-075's layer 2, for dispatched runs |
| `--session-id <uuid>` | Caller assigns the id | Correlating a run id with a session |
| `--max-budget-usd` | Per-run spend ceiling (with `--print` only) | Part of §7, for batch runs |

The CLI's own word for these is "dispatched sessions". **Claude Code already has a
dispatcher.** The open question is whether AgentJobs should drive it rather than
reimplement a worse one.

If it should, these change:

- **§9's process lifecycle is largely wrong.** No supervisor thread, no run directories,
  no pid tracking, no startup reconciliation — `--bg` returns immediately and
  `claude agents --json` is the ledger. Tasks 070 and 072 shrink to a wrapper.
- **"Runs do not outlive their supervisor" (§9) becomes indefensible.** It was chosen to
  avoid an unsupervised orphan. A remote-controlled session is not an orphan — it is
  reachable, visible, and stoppable from any device. Killing it because the AgentJobs
  server restarted would be actively wrong.
- **The wall-clock timeout (§7) needs different semantics.** Thirty minutes is right for
  a batch run and wrong for a session a human might pick up hours later.
- **The value argument changes.** §1 justified dispatch as removing a manual step. If a
  dispatched run is a real session that appears on Jeff's phone, dispatch stops
  competing with the VS Code extension and starts feeding it.

**Not verified, and load-bearing before deciding:** whether `--remote-control` composes
with `--bg`; whether a session started non-interactively actually surfaces on other
devices; and what permission mode a backgrounded session defaults to (note
`claude agents --allow-dangerously-skip-permissions`, which "make[s] bypass-permissions
mode available to dispatched sessions without defaulting to it" — an upstream posture
that task-076 should read before inventing its own).

D1–D4 are unaffected: who may cause a dispatch, and that approval is not dispatch, are
independent of what a dispatch starts. Tracked as task-077.

### Permission posture: the gap this design left (added 2026-08-11)

Nothing above says **what a dispatched agent is allowed to do**, and that turns out to
be the difference between a useful run and a useless one. A headless agent that cannot
run `pytest` cannot satisfy any acceptance criterion in the derived tasks; one that can
run anything is an unsupervised process with full shell access, which every gate in §6
exists to avoid granting casually.

Mechanically this lives in the runner's argv, so it looks like the operator's business.
It is not — it is the actual risk boundary of the whole feature, and burying it in a
config example means it gets chosen by whoever copies the example first. Deferred to a
task of its own rather than answered here (§13, task-076): the question is what a run
may do without a human present, and it deserves the same treatment §6 gave the enable
gate.

Related, and settled: **credentials are not AgentJobs' problem.** `claude -p` uses
whatever the local Claude Code install is authenticated with — a subscription login on
this machine, with no `ANTHROPIC_API_KEY` in the environment (verified 2026-08-11). The
child inherits the operator's own auth. This strengthens (b)'s rejection above: the SDK
would have made AgentJobs hold credentials that, under (c), it never sees. It also means
§7's limits govern a **usage window, not a per-token bill** — worth knowing when
choosing the numbers.

---

## 5. What starts a dispatch

**Dispatch is a distinct action from approval (D1).** Approve means *I agree with this
work*. Dispatch means *spend money now*. Collapsing them makes every approval a purchase,
and makes it impossible to approve five tasks in a review session without starting five
agents.

The trigger is an explicit `POST /api/tasks/{id}/dispatch` — a button in the review UI
next to Approve, and `agentjobs dispatch run <task-id>` in the CLI. Nothing else starts
a run.

**Auto-dispatch is designed here and built later.** A project may eventually set
`auto_dispatch: true`, which makes an approval that hands the ball to `agent`
immediately dispatch it. That is a one-line change on top of a correct manual path, and
it is gated behind everything in §6 and §7 — which is exactly why it should not ship in
the same breath as the machinery that protects it. Deferring it costs nothing; shipping
it early means no period during which the manual path was watched behaving.

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
   never do is let a repository choose what executes.
3. **Per-project enablement.** `projects.<id>.enabled`, off by default, set either by
   `agentjobs dispatch enable <project>` or by the GUI toggle (D2). The GUI may flip a
   project between enabled and disabled among runners already configured on the machine;
   it may **not** define or edit a runner's argv. So the browser-reachable surface can
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
- **The blunt one:** killing `agentjobs serve` terminates its runs too, by design — see
  §9 on why runs do not outlive their supervisor.

### Two preconditions checked at spawn time

- **The working tree must be clean** (`require_clean_tree`, default true). An autonomous
  agent committing on top of uncommitted human work entangles the two, and the resulting
  mess is hard to unpick precisely when you are least expecting it. `git_head` is
  recorded in the dispatch entry so the diff attributable to a run is always recoverable.
- **The causing actor must be human** (§2).

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
| Concurrent runs machine-wide | 1 | Refuse (do not queue — see below) |
| Wall-clock per run | 1800s | Terminate the run; `dispatch_result: timeout`; ball → human |

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
  `~/.agentjobs/runs/.locks/<task_id>.lock` held for the process's lifetime — the same
  primitive 055 chose, for the same reason (atomic on Windows, no dependency, and a
  stale lock from a killed process produces a timeout with an error naming it rather
  than a hang).
- **A run directory per run, no shared index.** `~/.agentjobs/runs/<run_id>/` is written
  only by its own supervisor, so listing runs is a directory scan and there is no
  contended index file to lock. Same reasoning as the task-per-file layout.

Re-dispatch of an already-`active` task (ball `agent`/`revise` after changes were
requested) does not claim — it verifies the existing owner matches the runner's agent
and that no live run holds the task lock.

---

## 9. Process lifecycle

The precedent to avoid is explicit in this repo: `WebhookManager._dispatch` runs in a
detached asyncio task, and a `NameError` inside it was invisible for months (task-047).
A detached coroutine whose exception nobody awaits is a silence generator. **Do not
repeat that shape.**

### The supervisor

One dedicated **thread per run**, doing a blocking `subprocess.Popen` + `wait()`. Not an
asyncio task, not a fire-and-forget coroutine. The thread body is wrapped so that a
terminal `dispatch_result` entry is written **on every path, including an unexpected
exception in the supervisor itself** — the `except` clause writes `outcome: crashed`
with the traceback rather than logging a warning and returning.

The ordering that makes this robust: the run directory and its `meta.yaml` (status
`starting`) are written to disk **before** `Popen`. A supervisor that dies between those
two points still leaves a row for reconciliation to find. A supervisor that dies before
writing anything never started a process.

### Output

`~/.agentjobs/runs/<run_id>/stdout.log` and `stderr.log`, streamed to disk, not buffered
in memory — a thirty-minute agent run can emit a lot, and holding it in RAM to write it
at the end loses all of it when the interesting case (a crash) happens. Logs stay
machine-local and are never written into the repository: they are large, they are not
review material, and they may contain content the agent read from files that are not
committed.

### Outcomes

| Outcome | Condition | Ball afterwards |
|---|---|---|
| `completed` | Exit 0 **and** the ball moved during the run | Whatever the agent set |
| `finished_without_handoff` | Exit 0 but the ball is unchanged | Forced to human/decision |
| `failed` | Non-zero exit | Forced to human/decision |
| `timeout` | Wall-clock limit hit | Forced to human/decision |
| `cancelled` | Human cancelled | Forced to human/decision |
| `crashed` | Supervisor raised | Forced to human/decision |
| `interrupted` | Found non-terminal at server startup | Forced to human/decision |

`finished_without_handoff` is the one worth arguing for. **A run that ends without
moving the ball is a failure**, even at exit 0, because the agent did not complete the
contract in schema-design §5 — it stopped without stating what it needs. Treating exit 0
as success regardless would reproduce, at the process level, exactly the limbo the ball
model was introduced to make unrepresentable.

Every forced ball move is logged with the dispatcher as actor, not the agent. This
requires a reserved `dispatcher` actor id, since `validate_actor` checks against the
project's configured vocabulary; folded into the schema task in §13.

### Cancellation, and Windows

Windows is the primary development platform, so it is the reference implementation, not
the port.

- Spawn with `CREATE_NEW_PROCESS_GROUP` (Windows) / `start_new_session=True` (POSIX), so
  the whole tree can be signalled. An agent that shelled out to `pytest` must not leave
  the `pytest` behind.
- Cancel: `CTRL_BREAK_EVENT` (Windows) / `SIGTERM` to the group (POSIX), then a 30s
  grace period, then `taskkill /T /F` / `SIGKILL`.
- The grace period exists so an agent can finish a `git commit` rather than being killed
  mid-write.

### Restart and reconciliation

**A run does not outlive its supervisor.** On graceful shutdown of `agentjobs serve`,
every live run is cancelled. On startup, any run directory in a non-terminal state is
declared `interrupted`, gets its `dispatch_result` appended to the task, and hands the
ball to a human.

The alternative — letting the child survive a server restart and re-adopting it by pid —
was rejected. Pid adoption is unreliable (pid reuse; matching process start times needs
`psutil`), and more importantly it produces an orphaned autonomous agent editing a
repository with nothing supervising it and no working kill switch. That is the precise
situation this entire document exists to prevent. Losing a mid-flight run to a server
restart is a visible, understandable cost; an unsupervised orphan is not.

Because shutdown cancels first, `interrupted` should only ever appear after a hard crash
or a power loss — which makes it informative when it does appear.

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

**D4 (agent's call, recorded for objection) — the loop is human-clocked (§2).** Not put
to a vote because it is a consequence of D1 rather than an independent choice, but it is
the load-bearing rule and the one to revisit first if the design ever feels too
restrictive.

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

7a. **Permission posture for a dispatched agent (task-076, added 2026-08-11).** What a
   run may do without a human present. Blocks task-070, because the runner cannot be
   written without an answer. See §4.

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
