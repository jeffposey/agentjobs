# 01 — Regression sweep of the 2026-08-21 findings

Auditor 01, Big Dawg Audit II, night of 2026-09-11. Read-only in the main clone at
`096f33ae` (2026-09-11 21:30 CDT), 1,119 commits after the first audit. Live server
on 8876 used for GET only; every mutation below ran against a throwaway SQLite store
(`scripts/sandbox_store.py`) or a TestClient sandbox with its own temp home.

Method per draft: read its spec and acceptance criteria out of the live record, then go
to tonight's code, and where a reproduction was cheap, run one. Each verdict says
whether it was **verified by running** or **inferred from reading**. Scripts are in the
session scratch directory (`repro.py`, `repro_api.py`, `repro_api2.py`); their output
is quoted inline so nothing depends on the transcript.

## Closure table

State as of tonight: nine closed completed (244, 248, 249, 251, 252, 268, 269, 270,
273), one ready (247), twenty-three drafts. Verdicts: **Still real** / **Changed
shape** (defect survives, description no longer accurate) / **Partly fixed** / **Fixed
incidentally** / **Obsolete**.

| Task | One-line finding (as filed) | Verdict tonight | Evidence |
|---|---|---|---|
| 245 | `git merge`/`git add`/`npm run` pre-approved; wake stub frames `ball_prompt` as the human's words; draft and human-ball tasks dispatchable | **Still real**, all three ACs; ac-1 now contradicts a recorded decision | `runner.py:394-405`; `wake.py:62`; `guards.py:835-846`; `tests/test_dispatch_runner.py:324-343` (read) |
| 246 | audits/ and machine identifiers on the public remote; decision needed | **Still open, and wider**: the synthesis report is now pushed too | `gh repo view` → PUBLIC; `git ls-files audits` = 15; `db55d7d7` on origin/main (run) |
| 250 | `POST /webhooks/{id}/test` always 500s; no webhook route tests | **Still real** | `RuntimeError: asyncio.run() cannot be called from a running event loop` out of the route (run) |
| 253 | Reopen/reband position computed under the queue lock, written outside it; duplicates the position | **Changed shape**: race survives, outcome is now an unhandled `IntegrityError`, not corruption | `UNIQUE constraint failed: task.project_id, task.priority_rank, task.queue_position` (run); `manager.py:1264-1265` vs `:1220` |
| 254 | `update_task` has no allowlist; reopen writes no transition | **Still real**; owner hit it 2026-09-07 | four axis patches accepted, `log=[]` empties the log (run); `manager.py:1192` |
| 255 | Webhooks re-fire on replay; replay returns current task; create/update outside the envelope | **Still real** | two `task.handoff` deliveries for one `operation_id` (run); `POST /tasks` replay → bare 201 (run); `mutation_tools.py:572,800` |
| 256 | Three error envelopes; OpenAPI 422 everywhere; human actions 404 on closed; `actionable:false` invented | **Still real** | 121 ops, 108 declare 422, 0 declare 400/404/409, no `ErrorBody` (run); `/reject` on closed → 404, `/handoff` → 409 (run); `summaries.py:52` |
| 257 | Webhook secrets returned; SSRF targets accepted; no delivery id; no retry | **Still real** | `secret` in POST/GET bodies and OpenAPI; `169.254.169.254` target and retired event name accepted (run) |
| 258 | Three projects' open tasks unpositioned; `next` says "nothing claimable" over a broken corpus; repair has no dry run | **Fixed incidentally** by the SQLite cutover (task-273); residual on `queue repair` | live `/next` answers for all three; `storage status` files=0; `ux_task_queue_slot` makes the state unrepresentable (run) |
| 259 | Held task claimable; active+available, draft+work constructible; dispatch accepts draft/human-ball | **Still real** | held ready task offered by `get_next_task` and claimed; active+available accepted, displays Ready (run); `manager.py:672-698`; `guards.py:835-846` |
| 260 | Shell served without Cache-Control; no SW update check; gate empties live bundle; `randomUUID` on http | **Still real** (doc sentence fixed) | live `GET /app/` → etag, no cache-control; no `.update()` in `frontend/src`; 6 `randomUUID` sites; `vite.config.ts:8` (run + read) |
| 261 | CLI identifies the server by port; `restart` starts an 8765 server; read-only commands create `tasks/`; `gui.port` ignored | **Partly fixed**; F5 moved rather than closed | `cli.py:651` substring match; `:804` foreground uvicorn; `open` probes (`:898`), `stop`/`status`/`restart` do not; `agentjobs list` in an empty dir created `~/.agentjobs/databases/local-tasks-5ac3195b10.db` at 21:48:50 (run) |
| 262 | `validate` red on its own corpus: 221 problems, 96 reserved/system actors | **Changed shape**: corpus retired from the repo; the validator still rejects what the product writes, now 272 of 447 | `storage export` → `validate --tasks-dir`: 447 problems / 410 files; dispatcher 113, finisher 83, system 76 (run) |
| 263 | Agents can act as `default_user`; reserved ids accepted; PATCH/DELETE unvalidated; CLI defaults to Jeff | **Partly refuted** by task-332; the rest still real | run→human refused (`capabilities.py:270`); loopback claim as `Jeff Posey` → 200 *by decision* (`:274-279`); log as `dispatcher`/`finisher` → 200; DELETE closes as cancelled by `system` (run); `cli.py:2972` |
| 264 | Cancel-vs-poller contradictory entries; ceiling is a directory scan; locks keyed by bare task id | **Partly fixed** (batch path, task-390); session path, ceiling and keying unchanged | `runner.py:3190` vs `:2913`; `guards.py:914-924`; `ledger.py:136`, `guards.py:916`, `wake.py:153` (read); 1 of 213 runs shows the contradiction, the original one (run) |
| 265 | `_ball_moved` counts dispatcher handoffs; poller fires no webhooks; `finished_at` is settlement time | **Still real** ×3; P3-15 (poller reaps) fixed | `runner.py:2884-2891`, `:2722`; `main.py:162` passes no managers; `runner.py:2932`; reap at `:2938` (read) |
| 266 | `.mcp.json` pre-approves a repository's own MCP servers | **Still real** | `runner.py:457-490`; `dispatch/config.py` has no MCP allow-list (read) |
| 267 | Receipt reads HEAD after the run; rename source unseen; bootstrap re-hijacks a broken env; wheel not proven | **Still real** ×4; blast radius of P2-1 reduced | `check.py:805` called at `:1021`; `gate_scope.py:223` no `--no-renames`, `:125` `fnmatch`; `bootstrap.py:124-125`; `build_release.py:73-100` (read) |
| 271 | Installed plugin is a hand-edited 2026-08-17 snapshot; version 0.1.0 forever; compat ignores `source_commit` | **Partly fixed** (repo `.mcp.json` no longer pins 8765); drift and vacuous version check still real | `diff -rq` 6 files differ; cache guard denies `head x 2>/dev/null`, repo guard allows (run); `compat.py:62-77` |
| 272 | `migrate-schema` re-run duplicates positions; writes outside `TaskStorage`; `TestTheRealCorpus` asserts nothing | **Obsolete** for the product; ac-3 satisfied by deletion | `migrate_schema.py:784,872` unchanged; `tests/test_migrate_schema.py:406` (removed by task-380); every project is SQLite (run) |
| 274 | No Host/Origin check; typeless body parses as JSON; body-less POSTs mutate | **Still real** | `Host: evil.example` → 200; PATCH with no Content-Type and a foreign Origin changed a title (run); `main.py:261-266` |
| 275 | Legacy Jinja pages: `marked.parse` → `innerHTML`, CDN scripts without SRI, still mounted | **Still real** | live `/p/agentjobs/tasks/task-244` → 200, 22 markdown-render hits; `base.html:30-32,47`; `main.py:365-366` (run + read) |
| 276 | Write guard refuses reads and unrelated writes that mention a task path | **Partly fixed**; one new false-positive class | Write-with-mention still denied (`task_write_guard.py:443-445`); `grep\|sed -n` now allowed; `python`/`awk` read-only one-liners denied by both copies (run) |
| 247 (ready) | Atomic writes for machine-level YAML | Not in scope (not a draft); glanced: **still real** | `storage_config.py:209`, `projects.py:169`, `webhooks.py:68` are `write_text` (read) |

Closed-task spot-checks (three largest blast radius, plus one that was cheap):

| Task | Fix | Held? | Evidence |
|---|---|---|---|
| 244 | Identity at the front door | **Holds for mutations**; reads are still open to any socket peer | non-loopback and forged-header clients → `403 no_proven_identity` on log/dispatch/register; `GET /api/projects` and task reads → 200 (run); server binds `127.0.0.1:8876` (netstat) |
| 273 | SQLite storage | **Holds** | `storage status`: every project rows, files=0; no `TaskStorage(` construction left in `src/`; `store.py:512-533` one transaction per mutation (run + read) |
| 249 | Run identity reaches `--bg` workers | **Holds** | `session_env: delivered` on 95 run records; `tests/test_dispatch_session_env.py` 21 passed (run) |
| 248 | Tolerant enum reader | **Holds** | a ready task with `ball_reason: made_up_reason` parses under `tolerant_enum_values()` and is refused without it (run) |

---

## Findings, per draft

### task-245 — allow-list and wake framing

- **Severity** P1 (unchanged). **Confirms task-245.**
- **Evidence.** `src/agentjobs/dispatch/runner.py:394-405` still lists `git add`,
  `git commit`, `git merge`, `npm run` in `ALLOW_PREFIXES`; `allow_rules()` at `:426`
  emits them for both shells. `src/agentjobs/dispatch/wake.py:62` still reads
  *"A human has moved the ball back to you. What they said:"* and `build_wake_prompt`
  (`:248-269`) interpolates `ball_prompt` with no actor. `guards.py:835-846` refuse
  only `CLOSED` and `agent/hold`; nothing refuses `DRAFT` or `ball is HUMAN`.
- **Inferred from reading.** No dispatch was launched.
- **New, and blocking for this draft:** `tests/test_dispatch_runner.py:324-343`
  *asserts* `Bash(git merge:*)` is in the list and cites Jeff's explicit authorisation
  on task-222. ac-1 ("git merge is not in the pre-approved prefix list for any
  posture") therefore reverses a recorded decision without saying so. The draft cannot
  be promoted as written; it needs a decision entry that either overrides task-222 or
  narrows ac-1 (e.g. drop the pre-approval only where `finish.enabled` is on, which
  the spec already floats). The merge-gate rationale in `runner.py:240-251` (the
  `finish --posture-release` clause) makes the case for narrowing: the sanctioned
  merge path no longer needs a bare `git merge`.

### task-246 — the public remote

- **Severity** P1 (decision). **Confirms task-246, and the record is now understated.**
- **Evidence (run).** `gh repo view --json visibility` → `PUBLIC`. `git ls-files
  audits` → 15 files. `.gitignore` has no `audits` entry. `git status -sb` →
  `main...origin/main` in sync, and `git log origin/main -- audits/2026-08-21/00-BIG-DAWG-REPORT.md`
  → `db55d7d7`. The task's own description says the synthesis report *"is committed
  locally and NOT pushed"*; that stopped being true at some point after 2026-08-22.
  `git ls-files tasks` → 0 (task-380 retired the frozen YAML), so the user-profile
  paths in tracked task records are gone from `HEAD` but not from history.
- **Also (run):** of the 410 records exported from tonight's store, 126 contain the
  user-profile path (dispatch `argv` and `log_path` fields). That is data in the
  database, not the repo, but it is what any future export or re-import carries.
  ac-2 ("dispatch records no longer write absolute user-profile paths") is not done:
  `runner.py:1429,1491` still record the resolved executable.
- **The question is still Jeff's**, and it has one more file in it than the record
  says. Tonight's `audits/2026-09-11/` will land in the same place unless the ignore
  rule is decided first.

### task-250 — webhook test route

- **Severity** P1 (unchanged). **Confirms task-250.**
- **Evidence (run, TestClient sandbox).** `POST /api/webhooks` → 201; `POST
  /api/webhooks/{id}/test` raised `RuntimeError: asyncio.run() cannot be called from
  a running event loop` straight out of the app (TestClient re-raises what a real
  server would turn into a bare 500), plus `RuntimeWarning: coroutine
  'WebhookManager._dispatch' was never awaited`. Same at manager level from inside
  `asyncio.run()`. `src/agentjobs/api/routes/webhooks.py:78-87` is unchanged
  (`async def`, catches `ValueError` only); `src/agentjobs/webhooks.py:198` still
  `asyncio.run(self._dispatch(...))`.
- Route tests: `grep -rl "/api/webhooks" tests` → only `test_run_authorization.py`,
  which tests that a run *cannot* touch webhooks; still no test of any webhook route
  succeeding. `tests/test_webhooks.py:144` still `assert True`.
- **Fix** as filed. Note `WebhookManager` now takes a `WebhookStorage`, not a path, so
  the spec's line numbers are stale but the defect is not.

### task-253 — the reopen/reband race

- **Severity** was P2; **P3 tonight. Changed shape.**
- **Evidence (run).** Same lock profile: `manager.py:1264-1265` takes
  `storage.queue_lock()` around `_place()` only, and the write happens in `_mutate`
  at `:1220`. Under `SqlTaskStore`, `queue_lock()` is a write transaction that ends
  when the block ends (`sqlstore/store.py:1046-1049`), so the window is real. With a
  create forced into it:

  ```
  concurrent create took high-band position 100
  PATCH priority=high failed: IntegrityError UNIQUE constraint failed: task.project_id, task.priority_rank, task.queue_position
  get_next_task afterwards: ok
  ```

  `ux_task_queue_slot` (`sqlstore/migrations/001_initial.sql:114-116`) makes the
  duplicate unrepresentable, exactly as its comment claims. What survives is that
  the PATCH fails with an exception nothing maps: over HTTP that is a 500 with no
  body, and the caller retries or gives up with no idea which. The queue is intact
  and `next` works, so the "takes down next until someone runs repair" half is gone.
- **Fix.** Hold one write transaction across `_rejoining_the_queue` and the write
  (the transaction is already the lock, so this is a restructure of `update_task`,
  not new locking), and map `sqlite3.IntegrityError` on the slot index to a 409 with
  a retry hint. The task-253 log entry from the cutover already says to carry this
  into "transactional SQLite claim/reorder acceptance"; tonight's run is the
  evidence that it was carried only halfway. ac-2 (replayed priority patch does no
  rebalance) not tested.

### task-254 — `update_task` allowlist

- **Severity** P2 (unchanged). **Confirms task-254.**
- **Evidence (run, throwaway store).** On a closed task:

  ```
  update_task(lifecycle,ball,ball_reason,outcome): ACCEPTED -> lifecycle=ready log_types=[..., 'transition', 'queue_move']
  update_task(ball_prompt): ACCEPTED
  update_task(archived): ACCEPTED
  update_task(log): ACCEPTED -> log_types=[]
  ```

  The reopen wrote a `queue_move` and no transition; `log=[]` emptied the append-only
  log. `manager.py:1192` is still `payload.update(updates)`; `_rejoining_the_queue`'s
  docstring (`:1236-1238`) still says there is deliberately no reopen verb.
- The task log's 2026-09-07 entry records the owner hitting the missing reopen verb
  from every interface in turn. That entry is the strongest argument on the backlog
  for promoting this draft: it is no longer a hypothetical.
- **Fix** as filed; ac-3 (reopen writes a transition) is the user-facing half.

### task-255 — the `operation_id` contract

- **Severity** P2 (unchanged). **Confirms task-255, all three parts.**
- **Evidence (run).** Manager with a counting webhook stub: two `handoff` calls with
  one `operation_id` → log length 4 and 4 (no second write), `task.handoff`
  delivered twice. `manager.py:1533-1534` and `:1676-1677` still `_fire` after
  `_mutate` unconditionally. REST: `POST /tasks` twice with one `operation_id` →
  201 and 201, identical bare `Task` bodies, no `replayed` field. `mutation_tools.py:572`
  and `:800` still hard-code `"replayed": False`; `routes/tasks.py:464` still wraps
  `OperationConflictError` in a bare `HTTPException(409)`.
- **Fix** as filed. `_mutate` returning `(task, wrote)` is still the smallest change.

### task-256 — error envelopes and OpenAPI honesty

- **Severity** P2 (unchanged). **Confirms task-256.**
- **Evidence (run).** `frontend/openapi.json`: 121 operations, 108 declare a 422, 0
  declare 400/403/404/409, `ErrorBody` absent from `components.schemas`, webhook
  `DELETE` documented `200` and `422` (the route returns 204). Sandbox: `POST
  /reject` on a closed task → `404 {"detail":"Task 'task-001' is already closed."}`
  while `POST /handoff` on the same task → `409 invalid_transition` with the
  structured body. `GET /next` → bare `Task`; `mcp/summaries.py:52` still `value if
  value is not None else False`. `main.py:327-345` still reports only the first
  validation error as a 400 with `{"detail": ...}`.
- Since the audit a fourth shape was added on purpose: `Forbidden` renders
  `{"code": ..., "detail": ...}` (`main.py:314-324`). The reasoning in that docstring
  (a code in the body because 403 alone cannot say which refusal) is the argument for
  the single envelope this task asks for, applied to one exception class.
- **Fix** as filed.

### task-257 — webhook secrets, targets, delivery

- **Severity** P2 (unchanged). **Confirms task-257.**
- **Evidence (run).** `POST /api/webhooks` and `GET /api/webhooks` both return
  `"secret":"hunter2"`; the `Webhook` schema in OpenAPI lists `secret`. A
  subscription to `http://169.254.169.254/latest/meta-data` for the retired event
  `task.status_changed` → 201. `webhooks.py:31` is `HttpUrl` with no address check;
  no `delivery_id` anywhere in `webhooks.py`; bookkeeping is still `write_text`
  (`:68`). Live: all projects still answer `[]` for webhooks, so this remains latent.
- **Fix** as filed.

### task-258 — unpositioned projects and the silent "nothing claimable"

- **Severity** was P2. **Fixed incidentally** (ac-1 and ac-2), by task-273.
- **Evidence (run, live GETs).** `job-hunting` `/tasks/next` → task-017 with
  `queue_position: 500`; `fantasy-football` `/next` → task-012; `product-strategy`
  `/queue` shows positions on every open task (all drafts, so "backlog" is the right
  `next_action`). `agentjobs storage status` → every registered project is rows,
  `files=0`. Under SQL an open row without a position cannot exist (`models_v2.py`
  rule 6 plus the unique index), and `/tasks/broken` → `[]`, so the "all-broken
  corpus that `next` silently skips" is not constructible any more.
- **Residual, not verified:** `queue repair` has no `--dry-run` (`agentjobs queue
  repair --help` lists none), and `manager.repair_queue` (`:2504-2510`) says it
  reads raw files while `SqlTaskStore.tasks_dir` (`store.py:1070`) raises. I did not
  run repair on a sandbox project. If it raises, ac-3 is moot and `repair` is dead
  code on every project; that is a question for the queue auditor.
- **Recommendation.** Close as superseded; file a one-liner for `queue repair` under
  SQLite if the queue auditor confirms it is unreachable.

### task-259 — ball/reason untied from lifecycle

- **Severity** P2 (unchanged). **Confirms task-259.**
- **Evidence (run, throwaway store).**

  ```
  after hold: ready agent/hold
  get_next_task offers the held task? True
  claim on held task: ACCEPTED, ball_reason now work
  handoff active->agent/available: ACCEPTED; lifecycle active owner 'claude' display Ready
  ```

  `manager.py:672-698` `_skip_reason` still never reads `ball_reason`;
  `models_v2.py:963-1060` still enforces six rules and none ties `ball_reason` to
  lifecycle. Dispatch half inferred: `guards.py:835-846` refuse closed and hold only.
- **Fix** as filed (rule 7 in the model). Since `SqlTaskStore.mutate_task`
  re-validates the whole aggregate (`store.py:528-531`), a model rule now covers every
  write path at once, which makes the schema-first fix cheaper than it was in August.

### task-260 — PWA staleness

- **Severity** P2 (unchanged). **Confirms task-260** on three of four parts.
- **Evidence.** Live (run): `GET /app/` → `etag` present, no `Cache-Control`;
  `spa.py:131-139` returns `FileResponse(index)` bare while manifest and `sw.js` set
  `no-cache` (`:107,121`). Read: `grep -rn "\.update()\|getRegistration" frontend/src`
  → only the `controllerchange` listener in `pwa.ts:9`; `crypto.randomUUID()` at
  `App.tsx:331,346,359,790`, `IssueReporter.tsx:101`, `report/attachments.ts:85`;
  `vite.config.ts:8-9` still `emptyOutDir: true` into `frontend_dist`.
- **Refuted in part:** `docs/mobile-access.md:182` now says foregrounding is *not* a
  navigation and does not pick up a bundle. The doc was fixed; the behaviour was not.
- Not exercised on a device. **Fix** as filed.

### task-261 — the CLI and "the server"

- **Severity** P2. **Partly fixed; F5 changed shape. Confirms F2, F7, F8, F9.**
- **Evidence.** Read: `cli.py:651` still `if f":{port}" in line` (8765 matches 18765);
  `restart` (`:770-810`) still runs `uvicorn.run(..., port=8765)` in the foreground
  when nothing listens; `probe_api_base` is called by `open` (`:898`) and `dispatch
  config` (`:1952`) only, so `status`/`stop`/`restart` still act on a PID they have
  not identified; `gui.port` is read only in `project mcp-setup` (`:1290`).
- Run: in an empty, unregistered directory, `agentjobs list` printed `No tasks
  found.` and exited 0. No `tasks/` directory was created — but
  `~/.agentjobs/databases/local-tasks-5ac3195b10.db` appeared, timestamped 21:48:50,
  eighteen seconds before I checked. The silent empty answer survived the cutover
  and the side effect moved from the working directory to the user's home, where it
  is less visible.
- **New:** a read-only command creating a database under `~/.agentjobs/` for any
  directory it is run from. `agentjobs list` in three scratch directories leaves
  three orphan databases. ac-3 should be reworded to cover this.

### task-262 — the validator versus the product

- **Severity** P2. **Changed shape.** The framing ("red on its own corpus", "the
  hook would refuse every commit") is obsolete: `tasks/agentjobs` is empty, `git
  ls-files tasks` → 0, and `validate` now refuses to run without `--tasks-dir`. The
  mechanism is untouched and the numbers grew.
- **Evidence (run).** `agentjobs storage export <scratch> --project agentjobs` wrote
  410 files; `agentjobs validate --tasks-dir <scratch>` → `447 problem(s) across 410
  task file(s)`: 300 unknown-actor (`dispatcher` 113, `finisher` 83, `system` 76,
  `jeff` 10, `Codex` 10, `Claude` 6, `human` 2), 146 unknown-category, 0
  non-canonical (the export is canonical, so that class is closed). 272 of the 300
  are ids the product writes as (`actors.py:388-391` `RESERVED`; `manager.py:999`
  `actor or "system"`), and `validation.py:266` still checks against `load_actors`,
  which excludes them.
- **Consequence tonight:** the round trip `storage export` → `validate` → `storage
  import` can never be green on this project. Whether the importer refuses on
  taxonomy findings I did not check (question for the storage auditor).
- **Fix.** Treat `RESERVED` and `system` as known in `_check_taxonomy`; the category
  decision is still open (config declares six, the corpus uses at least eleven).

### task-263 — actor kind

- **Severity** P2. **Partly refuted by task-332; partly confirmed.**
- **What task-332 changed (read + run).** `capabilities.py:253-313`
  `actor_disagreement`: a `run` principal may not claim a human id or another agent;
  a human principal may not claim a different human but *may* claim an agent id, and
  the docstring says why (the CLI and MCP run as the person and attribute to the
  tool; refusing would break every local agent write). So ac-1 as written is now a
  decision the record has rejected, and the draft should say so rather than re-ask.
  Run: from loopback with no credential, `claim agent='Jeff Posey'` → 200 and
  `release actor='Jeff Posey'` → 200 (by that decision).
- **Still real (run):** `POST /log` as `dispatcher` → 200 and as `finisher` → 200 from
  loopback; the log then reads `['dispatcher', 'finisher', 'system']` with nothing
  from AgentJobs in it. `PATCH` with no actor → 200 (validated only when sent,
  `routes/tasks.py:493-495`); `DELETE` with no actor → 200, closed as `cancelled`,
  attributed `system`. Read: `cli.py:2963-2980` still falls back to `default_user`
  for `promote`/`queue move`/`reprioritize`.
- **Fix.** Refuse `RESERVED` ids from HTTP and MCP (they call the manager directly
  and never need the transport); give `DELETE` and deliverables an actor; drop the
  CLI fallback for agent-shaped commands. Rewrite ac-1 against task-332.

### task-264 — dispatch ledger races

- **Severity** P2. **Partly fixed.**
- **P2-4 (cancel vs poller).** Batch runs: fixed by task-390 — `_finish_batch`
  (`runner.py:3190`) defers when `cancel_requested` is set and the flag is written
  before the kill (`ledger.py:1302`, atomically per `write_status`). Session runs:
  `_finish_session` (`runner.py:2913`) still guards on `status` only, so the race the
  audit observed on `run_a6deb292` (a session) is unchanged in code. Evidence it has
  not recurred: of 213 run directories, exactly one has `cancel_requested: true`
  with `outcome: interrupted`, and it is that August run. Inferred from reading for
  the mechanism, run for the tally.
- **P2-9 (ceiling).** Still a scan-then-count with no primitive
  (`guards.py:914-924`); the run directory is created later. Inferred.
- **P2-5 (keying).** `run_lock_path` is `f"{task_id}.lock"` (`ledger.py:136`);
  `live_runs` compares `run.task_id == task.id` (`guards.py:916`);
  `newest_session_run` filters on `task_id` alone (`wake.py:153`). Inferred. No
  collision today (the other projects' ids carry slugs).
- `record_dispatch_result` (`manager.py:2794-2806`) refuses only a replayed
  `operation_id`, not a second terminal entry for the same `run_id`.
- **Fix.** Port the `cancel_requested` check to `_finish_session`; the rest as filed.

### task-265 — dispatch settlement

- **Severity** P2. **Confirms P2-6, P2-7, P2-11, P3-13; refutes P3-15.**
- **Evidence (read).** `_ball_moved` (`runner.py:2884-2891`) counts any `handoff` or
  `transition` newer than the dispatch entry, and both park paths write a `handoff`
  as `dispatcher` (`:2641`, `:2722`). `main.py:162` calls
  `poll_sessions_forever(default_home())` with no managers, so the poller falls to
  `dispatch_manager_for(project)` (`poller.py:174`), whose `webhook_manager` defaults
  to `None` (`store_factory.py:261-266`). `finished_at` is written at settle
  (`runner.py:2932`); no `idle_at` anywhere in `runner.py` or `run_report.py`.
  `poller.py:183-189` still skips a live run when dispatch is refused.
- **P3-15 fixed:** `_finish_session` calls `stop_session` when `reap` is true
  (`runner.py:2938`) and `main.py:84` now describes the startup reap as the
  leftover pass. The two sentences the audit called wrong are now right.
- **Fix** as filed for the four that stand.

### task-266 — `.mcp.json` pre-approval

- **Severity** P2 (unchanged). **Confirms task-266.**
- **Evidence (read).** `runner.py:457-490` reads the project's own `.mcp.json` and
  `settings_json` (`:492-517`) puts every name into `enabledMcpjsonServers`;
  `supervisor_allow_rules` (`:429-454`) adds `mcp__<name>` per server. `grep -n mcp
  src/agentjobs/dispatch/config.py` → nothing: `dispatch.yaml` has no per-project MCP
  allow-list to intersect with. ac-2 (what a `--bg` run does with the plugin's
  second server) has no decision entry.
- **Fix** as filed.

### task-267 — gate receipts, renames, bootstrap, wheel

- **Severity** P2. **Confirms all four**, with P2-1's blast radius reduced.
- **Evidence (read).** `check.py:805-808` reads `head_commit` and `tree_is_clean`
  inside `issue_receipt`, called at `:1021-1023` after the stages; nothing captures
  HEAD before the first stage. But `gate_scope.py:10` now says no diff carries a
  task record and the `tasks/ -> pytest` rule is gone (`:79`), so the specific
  scenario (a task-YAML commit landing mid-gate) cannot happen; a roadmap
  regeneration commit still can. `gate_scope.py:223` is `git diff --name-only
  <commit>` with no `--no-renames`; `:41,125` use `fnmatch`, not `fnmatchcase`.
  `bootstrap.py:124-125` still treats `imported_checkout(...) is None` as "leave it
  alone". `build_release.py:73-100` still `--no-deps` into a target prepended to
  `sys.executable`.
- **Fix** as filed; P2-1 could be downgraded to P3 on the record.

### task-271 — plugin drift

- **Severity** P2. **Partly fixed; drift confirmed.**
- **Evidence (run).** `~/.claude/plugins/installed_plugins.json`: version `0.1.0`,
  installed 2026-08-17T20:18Z from `c30ca8c`. `diff -rq plugins/agentjobs <cache>`:
  `.mcp.json`, `README.md`, `.codex-plugin/plugin.json`, `hooks/task_write_guard.py`,
  `skills/agentjobs/SKILL.md` differ. The repo `.mcp.json` (changed 2026-08-27) no
  longer carries `AGENTJOBS_URL` at all, which is fix item 3 and half of ac-2; the
  cache still carries the hand-edited `8876`. `plugin.json` version still `0.1.0`.
  Running the cache's guard on `head <task path> 2>/dev/null` → deny (the pre-`d2ef033`
  behaviour); the repo's guard → allow. `compat.py:62-77` still compares
  `compatibility_key` pairs; no `source_commit` read.
- **Fix** as filed. Item 1 (a startup line naming the cache's `gitCommitSha`
  against HEAD) is the cheap one and would have made tonight's diff unnecessary.

### task-272 — the schema migrator

- **Severity** was P2. **Obsolete for the product.**
- **Evidence.** `migrate_schema.py:784` and `:872` unchanged (read), so the defects
  are still in the code. But every registered project is SQLite (`storage status`,
  run), the CLI's `migrate-schema` is a file-era command, and
  `tests/test_migrate_schema.py:406-408` records that `TestTheRealCorpus` was removed
  by task-380 (ac-3 satisfied by deletion). The task's own 2026-09 log entry already
  says to carry the retry/dry-run requirement into the importer rather than fix this.
- **Recommendation.** Close as superseded, pointing at the importer requirement.
  Whether `sqlstore/importer.py` meets it is the storage auditor's question.

### task-274 — cross-site and cross-host requests

- **Severity** P2 (unchanged). **Confirms task-274, server side.**
- **Evidence (run, TestClient sandbox).** A client with `Host: evil.example` →
  `GET /api/version` 200. A `PATCH /tasks/{id}` with no `Content-Type`, a raw JSON
  body and `Origin: https://attacker.example` → 200, and the title read back
  changed. `main.py:258-274` is still `CORSMiddleware` only, with `allow_origins`
  naming 8765 and 5173. No `TrustedHostMiddleware`, no custom-header requirement.
  netstat: the live server binds `127.0.0.1:8876`, so the path is exactly the one
  described — a page in Jeff's own browser, or rebinding — not the tailnet.
- Not exercised from a browser. **Fix** as filed; both changes are still one-liners.

### task-275 — legacy Jinja pages

- **Severity** P2 (unchanged). **Confirms task-275.**
- **Evidence.** Live (run): `GET /p/agentjobs/tasks/task-244` → 200 and the body
  contains `marked.parse`/`x-markdown` 22 times. Read: `api/templates/base.html:30-32`
  loads Alpine and marked from `cdn.jsdelivr.net` with no `integrity`; `:47`
  `el.innerHTML = marked.parse(content)`. `api/main.py:365-366` still mounts
  `web_router` and `web_legacy_router`. `frontend/src/App.tsx:105` still links
  `/projects/new` and `components/Dashboard.tsx:288` links `/docs`, pinned by
  `InAppLinks.test.tsx:82`.
- The injection itself was not exercised (needs a browser and a task carrying a
  script tag; I would not write one to the live store).
- **Fix** as filed.

### task-276 — the write guard

- **Severity** P3 by rating. **Partly fixed; one class still real; one new class.**
- **Evidence (run, calling `evaluate()` directly with the registry's managed
  directories).** A `Write` to a scratch file whose *content* names a record path
  under the managed tasks directory → `deny` ("the Write tool would write AgentJobs
  task records") — ac-1 still fails; `task_write_guard.py:443-445` still extends
  targets from `content`. `grep -n foo <record> | sed -n 1,3p` → `allow` (the
  auditor-4 case is fixed). `head <record> 2>/dev/null` → `allow` in the repo copy,
  `deny` ("shell redirection would write") in the installed cache copy.
- **New:** `python -c "print(open('<record>').read())"` → `deny`, and `awk '{print}'
  <record>` → `deny`, from *both* copies — the `INTERPRETERS` rule at `:125,480`
  treats any interpreter naming a managed path as a write. This one refused my own
  first attempt to run the guard script against a payload naming a record, which is
  how I noticed it. A guard that refuses `python` reads trains exactly the
  route-around habit the task describes.
- **Fix.** As filed for the content regex; add: an interpreter invocation is a write
  only when the path appears as a write-mode `open()` argument or after a redirect,
  and reads are allowed.

---

## Closed tasks: did the fix hold?

**task-244 (front door identity).** Holds for every mutation I tried and has a
seam on reads. A TestClient with client address `100.90.111.6` and no headers: `GET
/api/projects` → 200 (full project list with roots), `GET` of a task → 200, `POST
/log` → 403 `no_proven_identity`, `POST /dispatch` → 403, `POST /api/projects` → 403.
A loopback client with forged `X-Tailscale-User` and `X-Forwarded-For` and no
front-door secret → 403. The server binds loopback (`127.0.0.1:8876`, netstat), so
the only way a non-loopback address reaches it is the proxy, which I did not
exercise. Question for the security auditor: does the proxy's allow-list scope GETs,
or does every tailnet peer still read every project — the audit's original
exposure had a read half and the code path I tested still answers reads to anyone.

**task-273 (SQLite storage).** Holds. `agentjobs storage status`: every project is
rows with `files=0`, imported 2026-09-07/08. `grep -rn "TaskStorage(" src` finds only
two docstrings. `SqlTaskStore.mutate_task` (`store.py:512-533`) is one `BEGIN
IMMEDIATE` transaction per read-decide-write, and the reopen race above shows the
unique index doing what its comment says. The one regression surface is queue
repair (see task-258).

**task-249 (run identity to `--bg` workers).** Holds. `session_env: delivered` on
95 run records under `~/.agentjobs/runs` (the field did not exist before the fix);
`tests/test_dispatch_session_env.py` → 21 passed, 1 skipped. The mechanism is the
`--settings` `env` block in argv (`session_env.py:125-146`), which the daemon does
deliver.

**task-248 (tolerant reader).** Holds. A ready record with `ball_reason:
made_up_reason` is refused by `Task.model_validate` and accepted inside
`tolerant_enum_values()`.

---

## New findings not on any record

- **N1 (P2, blocks task-245).** ac-1 contradicts `tests/test_dispatch_runner.py:324-343`
  and the task-222 decision it cites. Needs a decision entry before promotion.
- **N2 (P3).** A read-only CLI command in an unregistered directory creates
  `~/.agentjobs/databases/local-tasks-<hash>.db`. Verified 21:48:50 CDT.
- **N3 (P3).** The write guard's `INTERPRETERS` rule refuses read-only `python`/`awk`
  invocations that name a managed path, in both the repo and installed copies.
- **N4 (P3).** The reopen/reband race now ends in an unmapped `sqlite3.IntegrityError`
  (a 500 over HTTP) rather than a corrupt queue. Successor to task-253.
- **N5 (decision).** `00-BIG-DAWG-REPORT.md` is on `origin/main` (`db55d7d7`), which
  task-246's own description says it is not.
- **N6 (question).** `manager.repair_queue` reads raw files and `SqlTaskStore.tasks_dir`
  raises; `queue repair` may be unreachable on every project. Not run.
- **N7 (P3).** `storage export` → `validate` is red by construction: 272 findings are
  the product's own reserved and system actors. Successor to task-262.

## What I did not get to

- Nothing was exercised in a browser: the PWA cache/update behaviour (260), the
  no-cors POST from a foreign page (274), and the stored XSS (275) are all verified
  server-side or from source only, as they were in August.
- MCP surfaces for 255 and 263 (only REST was driven); `task_update_content`'s
  `replayed: False` is cited from source.
- Two-project collision tests for 264 P2-5; the ceiling race for 264 P2-9.
- `queue repair` on a SQLite sandbox (258/N6) — I did not want to guess whether it
  would touch the registry.
- The tailnet proxy path with the real front-door secret (244); only the loopback and
  spoofed shapes were driven.
- task-247 (ready, not draft): one grep, no reproduction of the write window.
- Closed tasks 251, 252, 268, 269, 270: 252's headline sentence was confirmed fixed
  (`agent-dispatch-design.md:21`); the rest were not re-read.
- `finish.py` and the importer (`sqlstore/importer.py`) were not opened.
- ac-2 of task-253 (replayed priority patch does no rebalance) was not tested.

## Questions for other auditors

- **Queue:** does `agentjobs queue repair` run at all on a SQLite project, given
  `repair_queue`'s raw-file read and `tasks_dir` raising? If not, is anything left
  that can repair a queue, and is anything left that can break one?
- **Storage/importer:** does `storage import` refuse on the taxonomy findings that
  `validate` reports on every export (N7)? Does the importer meet the dry-run and
  repeatability requirement task-272's log moved onto it?
- **Security:** with the server on loopback and mutations gated, do tailnet peers
  still read every project through the proxy? The 244 fix as tested answers reads
  to any address.
- **Dispatch:** is the session-mode cancel race (`_finish_session` checking `status`
  only) reachable today, or does `claude stop` always land before the poller's
  settle? One incident in 213 runs, and it predates task-390.
- **API:** what does the live server return when `update_task` raises
  `sqlite3.IntegrityError` — a bare 500, or does some handler I did not find catch it?
- **Docs:** `agent-dispatch-design.md:728-736` (the merge pre-approval rationale) —
  does it still say the merge is gated on a recorded approval, now that
  `finish --posture-release` is the sanctioned path and `git merge` stays
  pre-approved for every posture?
- **Frontend:** is there any path by which an installed PWA on a tablet ever asks
  for a new service worker after load, given no `.update()` call and no
  `Cache-Control` on the shell?
