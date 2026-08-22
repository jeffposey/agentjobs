# Big Dawg Audit — dispatch plan

Night of 2026-08-21, before the weekly limit resets 7pm 2026-08-22.
Task record: `tasks/agentjobs/task-242.yaml`. This file is the executable plan:
twelve auditor prompts and one synthesis prompt, ready to fire verbatim. The
dispatcher (the session Jeff tells "go") follows the runbook, prepends the
shared preamble to each brief, and spawns sessions.

---

## Runbook

### Pre-flight (dispatcher does this before batch 1)

1. Jeff checks `/usage` and answers one question: **does Fable usage draw down
   the all-models meter?** If no → all auditors run on Fable (that meter is at
   ~12% used). If yes → budget against the ~25% all-models remainder and apply
   the cut order below.
2. `git -C C:/projects/agentjobs status` — the main clone should have no
   uncommitted source changes that would confuse auditors about what "current"
   is. Uncommitted task YAML is fine.
3. Confirm no gate is running (auditors never run the gate, but a running one
   means another agent is mid-task; note it, proceed anyway — auditors are
   read-only).

### Dispatch

- Mechanism: the `spawn-session` skill, **auto mode** (memory: bypassPermissions
  is classifier-refused; auto works and does not park). Read-only work makes
  unattended auto safe.
- Prompt = **Shared preamble** (below) + that auditor's **brief**, verbatim.
- **Max 3 concurrent** (machine dispatch limit). Batches, slowest first:

  | Batch | Auditors |
  |---|---|
  | 1 | 10 (dispatch), 12 (security), 1 (context) |
  | 2 | 3 (schema), 4 (storage), 7 (API/webhooks) |
  | 3 | 9 (frontend), 11 (gate), 2 (docs) |
  | 4 | 5 (queue), 6 (CLI), 8 (MCP) |
  | 5 | synthesis (alone, after all 12 land) |

- **Monitoring is a mechanism, not an intention**: the dispatcher backgrounds a
  wait on each batch (spawn-session's wait/reap, or a Monitor on the findings
  files appearing). It never ends its turn "planning to check back."
- Auditor death: if a session dies without writing its file, retry **once**.
  If it dies again, write a one-line stub findings file saying so and move on —
  the synthesis must know the gap exists.
- Auditors **write their findings file but do not commit**. The synthesis
  session commits everything at the end (twelve concurrent committers on one
  clone is a race nobody needs).

### Budget cut order (only if pre-flight says the pool is tight)

Merge 6→7, then 2→1, then 5→3. **Never cut 1, 3, 10, or 12** — 1 is the audit
Jeff named first, 3 and 4 are the trust anchors, 10 is newest and least
examined, 12 is the one with real-world blast radius.

### Morning after

Jeff opens `audits/2026-08-21/00-BIG-DAWG-REPORT.md`, then the draft tasks the
synthesis filed (dashboard, draft band). Promote what's real, close what isn't.

---

## Shared preamble (prepend to every auditor brief)

> You are one of twelve auditors in a coordinated overnight audit of the
> AgentJobs repository (`C:/projects/agentjobs`). Your brief below defines your
> system. Rules that bind you:
>
> - **Read-only.** You change no source, no config, no task records. You may
>   run read-only commands (grep, `--help`, `git log`, a curl GET against the
>   running server on 8876) but nothing that mutates state, and you never run
>   `scripts/check.py`.
> - **No worktree, no branch, no claim.** You work in the main clone as a
>   reader. Do not claim task-242 or any other task.
> - **Your only write is your findings file**, at the path your brief names.
>   Do not commit it — the synthesis session commits.
> - **Finding format:** severity `P1` (defect that bites now) / `P2` (should
>   fix) / `P3` (improvement) / `P4` (observation), a title, **evidence**
>   (file:line or a command and its output — a finding without evidence is a
>   guess and gets cut), and the concrete fix or the precise open question.
> - **Adversarial stance.** For every check, test, rule, or doc you read, ask
>   ENGINEERING.md's question: *what would this have caught?* "Nothing that has
>   ever gone wrong here" is itself a finding (P3, decoration). Praise is not a
>   finding; absence of findings in an area you actually examined is stated as
>   "examined, nothing found," which is different from not examining it.
> - **End with two sections:** `## What I did not get to` (silent truncation is
>   the failure mode this line exists to prevent) and `## Questions for other
>   auditors` (things outside your system you noticed and could not chase).
> - Depth over breadth. A findings file is typically 150–500 lines. Verify
>   before you assert: a claim about behavior cites the code path or the
>   command output that shows it.

---

## Auditor briefs

### 1 — Context architecture: static vs dynamic
**Findings file:** `audits/2026-08-21/01-context-architecture.md`

Audit the instruction stack every agent session loads: `CLAUDE.md` → `AGENTS.md`
+ `ENGINEERING.md` + `ALLAGENTS.md`, plus `C:/ai/shared/GLOBAL-AGENTS.md` and
`C:/projects/AGENTS.md` above it, the MCP server instruction text (in
`src/agentjobs/mcp/`), and the dispatch prompt scaffolding
(`src/agentjobs/dispatch/scaffold.py`). The governing question, from Jeff
directly: **is this the right way to split static context (loaded into every
session unconditionally) from dynamic context (task records, ball_prompt, docs
read on demand, dispatch prompts)?**

1. **Measure the static bundle.** Character/word counts of the full @-chain;
   estimate tokens (~4 chars/token). State what every session pays before its
   first thought, and what fraction of it a typical task actually uses.
2. **Redundancy map.** Every rule stated in more than one file: list the
   statements side by side and say whether they agree. Where they disagree, the
   disagreement is a P2.
3. **Staleness sweep.** Every number, timing, count, and version quoted in
   these files (test counts, gate seconds, "2608 tests", "three dispatched
   runs", dates) — check each against current reality. ENGINEERING.md is dense
   with measurements; some are marked as history, some are not.
4. **Load-bearing vs decorative.** For each major section: does it cite an
   incident or a mechanism (load-bearing), or is it advice nobody has needed?
   Recommend keep / compress / move-to-doc / delete, per section.
5. **Migration candidates both directions.** What static prose should become
   dynamic (belongs in dispatch prompts, task specs, or a doc read on demand)?
   What dynamic knowledge keeps getting re-derived per session and should
   become static?
6. **Corpus sample.** Read `docs/task-corpus-audit.md` first (prior art), then
   sample ~15 task records across eras (single digits, 100s, 230s) against
   ALLAGENTS.md's Resumption Contract: summary orients a zero-context reader,
   ball_prompt current, decisions recorded with rejected alternatives,
   handoffs self-contained. Report compliance drift since that audit.

### 2 — docs/ vs reality
**Findings file:** `audits/2026-08-21/02-docs-vs-reality.md`

Every file in `docs/`: agent-dispatch-design, agent-loops-design,
agent-workflow, api-reference, index, installation, mcp-clients,
mcp-integration-design, mcp, migration-guide, mobile-access, performance,
playbooks-design, quickstart, schema-design, schema/, task-corpus-audit,
task-schema, task-selection-design, webhooks. For each:

1. Classify: **accurate / drifted / aspirational (designed, not built) /
   abandoned**. Evidence: the code that does or does not do what the doc says.
2. Design docs specifically: which parts shipped, which silently didn't? A
   design doc that reads as description of the present but describes a plan
   from June is the dangerous kind — flag those hardest.
3. Follow quickstart and installation as a skeptical new user (read-only: you
   can run `--help` and inspect, not install). Would they walk off a cliff?
4. `api-reference.md` vs `openapi.json` vs the actual routes in
   `src/agentjobs/api/` — three-way diff, name the discrepancies.
5. `docs/index.md`: does it index what exists?

### 3 — Schema v2 & validation
**Findings file:** `audits/2026-08-21/03-schema-validation.md`

`src/agentjobs/models_v2.py`, `validation.py`, `schema_tolerance.py`,
`migration/`, `migrate_schema.py`, `docs/task-schema.md`, `docs/schema/`.

1. **The four axes** (lifecycle / ball+ball_reason / outcome / archived):
   enumerate every invariant the docs claim (e.g. "ball required while open",
   "ball_prompt required whenever ball is set", "outcome only when closed",
   "parent not claimable while a child is open") and for each, find where it is
   enforced — model validator, manager verb, storage, API layer, or nowhere.
   An invariant enforced only at one edge is constructible through another.
2. **Tolerance policy.** What does `schema_tolerance.py` accept that strict
   validation rejects, and where does tolerated data flow next? Does a
   tolerated-in task round-trip out still-tolerable, or silently normalized?
3. **Migration.** v1→v2 completeness; is it idempotent; what happens to a
   half-migrated corpus; does anything still emit v1 shapes?
4. **Doc/model drift.** `docs/task-schema.md` field-by-field against
   `models_v2.py`. The regen script (`scripts/regen-schema-docs.sh`) — is the
   committed doc actually regenerated output, and is anything checking that?
5. **`ball_reason` vocabulary**: is it scoped to holder as claimed, enforced,
   and consistent across CLI/API/MCP?

### 4 — Storage, manager, operations
**Findings file:** `audits/2026-08-21/04-storage-manager.md`

`storage.py`, `manager.py`, `operations.py`, `receipts.py`, `actors.py`,
`projects.py`.

1. **Write atomicity.** Trace the write path for a task file. Temp-file +
   rename? fsync? What does a crash mid-write leave on disk, and what does the
   next read do with it? (Windows rename semantics count here.)
2. **Concurrent writers.** The real deployment is: a long-running server on
   8876, multiple interactive agent sessions, and dispatched runs — all
   writing the same tasks/ directory through their own process. Where is the
   lock, if any? Construct the lost-update scenario on paper and say whether
   the code prevents it or the git history just absorbs it.
3. **operation_id idempotency.** Where are receipts stored, when are they
   evicted, what exactly is fingerprinted, and what happens when the same
   operation_id arrives with a *different* payload? Replay must return the
   original result — verify it does, and that it doesn't re-execute side
   effects (log append, file write, webhook emit).
4. **Abstraction bypasses.** Grep the whole codebase (src, scripts, tests) for
   direct reads/writes of task YAML outside TaskStorage. ENGINEERING.md says
   "avoid where possible" — list where it wasn't.
5. **manager.py verb integrity.** claim/handoff/release/close: each appends its
   log entry and moves the axes atomically with respect to the file write?
   Any path where state moves without a log entry?

### 5 — Queue system
**Findings file:** `audits/2026-08-21/05-queue.md`

`queue.py`, `docs/task-selection-design.md`, plus `task_next`/`task_queue_move`
surfaces in CLI/API/MCP.

1. Band semantics and `queue_position` numbering: what guarantees hold, what
   renumbers, when. The docs say hand-edited positions "can collide … which is
   corruption the queue refuses to answer over" — find the refusal in code and
   verify it actually triggers.
2. `task_next` claimability rules vs what `--why` reports: are the exclusions
   it prints the exclusions it applied?
3. `queue check` / `queue repair`: what invariants does check verify; does
   repair state everything it guessed, as claimed?
4. Lifecycle interactions: what happens to position on claim, release, close,
   promote, archive? Any operation that silently loses or duplicates order?
5. Read the live queue (read-only, `agentjobs queue list` or the API): any
   current corruption, collisions, or oddities in the real corpus.

### 6 — CLI
**Findings file:** `audits/2026-08-21/06-cli.md`

`src/agentjobs/cli.py` (Typer), `docs/quickstart.md` command examples.

1. Full command inventory. Map each to a manager verb / API route / MCP tool;
   produce the parity table and name what exists in only one surface.
2. Exit codes: do failure paths exit non-zero, uniformly? (Scripted callers
   depend on this; spot-check by reading the error paths.)
3. `serve` / `open` / `restart`: does `restart` detect that the running server
   on the port is not one it started (the "not yours to restart" doctrine, and
   the stale-8765-server incident class)? What does `open` do when the server
   is already running, stale, or on a non-default port?
4. Help text drift: options documented that changed behavior, defaults printed
   that aren't the defaults.
5. Error text quality: for the five most likely user mistakes (unregistered
   project, bad task id, invalid transition, stale server, wrong checkout) —
   what does the user actually see, and does it name the fix?

### 7 — REST API, client, webhooks
**Findings file:** `audits/2026-08-21/07-api-client-webhooks.md`

`src/agentjobs/api/`, `client.py`, `webhooks.py`, `scripts/export_openapi.py`,
`instrumentation.py`.

1. Route inventory vs manager verbs — parity both ways. Error contract: does
   every error return one consistent envelope (shape, status codes)? List the
   exceptions.
2. `openapi.json` honesty: schemas that say `object` where the real shape is
   known, responses documented that can't occur, missing error responses.
3. Generated client freshness: the gate's `api` stage compares against the
   working tree — trace exactly what it compares and what drift it would miss.
4. **Webhooks:** HMAC construction and verification — timing-safe compare?
   timestamp in the signed payload (replay window)? delivery retries/ordering?
   What does `task.handoff` actually carry vs what docs/webhooks.md says?
5. Instrumentation: `X-Response-Time-Ms` and `X-Task-Parses` — measured where,
   accurate under the caching the server does, and is the parse count actually
   used to enforce anything?
6. `client.py`: does it surface API errors or swallow them; coverage vs routes.

### 8 — MCP server
**Findings file:** `audits/2026-08-21/08-mcp.md`

`src/agentjobs/mcp/`, plus the instruction text it serves and the plugin
registration (the tool list shows both `mcp__agentjobs__*` and
`mcp__plugin_agentjobs_agentjobs__*`).

1. Tool inventory vs manager verbs; anything the MCP surface can do that the
   verbs forbid, or vice versa.
2. Contract enforcement: wrong `project_id`, actor not in vocabulary, actor set
   to `default_user` (instructions forbid adopting it — is that enforced or
   just requested?), reused `operation_id` with different payload, missing
   `ball_prompt` on handoff. For each: what error text does the calling agent
   actually receive, and would a confused agent know what to do next?
3. Instruction text audit: every claim in the served instructions ("there is no
   generic setter and none is coming", "replays the original result") verified
   against implementation.
4. Dual registration: are the direct server and the plugin the same code at the
   same version? What keeps them from diverging?
5. Schema quality of tool inputs: places where the schema accepts what the
   handler rejects (validation living in the wrong layer).

### 9 — React frontend
**Findings file:** `audits/2026-08-21/09-frontend.md`

`frontend/src/` — `App.tsx`, `components/`, `api/generated`, `queryClient.ts`,
`pwa.ts`, `service-worker.js`, `report/`, `styles.css`; tests (`*.test.tsx`,
vitest suite, `e2e` Playwright specs).

1. **Rendered-value correctness.** The `data-ball="Ball.HUMAN"` bug class:
   audit every place an enum or model value reaches the DOM (attributes,
   filters, class names). Do tests assert rendered values or markup presence?
2. Query invalidation: after each mutation, which queries invalidate? Find the
   mutation whose result a user won't see until reload.
3. **Staleness machinery.** `service-worker.js` + `pwa.ts` cache strategy vs
   the stale-bundle incident class (merged change invisible until rebuild).
   When the server has a new bundle, how does an open tablet PWA find out —
   and how long can it serve the old one?
4. Mobile/tailnet path: anything origin- or path-dependent that behaves
   differently behind `https://agentjobs.tailfed1df.ts.net/app/`?
5. Accessibility and focus: keyboard operability of the task list and review
   flows; the task-207 lesson (focus lost when React reinserts DOM nodes) —
   any other interaction with the same shape?
6. **Test quality pass:** pick ~10 vitest and ~5 Playwright tests and answer,
   for each, what defect it would catch. Name the ones that set up the state
   they claim to verify (the task-207 anti-pattern).

### 10 — Dispatch subsystem
**Findings file:** `audits/2026-08-21/10-dispatch.md`

`src/agentjobs/dispatch/` — runner, poller, auto, guards, ledger, phases, wake,
scaffold, record_commit, address, auth, config — plus
`docs/agent-dispatch-design.md`, `scripts/run_report.py`, and the run ledger
format in `~/.agentjobs/runs/` (read a few real run dirs). Newest system,
least audited. Trace one run's full lifecycle through the code first, then:

1. **guards.py:** what is allowed, what is blocked, and what a motivated agent
   inside a run could still do (the classifier memory: blocks are per-content,
   `git merge` is "a coin flip"). Are the guards enforcement or advice?
2. **Ledger integrity:** what gets written when a run starts, ends, dies, or is
   killed. Can a crashed run leave the ledger claiming it's still live? Who
   cleans up, and does `run_report.py` mislead on partial records?
3. **Poller races:** two pollers, or a poller and a manual dispatch, picking
   the same task. The three-concurrent-runs limit — enforced where, and what
   happens at the boundary?
4. **wake/resume:** the "you may be woken" contract — how does the code decide
   resume vs cold start, and can it resume the wrong session or resume into a
   worktree that no longer exists?
5. **scaffold.py:** supervisor prompt vs ordinary prompt selection ("a task
   with an open child gets the supervisor prompt") — verify the predicate.
   Task content is interpolated into prompts — any escaping/injection
   consideration at all? (Coordinate: auditor 12 owns the security framing.)
6. **auth.py + address.py:** what authenticates what, and what a tailnet peer
   could invoke.
7. `phases.jsonl` recording: gate_started/gate_finished plumbing, env-var
   inheritance, and what breaks silently when `AGENTJOBS_RUN_DIR` is absent.

### 11 — Gate & tooling
**Findings file:** `audits/2026-08-21/11-gate-tooling.md`

`scripts/check.py`, `gate_scope.py`, `bootstrap.py`, `bench.py`,
`build_frontend.py`, `build_release.py`, `project_setup.py`.

1. **`--since-gate` default-deny:** verify by reading `gate_scope.py` that an
   unclassified path selects all ten stages — then hunt for the hole: glob
   patterns that over-match, path normalization (case, separators, worktree
   paths) that could misclassify, a rename that dodges classification.
2. **Receipts:** where written, what invalidates one, and whether the
   chain-of-receipts claim ("a `--since-gate` green issues its own receipt,
   recording which receipt it derived from") holds in code. Can a receipt be
   issued for a tree the gate didn't fully verify?
3. `check.py`: stage isolation (a failing stage can't corrupt a later one's
   inputs), the PARTIAL RUN honesty guarantee, the `-n auto` parallel-safety
   assumptions vs `tests/conftest.py` (port-0 claim, per-test registries —
   spot-verify).
4. `bootstrap.py`: the wrong-checkout detection and the VIRTUAL_ENV disowning
   (task-210) — verify the mechanism, and find the environment shape it still
   gets wrong.
5. `build_release.py`: what goes in the artifact; would it install and run on
   a machine that isn't this one (hardcoded paths, Windows-isms, the tailscale
   host script)? This is the product-strategy question in code form.
6. `bench.py`: does it measure what performance.md says it measures; the
   browser-pane-hidden caveat (throttled timers) — does anything account for it?

### 12 — Security & exposure
**Findings file:** `audits/2026-08-21/12-security.md`

Cross-cutting. Threat model first, findings second. Three attackers: (a) a
device on the tailnet, (b) a malicious or compromised task YAML / task content,
(c) a misbehaving dispatched agent inside a run. Assets: the task corpus, the
repos on disk, Jeff's machine, anything the webhooks reach.

1. **Tailnet exposure:** `scripts/tailscale-service-host/`, the tsnet proxy,
   `docs/mobile-access.md`. What is reachable at
   `https://agentjobs.tailfed1df.ts.net/app/` — the whole API? Is there any
   authentication on the REST API at all? If tailnet membership is the entire
   auth story, say so explicitly and enumerate what a tailnet peer can do
   (create/close/claim tasks? trigger dispatch? read every project's tasks,
   including job-hunting?).
2. **YAML safety:** every `yaml.load` site — safe loader everywhere, including
   scripts, tests, migration?
3. **Path traversal:** task ids, project roots, attachment paths
   (`attachments.py`), findings/asset paths — anything user- or API-supplied
   that reaches a filesystem join. Registry `root` fields point anywhere; who
   validates?
4. **Prompt injection:** task spec/log content is interpolated into dispatched
   agent prompts (scaffold.py) and those agents run with write access. A task
   created over the tailnet API whose description carries instructions IS the
   attack chain (a)+(c). What, if anything, breaks it?
5. **Webhook secrets & outbound:** secret storage, target URL validation
   (SSRF), what payloads leak to a webhook receiver.
6. **Dispatch auth:** `dispatch/auth.py` — what does a valid credential prove,
   where does it live on disk, what honors it.
7. **Secrets hygiene:** grep the repo and its git history (read-only) for
   tokens, keys, tailnet identifiers that shouldn't be public — this repo has
   a public remote.

### Synthesis — the Big Report
**Runs alone after all twelve files exist. Model: Fable.**

> You are the synthesis session of a twelve-auditor overnight audit of
> AgentJobs. Read `audits/2026-08-21/PLAN.md` (context), then all twelve
> findings files `01-*` through `12-*`. You do not re-audit the code; you may
> open a file only to adjudicate a direct contradiction between two auditors.
>
> Produce `audits/2026-08-21/00-BIG-DAWG-REPORT.md`:
> 1. **Executive summary** — a page Jeff reads on his phone with coffee.
> 2. **Ranked findings** — every P1 and P2 across all audits, one global
>    ranking, deduplicated (two auditors finding the same defect from
>    different sides is one finding with two witnesses — say so, that's
>    corroboration).
> 3. **Contradiction sweep** — every place two auditors state incompatible
>    facts. These are the most valuable entries; adjudicate or mark unresolved.
> 4. **Themes** — failure classes that recurred across systems (e.g.
>    "invariants enforced at one edge", "docs describing plans as present
>    tense"). A theme cites at least three findings.
> 5. **Coverage honesty** — the union of every "What I did not get to"
>    section, plus any auditor that died (stub files), so the report states
>    what this audit is NOT evidence about.
> 6. **The weekly-ritual verdict** — what to do differently in next week's
>    Big Dawg Audit, given how this one went.
>
> Then file one AgentJobs **draft** task per P1/P2 finding (or per coherent
> cluster) via `task_create_draft`, actor `claude`, each self-contained per
> the Resumption Contract, referencing its findings file. Do not promote,
> claim, or start any of them.
>
> Finally, commit: `audits/2026-08-21/` (all files including PLAN.md if
> uncommitted) as `chore(task-242): big dawg audit findings and report`, and
> the new task files as `chore(tasks): draft tasks from big dawg audit` —
> both to `main`, explicit paths, no `git add -A`. Then append a `progress`
> entry to task-242 summarizing counts (auditors completed, findings by
> severity, tasks filed) and hand task-242 to `human`/`review` with a
> ball_prompt pointing at the report.

---

## Notes

- `audits/` is a new top-level directory. Additive, not a restructure; Jeff can
  veto before dispatch and the plan moves to `C:/ai/inbox/` instead.
- Auditors 10 and 12 overlap on dispatch security by design — different lenses
  on the highest-risk surface. The synthesis dedupes.
- Nothing here pushes to any remote. Findings may name sensitive exposure
  details; **review the report before any push of `audits/`** — or keep
  `audits/` untracked/gitignored if 12's findings turn out spicy.
