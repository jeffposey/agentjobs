# Big Dawg Audit II — combined report

Night of 2026-09-11, finished the afternoon of 2026-09-12. Fourteen auditors plus this
synthesis session, all on `claude-fable-5-1`, read-only in the main clone. Auditors 01–09
and 11 read `main` at `096f33ae`; auditors 10, 12 and 13 read `bb12f33f` (task-414's
design document had merged between batches; no auditor read it). Runbook:
`audits/2026-09-11/PLAN.md`. Durable record: task-410. The fifteenth input is task-410's
own log, entries 16 through 34, where the supervisor recorded dispatch defects it met
while running this audit.

Every finding below carries the auditor's own label: **verified by running** or
**inferred from reading**, and **New**, **Confirms task-NNN** or **Refutes task-NNN**.
Where two auditors disagreed, section 4 says who was right and how that was settled.

---

## 1. Executive summary

Five things, in order.

1. **The first audit did not land, and its findings are almost all still true.** Of the
   twenty-three findings filed as drafts on 2026-08-22, seventeen are still real on
   today's code, three changed shape, one was fixed by accident, and two are obsolete.
   Not one has been promoted since 2026-08-23. They are fully specified (auditor 14
   measured it); what is missing is a promotion decision nobody was asked to make. The
   closure list in section 2 is that decision, item by item. **Act on it first.** The
   three P1s among them are a demonstrated, tailnet-reachable stored XSS (task-275), a
   webhook route that 500s on every call (task-250), and a run-scoping rule the
   allow-list quietly undoes (task-245).

2. **The storage cutover was sound at the core and leaky at every edge.** WAL,
   `synchronous=FULL`, one transaction per verb, and the CHECK constraints held under six
   concurrent processes and a hard kill; the migration arrived whole; backups restore.
   But the one verb that rewrites history, `agentjobs redact`, records a redaction of a
   log body that the row never receives (**filed as task-425**); every review sandbox has
   served an empty project since 2026-09-10 (**task-427**); the CLI invents a private
   shadow database whenever it cannot resolve a project, including in every worktree;
   the "server is running" guard checks a port nothing listens on; and the storage
   design document describes a webhook outbox nothing writes.

3. **The run boundary is real on HTTP and absent everywhere else.** Task-332's scoping
   holds for a credentialed run over HTTP: verified by running, every review, dispatch
   and cross-task verb is refused. But the shell has the whole unprincipaled CLI; the
   plugin's write guard protects a directory that no longer holds anything while
   `sqlite3 delete` and `curl -X POST` pass; `git merge` and `git add` are pre-approved
   for every posture, and a test asserts they stay that way; a session a run spawns
   inherits the run id but not the credential, so it is the owner (confirmed on this
   session); and over MCP every one of the eight refusal codes reaches the agent as
   `internal_error` (**task-426**). A run can also PATCH `posture: autonomous` onto its
   own task and have the next cold dispatch honour it.

4. **Dispatch loses runs at the seams, and this audit lost three of its own.** A run
   finishing itself is killed at its shell tool's ten-minute ceiling mid-delivery, and
   the retry re-gates a branch already in `main` and records the merge twice; two
   finishes can hold the merge runway after one stale reclaim; a restarted epic walk
   grounds on its own live children; the resume path re-resolved a Fable-only audit's
   supervisor onto Opus; a five-hour usage-window stop is indistinguishable from a
   session that finished, and it stalled batches 3 and 5 and the supervisor. Every one
   of these is an instance of task-414's class, and this report folds them there rather
   than filing them again.

5. **The documentation the agents load first is the documentation that drifted most.**
   ENGINEERING.md's `--since-gate` safety properties describe a classification row and a
   test that no longer exist; "ten stages" is eleven in four files; two always-loaded
   files still branch on a file backend that was deleted; the MCP leading rule (the 512
   characters every client is guaranteed to show) describes task YAML in git and two
   tests pin the stale sentence; `docs/storage-sqlite.md` §6 records task-255's fix and
   task-047's durability guarantee as shipped when neither is. The bundle has 404 bytes
   of headroom and the ablation harness that is supposed to gate cuts has never been run
   on the model that now runs everything.

**Filed:** three P1 drafts, task-425, task-426, task-427, each specced to be claimable.
**Deliberately not filed:** every other P1 confirms an open record (275, 250, 245, 257,
262, 406, 407, 418, 322/414, 129) and is a closure-list disposition instead. Every P2
and below stays here.

---

## 2. Did the first audit land — the closure list

Auditor 01 re-examined each still-draft finding on today's code (`01-regression-sweep.md`),
and specialists 02–14 confirmed or refuted most of them a second time. This table
merges those verdicts with a recommended disposition. Task-264 was in the runbook's
twenty-three; it was promoted to `ready` and re-parented under task-414 on 2026-09-12
while the audit ran, so twenty-two are still drafts.

Dispositions: **Promote** (spec is accurate, claim it as written), **Promote, re-scope**
(spec needs the named edit first), **Fold** (close, carrying the live residue into the
named record), **Close** (superseded or obsolete), **Decide** (a human decision, not a
code change).

| Task | Finding (as filed) | Verdict tonight | Who confirmed | Disposition |
|---|---|---|---|---|
| 245 | `git merge`/`git add`/`npm run` pre-approved; wake stub frames the prompt as the human's words; draft and human-ball tasks dispatchable | **Still real**, all three | 01 (read), 04 F11.4 (run) | **Promote, re-scope.** ac-1 reverses the task-222 decision that `tests/test_dispatch_runner.py:324-325` asserts; add a decision entry that narrows it (drop the pre-approval where `finish.enabled` is on, since `finish --posture-release` is now the sanctioned merge path). P1. |
| 246 | audits and machine identifiers on the public remote | **Still open, wider**: the 2026-08-21 synthesis report is on `origin/main` (`db55d7d7`), contrary to the record; 126 store records carry the user-profile path; `docs/tailnet-front-door.md` names hostnames and the VIP | 01 (run), 03 §12 | **Decide** before the supervisor merges `audits/2026-09-11/`: ignore rule or not. ac-2 (no absolute paths in dispatch records) is not done. |
| 250 | `POST /webhooks/{id}/test` always 500s; no route tests | **Still real** | 01 (run), 09 F2 (run) | **Promote** as written. P1. |
| 253 | Reopen/reband position computed under the lock, written outside it | **Changed shape**: race survives; outcome is an unmapped `IntegrityError` (HTTP 500), not corruption | 01 (run), 08 F2 (run, real processes) | **Promote, re-scope**: drop the corruption half, keep the transaction fix, add "map `IntegrityError` to 409 retryable". P3. |
| 254 | `update_task` has no allowlist; reopen writes no transition | **Still real**; `log=[]` now desynchronises `log_count` instead of erasing (see §4) | 01, 07 F5, 08 F3 (all run) | **Promote** as written; add the `log_count` invariant test and 08's forgery finding (a `handoff`-typed entry that moves nothing is accepted). P2. |
| 255 | Webhooks re-fire on replay; replay returns the current task; no `replayed` on create | **Still real**, all three; plus cross-task reuse of one id performs a second write | 01, 08 F1, 09 F4, 10 F4 (all run) | **Promote** as written. Also fix `docs/storage-sqlite.md` §6, which records this as done. P2. |
| 256 | Three error envelopes; OpenAPI declares 422 everywhere | **Still real**; a fourth shape (`Forbidden`) added on purpose; PATCH revision conflicts now structured | 01, 09 F6 (run) | **Promote** as written. P2. |
| 257 | Webhook secrets returned; SSRF targets accepted; no delivery id | **Still real**, and a *run* can read the secret on the ungated list/get routes | 01, 03 F3, 09 F1/F3 (run) | **Promote, re-scope**: add "gate `list_webhooks`/`get_webhook` under `WEBHOOK_ADMIN`" and `min_length=1` on the secret. P1 by principle, latent (no live subscriptions). |
| 258 | Unpositioned projects; silent "nothing claimable"; repair has no dry run | **Fixed incidentally** by the SQLite cutover; the corruption it repairs is unrepresentable | 01 (run), 08 F6 (run) | **Close** as superseded. `repair_queue` still runs on SQLite (see §4) but has nothing to do; retire or reduce it in the same change as the §8 doc rewrite. |
| 259 | Ball/reason untied from lifecycle; held task claimable; approve on ready | **Still real** | 01, 07 F6, 08 F4, 09 F7 (all run) | **Promote** as written; cheaper now because `mutate_task` re-validates the aggregate, so a model rule covers every path, but rule 7 also means a DDL CHECK and therefore a table-rebuild migration (07 F4). P2. |
| 260 | PWA staleness: no cache-control, no SW update, gate empties the live bundle, `randomUUID` | **Still real**, now demonstrated on a device-shaped experiment; six `randomUUID` sites not four; the doc sentence was fixed | 01, 05 F13, 11 F3/F4/F5 | **Promote, re-scope**: add 11's `stale_probe.mjs` as the Playwright spec and "defer the `controllerchange` reload behind the dirty-form check". P2. |
| 261 | CLI identifies the server by port; `restart` binds 8765; read-only commands create side effects; `gui.port` ignored | **Partly fixed; changed shape**: no `tasks/` is created, but a shadow database appears under `~/.agentjobs/databases/`; the storage operator guard checks 8765 on a machine serving 8876; `init --port` is recorded and ignored | 01, 02 F2/F3, 10 F5, 12 F2 | **Promote, re-scope**: reword ac-3 to the shadow database; add "operator commands read the port dispatch reads, or detect the file holder"; add `init --port`. P2. |
| 262 | `validate` red on its own corpus: 221 problems | **Changed shape**: the corpus left the repo, `validate` refuses without `--tasks-dir`, and an export of the live store is red 453 times, 272 of them the product's own reserved actors | 01, 07 F2, 14 F12 (all run) | **Decide, then re-scope**: what are `actors:` and `categories:` in config for? If a vocabulary, treat `RESERVED` and `system` as known and grow a store-reading mode; if not, delete them. Three auditors rated this P1, P2 and P3; this report says P2 (nothing runs it today, so nothing is blocked). |
| 263 | Agents can act as `default_user`; reserved ids accepted; DELETE unattributed | **Partly refuted** by task-332 (a run may not claim a human id); the rest real: `dispatcher`/`finisher` accepted from any caller, DELETE closes as `system`, CLI falls back to `default_user`, MCP `task_claim` accepts the human | 01, 08 F5, 09 F8, 10 F3 | **Promote, re-scope**: strike what task-332 fixed; rewrite ac-1 against task-332's decision; add 10's "anyone can `task_release` anyone's claim". P2. |
| 264 | Cancel-vs-poller races; ceiling is a directory scan; locks keyed by bare id | **Partly fixed** (batch path, task-390); session path unchanged; lost `meta.yaml` updates demonstrated (49 of 50) | 01, 04 F10.1/F10.2 (run) | Already **ready**, critical, under task-414. Add 04's F10.1 numbers and the `_finish_session` port as evidence. |
| 265 | `_ball_moved` counts dispatcher handoffs; poller fires no webhooks; `finished_at` is settlement time | **Still real** ×3; P3-15 (reap) fixed | 01, 04 F10 (read) | **Promote, re-scope**: strike P3-15. The poller's manager is built with `webhook_manager=None`, so parked/stalled/finished-without-handoff fire nothing; that is the notification receiver's precondition. P2. |
| 266 | `.mcp.json` pre-approves a repository's own MCP servers | **Still real** | 01, 04 F11.7 (read) | **Promote** as written, with 10 F7's question: retiring the root `.mcp.json` (task-319) may take the tools away from dispatched runs. P3. |
| 267 | Receipt reads HEAD after the run; rename source unseen; bootstrap re-hijacks; wheel unproven | **Still real** ×4, three demonstrated by running; receipts are unsigned and one link deep; the wheel itself is sound today | 01, 05 F1/F2/F3/F4/F5/F12 | **Promote** as written; downgrade P2-1 to P3 on the record (the task-record scenario is gone). Auditor 05 calls the unspecced state of this task the finding above all others in its system. |
| 271 | Installed plugin is a hand-edited 2026-08-17 snapshot; version 0.1.0 forever | **Partly fixed** (task-317 removed the port pin); drift and vacuous version check real; the installed guard is the pre-fix copy | 01, 04 F11.6, 10 F6 (run) | **Promote, re-scope**: item 3 is done. Immediate one-liner: reinstall the plugin from the directory marketplace. P3. |
| 272 | `migrate-schema` re-run duplicates positions; writes non-canonical bytes | **Obsolete for the product**; the code is unchanged and an import of such a corpus now quarantines one record and reports success | 01, 07 F7 (run) | **Close** as superseded, carrying "`storage import` exits non-zero when a record is quarantined for a *constraint*" onto the importer. |
| 274 | No Host/Origin check; typeless body parses as JSON; body-less POSTs mutate | **Still real**, and the browser half is now exercised: a page on another loopback origin wrote a log entry as the owner | 01, 03 F2, 09 F9 (run) | **Promote** as written; fix is still two lines. P2. |
| 275 | Legacy Jinja pages: `marked.parse` into `innerHTML`, CDN scripts without SRI, still mounted | **Still real, and demonstrated executing**: one API call, script ran as whoever opened the page; three CDN scripts not two; still linked from the SPA | 01, 11 F1 (run, Playwright) | **Promote** tonight. P1. Cheapest mitigation is not mounting the two routers. |
| 276 | Write guard refuses reads and unrelated writes that mention a task path | **Partly fixed** (`grep|sed` allowed); `Write` with the path in content still denied; new class: `python`/`awk` read-only one-liners denied; heredoc bodies scanned; `Edit` allowed where `Write` is denied | 01, 04 F11.2, 14 F16 (run) | **Promote, re-scope** with the interpreter rule ("a write only when the path is a write-mode `open()` argument or after a redirect"). This audit's runbook works around it; a draft is not a fix. P3. |

**Closed tasks spot-checked** (auditor 01): task-244 (front door identity) holds for
every mutation, with reads still open to any socket peer; task-273 (SQLite) holds;
task-249 (run identity to `--bg` workers) holds; task-248 (tolerant reader) holds.

**Confirmations of other open records** carried into this list rather than refiled:
task-129 (04 F11.1: the guard now protects a retired, empty directory; re-scope it to
"the guard protects nothing on a database project"), task-197 (04 F10.6 and task-410
entry 17: the poller and the resume path both resolve the project default rather than
the run's group), task-355 (04 F7: a live task is in that state), task-389 (04 F10.5:
ten of 213 runs), task-395 (02 F4 answers it: a restic job exists, scrapes CLI prose, and
exited 1 on 2026-09-11), task-396 (05 F11, 14 F11: it now prints a table of zeros and
exits 0), task-406 (03 F8, 06 F1, 14 F2: widen to `roadmap` and flesh-out's duplicate
fold), task-407 (04 F4, 05 F14, 06 F6, 14 F14), task-408 (05 F10), task-411 (07 F10,
14 F9: 143 dangling pointers, not eighteen), task-413 (12 F9, 14 F14), task-418 (04 F9.1),
task-319 and task-323 (13 F3, F6), task-325/348/370 (04 F5: task-370 failed a finish
gate on the day task-390 shipped), task-053/task-063 (10 F8, 14 F3: the owner answered
"close it" on 2026-09-06 and nobody executed the answer), task-018 (14 F8).

**Refutations of records or documents:** task-247 does not apply to the new store
(02 F10, verified: a kill mid-transaction leaves nothing); task-301's "only one MCP
registration survives" is false today (10 F7, 13 F3, and this session's own context);
task-380's decision entry says `corpus_stats.py` refuses, and it does not (14 F11);
task-406's "flesh-out is unaffected" is true of the plain shape only (06 F1);
`docs/storage-sqlite.md` §6 and `docs/context-budget.md` §2 are refuted by running.

---

## 3. Ranked findings

Every P1 and P2 across the fourteen files and the supervisor's log, clustered, ranked by
blast radius. A cluster's marking is the strongest of its members. Section 2 already
holds the dispositions for confirmations; they appear here only so the ranking is
complete.

### P1

**R1. `agentjobs redact` on a log body records a redaction the store never performs.**
New. Verified by running, twice independently (02 F1, 07 F1). The verb mutates the
in-memory entry and persists through `_mutate`; `SqlTaskStore._replace_log` only inserts
entry ids it has not seen and never updates. Title and spec redactions work. The record
then says the words are gone; `quotations` still finds them; the repository is public.
The test asserts on the returned object and never reloads. **Filed: task-425.**

**R2. The legacy Jinja page executes script stored in a task record.** Confirms
task-275. Verified by running (11 F1): one `POST /tasks` with an `onerror` payload,
Playwright loads `/p/<project>/tasks/<id>`, `document.title` becomes `XSS-FIRED-…`. Still
mounted at `main.py:365-366`, linked from three SPA anchors, three CDN scripts with no
SRI. Reachable by anything that can write a task, executed as whoever opens the page.
Disposition in section 2: promote tonight.

**R3. Every review sandbox serves an empty project, and the CLI invents shadow
databases.** New (09 F10, 11 F2; 02 F2 confirms task-261). Verified by running on four
sandboxes. `sandbox_store()` is called before the project is registered, falls back to
`LOCAL_PROJECT_ID` and a directory-hashed file; the server opens `<project_id>.db`,
empty. The same `_local` fallback in `cli._build_manager` wrote five shadow databases
into the live `~/.agentjobs/databases/` during this audit, one of them from inside the
task-392 worktree, and fifteen throwaway tasks from auditors' sandboxes that did not set
`AGENTJOBS_HOME`. `storage status` lists none of them; the nightly backup snapshots none
of them. Every UI review "in the sandbox" since 2026-09-10 showed nothing. **Filed:
task-427** (sandboxes); the CLI half goes on task-261.

**R4. The run boundary holds on HTTP and nowhere else.** Mixed markings; all verified
by running unless noted.
- The write guard protects the retired record directory; `sqlite3 <db> 'delete from
  task'`, a Python one-liner and `curl -X POST …/log` are all allowed (04 F11.1; extends
  task-129).
- `ALLOW_PREFIXES` still pre-approves `git add`, `git commit`, `git merge`, `npm run` for
  every posture, `tests/test_dispatch_runner.py:324-325` asserts it, and
  `.claude/settings.local.json` re-grants `git merge` and `python -c '*'` to every
  session in the shared clone, dispatched runs included (04 F11.4, F11.5; confirms
  task-245).
- The CLI drives the manager directly with no principal: `dispatch`, `finish`, `queue
  repair`, `project register`, `redact`, `playbook run --actor <any human>` are one
  shell away for any run with a shell (03 F4, 06 F8; inferred, with the absence of any
  credential check in `cli.py` verified).
- A session a run spawns inherits `AGENTJOBS_RUN_ID` and `RUN_DIR` but not
  `AGENTJOBS_RUN_CREDENTIAL`, so it is `owner via loopback` with every capability, and
  `session_identity.py` also refuses to register it as interactive, so nothing watches it
  (03 F5). **Confirmed on this synthesis session**: its environment carries
  `run_991db1bf`'s id and directory, a run that has been terminal since 22:58 Central on
  2026-09-11, and no credential.
- `0600` is decorative on Windows; `CodexSandboxUsers` holds Modify on the front-door
  secret and every run's `session-settings.json`, which keeps the plaintext token after
  the digest is revoked (03 F9).
- Guard false negatives need no obfuscation: `f=<path>; echo x > $f`, `python.exe -c`,
  `poetry run python -c`, PowerShell `[IO.File]::WriteAllText`, `git apply`, `Write` to
  `<path>.tmp` (04 F11.3).
Disposition: promote task-245 (re-scoped) and task-129 (re-scoped); add a fifth limit to
`docs/authorization.md` and one sentence to ALLAGENTS.md saying the table binds HTTP and
MCP only; when `AGENTJOBS_RUN_CREDENTIAL` is in the CLI's environment, adopt the run
principal; decide what a spawned child *is*.

**R5. Every authorization refusal reaches an MCP agent as `internal_error`.** New (10
F1). Verified by running the classifier over all eight codes and end to end. `ErrorCode`
has no member for any principal or authorization code, the `except ValueError` branch is
marked no-cover and rewrites to `internal_error`, and an answered 5xx with no body maps
to `invalid_transition`, non-retryable. ALLAGENTS.md tells the run to branch on a code
it cannot see. **Filed: task-426.**

**R6. A run that finishes itself is killed at its shell tool's ten-minute ceiling,
mid-delivery.** New cause; confirms task-322 and task-414 (04 F1). Verified by ledger
scan: 10 of 145 finish records are `running` with no `finished_at`; two died after the
merge (`fin_d11a8f5e` at 9m58s, `fin_2f382546` with `phases.jsonl` ending after
`restart ok`); the run's own transcript says its shell call hit the ceiling. The retry
re-ran the full gate for eight minutes on a branch already in `main` and wrote a second
"Merged as `e26a460b`" entry. `finish_status` shows the dead finish as `running` until
the run ends. Fix: `--posture-release` should spawn the finish detached and return, the
way Approve does, and the run should wait on the record; until then `AUTOMATIC_CLAUSE`
must say to background it. Disposition: name this cause and the interim mitigation on
task-322; do not file (the class is task-414's, on the owner's instruction).

**R7. Three of four playbooks are refused at their first real write.** Confirms task-406,
widened (06 F1; refusal verified by 03 F8 and 14 F2). Since task-332 a run holds its
verbs only against its own task; groom closes others, reorder moves others, roadmap
PATCHes others, and flesh-out's duplicate fold hands off the counterpart. The design's
"audited, not enforced" position rests on "no per-run identity exists"; one does, and it
is narrower than every playbook contract. No test dispatches a playbook and performs its
first write. The September groom and reorder proposals sit on a closed task (task-392
entries 25–26) with three closures and one move nobody has executed.

**R8. `ROADMAP.md` is stale on `main` right now, so every finish gate is red.**
Confirms task-407 (04 F4, 05 F14, 06 F6, 14 F14, all verified by running
`export_roadmap.py --check`). Any task filed anywhere turns the stage red for a branch
that never touched it; the finish then escalates `gate_failed` and dispatches a session
to fix nothing. Tonight: regenerate and commit before approving anything, including this
audit's own branch.

**R9. `agentjobs validate` cannot read the corpus and is red by construction on an
export of it.** Confirms task-262, changed shape (07 F2, 01, 14 F12). 453 problems on
416 records, 272 of them the product's own `dispatcher`/`finisher`/`system` actors; 24
categories in use against six configured; `validate` refuses without `--tasks-dir`.
Separately (07 F3, new): the CLI's claim that "a record that would fail validation
cannot be a row" is false for six of nine rules — a dangling `needs`, a path outside the
project, an empty actor, an unknown category all land as rows.

**R10. Webhooks: the test route 500s, and a run can read every HMAC secret.** Confirms
task-250 and task-257 (09 F1/F2/F3, 03 F3, 01; all verified by running). `list_webhooks`
and `get_webhook` are not in `ROUTE_CAPABILITIES`, `Webhook.secret` is a plain response
field, so the principal refused the write holds the key that forges deliveries. Empty
secret accepted; unknown events accepted; link-local targets accepted; no delivery id.
Latent until somebody registers a subscription.

**R11. A restarted epic walk reports live children as a deadlock.** Confirms task-418
(04 F9.1, verified by probe). The frontier is built from claimable tasks, in-flight
children live only in memory, so a walk restarted while a child works returns
`no_eligible_child` and hands the parent to `human/decision` while the child runs on.

### P2

**R12. A run can raise its own task's posture to `autonomous` and a later cold dispatch
honours it.** New (03 F1); revisits the task-308 decision, which the private
`dispatch.yaml` comment acknowledges as "prevented only by this line". Verified: the
PATCH is accepted under `TASK_EDIT`, resolution order is dispatch > inherited > task >
project, ceiling is `autonomous`, `auto_dispatch` fires on request-changes. **Narrowed
by this synthesis (section 4):** a handback whose previous session still exists is a
wake, and a wake reuses the old argv including its posture flags, so the escalation
fires only when the old session is gone and `dispatch_task` cold-starts. Still a real
hole; refuse `posture` (and `parent`) on a PATCH from a run principal.

**R13. The dispatch ledger and poller lose state at every seam.** Confirms task-264,
task-389, task-197; several new (04 F2, F10.1–F10.5, F10.7; task-410 entries 17–19, 27,
33, 34). Verified by running unless noted: two `update_meta` writers lose 49 of 50 and
197 of 200 updates; cancel and the poller both write terminal results on the session
path; `read_run` treats a refused read as a live run; a run parked once is never
un-parked (inferred); ten of 213 runs lack `dispatch_entry_id`; a stale runway lock is
reclaimed by unconditional delete so two finishes can hold the runway (scripted
interleaving); a session can register against two tasks. From the supervisor: the
handback-resume path re-resolved the runner from the project default (a Fable-only
audit's supervisor ran on Opus); the selection record reported a disabled runner as
`eligible: true`; a dispatched run can file a task but not queue-move it; `dispatch run`
refused a chat-authorised start as `not_human_clocked`; a five-hour usage-window stop
reads `idle` and was settled `finished_without_handoff` four times in one night; `run
register` refused an interactive session under the project root as `session_unknown`
while `claude agents --json` listed it. Disposition: all of it is evidence on task-414
and its children; none is filed again.

**R14. The finish's other stops.** New and confirms task-355 (04 F3, F7, F9.2–F9.4; 03
question). An escalation after the restart is written through the server it just
restarted, unguarded, so a server that stays down leaves `main` moved and the ball on
nobody (inferred, not reproduced). Approve on a task with no recorded branch does
nothing and tells nobody; task-413 is in that state tonight. The epic walk's bounded
retry is effectively unreachable (`DIED` needs `ball is AGENT`, every settle path parks
to `human`); stopping does not spend the authorisation; a `DispatchRunError` escapes the
walk uncaught with the attempt already spent.

**R15. The `operation_id` contract is false, and the storage document says it was
fixed.** Confirms task-255; new for the document (08 F1, 09 F4, 10 F4, 02 F7). Verified:
one handoff, one replay, two signed `task.handoff` deliveries; a replayed claim reports
success on a task another agent now owns; the same id on a different task performs a
second write. `webhook_outbox` exists in the schema with a comment citing task-255's fix,
and nothing in `src/` writes it: 0 rows after 2,124 live operations.
`docs/storage-sqlite.md` §6 and `connection.py` describe the outbox as shipped. Delivery
also runs inside the mutating request: each subscribed receiver adds ~215 ms to every
handoff, dead or slow (09 F5).

**R16. Side doors into the record.** Confirms task-254; the forgery half is new (08 F3,
07 F5). Verified: `update_task` moves every axis with zero log entries;
`add_log_entry(type="handoff")` with `data={"ball": "human"}` lands while the axes stay
`agent/available`, and `dispatch/handback.py` reads the newest human `HANDOFF` entry to
decide whether feedback is waiting; `queue_anchor: strong` is writable on any move,
which the reorder playbook then treats as untouchable. `log=[]` no longer erases the log
(the store refuses to delete) but writes `log_count = 0` beside the surviving rows.

**R17. Ball, reason and lifecycle are still untied.** Confirms task-259 (07 F6, 08 F4,
09 F7). Verified: a `ready` task handed to `human/decision` is offered by `next` and
claimable, and the claim overwrites the pending ask with `WORK_PROMPT`; `/approve` on a
never-claimed ready task succeeds and fires a webhook; `manager.py` contains no reference
to `HOLD`. Two live `ready` tasks sit at `agent/answer` tonight.

**R18. Actor kind is unenforced; anyone can release anyone.** Confirms task-263 in part
(08 F5, 09 F8, 10 F3). Verified: `dispatcher`/`finisher` accepted from any caller;
`DELETE` closes as `system`; deliverables write no log entry; the Python client's
default author is `system`, which every project with an `actors:` block refuses; MCP
`task_claim` accepts the human `default_user`; `task_release` never compares the actor
with the owner, so a mistyped id releases a peer's in-flight work and the peer's next
write goes through.

**R19. Cross-site writes to the loopback API as the owner.** Confirms task-274 (03 F2,
09 F9). Verified from a real Chrome page on a second loopback origin: `no-cors` POSTs
land, the log entry is written, every write resolves as `owner via loopback`. The one
control not in the path was Chrome's public-to-private-address policy, which DNS
rebinding sidesteps and the `Host: evil.example` result shows the server would accept.

**R20. The PWA serves the stale shell, precaches it, and keeps it offline.** Confirms
task-260 (11 F3, F4). Verified by a six-step Playwright experiment: a launch after a
rebuild gets the old shell with zero server hits; the new worker bakes the old shell in
as its offline page; the `controllerchange` reload fires under a dirty form.
`VersionSkew` (2026-09-09) is the one improvement: an open tab learns within a minute.

**R21. Storage's edges.** New (02 F3, F4, F5, F6). Verified: the operator-command guard
checks 8765 on a machine serving 8876, so `import --replace`, `split` and `restore` run
against the live file (on Windows, a raw `WinError 32` traceback after verification; on
POSIX, two authoritative files); the nightly restic job scrapes `storage status` prose,
broke on 2026-09-11 when task-402 changed the output, and aborts the whole machine's
backup when it fails; `storage status` opens the live database with a write connection
and would apply a pending migration from a short-lived process; every open takes the
write lock, so a reader fails to open while an import holds a transaction.

**R22. Gate receipts attest to trees the gate never ran.** Confirms task-267 (05 F1, F2,
F3). Verified: HEAD moving mid-gate yields a receipt for the new commit; `--since-gate`
sees only a rename's destination, so a Python file moved into `docs/` skips Black, Ruff,
MyPy, build and Playwright; bootstrap installs into an activated venv that cannot import
`agentjobs`. Plus: runs still launch three to seven gates each after task-339's "one
gate per handoff" (05 F14, `run_report.py --since 4`: 3.6 per run).

**R23. The enum vocabularies now exist in three copies and nothing pins the SQL one.**
New (07 F4; inferred, premise checked). Every `IN (...)` list agrees tonight; the next
widening fails as an `IntegrityError` on the service for every process, and SQLite cannot
`ALTER` a CHECK, so it is a table-rebuild migration `migrations.py` has no example of.

**R24. Playbooks: gates are prose, verbs are a spelling check, and no run is
distinguishable as a playbook run.** New (06 F2, F3, F4, F5). Verified: 0 of 195
`task_run` rows carry `playbook`; a groom re-dispatched from the task page after its gate
loses the pointer and the hash; the roadmap leak scan misses a parent's title that the
relations line publishes (an email address rendered without error).

**R25. The corpus is sound as a database and unsound as a process.** New (14 F1–F4, F9,
F10). Verified by query: 22 audit drafts carry the placeholder prompt at `human/spec`
and the check that would flag it fires only at `agent/work`; 45 completed tasks closed
with criteria still `pending`, 27 with every criterion pending, 17 of them closed by the
scripted finish; 9 of 20 sampled records pass the Resumption Contract, 5 would mislead a
zero-context session (task-015 would rebuild a clamp that shipped); 143 dangling context
pointers, 55 on open tasks (task-411 says eighteen); `first_claimed_at` is NULL on 195
claimed closed tasks, so task-372's cycle-time analytics would compute over 5% of the
corpus.

**R26. The always-loaded bundle and the MCP leading rule describe a product that no
longer exists.** New; confirms task-319, task-323 (05 F6, 12 F4/F6/F7, 13 F1–F4, 10 F2,
10 F7). Verified: ENGINEERING.md's `--since-gate` properties 2 and 3 name a `tasks/`
row and a `TestRealCorpus` justification that are gone; "ten stages" is eleven in
README, installation.md, ENGINEERING.md and two scripts; ENGINEERING.md:291 and
ALLAGENTS.md:171 still branch on "a project still on `files`" and the latter tells an
agent to commit a handoff to `main`; the MCP leading rule says "task YAML is generated
state … Reading task YAML is allowed" and `tests/test_mcp_protocol.py:197` plus
`tests/test_mcp_server.py:493` pin it; the dual MCP registration is live (32 tool names,
the instruction block twice) and `docs/context-budget.md` says it is not;
`GLOBAL-AGENTS.md`, outside the repo, still says AgentJobs is one YAML file per task.
Bundle headroom is 404 bytes.

**R27. The quickstart blocks on prompts and `init --port` is recorded then ignored.**
New (12 F1, F2). Verified end to end in an isolated home: `init` and `create` abort with
no terminal; `init --port 8912` writes `gui.port` and `.mcp.json` and every CLI verb then
dials 8765 and prints a 60-line traceback.

---

## 4. Contradiction sweep

Every place two auditors' facts, or an auditor's and a record's, disagree. **Adjudicated**
means this session opened source or ran something; **not adjudicated** means it could
not, and says why.

1. **Does `update_task(log=[])` erase the log?** Auditor 01 (task-254): "`log=[]` empties
   the append-only log". Auditor 07 F5: stored entries 2, `log_count` column 0, reloaded
   log length 2. **Adjudicated for 07.** `SqlTaskStore._replace_log` (opened tonight)
   inserts only unseen entry ids and neither updates nor deletes, so the rows survive;
   01 read the returned object. The live defect is the `log_count` drift, not erasure.
   Task-254's spec should say so.

2. **Is `queue repair` unreachable on SQLite?** Auditor 01 N6: `repair_queue` "reads raw
   files while `SqlTaskStore.tasks_dir` raises", so it may be dead. Auditor 08 F6: it
   runs and finds nothing. **Adjudicated for 08.** `repair_queue` calls `_queue_records`,
   which now reads loaded tasks, and `_write_raw_position` goes through
   `storage.mutate_task`; nothing touches `tasks_dir`. The docstring at the top of
   `repair_queue` still says "reads the raw files" and is stale. Repair runs and cannot
   find anything to repair.

3. **Which lines assert `git merge` is pre-approved?** Auditor 01: `:324-343`; auditor
   04: `:308-319`. **Adjudicated for 01.** `grep` puts the assertions at lines 324–325;
   04's range is the docstring above them. Trivial, recorded because both tasks 245's
   re-scope will cite it.

4. **Does a request-changes handback reach `dispatch_task` with the task-record posture
   applied?** Auditor 03 F1 reads `guards.py:960` as the chokepoint and asks 04 whether
   a wake resumes the old session at the old posture instead; 04 did not answer.
   **Adjudicated: narrowed, not closed.** `deliver_handback` (opened tonight) settles
   any live run, and if one still exists marks it pending and returns
   `live_run_exists`; otherwise it calls `maybe_auto_dispatch` → `dispatch_task`.
   `dispatch_task` resumes with `--resume` when the previous session's conversation
   still exists (task-234), and `wake_argv` keeps "every posture flag … exactly where
   `build_argv` put it", so a wake carries the *old* posture. The escalation fires only
   on a cold start: when the old session was reaped, expired, or is on another machine.
   03's P2 stands with that qualifier.

5. **Does a read-only CLI command outside a project create anything?** Auditor 10 F5
   refutes task-261's fourth half: `agentjobs list` in an empty directory created no
   `tasks/` directory. Auditors 01 and 02: it created
   `~/.agentjobs/databases/local-tasks-<hash>.db` (timestamped, listed, read back).
   **Adjudicated for 01/02 on substance.** Both are true; 10 looked in the working
   directory and the side effect moved to the user's home. Task-261's ac-3 needs the
   rewording, not deletion.

6. **How many dangling context pointers?** task-411: eighteen. Auditor 07 F10: 143 raw,
   55 on open tasks. Auditor 14 F9: 55 open, 88 closed. **Not a contradiction.** 07 and
   14 agree (55 + 88 = 143); task-411's eighteen was measured before task-380 retired the
   record directory, which turned fifty pointers into dangling ones, and the test filters
   URLs, globs and absolute paths. Task-411's number is stale, not wrong.

7. **How red is `validate`?** Auditor 01: 447 problems on 410 files. Auditor 07: 453 on
   416. **Not a contradiction**: six records were filed between the two exports (the
   store moved from 409 to 416 rows during the audit). Same mechanism, same 272
   reserved-actor findings.

8. **What severity is task-262?** 07: P1 ("the brief said a red integrity tool is a
   P1"). 01: P2. 14: P3 and "refutes as written". **Adjudicated at P2.** The tool is not
   in any gate and the importer does not run it, so nothing is blocked; but the README
   sells it as "every client's portable backstop" (12 F8) and that sentence is false.
   The decision is the vocabulary question (section 2), not the number.

9. **Is the webhook-secret read P1 or P2?** 09 F1: P1 (the principal refused the write
   holds the forging key). 03 F3: P2 (latent; every live project answers `[]`).
   **Adjudicated: P1 by principle, latent in effect**; it confirms task-257 either way
   and is not filed twice.

10. **Is the dual MCP registration real?** task-301 (2026-08-25) and
    `docs/context-budget.md` §2: "the names collide and only one survives". Auditors 10
    F7 and 13 F3: both connect, 32 names, the instruction block twice. **Adjudicated for
    10/13 by observation**: this session's own context carries both prefixes and two
    identical `## agentjobs` instruction blocks. Task-301's observation predates
    task-317, when the plugin's server pointed at a dead port.

11. **Does the `--bg` harness still order `EnterWorktree`?** ALLAGENTS.md:331-334 and the
    runbook preamble: yes, and the rule wins. Auditor 13 F5: on 2.1.269 the preamble says
    the opposite ("work in place, skip EnterWorktree"). **Adjudicated for 13 on a second
    version**: this session, on Claude Code 2.1.270, received the same "work in place"
    preamble. The twelve lines and the dispatch stub's sentence override an instruction
    that no longer arrives.

12. **Does a spawned session carry the run credential?** Auditor 03 F5: one observation,
    cause not established. **Confirmed on a second session, cause still not
    established**: this session carries `AGENTJOBS_RUN_ID=run_991db1bf` and its
    `RUN_DIR`, no credential, and was spawned by an *interactive* supervisor that took
    over on 2026-09-12 at 16:30 Central, fourteen hours after that run went terminal. So
    the id is inherited from something more persistent than the run's own process tree.
    Whether the supervisor's spawn strips the variable or the harness drops secret-shaped
    keys remains 03's open question.

13. **Did task-390 close task-370?** The task-390 record implies so; auditor 04 F5: the
    task-370 test failed a finish gate on 2026-09-07 22:59, the finish of task-390's own
    branch. **Adjudicated for 04 by the finish log**; task-370 stays open.

14. **Is `agentjobs quotations` clean?** Auditor 14: "no quoted remarks across 417
    records". Auditor 13 F9: task-162's description quotes the owner, by name, dated,
    verbatim. Auditor 07 F9 explains: the detector matches a lexicon (vulgarity,
    dismissal, informality) near an attribution cue, not quotation as such. **Not a
    contradiction; a scope statement.** ALLAGENTS.md's sentence overclaims what the tool
    finds. This session hit the other edge while filing task-425: the record check
    flagged a synthetic probe sentence as a quoted remark (07 F9's machine-text false
    positive), and the description was paraphrased to pass.

15. **What is the `agentjobs search` command?** The runbook told fourteen auditors to use
    it; auditors 02, 04, 06, 07, 08, 09, 10, 13 and 14 each discovered it does not exist.
    **Adjudicated**: `agentjobs --help` lists no `search`; the read-only alternatives are
    REST `GET /search?q=`, MCP `tasks_search` (which this session used), and a read-only
    SQLite query. Nine auditors re-deriving one wrong sentence is the preamble's own
    warning applied to itself.

16. **Sandbox count.** 09: "all nineteen scripts". 11: nineteen that register do so after
    seeding, plus two that never register. **Not adjudicated** (twenty-one scripts total
    is consistent with both; the two that do not register were not run by either).

17. **Auditor 09's incident.** 09 started a sandbox on 8911, which was 11's port, and its
    cleanup killed 11's review-panel sandbox at about 22:56 Central. 11 reports all three
    of its servers stopped before its file was written and its XSS probe created
    `task-001` on the sandbox project. **Not adjudicated**: whether 11's evidence around
    22:56 was affected cannot be told from the two files; 11's findings 1 and 2 were
    re-run after the usage-window resume the next morning, per its header, so the risk
    is confined to the first evening's reads.

18. **Whether the poller's manager is really built without a webhook manager.** 04 F10
    and 01 (task-265) both read `main.py:162` and `store_factory.py:261-266` the same
    way; 09 asked 04 to confirm. **Not independently adjudicated** by this session, but
    two auditors read the same three lines identically and no auditor disagrees.

19. **The 240 ms handoff versus 11 ms claim.** 09 F5 measured it on its sandbox and did
    not explain it; no other auditor measured a handoff. **Not adjudicated**; open
    question for storage.

---

## 5. Themes

Failure classes that recurred across systems, stated once each with the members.

**A. The cutover moved the ground and the prose stayed where it was.** The record
directory was retired on 2026-09-07; three weeks later the always-loaded bundle
(`--since-gate` properties, "still on `files`"), the MCP leading rule, the plugin skill,
the write guard's managed directories, `validate`'s premise, `corpus_stats.py`,
`regen-schema-docs.sh`, `docs/storage-sqlite.md` §6, `repair_queue`'s docstring, the
playbook briefs and `GLOBAL-AGENTS.md` all describe the file era. Task-393's docs pass
missed all of them. The mechanism is that a storage change is invisible to a path-based
gate (task-409) and to a path-based docs sweep alike.

**B. Tests that assert on the object they were handed.** The redaction test reads the
value the code just wrote into the object it returned (R1). The plugin-version test
compares `0.1.0` to `0.1.0`. Two MCP tests pin the stale leading rule so a fix goes red.
`test_check_gate` stubs `head_commit` to a constant so HEAD can never move. The epic
walk's only retry test scripts a state the poller never produces. Same shape as
task-207, which ENGINEERING.md already names.

**C. Fallbacks that succeed silently.** The CLI's `_local` project; `sandbox_store`'s
directory-hashed file; the 8765 default in eight commands; `corpus_stats.py` printing a
table of zeros and exiting 0; approve on a task with no branch doing nothing; a usage-
window stop reading `idle`; a finish killed mid-delivery reading `running`; `queue check`
exiting 0 with problems. Each is a "no error" that a later reader mistakes for "no
problem". The repository's own rule against this ("a check that passes: what would it
have caught?") is stated in ENGINEERING.md and not applied to defaults.

**D. A boundary enforced on one transport.** Authorization on HTTP, not the CLI or the
shell. The update-allowlist on the API's request model, not the manager. `check_record`
on MCP, not REST (where the person writes). `validate` on files, not rows. MCP refuses a
`handoff`-typed log entry; REST accepts it. Every one of these is documented as a
property of the system and is a property of one door.

**E. Deferred decisions with no surface.** Twenty-two drafts at `human/spec` with a
placeholder prompt; groom's three closures and one move on a closed task; task-063's
"close it" unexecuted for six days; task-246's decision unmade for three weeks while
more identifiers landed; task-308's "may a task raise its own posture" answered by a
comment in a private config file. The tool records a decision request perfectly and
shows it nowhere a person looks.

**F. Counts in prose.** Ten stages (eleven), fifteen tools (sixteen), fourteen tools in
the plugin README, eighteen dangling pointers (143), 139 open tasks (149), 404 records
(416), "the gate is about a minute" (250 s), "581 s longest gate" (862 s), "the gate is
96 seconds". ENGINEERING.md forbids this and does it.

**G. Retry costs a whole cycle.** A killed finish re-gates for eight minutes; the epic
walk's retry is unreachable and its cooldown eats the one attempt; runs launch three to
seven gates per handoff; a red roadmap stage dispatches a session to fix nothing. The
expensive step (the gate) is re-run because the cheap state (what already passed) is
not kept, which is task-414's thesis restated from the gate's side.

**H. Severity is a judgement three people make three ways.** Task-262 was rated P1, P2
and P3 by three auditors on the same facts; the webhook secret P1 and P2; task-275 was
P2 last time and P1 now on the same code. A rubric ("blocks a workflow today" versus
"false claim in a document") would have settled all three without adjudication.

---

## 6. Coverage honesty

What each auditor said it did not get to, collected so the next audit can start there.

- **01.** Nothing exercised in a browser (260, 274, 275 server-side only). MCP surfaces
  for 255/263. Two-project collision for 264. `queue repair` on a sandbox. The tailnet
  proxy path with the real secret. Closed tasks 251, 268, 269, 270 not re-read.
  `finish.py` and the importer not opened.
- **02.** Attachment blobs under concurrent writers. Export-then-reimport of real data.
  `split` on a real shared file. Reader-connection growth and WAL checkpoint starvation.
  Whether `taskkill /F` skips `close_databases()`. `backfill.py`, `reporting_tz`,
  analytics queries. `TaskLockTimeout → 409` over HTTP. `_replace_children` cost at
  4,000 tasks.
- **03.** The Go proxy's tests (`go` not installed) and its path-normalisation edges.
  Chrome's local-network-access policy from a public origin. A real dispatched session's
  environment end to end (and a Codex batch run's). The transcript and tail routes as a
  leak channel. `identities.yaml` edge cases. The attachments route for traversal. The
  legacy routes' principal handling. Rate limiting (none exists).
- **04.** Reproducing F3 (a restart that leaves the server down). `runner.py` beyond the
  cited clauses; `codex_app_server.py`, `interactive.py`, `transcript.py`,
  `session_env.py`, `address.py`, `budget.py`; `docs/codex-dispatch*.md`. Racing F2 with
  real processes. The epic walk against a real dispatch. How the frontend renders the ten
  `running` finishes. Re-running its three delegated probes itself.
- **05.** The full gate (by instruction); pytest and e2e figures are the ledger's.
  Task-267's open `PATH` question. A receipt whose commit was pruned. `build_release.py`
  itself and `bench.py --corpus real`. `run_report.py`'s arithmetic. Three-gate
  contention. `context_eval.py`.
- **06.** Did not start `playbook_sandbox.py` or dispatch a playbook under a minted
  credential (F1 is a chain of verified links, not an observed 403). `flesh-out.md` and
  `reorder.md` end to end. The Playbooks page in a browser. `test_export_roadmap.py`'s
  clone-without-a-database path. Tasks 299 and 300.
- **07.** The dispatch door for `hold`. `storage import`'s exit code on a constraint
  quarantine. `_check_paths` on Windows junctions. The API's `TaskUpdateRequest` over
  HTTP. Whether any client re-posts a tolerated pseudo-member; `attachments.py`.
  `docs/schema/` generated-tree freshness (probably stale; not diffed). `record_check`
  over the live corpus. The Markdown importer.
- **08.** HTTP-level reproduction of the reband 500. `storage status` for the three
  projects task-258 names. `receipts.py` entirely (documents receipts for files that do
  not exist; still imported by four modules). `queue_listing`, `dependency_facts`,
  `--why` for eligibility beyond one smoke test. `redact`. `docs/agent-workflow.md`
  beyond the corruption section. Webhook signing and delivery.
- **09.** The browser half of task-274. Task-255 point 2 (replayed claim on a reassigned
  task). Task-265. Delivery ordering. `npm run check:api-client` (writes into the
  checkout). Tailnet-principal behaviour on webhook routes. The 240 ms handoff baseline.
  `remote_manager.py` beyond reading.
- **10.** `wrong_task` end to end with a live credential (the end-to-end evidence is for
  `unverified_run_credential`). `agentjobs restart` and `open`. The STDIO transport.
  The Codex plugin and `mcp-clients.md`'s Gemini/Cursor sections.
  `mcp-integration-design.md` in full. `project mcp-setup`. The task-276 false positive.
- **11.** The live surfaces on a browser (review panel, questions UI, slot board, live
  runs, dashboard next action): every sandbox it could have used was empty. Insecure-
  origin behaviour on a real LAN address. iOS specifics. `api/generated` drift beyond
  the gate. The Playwright suite's cost and flake. The 21:21 rebuild of `frontend_dist/`.
  Accessibility items from the prior audit.
- **12.** The bodies of the design records (dispatch, analytics, playbooks, task
  selection, agent loops, principals). `docs/agent-workflow.md`'s sentences.
  `task-schema.md`'s field table. Storage operator sequences. Authorization and exposure
  tables. `performance.md`'s figures. The tsnet proxy's procedures. Codex dispatch docs.
  The mkdocs build. `work --agent` and `open`. Whether `agentjobs` resolves on a
  stranger's PATH after a poetry-from-clone install. **The other auditors' files** (this
  one was written before reading neighbours so that it landed).
- **13.** A second measurement of the harness residual on 2.1.269. The Codex side of the
  bundle. `docs/agent-workflow.md` (62 KB, grepped only). A full `--tag quick` eval
  sweep on Fable (~$23, priced, not run). Whether the `--bg` preamble returns without
  `bgIsolation: none`. `MEMORY.md`'s 49 entries as content.
- **14.** The other four projects' corpora. The 88 dangling pointers on closed tasks
  (counted, not enumerated). The playbooks' full text. How the React log view renders
  147 null-body `dispatch_result` entries. task-410's own record (by instruction; this
  synthesis read it). Every completed task's decision entries (five read, rest counted).
  The `open_delta` series. Whether `playbook run` populates `task_run.playbook`.
- **Synthesis.** Opened source for the five adjudications in section 4 and nothing else.
  Did not re-run any auditor's probe. Did not read task-414's design document
  (`bb12f33f`), which no auditor read either; its children (264, 312, 375, 416, 418 and
  five more) may already cover parts of R13 and R14 in language this report does not
  match. Did not verify the supervisor's line counts or session ids. Did not read the
  2026-08-21 report beyond what auditor 01 quoted.

**What this audit did not look at at all:** the four other projects' data; the tsnet
proxy under real tailnet traffic; any device (phone, tablet); Codex as a runner or as a
client; the mkdocs site; `contexteval` beyond one case; the analytics design's queries;
the embedding program (twelve tasks, never started); anything under `evals/` except the
context cases.

---

## 7. What to change about the next audit

**The account is metered, and the runbook did not know it.** Batches 3 and 5 both
stopped on the five-hour session window (22:58 Central on 2026-09-11 and 12:10 Central
on 2026-09-12), each with no file written after nine to fourteen minutes of reading. The
supervisor was lost twice: once to the same limit, once because the desktop app hid the
session and the owner had to hand the task to a fresh interactive session. Each stall
was recovered by `--resume` with a two-line nudge and landed within minutes because the
reading was kept. So:

1. **Pace to the window, not to the roster.** Check the meter before every batch, not
   once; the runbook's pre-flight step 1 could not be executed from a dispatched session
   at all (`/usage` has no tool form). Launch a batch only when the window has room for
   the batch's expected wall clock plus a retry. Three concurrent Fable sessions is the
   ceiling that hit the limit twice; two per batch with a shorter cycle would have cost
   one more round and zero stalls.
2. **Make the done-signal a file, and write it early.** The file-as-done rule is what
   caught both stalls; a process-state watcher would have reported three completions.
   Go further: have every auditor write a stub with its two closing sections in the
   first five minutes and overwrite it as findings land, so a session cut off at any
   point leaves a truncated file rather than none, and the retry can be a resume that
   appends.
3. **Resume, never restart.** Both retries were resumes and both landed in under ten
   minutes. Write that into the failure rule as the first option and cold start as the
   second.
4. **Put the supervisor's state on the record, not in a transcript.** The second
   supervisor reconstructed the batch state from task-410's log entries and the files on
   disk; it worked because the first supervisor had logged every launch and landing.
   Make that a rule: a batch launch is a log entry naming session ids and the done-signal
   paths, so any session can take over from the record alone. That is task-414's
   contract applied to the audit itself, and the audit is the cheapest place to prove it.
5. **Register the supervisor and every auditor as a run, and fix what refuses.** `run
   register` refused the second supervisor as `session_unknown`; the first was watched by
   nothing for fourteen hours. A spawned session inherits a dead run's id and no
   credential. Until task-414 lands, an audit should register each session by hand and
   record the refusal when it fails, as entry 34 did.

**The brief.** Fix the sentences nine auditors tripped over: `agentjobs search` does not
exist (say `tasks_search` over MCP, `GET /search`, or a read-only SQLite query); the
"read it if you like" invitation to 8876 contradicts the preamble's ban; the `--bg`
preamble no longer says what the brief says it says; `scripts/project_setup.py` was
deleted; `audits/` is classified, not unclassified; the count of open tasks should be a
command and a date. Tell auditors what to do with `AGENTJOBS_HOME` before they start a
server: four of them wrote throwaway tasks into the live databases directory because the
preamble said "your own port" and nothing about the home. Allocate ports from a table
the supervisor writes to the record, so auditor 09 cannot take auditor 11's.

**The method, keep.** "Verified by running or inferred from reading, per finding" and
"New / Confirms / Refutes" are the two instructions auditors said changed what they did,
and they are why section 2 could be written at all. Keep both. Add a third: a severity
rubric with two axes ("blocks a workflow today" and "a documented property that is
false"), so three auditors stop rating one fact three ways.

**The synthesis.** Give it the fifteenth input explicitly, as this one had. Tell it
which records were filed *during* the audit (task-414 and its ten children, task-418,
task-419, task-420 all landed between the runbook and the synthesis) so it can fold
instead of duplicate. Have it open source for contradictions only, as this one did;
that budget was the most useful hour of the session.

**Filing.** Three drafts, not thirty-three, each with a spec, acceptance criteria and
context, and a `ball_prompt` that still reads "Finish specifying this task" because
`task_create_draft` writes that placeholder. The next runbook should either create P1s
as `ready` (auditor 14's recommendation) or have the synthesis promote them itself with
a decision entry; a draft with a placeholder prompt is the shape that produced the
twenty-two.

---

## Filed and not filed

**Filed as drafts, specced to be claimable:**
- **task-425** — `agentjobs redact` on a log entry body never reaches the row (R1).
- **task-426** — every authorization refusal reaches an MCP agent as `internal_error`
  (R5).
- **task-427** — every review sandbox serves an empty project (R3).

**Deliberately not filed**, because an open record already holds them and section 2
says what to do with it: R2 (task-275), R4 (task-245, task-129), R6 (task-322 under
task-414), R7 (task-406), R8 (task-407), R9 (task-262), R10 (task-250, task-257), R11
(task-418), and every supervisor-observed dispatch defect (task-414 and children). Every
P2 and below stays in this report.

**Conduct notes for the record.** Auditor 09 killed auditor 11's sandbox on 8911 at
about 22:56 Central and said so. Auditor 02 found its assigned port 8902 already held
and used none. Four auditors' sandboxes wrote fifteen throwaway tasks into
`~/.agentjobs/databases/` as shadow files; nothing serves or backs them up, and nothing
removes them — the supervisor should delete `local-tasks-*.db` files created on
2026-09-12 after confirming their roots are temp directories. No auditor wrote to the
live task store; this synthesis wrote three drafts and one content update to task-425,
as instructed.
