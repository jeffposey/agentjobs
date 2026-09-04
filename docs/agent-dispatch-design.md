# Agent Dispatch — Design Record

**Status: ACCEPTED 2026-08-10 (D1–D3, §11) and SHIPPED. This is a design record kept for
its reasoning, not a statement of what exists.** As of 2026-08-22 `src/agentjobs/dispatch/`
is 15 modules and 9,729 lines (`wc -l src/agentjobs/dispatch/*.py`), with a `dispatch`
CLI sub-app, REST routes under `/api/dispatch`, and a React surface.

Until 2026-08-22 this line read *"Nothing here is implemented"*, over all of that. It was
written before any of it existed and never revisited. A zero-context reader — the reader
this document is written for — was told the inverse of the truth in both directions, and
the correction below is deliberately specific so the next reader can check it rather than
trust it.

**Shipped — every §13 item.**

| §13 | What | Task |
|---|---|---|
| 1 | Machine-local `~/.agentjobs/dispatch.yaml`, runner resolution, master switch, sentinel | task-068 |
| 2 | `dispatch` / `dispatch_result` log entries, reserved `dispatcher` actor | task-069 |
| 3 | The runner and its supervisor, in both modes | task-070 |
| 4 | `POST /api/tasks/{id}/dispatch` and the guard layer | task-071 |
| 5 | Run ledger, cancellation, startup reconciliation | task-072 |
| 6 | The web UI's dispatch surface and per-project toggle | task-073 |
| 7a | Permission posture — three postures, default `supervised` | task-076 |
| 7d | Per-task posture and the project's `max_posture` ceiling | task-308 |
| 7e | An epic's children inherit its dispatch-time posture | task-316 |
| 7b | Session mode as the primary path, batch retained | task-077 |
| 7c | Runner groups | task-177 |
| 7 | Auto-dispatch on approval, opt-in per project | task-074 |

**Also shipped, and described nowhere below** — the design predates all of it:
task-075 and task-186 (worktree isolation, and its removal: dispatch no longer passes
`-w`), task-241 (`agentjobs finish`, `~/.agentjobs/finishes/`, and the scripted
post-approval merge — see §5a), the `WAKE_STUB` resumed-session contract
(`dispatch/wake.py`, and §8), run phases (`phases.jsonl`, `AGENTJOBS_RUN_ID`,
`AGENTJOBS_RUN_DIR`, `scripts/run_report.py`), and the `dispatch auth-check`,
`dispatch reconcile` and `dispatch status --live` commands.

**Not shipped.** Four things this document describes in the present tense do not exist.
Each is marked *(unbuilt)* where it appears, using the convention `dispatch/config.py`
already uses:

- the `difficulty` field on a task, and the difficulty → profile table (§4). Runner
  *groups* are built; `difficulty` is not, and there is no such field in `models_v2.py`.
- optional descriptive `model` and `effort` labels on a runner (§4). `DispatchRunner`
  carries `name`, `argv`, `env`, `mode`, `actor` and `driver`.
- the per-profile `strict` setting (§4), which the document's own open-question box
  already admits was never built.
- chain-aware cap semantics in the 2026-08-18 amendment to §2a. `dispatch/auto.py`
  counts dispatches; it has no concept of a chain.

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
    [What is checked, and what is merely claimed](#what-is-checked-and-what-is-merely-claimed-added-2026-08-20-task-188).

The feared failure mode is the circular one: agent finishes → something starts an agent →
it finishes → ... unbounded tokens, unbounded writes to a repository. The usual defence
is counters and cooldowns, which bound the blast radius of a loop that is still, in
principle, permitted to exist.

!!! danger "This section claimed the loop was structurally impossible. It is not — corrected 2026-09-04, task-334"
    What stood here said the rule made the cycle **structurally impossible** rather than
    merely capped, and demoted counters and cooldowns to a backstop against a bug in the
    dispatcher. That was a claim about *this design's own paths*, written as though it
    were a claim about the machine.

    The 2026-08-21 dispatch audit tested it (finding P1-2) and it did not hold. No route
    in `src/agentjobs/api/` carries authentication; every dispatched agent is told the
    server's address in the first sentence of its prompt; and one POST naming a
    configured human writes an authorising entry the dispatcher re-reads and accepts,
    indistinguishable from a click. That forged click arrived as `manual` — which was
    the one trigger §7's caps did not bind.

    The boundary honestly stated: on a single-user machine where the agent runs as the
    user, no local secret is unreadable by the agent, so a token would not make this
    structural either. **The loop is bounded, not impossible.** What bounds it is §7's
    caps — now applied at `dispatch_task`, on every trigger, plus a machine-wide hourly
    ceiling — and the run ceiling in §8. Those are the numbers to look at when reasoning
    about a runaway; this rule is not one of them.

    The rule still does real work, and it stays: it keeps an agent-clocked cycle out of
    every supported path, which means an unbounded loop needs someone to have gone around
    the front door rather than merely to have found a bug in the dispatcher. That is a
    weaker guarantee than the one this section used to assert, and it is the true one.

    A document that overstates a guarantee is worse than one that states none, because it
    stops people looking. The audit found the exposure precisely because an auditor tested
    the claim instead of believing it.

The price of the rule is real and worth stating plainly: **"agent finishes, next agent
picks up automatically" is permanently off the table** through the supported path. No
chained autonomy, no overnight queue that drains itself. Every turn of the wheel costs
one click. That was accepted (D1) as the correct trade for a system that spawns processes
with commit access on a personal machine.

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
  *D3 was itself reversed on 2026-09-04 by task-334 — the caps now bind every trigger —
  which strengthens this clause rather than changing it.*
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

**An agent still cannot cause a dispatch *under a name this project calls an agent*.** An
agent id offered as the authoriser is refused before anything is written
(`authorizer_not_human`), and the entry that *is* written still has to pass the
human-clocked check, which reads the actor's kind from config rather than from the
request. Both are pinned by tests.

!!! warning "A second reason stood here and was false — corrected 2026-09-04, task-334"
    The sentence "structurally: agents do not have browsers" was offered as an
    independent guarantee. It is not one. Nothing about this endpoint requires a browser:
    it is an unauthenticated `POST` on loopback, every dispatched agent is told the
    server's address in the first sentence of its prompt, and an agent that sends one
    naming a configured human writes an entry the mechanical check above then accepts.
    The 2026-08-21 audit's P1-2 is the demonstration. What remains true is the mechanical
    half — and what bounds an agent that does this is §7's caps, which since task-334 bind
    the `manual` trigger such a request arrives as.

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
  session_stale_seconds: 3600      # a session that ended its turn without handing off (§9)
  session_stall_seconds: 1800      # one still claiming to work but silent; reports, never kills
  dispatches_per_hour: 30          # machine-wide takeoffs, every trigger (§7)
  auto:                            # per-task; historical name, binds every trigger (task-334)
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
none — is gated on the address answering. `probe_api_base` asks `/api/version` with a five
second timeout (`PROBE_TIMEOUT_SECONDS`, `dispatch/address.py`), and `dispatch_task` refuses with `api_base_unreachable` if nothing
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

When the run was started from a playbook, it also carries `playbook` and
`playbook_hash` — the brief's name and the sha256 of the file at instantiation. That is
the only trace a playbook leaves in this design: nothing in §2, §4, §5 or §6 changes,
no gate reads either field, and the prompt gains one appended line pointing at the file.
See [the playbooks design](playbooks-design.md) §4.2–4.3 and §6.3, which restates gate
2 for repository content: a playbook declares difficulty and never names a runner, a
group or argv.
It is absent otherwise, so a flat configuration's entries are unchanged.

`argv` is recorded verbatim, which means **secrets must never appear in a runner's
argv** — put them in `env`, which is never logged. Stated here because the recording is
the safety feature and weakening it to hide a token would be the wrong fix.

**That advice was false for `--bg` session runners for as long as they have existed, and
task-249 made it true.** `env:` was set on the process AgentJobs launches, and for a
session that process is a *launcher*: it hands the session to a persistent Claude Code
daemon, and the daemon spawns the worker from its own environment. So a runner's `env:`
reached the worker only when that launch happened to be the one that started the daemon
— 12 launches in 61 on the machine where this was found — and a runner depending on it
worked about once per daemon lifetime, silently and unrepeatably.

It is now delivered through `--settings`, which takes "a settings JSON file or a JSON
string" and travels in argv, where the daemon does deliver it. The document is written
to `session-settings.json` in the run's own directory at mode `0600`, and **only its
path** goes into argv. So the property this paragraph claims — values that argv's
verbatim recording never sees — is the one you now get. The trade, stated so nobody
finds it by surprise: those values rest in a file beside the run for as long as the run
directory does. Machine-local, user-scoped, and never committed, but on disk.

Two consequences worth knowing. A runner whose own argv already carries `--settings`
has its document read and merged rather than replaced, because the flag is not
repeatable and a second one would silently win; if that document cannot be read, nothing
is spliced and `meta.yaml` records `session_env: conflict` rather than clobbering it. And
the same channel carries `AGENTJOBS_RUN_ID` / `AGENTJOBS_RUN_DIR`, which is what makes a
dispatched session's gate records land in its own run directory rather than in whichever
run last started the daemon.

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

**A supervisor could not dispatch its children until task-022, and the paragraph that
used to stand here said so as though it were permanent.** It is worth keeping what it
said, because the reasoning was right and only the conclusion moved: a dispatch must be
caused by a stored log entry written by a configured human (§2); nothing a supervisor
writes satisfies that; therefore a supervisor started its children with the runner CLI
directly, as its own subprocesses. Those children were not AgentJobs runs — no run
directory, no ledger row, no `dispatch_result`, no reaping by the poller, and no weight
against `max_concurrent_runs`, while the supervisor itself held a slot for the whole
epic. The paragraph ended by calling "whether the ledger should learn about
agent-started children" an open question §7's caps made safe to defer.

Task-022 answers it, and the answer is that they are ordinary runs. See
[the epic walk](#the-epic-walk-one-human-act-many-runs-task-022-2026-08-23) below for how the rule
in §2 is satisfied rather than bent, and for what it costs.

The protocol the supervisor prompt points at — which child, and what to do when one
finishes, parks, dies, or leaves the parent waiting — is in
[the workflow guide](agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them),
and is now performed by `agentjobs dispatch walk` rather than by the supervising agent.

### The epic walk: one human act, many runs (task-022, 2026-08-23)

Jeff's ask, 2026-08-19: *"if I dispatch an epic task and choose autonomous, it will go
through all sub-tasks, automatically merging each into main and moving on to the next,
(assuming no major issue it can't solve itself), until all subtasks are done and then
finishing the epic"*.

That is a composition of two decisions already recorded here — task-164's one-session-per-
child supervision, and task-021's release of the merge gate at posture `autonomous` — and
composition is where its risk lives. The failure mode is not one bad merge. It is a run
that merges a bad child and then builds four more on top of it.

#### The loop is code, not an instruction

`agentjobs dispatch walk <parent>` picks the next eligible child, dispatches it, watches
its task record to a terminal state, judges it, and either continues or stops. The
supervising agent runs that one command and blocks on it.

**Rejected: leaving the loop as prose in the workflow guide** for the supervising agent
to execute, which is what it was. Every step of it is mechanical — the queue already
decides which child, dispatch already starts one, the four terminal states are
enumerated and each is a fact written to a task record, and the gate a child passed is
recorded as `outcome: completed` by the child's own `agentjobs finish`. An agent adds no
judgement to any of that, and adds the chance of getting one wrong at three in the
morning with nobody watching. The guide already carries the evidence against the prose
version: *"a supervisor that ends its turn saying it will check back periodically is not
supervising, it is asleep"* — observed 2026-08-19, with the human finding the parked
child first. A rule already broken once by the party responsible for keeping it wants a
mechanism.

What the walk does **not** do is close the parent, ever. Whether an epic's acceptance
criteria are met is a reading of evidence against criteria, which is the one step here
that is genuinely judgement; a walk that took it would be grading an epic on the strength
of its children having stopped. Exit 0 means no open child remains and hands back.

#### The authorisation, which is the part that had to be got right

§2's rule is that a dispatch is caused by a stored log entry whose actor this project
configures as a human, which keeps agent-starts-agent out of every supported path. (It
does not make the cycle impossible — see the correction in §2; §7's caps are what bounds
it.) A walk that starts five child runs cannot be allowed to weaken the rule, and does
not.

`resolve_epic_authorization` reads the human entry that authorised the **parent's**
dispatch — the parent's newest `dispatch` entry names it in `caused_by`, so the walk
inherits the same authorisation the parent run is executing under rather than deriving a
different one. Each child dispatch then writes its own authorising entry on the child,
naming that person, that parent and that entry, and goes through the identical
`assert_human_clocked` on the stored row. The evidence is still an append-only row on
disk under a name the project configures as a person. What changed is which task the
person clicked: **they clicked the epic, and its children were named on its record at the
moment they did.**

This is the third caller of `_write_authorizing_entry` and the strictest of the three.
The browser's path takes an identity claim from a request and validates it. This one
takes no claim at all — there is no input a caller could get wrong or forge — which is
why it is the one authorisation path an agent is permitted to invoke.

**Rejected: having the walk render a command for the supervisor to run itself**, keeping
children as agent-owned subprocesses and D4 literally untouched. Two things killed it.
Children outside the ledger are invisible to §7's concurrency ceiling, so an unattended
epic would spawn an unbounded number of sessions that nothing counts — which is precisely
the runaway the caps exist for. And nothing would settle a finished child: the poller
reaps runs, and a walk watching subprocesses would have had to reimplement that.

One operational consequence, stated because it looks like a bug: **an epic walk needs at
least two concurrency slots**, one for the supervising run and one for the child. A
machine set to one refuses the first child, saying so.

#### The bound is mechanical

Two runs per child per human authorisation — the first and one retry — enforced in
`dispatch_task` rather than asked for in prose, and counted off the child's own log so it
survives the walk dying and being restarted. Only a run that *died* ever spends the
retry: a child that closed with a bad outcome or handed its ball to a person has said
something, and retrying it is ignoring it.

A fresh human authorisation of the epic resets the budget. That is deliberate and it is
the escape hatch: a person who looks at a child that burned both attempts and decides it
deserves another can dispatch the epic again, and the record shows they did.

Two properties of this were established by running it rather than by reasoning, and both
are worth stating because they are stronger than the design asked for:

- **The retry is rarely the thing that fires.** A dispatched run that fails is handed to
  `human`/`decision` by its own run supervisor, so the walk reads it as parked and stops
  without spending an attempt. `DIED` is what is left: a run whose supervisor wrote
  nothing at all. The asymmetry is the right way round -- retrying a child that said
  something is ignoring it.
- **Stopping spends the epic's authorisation.** The walk's handoff becomes the parent's
  newest entry, so the next child dispatch is refused `parent_not_human_clocked` until a
  person acts on the epic. One human act buys one walk, and a walk that could restart
  itself after stopping for a person would not be stopping for a person.

#### The children run at the epic's posture (task-316, 2026-08-25)

The sentence below about `autonomous` describing an arbitrarily long chain of merges was
written in task-022 and was not true of the code until task-316. `walk_epic.start_child`
built its `DispatchRequest` with no posture on it, so every child fell through to the
project default however the parent had been dispatched. On this repository — default
`auto`, ceiling `autonomous` — an epic a person deliberately dispatched at `autonomous`
therefore ran its first child at `auto`, that child correctly handed off for review, and
the walk stopped with `CHILD_NEEDS_A_HUMAN`. Nothing said why: that stop reads as a child
that genuinely needs a decision, not one handed the wrong authority. Meanwhile the
supervisor's own generated prompt told it the opposite in as many words. Task-269's epic
is the incident, and the workaround was to hand-write `posture:` onto every child.

A child now inherits the parent run's posture, and **which of the four sources may cross
that boundary is the decision, not the passing of the field**:

| Source of the parent run's posture | Crosses to the children | Why |
|---|---|---|
| Chosen for the parent's dispatch (`dispatch`) | **Yes** | A person choosing, for this epic, at the moment they authorised it. This is the same act the children already inherit their *authorisation* from. |
| Inherited by the parent from *its* epic (`epic`) | **Yes** | The same click, one generation further down. Dropping it at the second level would make behaviour depend on how somebody shaped the tree. |
| The parent's own task record (`task`) | **No** | A git-tracked field any agent that can write the repository can set. Crossing would mean one agent editing one field on its own parent widens *every* child at once — and the ceiling that bounds that source bounds nothing on a project already capped at `autonomous`. |
| The project default (`project`) | **No** | It already reaches every child on its own, as the bottom of `resolve_posture`'s precedence. "Inheriting" it would only relabel `posture_source` as `epic` and point a reader at the parent for an answer that is in `dispatch.yaml`. |

The rule is the one the walk's authorisation already runs on: **what crosses the
parent/child boundary is a human's act, and only that.**

Three properties keep this from widening anything:

- **It is read, never passed.** `dispatch_task` takes the posture off the parent's stored
  `dispatch` entry, exactly as it takes the authorising human's identity — so
  `on_behalf_of_parent` still gives a caller nothing to forge. Only the manager may
  append a `dispatch` entry (`MANAGER_WRITTEN_LOG_TYPES`), which is what makes that entry
  safe to take an execution envelope from at all.
- **The ceiling is unchanged and still applies.** An inherited posture goes through
  `resolve_posture` like every other source, and is *refused* above the ceiling rather
  than clamped — because it is a dispatch-time choice, merely one made about the parent.
  That is only reachable when somebody lowers `max_posture` mid-walk, and stopping loudly
  is the honest answer to an authority that no longer fits.
- **The record says where it came from.** A child run's `posture_source` is `epic`, not
  `dispatch` (which would claim somebody chose for *this* run) and not `project` (which is
  what the defect wrote, and pointed a reader at the wrong file entirely).

`agentjobs dispatch walk --posture` covers the case with nothing to inherit — a walk run
from a shell against an epic nobody dispatched. It outranks the inherited value, is
refused above the ceiling exactly as `dispatch --posture` is, and the walk prints what
envelope its children will start at before it starts any of them.

#### One bad child stops everything

Not skipped — stopped. A sibling that depended on the failed child would be building on a
gap, and the premise of the whole feature is that nobody is awake to notice. The walk
hands the parent to `human`/`decision` naming the child and the reason, and exits 1.
Since task-223 "everything" means every further **takeoff**: children already in flight
land, because none of them can depend on the failed one. See below.

The cost is real and was chosen: a walk halts at three in the morning on a child a person
would have waved through. That is the cheaper of the two mistakes and it is the one that
leaves a record.

#### The walk is a rolling frontier, and the merge is a runway (task-223, 2026-08-27)

**The walk ran one child at a time for its first four days, and no document said why.**
The instruction predated the code — `ALLAGENTS.md` said "pick exactly one eligible child
at a time" — and the only justification anywhere near it argued for something else: the
workflow guide's *"the reason is context, not parallelism"* justifies a **session per
child**, and context cost is a property of how many transcripts one session accumulates,
not of how many sessions run at once. A supervisor reads task records and diffs, never a
child's transcript, so three concurrent children leave its context exactly as small as
three sequential ones. The argument did not reach the conclusion attached to it.

The cost was measured rather than assumed. `scripts/run_report.py --epics`, which this
task added because nothing could answer the question before it, on the five epics in this
machine's ledger on 2026-08-27:

```
  epic                             kids  sit  peak     wall     work     idle   idle%      x
  task-160-dispatch-phase-two         7    5     2   642.8m   535.0m   111.3m   17.3%   0.83
  task-081-task-selection-ranking     3    1     1   167.5m    79.5m    88.0m   52.5%   0.47
  task-280                            1    1     1    27.9m    21.8m     6.0m   21.6%   0.78
  task-211                            6    5     1   117.3m    37.2m    80.1m   68.3%   0.32
  task-269                            8    5     2   331.1m   332.9m    22.2m    6.7%   1.01

  time in sittings      21.4h    turnaround idle 5.1h (24%)    parallelism 0.78x
```

Six of task-269's eight children declared no dependencies at all and were run strictly one
after another.

**Takeoff and landing are different resources.** That is the whole design, and it is
Jeff's framing rather than a metaphor invented afterwards: *"you don't send one plane from
Los Angeles to New York at a time … waiting to have another take off until the previous
lands is pretty stupid."* The work parallelises because each child has its own worktree and
its own session. The merge does not, because `main` is one branch. The previous design
conflated them and priced everything at the runway.

**A rolling frontier, explicitly not waves.** At every moment, every child whose `needs`
are satisfied and which is not already running is started, up to the slot count; a child
completing recomputes eligibility immediately. Waves — start every eligible child, wait for
all of them, recompute — is the easy loop and is wrong for the same reason the serial walk
was: it prices a group at its slowest member. There is no barrier anywhere in the loop, and
the invariant the tests assert is *at no point does an eligible, unclaimed child exist
while a slot is free.*

The frontier is recomputed from claimability rather than maintained as a ready-queue that
completing children push onto. That is correctness, not taste: the question is *are all of
this child's needs satisfied*, never *did the child that just finished name me*, and a
diamond — two unmet needs, one of which just closed — is where the two answers differ.

**The queue still decides order.** The graph decides eligibility, the queue decides order
among eligible children, and out-degree breaks the ties the queue does not. Sorting the
frontier by out-degree outright is the better scheduling heuristic and was rejected: it
would quietly overrule every `queue move` a human made, and `ALLAGENTS.md` is emphatic
that the queue is the answer. Since `(band, queue_position)` is already a total order, the
tie-break rarely fires — which is the intended outcome.

**The stop rule survives intact, and gets sharper.** It justifies *stopping*, which is not
the same as never having started. No further child takes off; children already in the air
land, because none of them can depend on the failed child or claimability would not have
offered them; anything that did need it never enters the frontier, automatically. What is
given up is exactly the pessimism about siblings that provably do not depend on the
failure.

**There is one concurrency cap and it is `limits.max_concurrent_runs`.** Task-081's brief
recorded that agent-started children were subprocesses uncounted against it; task-022
ended that by making them ordinary dispatches, and this design's own §3 says so. So the
walk's `--max-concurrent` can only narrow the machine's ceiling, never widen it, and
hitting that ceiling mid-walk is **backpressure rather than a refusal** — something else on
the machine holding a slot is a normal condition and not a fact about this epic. The walk
waits and retries; a machine full for the whole per-child ceiling stops it, saying which.

##### The runway, which is the harder half

Before this, **nothing serialised merges at all**. `acquire_run_lock` is keyed on task id,
so it stops two runs contending for one task and is indifferent to four tasks merging into
one `main`; `record_commit.py` detects having lost a race for git's own `index.lock` and
reports it, which is a symptom handler rather than a queue.

The expensive failure is not the merge collision, which git catches loudly. It is that a
child can pass a green gate against a base that has moved by the time it merges, so **the
commit that lands is not the commit the gate verified** — the one property the merge gate
exists to guarantee. `finish.merge` already refuses that case as `base_moved` and
escalates into a dispatched run; under concurrency, a check written for a rare race becomes
the ordinary outcome and costs a run every time.

So a **repo-scoped finish runway**: one lock per checkout, held across rebase, gate and
merge, so the winner's gate result is still true at the moment it merges and the next in
line rebases onto what actually landed. Keyed on the resolved, case-folded checkout path
rather than the project id, because the resource is a git repository — two projects over
one clone share a `main` and must share a runway.

Three decisions inside it:

- **Held across the gate, not taken at merge time.** Taking it late is much cheaper and
  needs an answer for "the base moved while I was gating"; the only correct answer is to
  rebase and re-gate, which spends the gate twice and under contention repeatedly. Holding
  it across spends the gate once. The honest cost is stated rather than hidden: at the
  ledger's four-minute mean finish, a four-child epic spends about sixteen minutes on the
  runway and parallelises the other five-sixths of each run.

  Task-297 later found the case this reasoning does not cover — the base being moved by
  sessions that hold no runway at all — and answered it with the *bounded* form of
  rebase-and-re-gate this bullet rejects: re-running only the stages the move can reach,
  never the whole gate, and only when the move classifies. See
  [catching up with a base that moved](#catching-up-with-a-base-that-moved-during-the-gate-task-297-2026-08-27).
- **Waiting rather than refusing.** Contention on a task lock means two runs want one
  task, which is an error. Contention here means the queue is working. The bound is an
  hour — long enough for a real queue, finite because a wait with no bound is a hang.
- **A queued finish says so on its own record.** A child third in line is otherwise
  indistinguishable from one that has stalled, which is precisely what a person reading the
  dashboard has to be able to tell.

It is the same primitive, in the same directory, under a reserved `runway-` prefix, so the
startup sweep that clears locks whose holders have ended clears these too, and there is no
second stale-lock convention to learn.

#### What is actually load-bearing now, and it is not much

**This is the highest-consequence behaviour in the dispatch feature, and it should be
read as such.** Every gate in §6 exists so that a human act starts *a* run. This makes one
human act start an arbitrarily long chain of runs, and at posture `autonomous` an
arbitrarily long chain of merges into `main` that nobody has read.

What is left holding, in the order it would fail:

1. **The objective gate.** Each child merges through `agentjobs finish
   --posture-release`, which runs the full unqualified `scripts/check.py` on the rebased
   branch and merges only on a green one. This is the floor, and it is run by the
   finisher rather than reported by the agent — which is the only reason the merge is
   defensible at all.
2. **Nothing is pushed.** `push` is per project, defaults to false, and is false here.
   AgentJobs runs no `git push` anywhere.
3. **`main` is local, so it is recoverable.** A bad chain of merges is caught by whoever
   next reads `main` and is reverted with `git reset`, because nothing left the machine.

Note what that ranking implies: **`push: false` matters more here than the merge policy
does.** A project that both walks epics autonomously and permits pushing has given up the
recovery, and would need a much stronger argument than this one. The same sentence is in
ENGINEERING.md for task-021 and it gets stronger, not weaker, when the merges come in
chains.

Note also what is *not* on the list. The attempt budget and the stop-on-first-bad-child
rule bound how much a walk can spend and how far a bad child can propagate; they are not
containment against work that is wrong but green. Nothing here is.

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

### The session is named after the run (task-324, 2026-08-29)

A dispatched session used to be launched with no display name, so Claude Code named it
for itself: a few seconds into the first turn it renames a `--bg` session after the
prompt, which yielded `task record reading`, `started without me knowing`, `queue
position schema`, and — from the run that fixed this — `git worktree task setup`. Those
are summaries of prompt prose. None of them names a task, none is stable across two runs
of the same task, and a second run started minutes later on a *different* task was
called `git worktree setup`, one word away from the first.

That name is not decoration. Three surfaces read it, and the second is load-bearing:

1. The **session picker** and the terminal title — "which of these nine sessions is
   task-231?" had no answer short of opening each one.
2. The **Remote Control peer channel**, which addresses a session *by name* (task-234).
   A name derived from prose is an unstable address.
3. A human debugging **reconciliation or stall detection** (task-296, task-320) is
   reading these names, even though the mechanism there is session ids.

AgentJobs now splices `--name <project>/<task>@<short run id>` — `agentjobs/task-324@11085a50`
— into the argv, at the same insertion point and for the same reason as the posture
flags: it is AgentJobs' business, not the operator's, and a template copied from the
scaffold must not be able to lose it by being edited. `session_name_flags` in
`dispatch/runner.py` is the whole of it.

**Which flag, established by observation on Claude Code 2.1.247 rather than from
`--help`.** The help text advertises two naming surfaces and does not say whether they
are the same one. They are not:

- `--name` is written into the session ledger (`~/.claude/sessions/<pid>.json`) at launch
  and is **not** overwritten by the prompt-derived rename. It is what `claude agents
  --json` reports and what the peer channel lists.
- `--remote-control [name]` is a different surface. A session started `--remote-control
  "task-999 probe-beta-rc"` appeared under the prompt text instead, and no peer row
  carried that name at all.

There is no length cap to design around — a 127-character name came back from `claude
agents --json` verbatim — so the name is short by choice. A picker holding two hundred
rows is scanned, not read.

**Why three ids and no title.** The title is the one candidate that reads well, and it
was rejected: it is editable, so two runs of one task could be named after two
descriptions of it, which is the instability this change exists to remove; and
truncating one rarely distinguishes, because the tasks that need telling apart are
neighbours whose titles share a prefix. The project is included because both surfaces
are machine-wide while a task id is only unique within its project.

**Two deliberate no-ops.** A template that already carries its own `--name` or `-n` keeps
it — the single opt-out, and an explicit act. And a **Codex** runner is left unnamed:
its session concept is an App Server thread with no display name and no flag that sets
one, so there is nothing to degrade, and this stays a Claude-only splice rather than a
runner capability one driver silently fails. Revisit if the App Server grows a name the
picker can read.

**Prior art.** Jeff reported an earlier attempt at this; nothing survived it — no task,
no commit, no log entry. The session that did the work above did not go looking, and the
plausible wall is written down here so a third attempt starts ahead: the auto-rename is
the thing that would defeat a naive fix, and `--name` beats it. If this regresses, that
is where to look first.

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
the URL from a session id.

*(built, task-070)* — the first of those. `REMOTE_CONTROL_URL` in `dispatch/runner.py`
matches the link out of the stripped transcript, and the run surfaces it in the
`ball_prompt` of a session that parks. The sentence above stands as the reason it is a
scrape; it no longer stands as a statement that nothing does it.

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

Machine-local in `~/.agentjobs/dispatch.yaml`, like every other dispatch setting. Since
task-308 a project declares two of them -- a default and a ceiling -- and two other
places may choose within that ceiling; see [Where a posture may come
from](#where-a-posture-may-come-from-task-308-2026-08-25).

| Posture | Flags | Merge | For |
|---|---|---|---|
| `read_only` | `--tools "Read,Glob,Grep,WebFetch"` | no branch | Review, triage, plans, defect reports. Verified enforceable: the agent has no shell at all. |
| `auto` **(default)** | `--permission-mode auto` plus the project allow-list via `--settings` | stop, hand off | Normal dispatched work. A classifier reviews each action, so the run keeps a gate and never needs a terminal. |
| `supervised` | `--permission-mode acceptEdits` plus the project allow-list via `--settings` | stop, hand off | A run a human is actually watching and willing to answer. |
| `autonomous` | `--permission-mode bypassPermissions` | merges itself | Per-project opt-in. Never the default. |

The **Merge** column is task-021 and is derived from the posture rather than configured;
pushing is a separate per-project switch that defaults to off. See [A posture also decides
what happens to the branch](#a-posture-also-decides-what-happens-to-the-branch-task-021-2026-08-23).

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

#### A posture also decides what happens to the branch (task-021, 2026-08-23)

Everything above is about **what a process may execute**. Until task-021 that was the
whole of what a posture meant, and what became of the *branch* afterwards was governed
entirely by prose — ENGINEERING.md's merge gate, which says work does not merge itself,
read as unconditional by every agent in the repository.

Jeff's requirement is that a posture carry both: `auto` and `supervised` stop for a
human, `autonomous` merges when it finds no serious problem. Push stays a separate
per-project switch, because the same posture should push in one of his repositories and
never in this one.

**Derived from the posture, not configured beside it.** A separate `merge_policy:` key
would be two switches with four combinations, of which "autonomous execution, but stop
for review" is one nobody has ever wanted and "classifier-gated execution, merges
unreviewed" is one nobody should get by typo. `Posture.merge_policy` is a property; there
is nothing to set.

**`auto` and `supervised` are identical here on purpose.** They differ in how the process
is gated *while it runs*. Who authorises the merge is a different question, and giving
them different answers would make the choice between two execution modes silently decide
a workflow policy nobody was choosing.

##### The weak point, and what is actually load-bearing

"No serious issue detected by the agent" is the agent grading its own homework, and it is
the least reliable gate available. It is therefore **not the authorising one**. The
authorising one is `scripts/check.py` exit 0, run by the finisher on the rebased branch,
unqualified — not `--only`, not `--from`, not `--since-gate`, and not reported by the
agent. Both have to hold: the agent's judgement is a veto it can always exercise, and the
gate is the floor it cannot talk its way past.

That is why an autonomous merge goes through `agentjobs finish --posture-release` rather
than through the agent running `git merge`. Routing it there means the merge, the rebuild,
the restart, the verification and the close are the identical sequence a human approval
takes, with one substitution: what authorised it. The authority is re-checked inside
`finish_task` against the project's machine-local posture, so a prompt that is wrong,
stale, or copied from another project's run fails closed.

**What that check is and is not.** It is a guarantee about the sanctioned path. It is not
containment against a misbehaving agent — `autonomous` *is* `bypassPermissions`, and a run
that ignored its instructions could merge by hand with nothing in the way. Containment of
that kind was given up when the posture was chosen, and pretending otherwise here would
be the kind of decoration this repository's verification rules warn about.

##### Push is not a posture property, and nothing here runs `git push`

Per project, defaulting to `false`, and `false` for AgentJobs. Two reasons it is not
folded into the posture: the same posture genuinely should push in one repository and not
another, and merging and publishing have different reach. A merge into a local `main` is
recoverable by anyone with a reflog. A push is not.

**The switch tells the agent; no code in AgentJobs pushes.** Making the scripted finish
push was rejected while there is no repository on this machine with a remote to prove it
against — an unverified publication step inside a script that runs unattended is exactly
the shape of thing that gets shipped on a hunch and discovered by someone else. Reopen it
when a project that wants it exists to test against.

##### What happens when an autonomous run merges something bad

It reaches `main`, it is found by whoever next reads `main`, and it is reverted. Nothing
left the machine. **That is the entire recovery argument**, and it is why `push: false`
matters more here than the merge policy does: a project that both releases the merge gate
and permits pushing has spent the recovery, and needs a much stronger justification than
this decision provides.

##### The policy travels in the prompt, and only there

A configuration key the agent never reads changes nothing. The posture's merge and push
policy is therefore rendered into the generated prompt (`dispatch.runner.policy_clause`)
as one or two sentences, which is a deliberate exception to §4's pointer-not-composition
rule and the second one after the worktree paragraph. The justification is the same
shape: everything else the stub gestures at is *in the record*, and this is not — it is
derived from a machine-local file the agent cannot read, and the repository's own
committed prose says the opposite by default. A run not told otherwise obeys the prose,
correctly, and `autonomous` would mean nothing at all.

A supervisor gets the same policy phrased for a run that holds no branch: what the
children it starts will do. It still approves nothing under either policy.

#### Where a posture may come from (task-308, 2026-08-25)

Everything above chose a posture in one place: a machine-local file a human edits. Jeff's
requirement on 2026-08-21 was that it also be settable **per task**, and he named the
problem in the same sentence: a task record is git-tracked and agent-writable, so a task
that can raise its own posture to `autonomous` is a privilege-escalation path.

##### A ceiling, not a provenance check

The design that was put up offered two answers, and Jeff rejected both on 2026-08-23 in
favour of a third: *"project should have max posture, and default posture"*.

```yaml
projects:
  agentjobs:
    posture: auto            # the default a run gets
    max_posture: autonomous  # the widest any run may get, whatever asks for it
```

Both rejected answers treated the danger as *who wrote the field*. **Narrow-only** --
a task may lower its posture but never raise it -- is safe and costs the feature the case
it was asked for, which is marking one specific task safe to run unattended.
**Provenance checks** -- accept the value only if a configured human actor wrote it, or
only if a human-clocked entry followed it -- are as strong as the identity machinery
behind them, and an agent with write access to a git-tracked file is well placed to
produce entries that satisfy them.

A ceiling makes the question moot instead of answering it. `dispatch.yaml` is
machine-local: not in the repository, not carried by a clone, and not written by any
AgentJobs API route, CLI verb or MCP tool. So it genuinely does not matter who wrote the
task-level posture. An agent that edits its own task record to `autonomous` on a project
capped at `auto` gets `auto`. That is the difference between a control and a convention,
and it is why nothing in `resolve_posture` inspects an actor.

The **push** switch and `assert_human_clocked` are both untouched by this. Choosing what
a run may do and being allowed to start one are separate gates; this section is entirely
about the first.

##### Four sources, most-specific-wins, and two different failure modes

| Rank | Source | Written by | Above the ceiling |
|---|---|---|---|
| 1 | Chosen for this dispatch: `--posture`, or the GUI control (task-307) | a person, per run | **Refused** (`posture_above_ceiling`, HTTP 403) |
| 2 | Inherited from the epic this task is a child of (task-316) | a person, when they dispatched the parent | **Refused** — it is a dispatch-time choice one generation up |
| 3 | `posture:` on the task record | anyone who can write the repository, including an agent | **Clamped** to the ceiling |
| 4 | `posture:` in the project's `dispatch.yaml` | a person, on this machine | Cannot happen -- the config parser refuses it |

Precedence is the reading anyone would guess, and it is stated and tested rather than
inferred, because a field whose effect cannot be predicted is worse than no field.

**Rank 2 sitting above rank 3 is the one placement that is not obvious**, because a field
on the child's own record is narrower in scope. It loses anyway, because the question the
order answers is not "which statement is about the fewest runs" but *which statement did
a person most recently make about this run*. Let the record win and "I dispatched the epic
`autonomous`" would silently mean something different per child — which is exactly the
predictability the paragraph above claims. Which sources may reach rank 2 at all is
decided in [The children run at the epic's
posture](#the-children-run-at-the-epics-posture-task-316-2026-08-25).

The asymmetry in the last column is the part worth understanding. A dispatch-time choice
has a caller waiting on an answer, so refusing beats quietly handing them something
narrower than they asked for -- and a chooser should never offer it in the first place,
which is why `ProjectDispatchSettings.offerable_postures()` exists and is sent to the
browser on the dispatch state view rather than re-derived there. A task-record posture
has nobody waiting, and refusing it would hand every agent a denial of service on its own
task: write an over-ceiling value once and every future dispatch of that task fails.
Clamping is strictly safer, and the run says on its record that it happened.

##### The width order, which is not the enum's order

`read_only` < `supervised` < `auto` < `autonomous`.

`supervised` sitting below `auto` surprises people and is deliberate. The question a
ceiling asks is what a run may do **unattended**: supervised runs nine allow-listed
command prefixes and parks on everything else, where `auto` is classifier-gated over a
far larger set. Supervised *feels* wider because a human at a terminal can approve
whatever it parks on -- but that is a second authorisation arriving, not something the
run was granted, and a `--bg` session at `supervised` gets no such approval and simply
stops. So a project setting `max_posture: supervised` is saying "nothing here runs
unwatched", and handing it `auto` under that ceiling would defeat exactly that.

The order lives in `Posture.rank`, and the comparison is `Posture.within(ceiling)` rather
than `<=`. `Posture` inherits `str`, so it already has comparison operators and they
compare spelling -- `Posture.AUTONOMOUS < Posture.AUTO` is true as text and catastrophic
as a ceiling check. Overriding some of the six would leave the rest still comparing
spelling, which is worse than overriding none.

##### The default ceiling, and what it costs

`max_posture` unset means "the same as `posture`". That is the only default that cannot
silently widen a machine on upgrade: before this existed, the project's posture was the
only posture a run could ever get, and an unset ceiling reproduces that exactly.

The cost is that the feature is opt-in twice. A task's `posture: autonomous` and
task-307's pulldown both do nothing on a project that has not raised its ceiling by hand,
and the only place to raise it is a file no AgentJobs surface writes. That is the point
of it, so this is a cost to state rather than a wrinkle to smooth.

##### The run record answers "why did this run get that envelope"

The `dispatch` entry carries `posture_source` (`project` / `task` / `dispatch`),
`posture_ceiling`, and -- only when the ceiling cut something down -- `posture_requested`.
The run directory's `meta.yaml` carries the same three. Both are needed because the other
two places the answer could live are invisible to a reader of the task file: the ceiling
is machine-local, and the task's own `posture` field may have been edited since the run.

`posture_source` is absent on every entry written before task-308. Read that as
`project`, which is what it always was -- never as unknown.

##### The control a person actually uses (task-307)

The pulldown beside the Dispatch button is labelled **Envelope**, and three things about
it are load-bearing rather than cosmetic.

**Its list comes from the server.** `offerable_postures` on the dispatch state view is
every posture at or below this project's ceiling, computed by the same
`ProjectDispatchSettings` the dispatch route will validate against. A browser that
derived its own list from the `Posture` enum would be the one place in the system able to
offer a choice the API then refuses -- which teaches the operator that the control lies.
The refusal still exists and is still tested; populating from the server is what makes it
unreachable by clicking.

**Each option says what it does to the branch, not what it is called.** "autonomous"
tells a reader nothing about whether their work merges without them, and since task-021
that is precisely what it decides. The option text is keyed off `posture_merge_policies`,
which the server sends for the same reason as the list -- the mapping is fixed in code,
so a client carrying its own copy could tell an operator the opposite of what happens.

**`autonomous` is offered disabled, with the reason, where the project has no scripted
finish.** task-021 accepted that an autonomous merge runs through `agentjobs finish
--posture-release` and that a machine without it has no sanctioned mechanism for one.
Picking it there would produce a run told in its prompt that it may merge, with no way to
do it -- which is how an agent talks itself into an improvised `git merge`. Disabled with
a stated cause is right where omitting it silently is wrong: the fix is one line of the
reader's own `dispatch.yaml`, and they can only make it if they know that is the cause.

The panel also names whether the project **pushes**, which task-021 identified as a real
gap and left for a later task: *"this project will merge my work without asking me"* is
exactly the sort of thing that should be visible where the Dispatch button is. It is said
only where `push` is true, because false is the answer everywhere today and a sentence
repeating the universal default on every task is noise.

The control is **absent entirely** when `offerable_postures` holds one entry. A pulldown
whose single option means "the only thing that can happen" is furniture, and such a
project reads exactly as it did before the control existed.

That is rarer than it sounds, and worth stating precisely because the obvious guess is
wrong. An unraised ceiling does **not** remove the control: `max_posture` unset means the
ceiling is the project's own posture, so a project at `auto` still offers `read_only`,
`supervised` and `auto` — everything at or below it. What an unraised project cannot do
is *escalate*, which is the double opt-in the ceiling exists to create. The control
disappears only where the ceiling is the narrowest posture there is, `read_only`, because
that is the only ceiling with nothing underneath it.

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
built the candidate-list half of it. **Runner groups are built. `difficulty` is
*(unbuilt)*, and so is the difficulty → profile table** — see the reopen triggers at the
end of this
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

#### Task difficulty — *(unbuilt)*

*(unbuilt — no such field exists on `Task`; everything in this subsection is the
design as accepted, not behaviour you can use.)*

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
per-profile **`strict`** setting *(unbuilt)* inverts that — refuse instead, naming the profile, the
difficulty and the missing rule. Strict is opt-in and off by default, because deliberate
spend is the whole point of the feature and someone who chose a conservative profile and
silently got a frontier model was failed quietly.

One consequence of rung 5 sitting above rung 6, stated so it is not rediscovered as a
bug: **adding `default_group` takes effect for every project that has not named a group
of its own**, including projects that name a plain `runner`. That is what a machine
default means. A project that wants its runner regardless should name its own group, or
the file should not have a `default_group`.

##### Labels, and what they are worth

*(unbuilt)* A runner may additionally declare optional descriptive `model` and
`effort` **labels**. `DispatchRunner` (`dispatch/config.py`) has `name`, `argv`, `env`,
`mode`, `actor` and `driver` and no label fields; setting either in
`~/.agentjobs/dispatch.yaml` is a silent no-op.
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
[What is checked, and what is merely claimed](#what-is-checked-and-what-is-merely-claimed-added-2026-08-20-task-188)
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

### 5a. The approval that starts no dispatch at all (task-241)

An approval can now cause something other than a run: a **scripted finish**, which does
the fixed post-approval sequence — rebase, gate, merge `--no-ff`, rebuild, restart,
verify, close, retire the worktree — with no agent anywhere in it. Where it is enabled,
that is what Approve starts, instead of a dispatch. `src/agentjobs/dispatch/finish.py`.

It belongs in this document rather than beside it because it sits inside §2 rather than
around it, in three ways worth being explicit about:

- **It consumes no dispatch authorisation, because it starts no agent.** §2 is a rule
  about what may start a *run*. A finish reads git and runs a configured command; the
  human-clocked rule is not weakened, because nothing it does is a dispatch.
- **When it escalates, it spends exactly the authorisation the approval already carried.**
  One human act, one dispatch, unchanged. It attributes that dispatch to the human's own
  approval entry rather than to the newest one — by then the newest is the finisher's,
  and a dispatch defaulting to it would be refused as not human-clocked, correctly and
  uselessly. It only dispatches at all where `auto_dispatch` is on; elsewhere it parks
  the task at `agent`/`work` and the human's existing Dispatch click resumes the session.
- **It writes as `finisher`, a reserved *agent* actor.** So an entry it wrote can never
  clock a later dispatch as a human act, exactly as `dispatcher` cannot. It is a separate
  id from `dispatcher` because the two make different claims: one says AgentJobs started
  an agent, the other says AgentJobs merged with none.

The switch is `finish.enabled` in machine-local `~/.agentjobs/dispatch.yaml`, off by
default and gated behind the project having dispatch enabled at all. Same shape as
`auto_dispatch`, for a stronger reason: what this runs is `git merge` in a shared clone,
in response to an HTTP request.

---

## 5a. What *ends* a dispatch: the scripted finish (task-241, shipped)

Not in the original design, because in the original design a human approval woke an agent
and that agent did the merge. Measuring that (`scripts/run_report.py`) showed the
post-approval run averaging about eleven minutes, almost none of it the git commands: it
was a cold agent working out which branch it owned. None of that work needs a model.

**On a machine with `finish.enabled` for a project, clicking Approve runs the whole
close-out itself, with no agent anywhere in it.** It rebases onto `main`, runs the full
gate in the task's own worktree with that worktree's interpreter, merges `--no-ff`,
rebuilds the frontend if the merge touched it, restarts the server the way this machine's
config says it was started, proves the running process is serving the merge commit, closes
the task, removes the worktree and deletes the branch.

Nothing about the merge gate is relaxed by this. **A person still approves, per task,
before anything merges**, and the merge is still a `--no-ff` merge commit. What is removed
is the agent between the approval and the merge, not the approval.

- `agentjobs finish <task> --project <id>` is the same code by hand, and is how a finish
  that escalated is retried once its cause is fixed. Exit 0 means merged, closed and
  verified; 1 means it stopped and the task record says where; 2 means the task was never
  a candidate and nothing happened.
- It declines rather than guessing whenever the answer is a judgement: no active branch,
  two of them, a clone with something else checked out, a missing or dirty worktree, or a
  branch somebody already merged by hand.
- Each finish writes itself to `~/.agentjobs/finishes/`. A finish is not a run — no agent,
  no session, no tokens — so `run_report.py` counts it in its own block rather than folding
  it into run statistics, which is what makes the saving attributable instead of showing up
  only as runs-per-task quietly falling.
- When it stops, it writes the dispositive sentence onto the task itself: either "The merge
  is done: `<sha>`" (the merge landed, the delivery did not — finish that, do not re-merge)
  or "Nothing was merged", with which of the rebase or the gate failed, and for a conflict
  whether the branch was restored to the commit it was on, having read the tip back and
  compared.

#### Watching one happen (task-321, 2026-08-27)

Everything above is what a finish *does*. For its first six weeks none of it was visible
while it was doing it: the ball moved to `agent`/`work` carrying the approval's own
prompt, and for the three or four minutes the sequence takes the page said nothing at
all. Reported by Jeff from the task page -- *"when I click approve and it auto finishes,
there is no feedback really"* -- and the honest reading is that a click with a
three-minute silent consequence is indistinguishable, to the person who made it, from a
click that did nothing.

It was almost entirely a reading problem. The records already existed and nothing
assembled them.

- **`dispatch/finish_status.py` is the reading half**, and only that half: it starts
  nothing, holds no lock and waits for nothing. `GET /api/dispatch/finishes/{task_id}`
  and its `/output` sibling serve it; the task page polls the first every two seconds
  while something is live and renders it beside the review verbs.
- **Liveness is the run lock's answer, through `stale_lock_reason`** -- not a heartbeat
  and not a timeout. A second staleness rule would disagree with the first the moment
  somebody killed a gate, and reusing this one means a finish whose machine rebooted
  mid-gate reads as `interrupted` rather than as a spinner nothing ever stops.
- **`spawn_finish` writes a marker before it spawns.** The child spends a second or two
  importing Python before it creates anything, and the approve request answers well
  inside that window; a page that reloaded on the answer and found nothing would
  conclude nothing was happening and stop looking. The ordering is the feature, so it is
  the ordering a test asserts.
- **Two things now record progress they always could have.** Each step writes itself as
  it lands rather than only into the table at the end (`StepLog`), and `scripts/check.py`
  writes a record per stage -- so the gate, which is around 85% of a finish's wall clock
  and was one silent block, reports "pytest, 7 of 10".
- **The Dispatch button is disabled while a finish is live**, saying why. The server
  refused it before and still does -- the finish holds the task's run lock -- so nothing
  about what can happen changed. What changed is that the page says so *before* the
  click, which matters more here than for most refusals because the reason is something
  the reader started themselves thirty seconds earlier by pressing Approve.

What is deliberately absent is a stream. Teeing the finish's stdout to the browser would
have needed `run_command` to stop capturing and the CLI to print per step, and would
still have shown nothing during the gate, whose own output is buffered until it exits.
The step table is both live and more legible than the terminal text; the log is shown
when it exists, which is when the process ends.

### Catching up with a base that moved during the gate (task-297, 2026-08-27)

`finish.merge` refuses to merge when the base moved since the gate started, on the sound
reasoning that *what was verified is no longer what would be merged*. On a quiet machine
that check fires almost never. On a busy one it fired almost always, and the reason is
structural rather than bad luck:

- **ENGINEERING.md requires every session to commit its task records to `main`** — claims,
  progress, handoffs, decisions — so the base moves every couple of minutes whenever
  anything is happening. That rule is not negotiable here; a handoff on a branch is
  invisible to the human it addresses.
- **A gate takes minutes.** 96 seconds idle, 169–285 seconds measured under load.

Two intervals, one of them minutes long and the other seconds long. Task-224's finish lost
that race twice in one evening and merged only after a human hand-built a quiet window.
And the failure fed itself: an escalation writes a record commit, which moves `main`, which
escalates the next finish mid-gate, which writes another record commit.

**The runway (task-223) closed the self-amplifying half and only that half.** With one lock
held across rebase, gate and merge, no second finish is ever gating when the first one
escalates. What it cannot touch is the trigger — ordinary sessions, holding no runway,
doing exactly what they are told to do. So `base_moved` still fired on other people's
bookkeeping.

**The fix is to re-verify rather than to declare the bookkeeping inert.** Between the gate
and the merge there is now a `catch_up` step. When the base has moved, it asks
`scripts/gate_scope.py` — *the same table `--since-gate` runs on* — what the moved paths
can affect. If every moved path is classified, it rebases onto the new tip and re-runs
exactly those stages; then it merges. If any moved path is unclassified, it does nothing
and `merge` refuses with `base_moved`, the message it always had.

Four properties, none of them politeness:

- **`tasks/` is not treated as inert, because it is not.**
  `tests/test_validate.py::TestRealCorpus` loads the corpus of *the checkout it runs in*,
  and the gate ran in the branch's worktree, whose corpus is the pre-move one. The delta
  really is unverified. So a `tasks/`-only move costs one `pytest` — which contains
  `TestRealCorpus` — instead of one wasted dispatched run. That is the argument the task's
  constraints demanded against a blanket exemption, and the reason this is not one.
- **A code commit still refuses**, unchanged. The narrowing happens before `merge` is
  called, never inside it, so the refusal is the one it was written for. The escalation now
  names the paths that moved, so the woken session does not have to diff for them.
- **Default-deny, from the same table, twice.** `gate_scope.classify` answers `None` for a
  path nothing claims, and `None` costs a merge here exactly as it costs a minute there.
  The table is loaded from *the finished repository's own* `scripts/gate_scope.py`: a
  project that publishes none gets the unconditional refusal, so the exemption is opt-in by
  the repository, in a file that goes through review.
- **Bounded, and silent on the record.** At most two rounds; a third move escalates as
  `base_moves_repeatedly` rather than chasing a machine busier than a finish can follow.
  And `catch_up` writes nothing to the task while it runs, though every other step does —
  a progress note would be committed to the base, moving the base it is catching up with.
  The step table in the closing entry carries it, and the finish directory records each
  round with the paths and stages.
- **It always reports itself**, like a merge that needs no restart: a quiet base gets a
  skipped step saying so, and an unabsorbable move gets one saying that too, before
  `merge` refuses it. A step that vanished when it did nothing would leave the live view
  of §5a deriving "what is running now" from a gap.

Two seams with that live view were closed at the same time, both of which would have
failed silently. `StepLog` recorded a step by overriding `append`, and `list.extend`
does not go through it — so `catch_up`, which returns a list, would have been on the task
and absent from the page. And `finish_status.STEP_ORDER`'s duplication check matched step
names with `[a-z]+`, so the first step name carrying an underscore was invisible to
exactly the check written to catch a missing one.

**One commit the finisher itself made was removed.** `Runway.take` announces on the record
when the runway is contended, which is right — a task queued behind three others must not
read as stalled. It used to *commit* that note, and the finish it is queued behind is
mid-gate, so the finisher was landing a commit on the base at the one moment guaranteed to
escalate somebody. The note is still written, which is what the dashboard reads; the commit
is dropped, because `announce_start` commits the same file a minute later once the runway
comes free, and the escalation commits it if the wait times out.

The escalation's own record commit was deliberately **not** deferred. That entry is the
whole explanation of why a merge did not happen, and leaving it uncommitted in a shared
clone for the next agent to find dirty is the failure `record_commit.py` exists for
(task-203). With the runway held it can no longer land under another finish's gate anyway.

---

## 6. Safety

Dispatch converts an unauthenticated localhost HTTP API into **remote code execution on
Jeff's machine**. That sentence is the whole reason this section exists, and it should
be read before every change to this subsystem.

**Who the attacker is, and who it is not.** Every gate below answers one threat: *a
repository choosing what executes on this machine*. That threat is real and the gates
hold against it. It is not the only one, and this section used to read as though it were.

*(added 2026-08-22)* **A network peer is not covered here.** The REST API has no
authentication (`docs/api-reference.md` says so first, and means it), so anything that
can reach the port can reach dispatch. Gates 1 and 2 still hold — a peer cannot define a
runner, because runners are machine-local and never come from a repository — but gate 3
is a plain HTTP call away from being flipped, and the per-project toggle is exposed to
the browser by design (D2). The honest reading is therefore that **the four gates are
independently sufficient against a repository and not against a network peer**, and what
actually defends the second case is the deployment: loopback binding, which
`_validated_bind_host` enforces by refusing wildcards, and tailnet membership in front of
it. See [mobile access](mobile-access.md) for that half.

*(added 2026-08-23, task-022)* **One human act can now start many runs.** The gates below
each still hold -- an epic walk passes through all four for every child it starts, and
`assert_human_clocked` still judges a stored entry naming a configured person. What
changed is the ratio: a person clicking Dispatch on an epic authorises a run *per child*,
and at posture `autonomous` a merge into `main` per child, without being asked again. The
argument for why that is acceptable, and the three things left holding when it is not, is
in [the epic walk](#the-epic-walk-one-human-act-many-runs-task-022-2026-08-23). Read it before
raising a project's posture.

### Four gates, each independently sufficient to stop a run — against a repository

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

**These limits are what bounds a runaway.** §2 used to say they were a backstop against
a bug in the dispatcher, on the strength of a structural argument that did not survive
being tested — see the correction in §2. They are the primary defence, and task-334
rewrote them to be one: every cap below is checked in `dispatch_task`, the single
chokepoint all three triggers pass through, and the reasoning lives beside the code in
`src/agentjobs/dispatch/budget.py`.

**Budget limits — every trigger (task-334, reversing D3).** D3 exempted manual dispatch
on the grounds that a human clicking Dispatch repeatedly is a decision rather than a
malfunction. That premise requires the server to be able to tell a human's click from an
agent's, and it cannot: a forged click arrives as `manual`, which was the exempt trigger.
The cost D3 was protecting is now paid — a fourth manual dispatch of one task in a day is
refused, and the remedy is to read why the first three did not finish.

| Limit | Default | Configured as | On trip |
|---|---|---|---|
| Dispatches per task per 24h | 3 | `limits.auto.per_task_per_day` | Refuse; log; ball → human/decision |
| Dispatches per task, lifetime | 10 | `limits.auto.per_task_lifetime` | Refuse; log; ball → human/decision |
| Cooldown between dispatches of one task | 60s | `limits.auto.cooldown_seconds` | Refuse; log (no ball change — it is transient) |
| Dispatches machine-wide per rolling hour | 30 | `limits.dispatches_per_hour` | Refuse; log (no ball change — it is transient) |

The `limits.auto:` key is historical and is deliberately not renamed: it lives in
machine-local `~/.agentjobs/dispatch.yaml`, and renaming a key there would silently
return a machine whose caps had been tuned to the defaults.

**The hourly cap is the one no per-task budget can stand in for.** Per-task caps bound
one task; N tasks each dispatching at their own limit have no ceiling between them.
`max_concurrent_runs` does not supply one either — it bounds how many runs are *alive*, so
a loop that starts a run, fails it, and starts another never holds a slot long enough to
be refused by it. This counts takeoffs.

**Choosing 30.** The busiest rolling hour in this machine's entire run ledger was
**twelve** dispatches (137 runs, measured 2026-09-04) at `max_concurrent_runs: 3`. Thirty
is two and a half times that: an epic walk would have to turn a run over every six minutes
on all three slots to reach it, and no real run here has ever been that short, while a
dispatch loop reaches thirty in seconds. That gap is the whole design of the number — a
cap that fires on real work is one somebody raises to infinity.

**Safety limits — every run, manual included.** These are correctness, not spend.

| Limit | Default | On trip |
|---|---|---|
| Live runs per task | 1, always | Refuse the second immediately |
| Concurrent runs machine-wide | 1 as shipped; set per machine from a measurement | Refuse (do not queue — see below) |
| Wall-clock per run (**batch only**) | 1800s | Terminate the run; `dispatch_result: timeout`; ball → human |
| Staleness (**session only**) | 3600s idle with an unmoved ball | `finished_without_handoff`; ball → human. **The session is not killed** (§9) |

**Tripping a cap is never silent, whatever started the dispatch.** The run is refused,
and a `note` entry naming the specific limit, its value and the trigger is appended to the
task. That entry is written for every trigger and not only for auto-dispatch: an HTTP
refusal is read once by whoever is holding the mouse and by nobody afterwards, and the
record is what the next session reads.

A **count** cap additionally moves the ball to `human`/`decision`, with a `ball_prompt`
saying the task has been dispatched N times without reaching a conclusion — because a task
that burns through its budget is telling you something is wrong with the task, and it
should land in the human inbox for that reason rather than stop quietly. Two exceptions,
both for the same reason: not on a `manual` dispatch, where a person is reading the
refusal in the same second and taking their ball away would be the tool responding to a
click by changing the thing clicked on; and not for the transient caps (cooldown, hourly),
where waiting is the whole remedy.

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

**These figures describe the serial gate and are kept only as history.** Task-233 later
took `pytest` from 326s to about 52s by running it under `-n auto` without coverage, so
the solo gate is now about 96s rather than 355s and the table's absolute numbers no
longer describe anything you can reproduce. The *shape* is what it was kept for, and the
shape is untested at the new speed: `-n auto` asks for every core, so two concurrent
gates now contend for the same 32 threads rather than each taking a slice, and **nobody
has measured concurrent parallel gates**. Do not quote a contended figure from this
table; measure one.

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
`git worktree add ../worktrees/agentjobs-<nnn> -b <type>/task-<nnn>-<slug>`, before anything is
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

#### The one session a sweep keeps: waking instead of starting cold

**Added 2026-08-21, task-234.** Reaping is now conditional, because `claude rm` deletes
the *conversation* and the conversation turned out to be worth something.

The measurement: across 49 timed runs on 23 tasks, runs per task was 2.13. The second run
is almost always the post-approval one — rebase, `merge --no-ff`, mark the branch merged,
close the task, rebuild the frontend, restart the server — and it averaged **about eleven
minutes**. Almost none of that is those commands. It is a cold agent booting and working
out which branch and which worktree it owns, which the *first* session had in memory when
it handed off.

So dispatching a task whose previous session still exists resumes that conversation
instead of starting a new one. Three pieces:

- **`reap_finished` keeps exactly one run per open task** — the newest session run, which
  is the only one `wake.find_wake_target` will ever offer. The reaper asks that module
  for it rather than implementing a second rule, because the failure of two rules
  disagreeing is a wake that resumes a session the sweep deleted. Closing the task
  collects it on the next sweep. A kept session is a row in a list, not a process and
  not a concurrency slot: a stopped session has no `pid`.
- **`_start_session` rewrites its argv** — the element carrying the prompt becomes
  `--resume <uuid>`, and everything else, posture flags included, stays where
  `build_argv` put it. A wake and a cold start differ in one argument, which is what
  stops the two drifting apart in what the run may do.
- **The prompt goes on stdin.** Not a style choice. `--remote-control` and `--resume`
  do not compose over a positional prompt: the session comes up with its conversation
  correctly restored and the prompt argument **silently dropped**, sitting at
  `idle`/`blocked` with an empty box. `classify_session` reads `idle` as `FINISHED`, so
  dispatch would settle a session that never got its instruction as one that finished
  without handing off. Verified on 2.1.238, reproduced twice.

Two properties are worth stating because they are what make this safe rather than clever:

**Waking is an optimisation and never a precondition.** No previous session, a
conversation the manager no longer lists, a runner that does not answer `agents --json`,
an argv with no single prompt element — every one of them falls back to a cold start.
None may turn into a failed dispatch, because starting cold is a correct if slower answer
to all of them.

**Only the newest run is ever a candidate.** If it is disqualified the answer is a cold
start, not the run before it. Resuming a stale conversation would hand the human an agent
whose picture of the branch is a run out of date, which is worse than the cold start it
avoided.

The session lookup passes `--all`, and that flag is load-bearing: `agents --json` prints
*active* sessions, so a stopped one — the entire population a wake looks at — is absent
without it. Measured on 2.1.238 against one stopped session, `--json --cwd` returned zero
rows and `--json --all --cwd` returned it with its `sessionId`. Polling deliberately keeps
the active-only view, because a session missing from *that* one is a session that is gone.

Off switch: `resume_sessions: false` on a project. It changes speed and nothing else.

---

### Waking a session instead of starting one (`WAKE_STUB`, shipped)

A second dispatch of a task may **resume the session that worked it** rather than start a
cold one. This is not in the design above and belongs here because it is a concurrency
property: the resumed session still holds the worktree it took and the branch it is on, so
it is the same actor, not a second one racing it.

When dispatch resumes, the prompt it sends says so explicitly — it names the run it
resumed and carries whatever the human just wrote (`dispatch/wake.py`). The contract that
prompt states has two halves, and both exist because a resumed conversation is confident
by construction:

- **Check before acting on memory.** If the worktree is gone, the branch is not where it
  was left, or the session's account of the task no longer matches what is on disk, say so
  on the record and hand the ball back rather than improvising a recovery.
- **Do not assume you were resumed.** A cold start is the fallback for every uncertainty
  and remains the ordinary case for a task's first run. The prompt is what distinguishes
  them.

Until 2026-08-22 this contract was written down only in `ALLAGENTS.md` and in `wake.py`
itself, so a reader who came to this document for the dispatch model would not learn that
resumption exists at all.

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
- **No structured stdout stream.** `claude logs <id>` owns the output, and it is an
  **ANSI pty scrape, not structured events**. This is a real loss against batch mode's
  `stream-json`, accepted knowingly.

  *(amended — this bullet used to begin "No run directories, no stdout capture", and both
  halves are now false.)* A session run gets a `RunDirectory` like any other, and the
  transcript is captured to `transcript.log` on every poll and served by
  `GET /api/dispatch/runs/{id}/output` and `/tail`. What survived from the original
  reasoning is the warning, not the absence: the file is a **raw TTY capture**, so a line
  appears in it once per terminal repaint and any count derived from it is an artefact of
  that. Link to it and read it; do not compute from it. `phases.jsonl` exists because
  that question should not have to be asked of the transcript at all.
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

**Batch: a run does not outlive its supervisor.** On startup any run directory in a
non-terminal state is declared `interrupted` and hands the ball to a human.

*(corrected 2026-08-22 — this paragraph also claimed "on graceful shutdown every live run
is cancelled". Nothing does that.)* The server's lifespan
(`api/main.py`) cancels the **poller** and nothing else, and a batch run's supervisor
thread is `daemon=True`, so stopping the server orphans the child rather than signalling
it. `stop_everything` — which does cancel every live run — is reached only from
`agentjobs dispatch stop`. The startup reconciliation above is therefore what actually
closes the gap, one restart later, and the honest statement of the invariant is that a
batch run does not outlive its supervisor *silently*: it is labelled `interrupted` rather
than left reading `running` for ever. Pid adoption was rejected — it is unreliable (pid reuse;
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

### Every protection above is keyed on a run record (task-320, 2026-08-27)

The park, the auth-expiry check, the staleness settle and the stall report are all
reached the same way: `poll_live_sessions` iterates `live_runs(home)` and hands each run
to `poll_session`. **A session with no run directory is never iterated**, so it gets none
of them, and the task it is working reads `agent`/`work` throughout — which is exactly
what a supervisor following "the signal is the task record" waits on.

Task-217 is the incident. It sat dead for four hours on 2026-08-23 while `_park_session`
had been written, correct and deployed for five days; it never ran, because the session
was hand-spawned by its supervisor rather than dispatched and nothing knew it was there.
Its two siblings in the same epic have run directories and it has none. Task-296
established that; this is the other half of it.

**The fix is adoption, not detection.** `agentjobs run register --task <id>` verifies a
session against the runner's own ledger and writes the run record a dispatch would have
written. From the next poll the session is followed by the existing code, with no second
implementation of any judgement — the poller, `_handle_from`, `poll_session` and
`_finish_session` are untouched.

Four properties, each a refusal in `dispatch/registration.py`:

1.  **A dispatched run registers to nothing.** `AGENTJOBS_RUN_ID` naming a live run for
    this task answers "already known" and writes nothing, which is what lets ALLAGENTS.md
    say *register* with no exception for an agent to evaluate. A rule with a condition
    attached is a rule some fraction of readers gets wrong.
2.  **An interactive session cannot be registered at all.** A session a person is sitting
    in is a normal, common state that must never be reported as a fault — and an adopted
    one would eventually be reported stalled, or `stop`ped by the settle path, while
    somebody was typing into it. Refusing by construction beats any later heuristic.
3.  **The claim is verified before anything is written.** The session must be live in the
    runner's ledger, under this project's root — which is also how the poller looks it up,
    so a session launched from inside a worktree is refused rather than adopted and then
    reported gone. A run record naming a session nothing can follow is worse than none: it
    looks covered.
4.  **Registration is not a dispatch.** Nobody authorised a run and AgentJobs started no
    process, so §2's human-clocked chain must not be forged by a command any agent can
    invoke. The record gets a `note` naming the session, the run and what the adoption
    buys, and its entry id is what the run's `dispatch_entry_id` points at.

The run lock is taken, so a later dispatch at the same task is refused `live_run_exists`.
The machine's `max_concurrent_runs` is deliberately **not** applied: the session is
already running, and refusing would not stop it — it would only keep it invisible, which
is the whole defect.

**Rejected: making the protocol the fix.** `dispatch walk` already starts children as
real dispatches, so an epic's children are covered by construction. That closes the gap
only for epic children; a human or a supervisor can still hand-spawn a session against
any task, which is precisely what happened to task-217.

**Rejected for now: record-side detection** — flagging a task that is `active`/`agent`/
`work` with a live run behind it and no log movement. It is runner-agnostic and catches
causes registration cannot, including a dispatched run whose process died without the
ledger noticing. It was not built because it cannot currently tell an unattended session
from an interactive one a person is working in, and both look identical from the record.
Registration is what changes that: once an unattended session is expected to have a run
record, *not* having one becomes a signal rather than the norm. Detection is the natural
follow-up and is a better task after this than before it.

**Recovery is out of scope, argued rather than inherited.** Task-296 left it out for
stall detection because a stalled session may hold a dirty worktree and restarting risks
two sessions on one branch. That argument applies here and one stronger one is added:
AgentJobs never had this session's argv, its environment or its prompt, so it could not
restart it faithfully even if it wanted to. What it can do it does — say on the record
that the session is parked, stalled, expired or gone, and leave it attachable.

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

    Amended again 2026-09-04 (task-334): **the budget caps now bind every trigger**, and
    a machine-wide dispatches-per-hour cap joins them. What D3 got wrong is not the value
    it placed on a human's decision but the assumption underneath it — that the server can
    tell a human's click from an agent's. It cannot: the API is unauthenticated on
    loopback and every dispatched agent is told its address, so a forged click arrives as
    `manual`, the exempt trigger. The cost D3 was avoiding is now paid deliberately: a
    fourth manual dispatch of one task in a day is refused, and the refusal names the cap
    and where to raise it.

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
