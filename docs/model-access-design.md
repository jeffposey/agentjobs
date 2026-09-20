# How AgentJobs is allowed to call a model

**Accepted 2026-09-19 (task-174). Nothing here is built.** It is the fork task-175 was
blocked on: AgentJobs starts coding agents but has never called a model itself, and
"flesh this out with AI" needs one. Read it for the reasoning and the constraints a
first implementation has to satisfy, not as a description of anything that runs today.

The scope is narrow on purpose and the narrowness is load-bearing: **short, bounded,
human-triggered calls that return text into a form.** A general in-app assistant, an
agentic loop inside the server, model-driven triage and automatic task edits are all
outside it, and every rule below is sized for the narrow case rather than for the
general one.

## 1. The decision

**A direct HTTPS call from the server to a model provider, with a credential this
machine holds, behind a new `model.draft` capability that no run may hold.**

The three rejected alternatives, and why:

**A — reuse the dispatch runner.** Rejected. Measured below at **9.8s to 20.0s** for the
cheapest model and 27.9s to 56.6s for a mid one, against a **0.085s** transport floor for
option B. That alone is close to disqualifying for a person waiting in a form, but the
structural objection is the one that settles it and would settle it at any latency: see
§3.

**B — a direct API call.** Chosen. Its costs are real and are priced in §4 and §5: a
credential to hold and keep out of six places, a dependency, and a second path by which
this machine spends money. What makes them payable is that all three are *bounded and
stateable* — one credential, one endpoint, one cap, one capability row — where option A's
costs are open-ended.

**C — a local model.** Rejected, but on the weakest grounds of the three, so it is
recorded as reopenable rather than closed. Nothing is installed on this machine
(measured: no listener on `127.0.0.1:11434`, 2026-09-19), so it could not be measured,
and a convenience feature that requires an operator to install and maintain a model
server is not a convenience. **The reopen trigger is explicit**: if a local model is
already running on the machine for other reasons, §6's config should be able to name it
as the endpoint, because at that point the install cost is zero and every argument in §4
and §5 about credentials and spend disappears. The design below is deliberately written
so that pointing it at a different base URL is a config change and not a rewrite.

**D — call the model from the browser.** Rejected before the task was written, recorded
so nobody re-proposes it: it means a credential in a client bundle.

## 2. What was measured

`scripts/model_access_probe.py`, run on this machine on 2026-09-19. It times one
realistic drafting prompt — a one-line task description expanded into a full spec, which
is exactly task-175's job — down each path, and reports a path it cannot reach as
unavailable rather than as slow. **It spends real quota, so it is not a gate stage and
nothing imports it.**

| Path | Result |
| --- | --- |
| A, one-shot CLI, `claude-haiku-4-5` | **9.78s, 14.17s, 20.04s** |
| A, one-shot CLI, `claude-sonnet-5` | **27.93s, 41.46s, 56.60s** |
| B, transport floor (TLS + HTTP, no inference) | **0.116, 0.095, 0.085, 0.078, 0.085s** |
| B, end-to-end | **not measured** — no credential exists on this machine |
| C, Ollama | unavailable — nothing listening on `127.0.0.1:11434` |

Three things to read carefully, because each of them is easy to get wrong in the
direction that flatters the measurement.

**Option A was measured in its fairest form, which is not the form a dispatch runner is
configured in.** The probe passes `--strict-mcp-config` with no servers, an empty
`--allowed-tools` and an empty `--setting-sources`: no MCP, no tools, no repository
context. A real runner argv from `dispatch.yaml` carries none of those, so the numbers
above are a **floor** for option A and the gap to option B is understated, not
overstated.

**The comparison that matters is overhead, not total.** Both paths pay the same
inference; what differs is what sits above it. Option A's overhead is process startup,
measured at most of those 9.8 to 20.0 seconds. Option B's is 0.085s. That is the number
the decision turns on, and it is measured on both sides.

**Option B's end-to-end latency is unmeasured and that is a gap, not a footnote.**
This machine holds no credential in any scope — not the process environment, not the user
or machine environment, not `~/.claude/settings.json` (checked 2026-09-19) — so the
inference half could not be timed without obtaining one, which was not this task's to do.
The floor is honest about what it covers and no claim below rests on the unmeasured half.
**This is a precondition on task-175, not a caveat**: its first act is to re-run the probe
with `--path api` once a credential exists, and if the end-to-end number is not
comfortably better than option A's, this decision is wrong and should be revisited rather
than built on.

## 3. Why option A is rejected even where it is fast enough

Latency is the visible objection. These are the ones that would stand if the process
started instantly.

**A drafting call is not a run, and the dispatch guards are all about runs.** Every gate
in `dispatch/config.py` and `dispatch/guards.py` answers a question about starting an
unattended process in a working tree: is this machine willing to execute, is this runner
configured, is this project enabled, was this caused by a human, is the tree dirty, is
there a concurrency slot. Filling in a form field is none of those. Routing it through
them means either it inherits them — so typing in a form consumes a dispatch slot, and
can be refused because a *different* task hit its hourly cap — or it is exempted, and
**every exemption is a hole in the argument that dispatch is fully governed.** The task
was explicit that the guards must not be widened to accommodate this, and there is no
version of A that does not either widen them or hollow them out.

**A runner argv is an opaque human-written list, and AgentJobs must not start editing
it.** `dispatch.yaml` models a runner as `argv: List[str]` with a deliberately narrow
placeholder set — `prompt`, `task_id`, `project_id`, `project_root`, `run_id`, `agent`,
`api_base` — and nothing that means "this is a bounded prompt, not repository work". To
make a drafting call safe, AgentJobs would have to splice flags into a command line the
human wrote, and the flags are **CLI-specific**: `--setting-sources` is a Claude Code
flag, and this machine's `dispatch.yaml` also configures Codex runners whose argv shares
no vocabulary with it at all — a subcommand and repeated `-c key=value` pairs. Whether
Codex has an equivalent flag was *not* established: the binary its argv names no longer
exists at that path, so the honest claim is the one that does not need it. AgentJobs
would be maintaining a per-CLI matrix of safety flags against surfaces it has
deliberately treated as opaque since task-068, and would have to keep that matrix correct
for every runner an operator adds.

**Measured, and the reason the paragraph above is not hypothetical.** Asked on
2026-09-19 what it had been given, a one-shot invocation with MCP and tools stripped but
settings left at their default **reported loading the operator's global standing
instructions**, and named private repository paths from them, including one the operator
marks sensitive and local-only. It also produced a draft citing machine specifics that
were nowhere in the prompt. Adding `--setting-sources ''` closes it — that was verified,
and the finding is stated at its true strength: **the leak is on by default and is
suppressible by a flag AgentJobs does not own.** A feature whose blast radius is decided
by a file that has nothing to do with AgentJobs, and that changes whenever the operator
edits it, is not a bounded call.

**An agent CLI is not a completion endpoint and does not behave like one.** Of five
timed one-shot invocations at the two larger models, one returned prose instead of the
JSON object it was asked for, and one — before the flags above were applied — answered
about a background search from some other context entirely. Option B's failure mode is an
HTTP status code. Option A's is a plausible paragraph in the wrong shape.

## 4. Where the credential lives, and where it may never appear

**It lives in `~/.agentjobs/model.yaml`, machine-local, mode `0600`, never committed** —
or in the server process's `ANTHROPIC_API_KEY` if that is set, which is what makes a
container or a systemd unit workable without a file.

It follows `dispatch.yaml`'s split, for `dispatch.yaml`'s reason and not merely by
analogy: *what a project is* is versioned with the project, *what this machine will
spend* is machine-local. **A credential must never be reachable from a project's
`.agentjobs/config.yaml`**, because cloning a repository would then carry a "spend money
on my machine" payload that the project's own config legitimises — the precise thing
dispatch was built to make unrepresentable.

The rule, which is not negotiable and is the reason option D is dead:

> The credential is read at call time and exists only in the outbound request header. It
> is never written to a task record, a log entry, an error message, an API response, the
> run ledger, or the frontend bundle — and it is never echoed back by any route that
> reports configuration.

Three consequences that a first implementation has to get right rather than intend:

- **Provider errors are mapped before they cross the API boundary.** A provider's error
  body can echo request metadata, so the server translates failures into a small closed
  set of reasons — `unconfigured`, `refused`, `rate_limited`, `over_cap`, `upstream`,
  `timeout` — and never forwards an upstream body verbatim. The probe script already
  follows this rule: its failure path reports an exception *type* and never the request.
- **The status route answers a boolean, not a value.** Whatever tells the frontend
  whether drafting is available returns `available: true|false` and, when false, one
  sentence of reason. It never returns the key, a prefix of the key, or its length.
- **Redaction is not the mechanism.** Nothing relies on scrubbing a key out of a string
  that should not have contained it; the key never enters a string that anything logs.

## 5. What governs the spend

**The `~/.agentjobs/DISPATCH_DISABLED` sentinel stops model calls too.** This is the one
piece of dispatch machinery that is shared, and the reason is not that a drafting call is
a dispatch — §3 argues at length that it is not — but that **the sentinel is the
operator's single blunt stop and it must not surprise them.** Someone who creates that
file wants this machine to stop causing model work. Discovering afterwards that a second
spending path kept running, and had to be disabled separately somewhere else, is exactly
the failure the task named: a kill switch that stops runs while another feature keeps
spending is not a kill switch. So the sentinel is re-read, one rung more general than its
filename: *this machine must not cause model work*, of which dispatch is one kind.

This widens no guard. It is one call to the existing `sentinel_active()` and no change to
`assert_dispatch_permitted`, which keeps answering only the question it answers today.

**The other three dispatch gates are not shared, and neither are the run budgets.**
`enabled:`, the runner map and per-project enablement all authorise *spawning a process
for repository work*; a project that is not enabled for dispatch should still be able to
draft a task, and a machine with no runner configured at all should still be able to. The
per-task and machine-hourly caps in `dispatch/budget.py` count **runs**, and a drafting
call must not consume a run slot — a person tidying a backlog on their phone would
otherwise exhaust the budget that exists to bound a runaway agent, which inverts what the
counter is for.

**Model calls get their own cap, in their own config, counted separately.**
`model.yaml` carries `calls_per_hour`, machine-wide, defaulted low. It is a separate
counter for the same reason `budget.py` exists as its own module: the thing being bounded
is different, and a counter that bounds two different things well bounds neither.

**No run may call it.** A new row on the capability table in
[authorization.md](authorization.md):

| Capability | Routes | `owner` | `tailnet` | `run` |
| --- | --- | :-: | :-: | :-: |
| `model.draft` | the drafting route | ✓ | ✓ | — |

It sits with `dispatch.start` rather than with `task.create`, on that row's own stated
reasoning: a model call is a purchase, and a purchase is something a person signs for.
Since task-332 the server can tell a person from a run, so this is enforced at the
route rather than asserted in prose. It also forecloses the loop without needing a new
argument: an agent cannot spend the operator's model budget, so no amount of agent
activity can spend it.

**Embedded host apps get it, and the honesty matters more than the restriction.** A host
app reaching the API resolves as `owner` or `tailnet` — the table keys on principal kind,
never on surface — so it holds `model.draft` for the same reason a person at the keyboard
does. Inventing a "not for embedders" rule would be a restriction the server cannot
actually enforce, and an unenforceable rule in a security section is worse than none.
What bounds an embedder is `calls_per_hour` and the sentinel, the same as everything else.

## 6. Configuration, and what happens with none

**The model is named in config, never compiled into the source.** Model ids change
faster than this repository releases, and a drafting feature should be able to move to a
cheaper or better one without a code change. `~/.agentjobs/model.yaml`:

```yaml
version: 1
provider: anthropic          # the seam option C reopens through
base_url: https://api.anthropic.com
model: claude-haiku-4-5-20251001
max_tokens: 2048
calls_per_hour: 60
timeout_seconds: 30
# api_key: read from this file or from ANTHROPIC_API_KEY; never from a project's config
```

`provider` and `base_url` are present from the start, and are what makes §1's reopen
trigger for option C a config change rather than a rewrite.

**With no configuration, the feature is absent and everything else is unchanged.**
An absent `model.yaml` and an unset environment variable mean `available: false` with
reason `unconfigured`, and:

- the drafting control is absent or disabled with one sentence saying why;
- every form works exactly as it does today, and filing a task by hand is untouched;
- no route 500s, no startup warning, no degraded mode, nothing else about AgentJobs
  changes.

**This is the current state of every machine, including this one**, which is worth
stating plainly: as of 2026-09-19 no credential exists here in any scope, so the
unconfigured path is the one that will run first and it is the one that has to be right
first. **AgentJobs runs with no model access at all, and that stays true.**

## 7. What this decision does not settle

- **The drafting feature itself** — the control, the prompt, how a draft reaches the form
  fields without destroying what a person typed. That is task-175.
- **Option B's end-to-end latency**, per §2. Task-175 measures it before it builds on it.
- **Streaming.** A 2KB draft at option B's overhead probably does not need it; if the
  measurement in §2 says otherwise, streaming is a change to the route and not to any
  argument here.
- **Cost accounting.** `calls_per_hour` bounds the spend; it does not report it. What a
  month of drafting costs, and where an operator reads that, is unanswered and should be
  its own task if the feature survives contact.
- **Any use of a model beyond a short, bounded, human-triggered call.** Triage,
  prioritisation, automatic edits and a conversational loop are all out of scope, and
  nothing above should be read as clearing a path for them.
