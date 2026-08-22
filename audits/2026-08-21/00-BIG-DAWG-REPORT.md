# Big Dawg Audit — the report

Twelve auditors, one codebase, the night of 2026-08-21 into 2026-08-22. Synthesis written
2026-08-22 by the session that read all twelve findings files and nothing else at depth;
source was opened only to adjudicate contradictions (five of them, each marked below).
Task record: task-242. Findings files: `01-` through `12-` in this directory. Draft
tasks: task-244 through task-276, one per P1/P2 finding or cluster.

Raw severity counts across the twelve files, before deduplication: **9 P1, 59 P2,
≈80 P3, ≈45 P4**. After clustering, the P1/P2 set is 33 items.

---

## 1. Executive summary

**The product works and the code is better than the docs say; the deployment is
wide open and the docs say so.** Every auditor who examined correctness of the core —
storage locks, queue selection, schema rules, the React render path, YAML loading,
path containment — reported "examined, nothing found" on the thing they expected to
break. What they found instead clusters into five stories.

1. **Anyone on the tailnet is Jeff.** The whole REST API — 99 operations, 61 of them
   mutating — is proxied to `agentjobs.tailfed1df.ts.net` with no authentication;
   identity is a body field that `GET /api/projects` hands out. A peer (or a page Jeff
   visits, or an agent inside a dispatched run) can read every project including the two
   marked "local only, never push", start a paid Claude session on any task as Jeff,
   enable dispatch, register any directory on the machine as a project, and read session
   transcripts. The dispatch design's "structurally impossible" safety claim is false.
   And `audits/` — including the security file that says all this with curl shapes — is
   already on the public GitHub remote. **Decide today: task-246. Fix: task-244, 274.**
2. **The merge gate has no mechanism inside a dispatched run.** `git merge`, `git add`
   (including `-A`), `git commit` and `npm run` are pre-approved for every dispatched
   session, classifier bypassed; task content any API caller can write is delivered to
   the agent verbatim as "what the human said". The only thing between an injected task
   and an unapproved merge into `main` is that the model read ALLAGENTS.md. Task-245.
3. **Three real defects in the core nobody has hit yet.** Task-file writes are a bare
   `write_text` — truncate then write, no rename — so every write has a window readers
   see as a broken task and a crash loses a session of log entries (task-247). The
   tolerant-enum reader rejects exactly the enum that was widened two days after it
   shipped, so a stale MCP client cannot read a task on hold (task-248). A reopen or
   priority change computes its queue position under the lock and writes it outside —
   reproduced, reachable over the tailnet, takes down `next` until someone runs repair
   (task-253). Four auditors found that last one from four sides.
4. **The instrumentation you built last week mostly did not record.** `--bg` hands the
   session to a daemon that keeps its own environment, so `AGENTJOBS_RUN_ID` reached one
   run in seven and `run_report.py`'s gate lines are empty for task-241's own run;
   the same hop means a runner's `env:` is never delivered (task-249). Separately,
   `finished_at` is settlement time, so task-233's baseline table was built on numbers
   that are ≥60 minutes by construction for one class of run (task-265).
5. **The prose lags the code by about a week, in both directions.** The dispatch design
   says "nothing here is implemented" above 11.7k shipped lines and describes three
   unbuilt features in the present tense; the queue design is labelled a proposal after
   shipping in full; `task-schema.md`, `webhooks.md` and `mobile-access.md` each passed a
   structural check and failed a sentence-level one; two of three test counts in
   ENGINEERING.md's dated table were wrong the day it was dated; 634 words of static
   context describe a scripted finish that is switched off on this machine. Every
   session pays ≈17k tokens for that bundle and acts on about a third of it
   (task-252, 269, 270).

Two questions Jeff weighted got direct answers. **Backend:** the files are not too big,
they are in the wrong place for concurrent writers; stop the accrual now with a
`TaskStorage` Protocol, index when listing p50 passes 250 ms, and price "records out of
the working tree" before pricing a database (task-273). **Gate speed:** the true critical
path is ~52 s against 96 s serial; copy the mypy cache, add `--durations`, drop nested
`poetry run`, then prototype concurrency behind a flag with contended runs on record
(task-268).

Also worth a morning coffee: **job-hunting's backlog is invisible** — every open task
there lacks a queue position, so the dashboard says "nothing claimable" while `/queue`
says broken (task-258). And `agentjobs validate` reports 221 problems on this
repository's own corpus, 96 of them the validator objecting to actors the product
itself writes as (task-262).

---

## 2. Ranked findings — every P1 and P2, deduplicated

Rank is the synthesis session's judgement of blast radius × likelihood × cost of
waiting. "Witnesses" names every auditor who found the defect independently; two
witnesses from different sides is corroboration, not duplication.

| # | Sev | Finding | Witnesses | Task |
|---|---|---|---|---|
| 1 | P1 | Whole API on the tailnet with no auth; identity is a body field; any directory registrable; local-only projects and run transcripts served; dispatch-as-Jeff from any peer or from inside a run | 12 (S-1, S-6, S-8), 10 (P1-2), 7, 2 | 244 |
| 2 | P1 | `audits/` including `12-security.md` already on the public remote; user-profile paths and tailnet IPs in ~30 tracked task records | 12 (S-9), synthesis git check | 246 |
| 3 | P1 | Prompt injection chain open: task content → agent instructions, wake stub frames `ball_prompt` as the human's words; `git merge`/`git add -A`/`npm run` pre-approved, classifier bypassed; merge gate unenforced inside a run | 12 (S-2), 10 (P2-10, P2-8, item 5) | 245 |
| 4 | P1 | Task write is `write_text`: no temp file, no rename; readers see truncated files; crash loses log entries; stale lock has no reaper | 4 (F1, F2); 7 contradicted and was wrong (see §3 C1) | 247 |
| 5 | P1 | Tolerant reader rejects an unknown `ball_reason`; the enum was widened 2026-08-21; task-233 (on hold) unreadable to a stale client | 3 (F1, F12) | 248 |
| 6 | P1 | Run identity and runner `env:` do not reach `--bg` workers in steady state; 6 of 7 instrumented runs have no phase records; secrets-in-env advice is broken | 10 (P1-1); symptom seen by 11, 1 | 249 |
| 7 | P2 | Reopen/reband position computed under the queue lock, written outside it; reproduced both forms; reachable via `PATCH priority` | 5 (F1, reproduced), 4 (F4), 2, 3 | 253 |
| 8 | P2 | Three projects' open tasks have no positions (17 tasks invisible, job-hunting among them); `next` says "nothing claimable" over an all-broken corpus instead of refusing; repair has no dry run | 5 (F2, F3, F7); 8 corroborates | 258 |
| 9 | P2 | Held task offered by `next` and claimable; ball/reason untied from lifecycle (active+available, draft+work, ready+work all constructible); draft and human-ball tasks dispatchable | 3 (F2, F3), 10 (P2-8) | 259 |
| 10 | P2 | `manager.update_task` has no allowlist — `lifecycle`, `ball_prompt`, `archived`, `log: []` all accepted from Python; reopen writes no transition | 4 (F6), 3 (F4), 8 | 254 |
| 11 | P2 | `operation_id` contract: webhooks re-fire on replay; replay returns the current task not the original; create/update hard-code `replayed: false` and bypass the error envelope | 4 (F3, F5), 7, 8 (F2, F3), 2 | 255 |
| 12 | P1 | `POST /webhooks/{id}/test` always 500s (`asyncio.run` inside the loop); zero route tests | 7 | 250 |
| 13 | P2 | Webhook secrets returned by list/get and in OpenAPI; SSRF-capable targets; full record in every delivery; no delivery id; no retry; unlocked bookkeeping | 7, 12 (S-4, S-5) | 257 |
| 14 | P2 | CSRF from Jeff's own browser: no Host/Origin check, typeless body parses as JSON, body-less POST dispatches | 12 (S-3) | 274 |
| 15 | P2 | Stored XSS in legacy Jinja pages via `marked.parse` → `innerHTML`; CDN scripts without SRI; still mounted and proxied | 12 (S-7), 9 | 275 |
| 16 | P1 | Install/quickstart recipe ends at a JSON 404; `open` opens the browser before checking; quickstart step 3 creates a draft and `work` reads the wrong directory | 2, 6 (F1, F4, F9, F12) | 251 |
| 17 | P1 | `agent-dispatch-design.md` says "nothing implemented" over 11.7k lines; present-tense unbuilt features; false absences; Safety section argues against the wrong attacker; README denies the loops design exists | 2, 10, 12 (S-14) | 252 |
| 18 | P2 | Three error envelopes plus bare 500; OpenAPI declares a 422 that never happens and no `ErrorBody`; seven human actions 404 on "task is closed"; `task_next` reports its winner `actionable: false` | 7 (×3), 8 (F1, F13), 9 | 256 |
| 19 | P2 | `default_user` adoption requested in four places, enforced in none; CLI defaults agents to Jeff; reserved `dispatcher`/`finisher` accepted from any caller; PATCH/DELETE/deliverables unvalidated | 8 (F5), 6 (F6), 12 (S-10), 7, 4 (F8) | 263 |
| 20 | P2 | Dispatch ledger: cancel vs poller race observed; ceiling is a directory scan; locks/wake/reap keyed by task id without project | 10 (P2-4, P2-9, P2-5) | 264 |
| 21 | P2 | Dispatch settlement: `_ball_moved` counts the dispatcher's own handoffs; poller handoffs fire no webhooks; `run_report` measures settlement time | 10 (P2-6, P2-7, P2-11) | 265 |
| 22 | P2 | PWA: shell served without `Cache-Control`, precached through the HTTP cache; no SW update check after load — a foregrounded tablet keeps its bundle unbounded; main-clone gate blacks out the live dashboard; `randomUUID` throws on the http fallback | 9 (P2-1..3), 11 (P3-2) | 260 |
| 23 | P2 | CLI identifies the server by port only; `restart` with defaults starts the 8765 server ENGINEERING warns about; `restart --port 8876` kills the phone UI; read-only commands create `tasks/` anywhere; `gui.port` ignored | 6 (F2, F5, F7, F8, F9) | 261 |
| 24 | P2 | Gate receipt issued for HEAD read after the run (HEAD moved during the audit); `--since-gate` misses a rename's source; bootstrap re-hijacks a broken env; `build_release` does not prove the wheel runs elsewhere | 11 (P2-1..4), 4 | 267 |
| 25 | P2 | Static context: harness says EnterWorktree and enforces it, repo forbids it and never says so (three auditors hit it); 634 words on a finish that is off here; GLOBAL-AGENTS wrong on parent claimability; ALLAGENTS wrong on `ball_prompt`; no budget | 1 (F-2, F-3, F-4, F-1), 3 (F7), 10 (P3-17), 4, 11 | 269 |
| 26 | P2 | Dispatch gate 2 false for `.mcp.json`: a repository pre-approves its own MCP servers and, for a supervisor, every tool of them | 10 (P2-3) | 266 |
| 27 | P2 | `agentjobs validate` red on its own corpus: 221 problems, 96 of them reserved/system actors; the documented hook would refuse every commit | 3 (F5), 6 (F3) | 262 |
| 28 | P2 | Docs drift: api-reference 74/94 + false actor claim; index 12/19; queue design "proposed"; sentences in task-schema, webhooks, mobile-access overturned | 2, 7, 3 (F11), 9, 8 (F10) | 270 |
| 29 | P2 | Installed plugin is a hand-edited snapshot from 2026-08-17; version 0.1.0 forever makes every version check vacuous; compat probe ignores `source_commit` | 8 (F4, F9), 11 (P3-9) | 271 |
| 30 | P2 | `migrate-schema` re-run on a half-migrated corpus duplicates positions; migrator writes outside `TaskStorage`; its real-corpus test asserts nothing | 3 (F6, F8, F9), 4 | 272 |
| 31 | — | Backend decision point (Jeff-weighted): coupling inventory, ~2 new leak sites per working day, triggers | 4 (§6) | 273 |
| 32 | — | Gate speed (Jeff-weighted): true graph, ranked proposals, rejections on the record | 11 (Part A) | 268 |
| 33 | P3↑ | Task write guard refuses reads and unrelated writes that mention a task path; four auditors and the dispatcher lost time | 1 (F-12), 4, 8 (F4), task-242 log | 276 |

P3/P4 findings are not ranked here; each lives in its findings file and the ones that
fit a cluster above are named inside that cluster's task. Notable P3s that fit nowhere:
the top of a band exhausts in five inserts and then rewrites 57 files (5 F6); `/api/projects`
parses every task file to print a count, 10× the task list's cost (7); the Python client
covers 20 of 99 operations and the CLI does not use it (7); three tests reserve-then-release
a port under 32 workers (11 P3-4); reorder has no touch path on the device the backlog is
read from (9 P3-7); `_find_created_by` is O(corpus × log) per create (4).

---

## 3. Contradiction sweep

Every place two auditors stated incompatible facts, or one auditor's verdict was
overturned by another's evidence. Adjudicated where the synthesis could; marked
unresolved where it could not.

**C1. Are task-file writes atomic?** Auditor 7 (webhook bookkeeping finding): `_write_webhooks`
is a plain `write_text`, "no temp-file rename, *unlike task files*". Auditor 4 F1:
`_write_task` is `path.write_text`. **Adjudicated for 4** — `storage.py:496-497` read by
the synthesis is `yaml.safe_dump` then `path.write_text`. Auditor 7's aside was an
assumption; the task-247 record says so.

**C2. What does a replay return?** Auditor 8's instruction-text table marks "reusing an
operation_id replays the original result" as *true for 8/10 tools*. Auditor 4 F5: no
original result is stored; `mutate_task` returns the task as it is now. **Both right on
their facet, 4's is the one that matters.** The synthesis read `storage.py:440-463`:
on `None` from the mutator it returns `current`. "Does not write twice" is true; "original
result" is not. The served text promises the second.

**C3. Is the clean-tree check on?** Auditor 12 S-2: "consider `require_clean_tree: true`
for dispatched projects (it is not set)". Auditor 10's lifecycle trace lists "clean tree
outside `tasks/`" as a gate step. **Adjudicated for 10** — `dispatch/config.py:366` defaults
it to `True` and `guards.py:729` checks it. 12's observation that `dispatch.yaml` does not
set it is literally true and immaterial.

**C4. What does auto-dispatch order by?** Auditor 5: `get_next_task` has no caller in
`dispatch/`, yet design §14 says auto-dispatch consumes it. Auditor 3: `auto.py:193` admits
any open task with `ball: agent` that is not `hold`. **Resolved by a grep:** `auto.py` never
calls `get_next_task`, `explain_next`, `_claimable` or `order_key`; `maybe_auto_dispatch(task)`
is reactive — it fires on the task a human just acted on. It does not order at all. The
design sentence is wrong (folded into task-252); auditor 3's draft+work auto-dispatch case
stands (task-259).

**C5. Is `task-schema.md` accurate?** Auditor 2: accurate — 25/25 fields, 8/8 enums
identical. Auditor 3 F11: five sentences are not true of the code (seven required fields,
manager validates category, update_task cannot touch the axes, five rules of six, dispatcher
valid in every project). **Adjudicated for 3.** Auditor 2 checked structure; 3 checked
sentences. Same pattern for **`webhooks.md`** (2: accurate; 7: `task.handoff` fires only from
the handoff verb, not on every ball move) and **`mobile-access.md`** (2: accurate; 9:
foregrounding does not pick up a new bundle and `randomUUID` throws on the documented
fallback). In all three the docs auditor's method — keys, signatures, links — passed and the
system auditor's — behaviour — failed. This is a finding about method (§6).

**C6. How many tests are there?** ENGINEERING.md: 2608 pytest, 26 Playwright. `check.py:183`
and `pyproject.toml`: 2538. Auditor 1 ran `pytest --collect-only` → **2723**; auditor 1 and
auditor 9 each counted Playwright → **22 in 8 files**. Three pytest numbers, one measurement.
Not a disagreement between auditors; a disagreement between documents and a tool, and the
lesson is auditor 1's: the numbers written with a date and a command survive, the bare
ones go stale within a day.

**C7. Is a parent claimable while a child is open?** GLOBAL-AGENTS.md:125 says no.
Auditor 3 F7: `manager.py:1180-1195` (task-164), `task-schema.md:84` and `schema-design.md:89`
say yes, only `/next` skips it. Auditor 10's trace adds that the supervisor dispatch path
*claims* exactly such a parent. **Adjudicated for the code.** Auditor 1's redundancy map
(18 rules, 58 statements) did not catch this one — it compared the repo's files with each
other and the harness, and GLOBAL-AGENTS's sentence has no counterpart to disagree with.

**C8. Does the scripted finish run on approval?** Auditor 2 treats "on a finish-enabled
machine, approve runs git" as making `api-reference.md:65-66` false now. Auditors 1 (F-3)
and 10 (P3-17): `dispatch.yaml` has no `finish:` block, `~/.agentjobs/finishes/` does not
exist, the finish has never run here. **Compatible** — 2's claim is conditional and the
condition is false on this machine. What that makes of 634 words of static prose is
task-269.

**C9. Does `/revision` read the corpus?** Auditor 4: `project_revision` hashes every file
per poll, 3.76 MB per hit. Auditor 7: `/revision` answered in 49 ms with `X-Task-Parses: 0`.
**Both true** — it reads bytes without parsing. The parse counter is honest about what it
counts; auditor 4's point is that the byte read is the leak.

**C10. Does a runner's `env:` reach the session?** Auditor 12 ("examined, nothing found"):
`env:` is merged into the child environment and never written to `meta.yaml`. Auditor 10
P1-1: it reaches the launcher, and the daemon discards the launcher's environment.
**Compatible; 10 is deeper.** 12 was answering "is it logged" and was right.

**C11. Which four state verbs?** Auditor 1's "examined, nothing found" on the MCP
instruction text: the four claims are each backed by a verb. Auditor 8 F10: the leading
rule names claim/handoff/release/close where `task_promote` is a fifth lifecycle move and
calls itself "the only exit from draft". **Adjudicated for 8** — an agent obeying the rule
literally will not promote.

**C12. Worktree naming.** `aj-<nnn>` (ENGINEERING, ALLAGENTS) vs `<repo>-<nnn>`
(`PROMPT_STUB`, the workflow guide). Auditors 1 (F-11) and 10 (P4) both flagged it;
no disagreement, two conventions. Task-269.

**C13. The poller reaps.** `main.py:77` and the design say so; auditor 10 P3-15 shows
`_finish_session` stops and `reap_finished` runs at startup only, with `meta.yaml` mtimes
clustering at the last server start as evidence. Doc vs code, not auditor vs auditor; task-265.

**Answered cross-auditor questions** (the seam the brief called the richest):
- 2 → 5, "can the reopen window produce a duplicate?" — **yes, reproduced** (5 F1); 4 F4
  reached the same conclusion from lock-order analysis; 3 had flagged it as not constructed.
  Four witnesses, one mechanism, one task.
- 2 → 8, "`replayed` hard-coded false — doc fix or shape fix?" — **shape fix at the REST
  layer**, doc says "eight of ten" until then (8). Consistent with 4 F5 and 7's replay-refire.
- 2 → 6, "should `open`/`serve` print the missing-bundle sentence?" — **yes, both, and
  `open` must not open the browser until it has checked**, with a ~50-line shape (6).
- 1 → 2, "is `agent-workflow.md` accurate?" — yes, every signature and anchor (2).
- 12 → 7, "does OpenAPI document the webhook secret?" — yes (7, independently).
- 6 → 8 and 10 → 8, "is `default_user` / a human actor refused on agent verbs?" — **no,
  not anywhere** (8 F5, live). Which means 10's P1-2 has a second door: write a human note
  as "Jeff Posey", then `agentjobs dispatch run`.

**Unanswered cross-auditor questions** (carried into Coverage, §5):
7 → 4 on whether `maybe_auto_dispatch` is gated on a real write; 8 → 10 on what a `--bg` run
does with the plugin's second MCP server; 12 → 10 on whether `reconcile` trusts `meta.yaml`
fields the agent can write; 11 → 10 on the 30% gap between the table's 95.8 s and the run's
124.5 s; 9 → 4 on whether every write path (repair, migration, checkout, attachments) bumps
`/revision`; 6 → 11 on whether the gate deliberately never runs `validate`; 5 → 4 on whether
the three unpositioned projects were written by a pre-task-204 build or a bypass; 1 → 3 on
whether the two id series are task-105's fix working.

---

## 4. Themes — failure classes that recurred across systems

**T1. Invariants enforced at one edge, constructible through another.** The brief asked
auditor 3 to look for this; nine auditors found an instance. The content allowlist lives
at the API and MCP edges and not in the manager (4 F6, 3 F4); `hold` is refused by dispatch
and offered by `next` (3 F2); `agent/available` is refused by MCP and accepted by REST and
the manager (3 F3); actor validation covers the nine verbs and skips PATCH, DELETE and
deliverables (7, 12 S-10); "never adopt default_user" is in four docstrings and no code
(8 F5, 6 F6); the merge gate is in ALLAGENTS and not in the allow-list (10 P2-10, 12 S-2);
the human-clocked dispatch rule is a body field (12 S-1, 10 P1-2); `operation_id` is a UUID
in every schema and checked in none (8 F6); `expected_revision` exists on handoff/close/
promote and not release (4). The pattern has a single cause: the rule is stated where a
reader will see it rather than where every writer must pass. The fix is the same each
time — one set, in the manager or the model, that the edges derive from.

**T2. Docs describing plans as present tense, and shipped code as plans.** Dispatch design
"nothing implemented" over 11.7k lines and `difficulty`/`model`/`strict` described as built
(2); queue design "proposed" after shipping (2); `schema/v2` banner "nothing here is
implemented yet" on generated docs that are current (2, 3); README denying the loops design
exists (2); `main.py` and the design saying the poller reaps (10); `task-selection-design`
§5.3 saying the priority patch routes through `reprioritize` when it does not, which is
why it has a different lock profile (5 F11); `storage.py:220` describing an atomic writer
that does not exist (4); `storage.py:497` saying the migrator produces receipts (3 F8).
The dangerous direction is the first: a reader who trusts §4 of the dispatch doc sets a
field that is silently ignored.

**T3. Numbers written bare go stale within a day; numbers with a date and a command
survive.** 2608/2538/2723 tests (1, 11); 26/22 Playwright (1, 9); "two second" probe vs 5.0
(2); gate "355s" vs 95.8 (2); 95.8 s vs 124.5 s in a run (11); "about eleven minutes" stated
six times and not reproducible from the instrument named for it (1 F-8); "fourteen tools"
vs 15 (2, 8); the bench baseline at 119 files vs 240 now (11). Auditor 1's rule is the fix:
quote the command and the date, have the tool print the count.

**T4. Decorative tests — green while proving nothing the brief asked about.** `assert True`
after a webhook handoff (7); `TestTheRealCorpus` for migration iterating 247 files and
skipping every one (3 F9); `ConnectionUnavailable` testing an `offline` state the app never
renders, `TaskCount` for a component nothing imports (9); `test_the_seed_list_covers_the_boring_commands`
asserting the allow-list's contents rather than what a pre-approved command can do (10);
`test_every_stage_is_named` (11); the `open` tests mocking away the two things that fail
(6 F13); `test_client.py:142` pinning the `"system"` default the server refuses (7);
`test_claude_plugin.py:74` comparing two version numbers that never move (8). And the
subtler class: tests that pass against fake runners because the mechanism that fails is a
hop they cannot reach — `TestTheRunIsMeasurable` (10 P1-1), `test_it_captures_the_id_the_cli_assigned`
with a clean banner where the real one has escape codes (10 P3-12).

**T5. Replay and retry are not idempotent one hop downstream.** Webhooks re-fire on replay
(4, 7); a replayed priority patch rebalances a band before discovering it is a replay
(5 F8); a replay returns the current task with no sign the state moved (4 F5); create and
update cannot report replay at all (8); no delivery id for a receiver to dedup on (7);
`record_dispatch_result` accepts a second terminal entry for one run (10 P2-4). The file
write is idempotent; nothing around it is.

**T6. "The server" is identified by a port.** CLI `status`/`stop`/`restart` (6 F2); the
plugin's `.mcp.json` hard-wired to 8765 where nothing listens (8 F4); the Vite dev proxy at
8765 (9 P3-12); CORS listing 8765 and 5173 for a deployment on 8876 (7); `restart` starting
the very second server the docs warn about (6). `/api/version` already answers the question
(`source_root`, `source_commit`, `started_at`) and nothing on the CLI side asks it.

**T7. The harness fights the repository.** The background-session preamble demands
`EnterWorktree` and refuses writes until it is called; the repo forbids it (1 F-2; auditors
1, 4, 11 each routed their file through `cp`). The write guard refuses a heredoc because
the prose contains a task path (1 F-12, 4, 8 F4, dispatcher log). `TaskCreate` nudges fire
four times in a read-only session against GLOBAL-AGENTS's rule (1 F-13). Every one of these
is resolved per session, by each session, from scratch.

**T8. Static context restating dynamic payloads.** The finish escalation, the wake
contract and the supervisor protocol are each delivered to the session that needs them
by the record or the prompt — and each is also restated in the @-chain at 200-600 words,
loaded by every session including the ones that cannot hit the path (1 §5). Auditor 1's
short answer: the split is right in principle and wrong in proportion.

---

## 5. Coverage honesty — what this audit is NOT evidence about

All twelve auditors completed; no stub files. Batch 4 (auditors 5, 6, 8) was initially
cut under the limit policy at 92% weekly usage and then run by Jeff once the meter allowed,
so their files are several hours younger than the rest (12:48–13:36 vs ≈00:00–00:45).
Auditor 2 used four read-only subagents for the four largest documents and re-checked
every P1/P2 they surfaced. The brief's "read-only" was read as "no execution" by most
auditors; Jeff has since ruled that too tight (task-242 log, decision entry 9).

**Nothing dynamic was reproduced for:** the non-atomic write window (4 — constructed from
code; a 20-line script would settle it); CSRF, SSRF, init-into-any-directory, stored XSS
(12 — verified server-side, never exploited); the PWA cache and update behaviour on a
device (9 — argued from headers and the SW spec); `serve`/`open`/`restart`/`stop` (6 —
not run, they mutate processes); any MCP mutation that could succeed, including
`operation_conflict` and `revision_conflict` live (8); any POST to the live server (7).

**Not examined at all, by anyone:**
- The tailnet ACL contents — whether `svc:agentjobs` is reachable by all four peers or a
  subset. Auditor 12 calls this "the single most important number I do not have."
- The `jobsearch` proxy on 8766 and `C:/ai/shared/launchers/` beyond one grep (12).
- Windows file ACLs on `~/.agentjobs/` and the tsnet state directory (12).
- Dependency CVEs in `poetry.lock` / `package-lock.json` (12).
- `finish.py` in full (1500 lines) — 12 read the git argv composition and restart plumbing,
  10 read it for the lifecycle, nobody read `verify_base`.
- `api/routes/web.py` (legacy Jinja, 374 lines) for correctness (7) — moot if task-275
  deletes it.
- The harness system prompt as static context; whether `CLAUDE.md`'s HTML comment is
  really stripped; MCP tool input schemas as loaded context (1).
- `docs/agent-dispatch-design.md` §8 and §11 — read by 2's subagent only; §2a (bounded
  autonomy) taken at its "nothing implemented" word (10).
- `docs/agent-workflow.md`'s parent-task protocol, which the supervisor stub points at (10).
- Whether `claude --bg` accepts a per-session environment — the fix for rank 6 depends
  on it (10).
- Whether `"worktree": {"bgIsolation": "none"}` actually disables the harness guard (1).
- Whether Claude Code honours a per-plugin MCP env override (8).
- Whether `--resume` of a session under one cwd works from another project root (10).
- Poetry's base-interpreter resolution with a foreign venv first on PATH (11).
- A spec-by-spec shared-state audit of the 8 Playwright files — required before e2e
  workers > 1 (11).
- The 13 non-canonical task files' history — hand edits or an older writer (3, 6).
- `validation._check_paths` on Windows junctions (3); PATCH `spec` replacing the whole
  block (3); `DispatchOutput.tsx` tail polling while a reader has scrolled up (9);
  `explain_next` under a `priority` filter (5); lock contention during a 57-file rebalance
  (5); `scripts/review_queue_sandbox.py`, `bench.py`'s queue usage, `run_report.py` and
  `finish_sandbox.py` bodies (5, 11).
- No timing measurements of the gate or any test by any auditor — every figure in the
  gate-speed analysis is derived from ENGINEERING's table and two ledger records (11).
- The frontend's `generated/` client (≈10k lines) beyond its config (9); the Python client's
  method-by-method parity (6, 7 partially).
- Other projects' `AGENTS.md` chains loading the same GLOBAL-AGENTS block (1).
- The 61 run directories were read for `meta.yaml`; `stdout.log`s only where named (10).

Plus the eight unanswered cross-auditor questions in §3.

---

## 6. The weekly-ritual verdict — what to do differently next time

The audit worked. Twelve files, ~380 KB, 9 P1s of which six are real defects nobody had
hit, one exposure that needed saying out loud, and a contradiction seam that produced the
best-evidenced finding of the night (rank 7, four witnesses, one reproduction). Do it again.
Change these:

1. **Drop the execution ban; keep the mutation ban.** Already Jeff's ruling (task-242
   decision 9). The cost was visible: auditor 4's P1 is "constructed, not reproduced" and
   says a 20-line script would settle it; auditor 12's four exploitable findings are
   "verified from code, not by a successful exploit"; auditor 9's two P2s rest on the SW
   spec. Next preamble: no source edits, no task mutations, no commits, one findings file;
   run focused tests, start throwaway servers on your own port, reproduce races in a
   scratch directory. Keep only the gate ban, with its reason.
2. **Fix the two harness fights before dispatch, not in the prompt.** Set
   `"worktree": {"bgIsolation": "none"}` in the repo's `.claude/settings.json` (verify it
   works — nobody has) and fix the write guard's content regex (task-276). Three auditors
   and the dispatcher paid for these; the synthesis prompt carried a workaround. A
   workaround in a prompt is a defect with a bow on it.
3. **Run the docs auditor last, with the other eleven files open.** C5 is the lesson: a
   docs-vs-reality pass that checks keys, signatures and links says "accurate" about
   documents whose sentences three system auditors then overturned. Either give auditor 2
   the findings files as input, or re-scope it to "for each sentence that makes a
   behavioural claim, find the code" and accept it covers fewer files.
4. **Name a contradiction owner.** The questions-for-other-auditors sections were the
   richest seam, and three of the questions got answered only because batch 4 ran after
   batch 3. Next time, a short pass between the last batch and synthesis in which each
   auditor answers the questions addressed to it in its own file — cheap, and it turns
   eight unresolved items into evidence.
5. **Commit nothing to a public remote until the report is read.** The plan said so; the
   dispatcher's commits were pushed anyway. Gitignore `audits/` by default, or keep a
   review gate on it — task-246 is the decision. Whatever is decided, the synthesis
   session's own commits from this run are local and unpushed.
6. **Give every auditor the live server and the ledger as first-class inputs.** The
   findings that cite a live GET, a `meta.yaml`, or `daemon.log` (5 F2/F3, 10 P1-1, 12 S-1,
   7 P1, 8 F1) are the ones nobody can argue with. The briefs implied it; say it.
7. **Weighted questions get weighted output.** The two Jeff-weighted questions (backend,
   gate speed) produced the two findings files with real analysis sections (4 §6, 11 Part
   A) and the two tasks most worth his own time (273, 268). Ask more of those and fewer
   "audit everything in this directory" briefs; the system auditors found their P1s in the
   first hour and spent the rest on P3s.
8. **Track the cut order's premise.** The plan budgeted for Fable draining the all-models
   pool and front-loaded the weighted auditors; batch 4 was in fact cut at 92% and
   recovered only because Jeff ran it by hand. The order was right. Next week start
   earlier or run four batches of three with the synthesis after the reset by default.
9. **Measure the audit itself.** Nobody knows what it cost in tokens or wall time per
   auditor; the batch-1 note says "~75 min", batch 2 "~20 min". `run_report.py` cannot see
   spawn-session children. If this is weekly, one line per auditor (start, end, bytes
   written, P1/P2 count) in the task log is the baseline the next one is judged against.

---

*Files in this directory: `PLAN.md` (the brief), `01-` to `12-` (findings), this report.
Draft tasks task-244 to task-276 are in the dashboard's draft band; promote what is real,
close what is not.*
