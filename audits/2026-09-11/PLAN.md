# Big Dawg Audit II — runbook for the night of 2026-09-11

Follow-up to the first Big Dawg Audit (task-242, night of 2026-08-21). Read that task's
log before dispatching: entry 9 is a binding decision about how the auditors are
constrained, and this plan implements it.

**Runner:** `claude-fable-5-1`, via the `big-dawg` runner group. That group has one
member on purpose — if Fable cannot run, the dispatch refuses rather than quietly doing
the work on something cheaper.

**Posture:** `autonomous`. The parent task holds the supervisor session; children are
spawned in `auto` mode. `bypassPermissions` is classifier-refused for a spawned session
and must not be used.

---

## What is different this time

The first audit ran against a repository whose task records were YAML files under git,
and it told its auditors to execute nothing at all. Both of those facts have changed,
and each changes a rule.

**1. Execution is allowed, and expected.** The first audit's shared preamble read
"read-only" as "no execution", which stopped auditors turning hypotheses into
demonstrated findings — recorded as a binding decision on task-242. Auditors here run
tests, stand up servers, drive sandboxes and read the output. What stays banned is
mutation, commits, and the gate.

**2. The task store is a database outside the checkout.** Since 2026-09-07 the records
live in `C:/Users/jpose/.agentjobs/databases/agentjobs.db`, not in `tasks/`. Under the
old arrangement a stray write was a git diff away from recovery; now it is not. So the
single hardest rule in the preamble below is that no auditor writes to the live store,
and every auditor that needs to exercise storage uses `scripts/sandbox_store.py`.

**3. There is a prior audit to be held to account.** Twenty-three of the thirty-three
findings the first audit filed are still unspecced drafts three weeks later. Auditor 01
exists to ask whether they are still true, and auditor 14 exists to ask whether filing
them was the right move at all.

**4. The surface is much larger.** 1,132 commits and roughly 181,000 added lines since
2026-08-22. Whole subsystems that did not exist at the last audit: `sqlstore/`,
`store_factory.py`, `storage_split.py`, `cutover.py`, `principals.py`, `identities.py`,
`session_identity.py`, `capabilities.py`, `front_door.py`, `exposure.py`, `playbooks/`,
`roadmap.py`, `record_check.py`, `quotation.py`, `branch_report.py`, `contexteval/`, and
most of `dispatch/`.

---

## Pre-flight, before the first batch

Run these in order. Each is cheap, and the second one is the reason the others matter.

1. **Check the meter.** `/usage`. The roster is fourteen auditors plus a synthesis
   session on Fable. If headroom looks tight, apply the cut order at the bottom of this
   file rather than shortening individual briefs.
2. **Back up the task database.** Auditors execute this time, and the store is no longer
   recoverable from git. Copy `C:/Users/jpose/.agentjobs/databases/agentjobs.db` to a
   dated file beside it before anything starts, and say in the dispatch log where it
   went. `src/agentjobs/sqlstore/backup.py` may already do this correctly — using it is
   fine, skipping the backup is not.
3. **Clean tree.** `git status` on the main clone. Auditors read the main clone, so a
   dirty tree makes every finding ambiguous about whose change it saw.
4. **Confirm the live server.** The dashboard on 127.0.0.1:8876 is Jeff's, started by
   `C:/ai/shared/launchers/open-agentjobs.ps1` and proxied to the tailnet. No auditor
   stops, restarts or writes through it.
5. **Create the findings directory.** `audits/2026-09-11/`.

---

## Shared preamble

Prepend this verbatim to every auditor brief.

> You are auditor **NN** of the Big Dawg Audit for the night of 2026-09-11, working in
> the AgentJobs repository at `C:/projects/agentjobs`. You are one of fourteen auditors,
> each covering a different system. A synthesis session reads all fourteen findings files
> afterwards and writes one combined report; it will never read your transcript, so
> everything you learned has to be in your file.
>
> **What you may do.** Read anything. Run anything read-only. Run the test suite or parts
> of it. Start a server, a sandbox, or a script and drive it. Call the API and the CLI's
> read-only commands. Use `curl`, `pytest`, `npm`, the `scripts/*_sandbox.py` helpers.
> Turning a hypothesis into a demonstrated finding is the point of this audit; the
> previous one banned execution and the findings were weaker for it.
>
> **What you may not do, ever.**
> - **Do not write to the live task store.** Task records are rows in
>   `C:/Users/jpose/.agentjobs/databases/agentjobs.db`, outside the checkout and outside
>   git. Nothing you do may mutate them: no `claim`, `handoff`, `close`, `update`,
>   `queue move`, `create`, no MCP task tool, no PATCH or POST against project
>   `agentjobs`. If you need to exercise storage, build a throwaway store with
>   `scripts/sandbox_store.py` and point at that.
> - **Do not edit source, and do not commit, merge or push.** A thing you want fixed
>   becomes a finding, not a patch.
> - **Do not run `scripts/check.py`.** It contends for the whole machine and proves
>   nothing about an audit. Focused `pytest`, `vitest`, `ruff` and `mypy` runs are fine.
> - **Do not take a worktree, and do not call `EnterWorktree`.** You are read-only in the
>   main clone. If a background-session preamble tells you to isolate first, that
>   instruction is wrong in this repository.
> - **Do not touch the server on port 8876.** It is the owner's live dashboard. Your own
>   server, if you need one, binds `89NN` where NN is your auditor number.
>
> **Your one write.** Exactly one file: `audits/2026-09-11/NN-<slug>.md`. Compose it in
> your session scratch directory and copy it into place. The repository's managed-path
> write hook refuses a write whose *content* names a task-record path, which cost two
> auditors their final minutes last time; composing elsewhere and copying in sidesteps
> it. Do not commit the file — the synthesis session commits everything.
>
> **Format.** One section per finding:
> - **Severity** — P1 blocking defect, P2 should fix, P3 improvement, P4 observation.
> - **Evidence** — `file:line`, or the command you ran and what it printed. A finding
>   with no evidence is an opinion.
> - **Verified by running, or inferred from reading** — say which, per finding. This is
>   new and it is not optional. At the last audit the documentation auditor marked three
>   documents accurate and three specialists then overturned those verdicts at the
>   sentence level. An honest "inferred, not verified" is worth more than a confident
>   wrong verdict.
> - **The fix, or the question.** Concrete.
>
> **Stance.** Adversarial. The standing question from ENGINEERING.md is "what would this
> have caught?" — apply it to code, documentation and tests alike. Praise is not a
> finding. If a system is genuinely sound, say so in one line and spend your budget
> where it is not.
>
> **End with two sections.** "What I did not get to" — silent truncation is the failure
> mode of an audit, so name the corners you never reached. And "Questions for other
> auditors" — anything you believe about a neighbouring system but could not check.
>
> **Do not file tasks and do not write to the AgentJobs log.** Your file is your report.

---

## The auditors

### 01 — Did the first audit land? Regression sweep of the 2026-08-21 findings

Read `audits/2026-08-21/00-BIG-DAWG-REPORT.md` and the thirty-three findings it filed as
task-244 through task-276. Nine closed completed, one is ready, twenty-three are still
unspecced drafts.

For each of the twenty-three still-draft findings: **is the defect still there?** Go to
the code as it stands tonight and give one of three verdicts, with evidence.

- **Still real** — reproduce it, cite today's `file:line`, and say whether the original
  description is still an accurate account of it.
- **Fixed incidentally** — name the commit or the subsystem replacement that fixed it.
  The storage migration and 1,132 commits will have closed some of these by accident,
  and nobody has checked which.
- **Invalid or obsolete** — the code it describes is gone, or the finding was wrong.

Then take the nine that closed completed and spot-check three of them: did the fix hold,
or has it regressed? Pick the three with the largest blast radius.

Produce a table at the top of your file: task id, one-line finding, verdict, evidence
pointer. The synthesis session turns that table into a closure list for the owner to
approve in one pass, so it has to be complete and it has to be honest about the ones you
could not settle.

### 02 — SQLite storage: the new source of truth

`src/agentjobs/sqlstore/` (store, connection, migrations, importer, backfill, blobs,
backup, reporting_tz), `store_factory.py`, `storage_protocol.py`, `storage_split.py`,
`storage_config.py`, `cutover.py`, `taskfiles.py`, `docs/storage-sqlite.md`, and
`docs/migration-guide.md`.

This replaced the source of truth six days before this audit and has never been audited.
Weight it heaviest.

- **Durability and atomicity.** The first audit found task-file writes were not atomic
  (task-247). Is the replacement actually atomic? Journal mode, synchronous setting,
  transaction boundaries, what a crash mid-write leaves behind.
- **Concurrency.** Several agents, a running server, and a CLI all reach the same
  database. Locking, busy timeouts, writer contention, what a second writer sees. Prove
  it by running concurrent writers against a sandbox store, not by reading the connection
  setup.
- **Migration completeness.** `agentjobs storage status` reports `rows=404 files=376` for
  this project. Explain the gap. Is the importer idempotent on a re-run? What happens to
  a half-imported project? task-380 is actively retiring the frozen YAML directories and
  task-398 says SQLite is not yet the only backend — audit the state the code is actually
  in, not the state either task describes.
- **The abstraction boundary.** ENGINEERING.md says every call site must reach storage
  through `store_factory.task_manager_for`. Find the ones that do not. This is a grep
  with a real payload attached.
- **Backup and recovery.** Records are no longer in git. What is the recovery story if
  this file is corrupted or deleted? Is anything scheduled? Is `sqlstore/backup.py`
  reachable from the CLI, and does it produce a restorable artefact — test a restore.
- **Per-project databases.** Five projects, five files, one default. Cross-project leaks,
  project-id-to-filename resolution, what an unregistered project gets.

### 03 — Authorization, identity, and exposure

`principals.py`, `identities.py`, `session_identity.py`, `capabilities.py`,
`front_door.py`, `exposure.py`, `api/authorization.py`, `api/dependencies.py`,
`dispatch/auth.py`, `dispatch/credentials.py`, `docs/authorization.md`,
`docs/principals-design.md`, `docs/identity-registry.md`, `docs/tailnet-front-door.md`,
`docs/exposure.md`, `scripts/tailscale-service-host/`.

The first audit's headline P1 was that the API was unauthenticated on the tailnet with
identity as a body field (task-244). A great deal of machinery has landed since. The
question is whether it closed that hole or documented it.

- **Does run scoping hold?** ALLAGENTS.md claims a run may work its own task and file new
  ones, and may not approve a review, dispatch anything, change dispatch configuration,
  or act on another task. Try to break each of those, using a sandbox project. A 403 that
  arrives for the wrong reason is a finding.
- **Credential lifecycle.** Where does a run's credential come from, how long is it good
  for, what revokes it, what happens when a run dies holding one, and can a second
  process present it.
- **The front door.** What is exposed on the tailnet today, to whom, and with what
  authentication. Read the exposure code, then verify against a running server rather
  than trusting the documentation.
- **Cross-site and cross-host.** task-274 proposed a custom header on non-GET plus
  TrustedHostMiddleware, and is still an unspecced draft. Is a browser on the same machine
  able to drive the loopback API today? Demonstrate or refute.
- **Injection.** Task content reaches dispatched prompts. What can a malicious or merely
  careless task record make a dispatched agent do? The first audit raised this; check it
  against today's `dispatch/scaffold.py` and `wake.py`.
- **Secrets.** Webhook secrets, credentials, anything written to the ledger, the run
  directory, or the transcript. The repository has a public remote.

### 04 — Dispatch, finish, and the merge runway

`src/agentjobs/dispatch/` in full — runner, poller, auto, guards, ledger, phases, wake,
scaffold, record_commit, address, config, epic, finish, finish_status, handback,
registration, budget, interactive, transcript, session_env, codex_app_server,
atomic_yaml — plus `docs/agent-dispatch-design.md`, `docs/codex-dispatch*.md`,
`branch_report.py` and `scripts/run_report.py`.

Mostly new code since the last audit, and it is the system that will be running *this*
audit.

- **The scripted finish.** It rebases, gates, merges `--no-ff`, rebuilds, restarts,
  closes and cleans up, unattended, on one click. Walk every branch of it. What does it
  do when the rebase conflicts, when the gate goes red halfway, when the restart fails,
  when the process dies between the merge and the close. Does it ever leave `main` moved
  and the record saying otherwise? The record's two sentences are load-bearing.
- **The merge runway and gate slots.** `gate_slots.py`, the runway lock, task-223 and
  task-339. Does a queued finish actually queue, or does it time out and report success?
  Can two finishes interleave?
- **Run registration and stall protection.** task-320 added the run record every stall
  protection is keyed on. What is polled, on what interval, and what happens to a session
  that parks on a permission prompt at 3am. This audit will find out empirically tonight
  either way — but audit the mechanism, because the epic walk and the poller both depend
  on it.
- **Guards and the classifier.** What the guards actually block, what they let through,
  and the false positives. The managed-path write guard refusing writes whose content
  merely names a task path (task-276) is one; find the others.
- **The epic walk.** Bounded retries, the grounding rule on a failed child, what happens
  when a child parks rather than fails. Read `epic.py` against what the documentation
  promises.
- **Ledger integrity.** The first audit found races between cancel and the poller
  (task-264), still an unspecced draft. Still true?

### 05 — Gate, receipts, and the performance claims

`scripts/check.py`, `gate_scope.py`, `gate_slots.py`, `bootstrap.py`, `bench.py`,
`run_report.py`, `build_frontend.py`, `build_release.py`, `project_setup.py`,
`corpus_stats.py`, `docs/performance.md`.

- **Is the `--since-gate` table still default-deny?** ENGINEERING.md stakes the safety of
  narrowing the gate on that property. Dozens of new modules have landed since the table
  was written. Confirm that an unclassified path selects all ten stages, then confirm
  that every classified path's entry still names something true. `audits/` is
  unclassified today — check whether that is the right answer or an accident.
- **The receipt chain.** A green run attests to a commit. Can a receipt be made to attest
  to a tree that was not what ran? What happens across a rebase, a worktree, a branch
  switch. Is a chain of `--since-gate` receipts genuinely auditable by a third party?
- **The numbers in the documentation.** `docs/performance.md` carries per-stage costs,
  contention figures and a corpus of dispatched-run measurements. Re-measure enough of
  them to say whether the document is current. Quote a command and a date, never a bare
  count — that is the repository's own rule and this is the auditor that should hold it.
- **Bootstrap's wrong-checkout defences.** The `VIRTUAL_ENV` hijack, the editable-install
  check, the refusal that task-194 produced. Try to defeat them in a scratch worktree.
- **Release build.** `build_release.py` — does the artefact it produces actually install
  and run somewhere clean? This matters more than it used to.

### 06 — Playbooks

`src/agentjobs/playbooks/`, the four files in `playbooks/` (flesh-out, groom, reorder,
roadmap), `docs/playbooks-design.md`, `scripts/playbook_sandbox.py`, and the MCP
`playbooks_list` tool.

A system that did not exist at the last audit.

- **Are the gates enforced, or only described?** Each playbook declares a gate — a thing
  that must be true before a verb runs. Find out whether anything in code stops a run
  crossing one, or whether the gate is prose an agent is trusted to obey. Say plainly
  which it is; the design documentation should not be allowed to imply the stronger one.
- **The declared verb list.** A playbook names the verbs a run may use. Is that a
  capability restriction or a comment?
- **`run_task` scaffolding.** What gets created, with what defaults, and whether a
  playbook run is distinguishable afterwards from hand-done work.
- **Would they survive a bad run?** Walk the groom playbook in particular: it closes
  tasks. What stops a run closing something outside the approved list, other than the
  instruction not to?
- **Coherence with the roadmap generator.** The roadmap playbook edits records that the
  generator then projects into a file with a public remote. Audit that path end to end.

### 07 — Schema, validation, and record integrity

`models_v2.py`, `validation.py`, `schema_tolerance.py`, `record_check.py`,
`quotation.py`, `migration/`, `migrate_schema.py`, `docs/task-schema.md`,
`docs/schema/`, `docs/schema-design.md`.

A repeat of the last audit's auditor 3, with the ground shifted: records are rows now,
and validation has grown two new guards.

- **Are the four state axes enforced where it counts?** lifecycle / ball / ball_reason /
  outcome. The last audit's question was whether states the verbs forbid are
  constructible through storage. Ask it again of the SQL store — the constructible set
  may be entirely different.
- **Tolerance policy.** task-248 was the tolerant reader rejecting an enum that widened
  two days later; it closed completed. Is the policy now coherent, and would the same
  class of failure be caught?
- **`agentjobs validate` on its own corpus.** The last audit found it red with 221
  problems (task-262, still an unspecced draft). Run it tonight and report the number and
  what the problems are. If it is still red, that is a P1 for an integrity tool.
- **The quotation guard.** It refuses records that quote a person. Does it work, what are
  its false positives, and what does `redact` leave behind.
- **Migration re-runs.** task-272 says a re-run on a half-migrated corpus duplicates queue
  positions. Test it against a sandbox store.

### 08 — Manager, operations, and the queue

`manager.py`, `operations.py`, `receipts.py`, `actors.py`, `projects.py`, `queue.py`,
`queue_check.py`, `docs/task-selection-design.md`, `docs/agent-workflow.md`.

- **Idempotency.** The `operation_id` contract promises a replay returns the original
  result rather than writing twice. The last audit said it returns the *current* task
  instead, and that webhooks fire on replays that wrote nothing (task-255, still draft).
  Test it directly against a sandbox store.
- **Verb coverage.** claim, handoff, release, close, promote, queue_move. Is there any
  path that moves a state axis without writing its log entry? The whole design rests on
  there not being one.
- **Queue semantics over SQL.** Bands, positions, collisions, what `task_next --why`
  reports versus what the code does, whether `queue check` detects the corruption
  `queue repair` claims to fix. The reopen/reband position race (task-253) was reproduced
  last time — reproduce it again or show it gone.
- **Concurrency.** Two agents claiming the same task; a create and a move racing for a
  band position. Demonstrate, with a sandbox store and real concurrent processes.
- **Actor and project registries.** Reserved ids, agents acting as `default_user`
  (task-263), what an unregistered project resolves to.

### 09 — REST API, client, webhooks, OpenAPI

`api/` (main, routes, models, contract, dependencies, spa), `client.py`, `webhooks.py`,
`export_openapi.py`, `instrumentation.py`, `remote_manager.py`, `docs/api-reference.md`,
`docs/webhooks.md`, `docs/analytics-design.md`.

- **The error contract.** One envelope or several? The last audit found `HTTPException`
  rendering outside `ErrorBody` and status codes undeclared in OpenAPI (task-256, still
  draft). Hit the routes and see.
- **Is OpenAPI honest?** Generate it, diff it against what the routes actually accept and
  return, and check that the generated client in `frontend/src/api/generated` is
  reachable from a real freshness check rather than a promise.
- **Webhooks.** HMAC correctness and timing-safe comparison, replay protection, delivery
  guarantee, whether secrets come back out of the API (task-257), and
  `POST /api/webhooks/{id}/test` which the last audit says always returns 500 with no test
  covering it (task-250). Both still unspecced drafts — settle them.
- **Instrumentation.** `X-Response-Time-Ms`, `X-Task-Parses`. Do they reach every route,
  and do the numbers mean what `docs/performance.md` says.
- **Parity.** Every manager verb reachable over HTTP, with the same refusals.

### 10 — CLI and MCP

`cli.py`, `src/agentjobs/mcp/`, `dashboard.py`, `docs/mcp.md`, `docs/mcp-clients.md`,
`docs/mcp-integration-design.md`, `docs/tasks-shell.md`, and the installed Claude Code
plugin.

Merged from two of last year's auditors; they overlap on parity and the failure-mode
question is the same for both surfaces.

- **Three-way parity.** CLI, HTTP and MCP against the manager's verbs. Name the gaps in
  each direction. Newer verbs are the likely holes: `finish`, `branches`, `storage
  status`, `redact`, `quotations`, `queue check`/`repair`, `dispatch` and its
  subcommands.
- **What a confused agent sees.** Deliberately misuse each MCP tool — wrong actor, reused
  operation id, stale `expected_revision`, a task belonging to someone else — and judge
  the error text by whether it tells an agent what to do next. This is the ergonomics
  that decides whether dispatched runs get stuck.
- **The MCP instruction text.** It is loaded into every session before its first thought.
  Is every sentence still true?
- **Duplicate registration.** Both `agentjobs` and `plugin:agentjobs:agentjobs` are
  live in this environment, doubling every tool declaration in every session's context.
  Cost it and say whether it is intended.
- **Plugin drift.** task-271 says the installed plugin is a hand-edited snapshot pinned at
  version 0.1.0 forever. Check it tonight.
- **CLI lifecycle rules.** `status`/`stop`/`restart`/`open` against the "which server is
  yours to restart" rule (task-261). Does the CLI probe `/api/version` before acting, or
  does it still bind the default port and report success?

### 11 — React frontend and the PWA

`frontend/src` — App, components, `api/generated`, queryClient, `pwa.ts`,
`service-worker.js`, the report views — plus the vitest and Playwright suites and the
`scripts/*_sandbox.py` review harnesses.

- **Rendered values, not markup.** The repository's own standard, from the
  `data-ball="Ball.HUMAN"` incident. Assert on what a browser acts on. Sweep for the
  same class of bug elsewhere.
- **Test honesty.** Apply "what would this have caught" to the suite, and look
  specifically for the task-207 pattern: a test that sets up the state it was written to
  verify. That defect was invisible in both jsdom and Playwright because both harnesses
  established the focus the bug destroyed.
- **Staleness.** Service worker update checks, cache headers on the shell, what a phone
  that has not reloaded in a week is served (task-260, still draft). Test it, do not read
  it.
- **Query invalidation.** After a handoff, an approve, a dispatch, a finish — does every
  affected view refresh, or do some rely on a manual reload.
- **The live surfaces.** The review panel, the questions UI, live runs, the slot board,
  the dashboard's next action. These are what the owner actually looks at from a phone;
  weight them over the ones he does not.
- **Legacy pages.** `api/templates` and `api/static` — task-275 says the Jinja/Alpine
  pages carry a stored XSS through `marked.parse` into `innerHTML`, load CDN scripts with
  no SRI, and are still mounted and proxied to the tailnet. Confirm whether they are
  still mounted tonight, and whether the XSS is still reachable.

### 12 — Documentation versus reality

Every file in `docs/`, plus `README.md`, `ROADMAP.md`, `docs/index.md`,
`docs/quickstart.md`, `docs/installation.md`, `docs/mobile-access.md`, and the
`docs/integration/` and `docs/schema/` subdirectories.

**Method, which matters more than the coverage here.** At the last audit this auditor
marked three documents accurate and three specialist auditors then overturned those
verdicts at the sentence level. The failure was checking documents for plausibility
instead of against code. So:

- A document is **accurate** only for claims you verified against source or by running
  something. Say which claims those were.
- Everything else is **unverified**, and unverified is a perfectly good verdict. Do not
  round it up.
- Prefer depth on the documents a newcomer follows — quickstart, installation,
  index, README, ROADMAP — over breadth across design documents nobody executes.
- Flag anything a new user would follow off a cliff, and anything written in the present
  tense about something unbuilt. `agent-dispatch-design.md` had both problems last time.
- `ROADMAP.md` is generated and public. Read it as a stranger and say what impression it
  gives.

### 13 — Context architecture and the agent contract

`CLAUDE.md` and its @-chain, `AGENTS.md`, `ALLAGENTS.md`, `ENGINEERING.md`,
`C:/ai/shared/GLOBAL-AGENTS.md`, the MCP server instruction text, the dispatch prompt in
`dispatch/scaffold.py`, `docs/context-budget.md`, `tests/test_context_budget.py`,
`contexteval/`, `scripts/context_eval.py`, `evals/context/`.

A repeat of the audit the owner named first last time, now with measuring machinery that
did not exist then.

- **Measure the bundle.** What a session loads before its first thought, today, in
  tokens. Against the budget the test enforces, and against what task-301 measured.
- **Is every rule still true?** These files name timings, counts, versions and probe
  dates. Find the sentences that describe a world that no longer exists — the file
  backend, `tasks/` directories, Claude Code version-specific probes, the frozen corpus.
- **Contradictions and duplication across the four files.** The merge-gate numbering
  incident is the model: two files numbered the same procedure differently and every
  cross-reference became ambiguous. Find the rest.
- **Static versus dynamic.** What is in the always-loaded bundle that should live in a
  task record, a playbook, or a document read on demand — and the reverse, rules that are
  load-bearing and buried where nobody reads them.
- **Does the eval harness work?** `scripts/context_eval.py` measures which of these words
  change what an agent does. Run at least one case end to end and report what it cost and
  what it told you. A harness nobody can afford to run is a finding.
- **The instructions this audit is running under.** You are reading them. Say what is
  wrong with them.

### 14 — The corpus and the process

The 404 records in the agentjobs project — 265 closed, 109 ready, 29 draft, 1 active —
plus `docs/task-corpus-audit.md`, `corpus_stats.py`, `record_check.py`, the queue's
stored order, and the three playbooks that exist to tend all of this.

This is the auditor that asks whether the system is being used the way it was designed,
and it is the one whose findings the owner is most likely to act on.

- **The twenty-three.** Auditor 01 asks whether those findings are still true. You ask
  the different question: what does it mean that thirty-three tasks were filed in one
  night and twenty-three are still unspecced three weeks later? Is the draft band a queue
  or a landfill? Would the groom playbook help, or is the problem upstream of it?
- **The Resumption Contract.** Sample at least fifteen records across lifecycles and
  judge each against it: could a session with no other context resume from this record
  alone? Report the rate, not an impression, and quote the worst example's shape without
  quoting any person.
- **Queue health.** Run `agentjobs queue check`. Are the bands meaningful? Do positions
  reflect decisions or accidents? How many ready tasks have never been looked at since
  they were filed?
- **Age and staleness.** Distribution of open-task age. What is the oldest ready task and
  why is it still there. Which tags and categories are live and which are dead.
- **Does the record survive the work?** Take five recently completed tasks and read only
  their records. Do they say what was decided and why, or does the reasoning live in a
  transcript nobody can read? That is the whole premise of the tool.
- **The public face.** `ROADMAP.md` is generated from these records and pushed. What does
  the backlog look like to a stranger.

---

## Batches, and the mechanism that watches them

Three concurrent sessions, per this machine's `max_concurrent_runs=3`. Order is
highest-value first, so that a night that runs short still produced the things worth
having.

| Batch | Auditors |
|---|---|
| 1 | 02 SQLite · 03 authorization · 01 regression sweep |
| 2 | 04 dispatch · 05 gate · 08 manager and queue |
| 3 | 07 schema · 09 API · 11 frontend |
| 4 | 13 context · 14 corpus · 10 CLI and MCP |
| 5 | 06 playbooks · 12 documentation |
| — | synthesis, alone |

**Spawning.** Each auditor is a background session on `claude-fable-5-1` in `auto`
permission mode, named `bda2-NN-<slug>`. Do not use `bypassPermissions`: it is
classifier-refused and the session parks.

**Watching.** Back a real poll — the supervisor checks session state on an interval and
does not simply promise to check back. The done-signal for an auditor is *its findings
file existing and ending with its two closing sections*, not the process exiting; a
session that ends idle with a complete file needs no retry, and one that exits cleanly
with a truncated file does.

**Failure rule.** One retry per auditor. If the retry also fails, write a two-line stub
in its place naming the auditor and what went wrong, and carry on. A missing file that
nobody accounted for is the one outcome the synthesis cannot work around.

**Cut order**, if the meter drops between batches: 12, then 06, then 10, then 11. Never
cut 01, 02, 03, 05 or 14.

---

## Synthesis

One session, `claude-fable-5-1`, after all fourteen land. It reads the fourteen findings
files and nothing else at depth.

> You are the synthesis session for the Big Dawg Audit of 2026-09-11. Read
> `audits/2026-09-11/PLAN.md` and every findings file `01-` through `14-` in that
> directory. Read nothing else at depth; open source only to adjudicate a disagreement.
>
> Write `audits/2026-09-11/00-BIG-DAWG-REPORT.md` with these sections:
>
> 1. **Executive summary** — phone-sized. The five things that matter, in order.
> 2. **Did the first audit land** — auditor 01's table, tidied into a closure list: for
>    each of the twenty-three still-draft findings, the verdict and the recommended
>    disposition. This is the section the owner acts on first, so put it above the new
>    findings.
> 3. **Ranked findings** — every P1 and P2 across all fourteen auditors, deduplicated
>    into clusters, ranked by blast radius rather than by auditor number.
> 4. **Contradiction sweep** — every place two auditors' facts disagree. Adjudicate as
>    many as you can by opening source, and say which you adjudicated and which you could
>    not. These were the most valuable section of the last report.
> 5. **Themes** — failure classes that recurred across systems. Not a summary of section
>    3; the pattern behind it.
> 6. **Coverage honesty** — every auditor's "did not get to", collected. What this audit
>    did not look at.
> 7. **What to change about the next audit.**
>
> **Filing.** File a draft task only for a genuine **P1**, and spec it well enough to be
> claimed — summary, description, constraints, acceptance. This is a deliberate change
> from the 2026-08-21 audit, which filed thirty-three draft tasks of which twenty-three
> are still unspecced: a finding that lives in the report is recoverable, and a task
> nobody can claim is clutter. Every P2 and below stays in the report. Say in the report
> which findings you filed and which you deliberately did not.
>
> **Committing.** Commit `audits/2026-09-11/` to `main` with `chore` commits. Do not push
> — the remote is public and this repository's rule is that approval to merge is never
> approval to push. Then hand the parent task to `human` / `review` with a prompt that
> names the report path, the five headline findings, the closure list's size, and the
> count of tasks you filed.

---

## Deliverables

- `audits/2026-09-11/00-BIG-DAWG-REPORT.md`
- `audits/2026-09-11/01-` .. `14-`, the fourteen findings files
- A closure list for the twenty-three shelved findings of the first audit
- Draft tasks for P1s only, each specced enough to claim
