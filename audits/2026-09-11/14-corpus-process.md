# 14 — The corpus and the process

Big Dawg Audit II, night of 2026-09-11. Auditor 14. Written 2026-09-12.

**What was examined.** A consistent snapshot of the live `agentjobs` database, taken with
SQLite's backup API at 11:37 Central on 2026-09-12 into the session scratch directory,
and every query below ran against that copy. The store had moved since the runbook was
written: 416 rows, not 404 — 267 closed, 118 ready, 28 draft, 3 active. Twelve records
were filed between the brief and the snapshot (task-410 through task-420 and their
kin), which is itself a data point about the process.

Also read or run: `docs/task-corpus-audit.md`, `scripts/corpus_stats.py` (bare, and
against a fresh `agentjobs storage export`), `src/agentjobs/record_check.py`, the four
playbooks, `ROADMAP.md` against a fresh `export_roadmap.py`, and the read-only CLI
reports `queue check`, `next --why`, `quotations`, `branches`, `validate`,
`storage status`. Nothing was written to the store.

**Baseline records read first:** task-411, task-409, task-398 and its children, task-242
(the first audit), task-288/289/290 (the August groom and reorder runs), task-392
entries 24–27 (the September groom and reorder runs), task-405, task-406, task-413.

---

## Summary

The corpus is sound as a database and unsound as a process. The queue checks clean, the
schema's consistency rules hold by construction, decisions from the last fortnight are on
the record with their rejected alternatives, and the record-quality baseline has moved in
the right direction since 2026-08-27. But the things that need a **human** act have
piled up in every place a human act is required: 22 fully specified audit findings have
sat in `draft` for three weeks behind a prompt that says nothing; the September groom
and reorder proposals are parked on a closed task and nothing has acted on them; 45
completed tasks were closed with acceptance criteria still `pending`, 27 of them with
every criterion pending; and an answered question ("close it") has sat unexecuted for six
days. The tool records everything and executes nothing that it cannot execute itself,
and the process has no owner for the residue.

Findings are numbered F1–F16. Severity, evidence, verified-or-inferred, backlog status
and the fix are given per finding.

---

## F1. The twenty-two audit drafts are specified, not unspecced — the missing act is a promotion decision nobody owns

**Severity:** P2. **Verified by running** (queries over the snapshot; playbooks read).
**Status:** New. Partly raised as question 4 of the 2026-09-11 groom proposal
(task-392 entry 25), which sits on a closed task and was never answered.

**Evidence.**

- 51 tasks were created on 2026-08-22. Today: 26 closed completed, 3 ready, 22 draft.
  The 22 drafts are task-245, 246, 250, 253–263, 265–267, 271, 272, 274–276.
- They are not unspecced. Description lengths run 1,410 to 3,674 characters; every one
  has 2–4 acceptance criteria; all 76 of their `spec.context` pointers resolve on disk
  today. Auditor 01 can judge whether the findings are still true; on the record's own
  terms they are ready to promote.
- Every one carries the identical `ball_prompt` **"Finish specifying this task."** at
  `human`/`spec`. That string is in `record_check.DEFAULT_BALL_PROMPTS`
  (`src/agentjobs/record_check.py:97`) — the product already knows it is a placeholder.
  The write-path check for it fires only at `agent`/`work` (`record_check.py:265`), so
  a draft can carry it forever without a warning.
- 18 of the 22 have exactly one log entry (their creation). Across all 28 drafts, four
  have ever received a decision, instruction, progress or question entry.
- Promotions ever, by actor: the owner 10, claude 20, codex 1. Promotion days:
  2026-08-21 (12), 08-22 (3), 08-23 (5), then singles. No audit draft has been promoted
  since 2026-08-23.
- The first audit's synthesis handoff (task-242 entry 11) states the filing decision:
  33 clusters "each filed as a draft task... nothing promoted, claimed or started". The
  decision to promote was handed to the owner with no prompt naming what he was being
  asked to decide.
- `playbooks/groom.md` §1 says drafts are in scope and "a draft nobody will ever promote
  is exactly the kind of thing that accumulates" — but §5 limits groom's executable set
  to `{close-as-duplicate, close-as-superseded}`, and the September run declined to
  propose any draft because "a draft's premise is the thing only you can judge"
  (task-392 entry 25, Q4). `playbooks/flesh-out.md` §8 is the verb that promotes, one
  task per run; it has never been run (F15), and the two tasks that would make it
  reachable — task-299 (run it from the task page) and task-300 (triggers) — are
  themselves untouched since 2026-08-23.

**Is the draft band a queue or a landfill?** A landfill, by construction rather than
neglect. The filing decision put a human decision on 33 records at once, the prompt on
each says nothing, no surface lists "drafts waiting on you" as a decision queue, groom
will not propose them, and flesh-out cannot reach them. The problem is upstream of the
groom playbook: it is that filing a finding as a draft is treated as filing it, when it
is actually deferring a decision to someone who was never told the decision exists.

**Fix.**

1. A one-pass triage, per item, by the owner: promote / close `cancelled` with a
   sentence / fold into a live task. Twenty-two yes-or-no calls, and groom's Q4 already
   asked for exactly this.
2. The synthesis session of *this* audit should not repeat the pattern. File P1s as
   `ready` with a real spec; file P2s as drafts only with a `ball_prompt` that names the
   decision ("Promote, or close as not worth fixing? The cost is X, the risk is Y").
3. Product: a draft's prompt should not be allowed to be the placeholder for longer
   than a promotion window — a groom rule, or a `record_check` warning at `human/spec`
   once the record is older than N days.

---

## F2. The September groom and reorder results are on a closed task, and nothing has acted on them

**Severity:** P2. **Verified by running.** **Status:** Confirms task-406 (the cause);
the residue itself is new.

**Evidence.**

- task-392 entry 24 (decision, 2026-09-11): a dispatched run cannot execute groom or
  reorder, because `task.verb` and `task.queue` are own-task-only for a run principal.
  The run created task-405 as the groom run task and then could not write to it.
- task-392 entry 25: the groom proposal — three closures (task-349 duplicate of
  task-179; task-351 superseded by commit `e06b3562`; task-015 superseded by
  `1e20f09e`) and five questions. Entry 26: the reorder result — one move,
  `task-404 --before task-243`. Entry 27 calls task-405 "litter created by this run".
- Today, 23 hours later: task-349, task-351 and task-015 are all still `ready`
  (positions 9300, 15200, 450). task-404 is still at 10100. task-405 has one log entry.
  task-392 is closed and does not appear on any open-work surface.
- The two closed parents that groom's Q2 named — task-078-agent-loops (6 open
  children) and task-080-dispatch-model-profiles (1) — are still closed with open
  children. This confirms task-018 (still open, high band, position 328) three weeks on.

**Why it matters for the process.** Groom's design (§3, §8) rests on a proposal waiting
on the record for approval. When the record it waits on is a task that closes fifteen
minutes later, the wait is invisible. The one durable surface a run could reach was the
wrong one, and the human-facing surfaces (the review panel, `inbox`) never saw it.

**Fix.** Short term: re-home entries 25–26 onto task-405 (or dispatch a run at task-405
that can write to it) and decide the three closures and the one move. Structural:
task-406. Until it lands, a groom or reorder run should be started at its own run task
so the proposal is at least on an open record, and the playbook should say so.

---

## F3. Resumption Contract: 20 records, 9 resume cleanly, 5 would mislead a zero-context session

**Severity:** P2. **Verified by reading the records in full** from the snapshot.
**Status:** New as a measurement; the baseline in `docs/task-corpus-audit.md` measured
proxies (summary length, question rate), not resumability.

**Rubric.** A record passes if a session with no other context could (a) tell what the
task is from the summary, (b) tell what is needed *now* from `ball_prompt` (exempt at
`agent/available`), (c) tell what done means from `acceptance[]`, (d) reconstruct what
is done and what remains from the log, and (e) follow its context pointers. Partial =
resumable with a defect. Fail = a session would do wrong or wasted work.

| Record | Lifecycle | Verdict | Why |
|---|---|---|---|
| task-299 | ready | pass | full spec, 5 AC, pointers resolve |
| task-350 | ready | pass | needs task-092, now closed; AC concrete |
| task-387 | ready | pass | incident dated, options enumerated |
| task-396 | ready | pass (no AC) | fix is fully specified in prose |
| task-104 | ready | pass | ac-5 names a person by name; see F11 |
| task-137 | ready | pass | umbrella; children listed; 2 dangling pointers |
| task-414 | active | pass | exemplary: incident, model, children, decisions |
| task-413 | active | pass | complete review request, AC marked |
| task-092 | closed | pass | 7 AC met, 1 pending explained |
| task-106 | ready | partial | 3 of 4 context pointers dangling (F9) |
| task-314 | draft | partial | placeholder prompt, but a fresh decision entry orients |
| task-014 | closed | partial | all 6 AC `pending` on a completed task (F4) |
| task-384 | closed | partial | close entry says "all six met"; all six fields say `pending` (F4) |
| task-367 | ready | partial | open question (entry 3) since 09-06, ball `agent/available`, nothing surfaces it |
| task-053 | ready | fail | oldest ready task (45 days); asks for CLI verbs some of which shipped; record does not say which |
| task-063 | ready | fail | owner answered "Nothing — promote shipped, close it" on 2026-09-06; still open, ball `agent/answer` |
| task-342 | ready | fail | summary is the title; 260-char description; no AC; a reporter stub |
| task-250 | draft | fail | placeholder prompt at `human/spec` tells the holder nothing |
| task-262 | draft | fail | placeholder prompt, and the premise moved (F12) |
| task-015 | ready | fail | see below |

**Rate:** 9 of 20 pass (45%), 6 partial (30%), 5 fail (25%).

**The worst example's shape (task-015).** Nineteen log entries. Two dispatches on
2026-08-20, each followed by a `dispatch_result` whose body says the session is no
longer in the ledger and "whatever it did is in its own transcript, not here"; a release
"back in the pool"; a queue move. The fix shipped on 2026-09-06 as commit `1e20f09e`
(per groom's proposal) and the record does not know: it is `ready`/`agent/available` at
position 450 in `medium`, so `next` will hand it out once the head of the band clears,
and a session would rebuild a clamp that exists. The record survived the work; the
work's outcome never reached the record.

**Fix.** The two fails that cost nothing to fix are task-063 (execute the answer) and
task-015 (close superseded per the proposal). The pattern behind them — a decision made
in the UI that no agent is woken to execute — is task-384's subject from the other
direction and belongs with task-414.

---

## F4. Completed tasks are closed with acceptance criteria left `pending`

**Severity:** P2. **Verified by running.** **Status:** New — no record found by
full-text search for this.

**Evidence.**

```
completed tasks with acceptance criteria:            239
  ...with at least one criterion still `pending`:     45   (19%)
  ...with EVERY criterion still `pending`:            27
```

Closing actor for the 45: claude 18, finisher 17, codex 10. The scripted finish
(task-241) closes a task after a green gate and touches no acceptance status — 17 of
its closes are in this set. task-384's own close entry says "All six acceptance criteria
met; evidence in entries 6, 7 and 22" while all six rows read `pending`. task-014 is
the same shape.

**What this would have caught.** Nothing today, because nothing reads the statuses: the
finish does not check them, `close` does not require them, and the UI shows them as
decoration. That is the point — the field exists to make "done" checkable, and 19% of
the time the check was never made against it.

**Fix.** `close` with outcome `completed` should require every criterion to be `met`,
`failed` or `dropped`, or an explicit `--acceptance-unverified` with a reason that is
logged. The scripted finish should refuse to close a task whose criteria are all
`pending` and hand it back with "mark them or drop them". Cheap, and it turns
`acceptance[]` from prose into a gate.

---

## F5. Decisions do survive the work — for the last fortnight

**Severity:** P4 (sound). **Verified by reading** the five most recently completed
records' decision entries in full.

task-380 (entry 11), task-392 (entries 9 and 24), task-403 (entries 10 and 13) each
record what was decided, why, and the rejected alternative, in a form a stranger can
audit. task-392 entry 27 records that entry 9's judgement was wrong and where the
correction went (task-407). task-398, a parent, closes on a per-criterion verification
entry (entry 7). task-402 has no decision entry; its scope was mechanical.

Corpus-wide: 69 of 243 completed tasks have no `decision` entry. Many are small; the
number is here as a baseline, not a finding. The tool's premise holds where it was
tested.

One observation for auditor 11: 147 of 206 `dispatch_result` entries have a **null
body** — the outcome, duration and run id are in `data_json` only. Whether the log view
renders those legibly I did not check.

---

## F6. Queue health: sound structure, bucket-sized bands, tails set by accident

**Severity:** P3. **Verified by running** `agentjobs queue check` (clean) and queries.

- `queue check`: "The queue is sound: no problems in any band." No duplicate positions
  within a priority.
- Bands are priorities. `high` holds 62 ready, 18 draft, 1 active; the September reorder
  called it "a bucket rather than a band" and correctly declined to re-band.
- Of 118 ready tasks, 36 have a `queue_move` log entry with a written reason; 45 have
  never been moved by any event, native or reconstructed; 28 have exactly one log entry
  and one event since filing — never looked at. Thirteen of those 28 are older than
  five days: task-299, 300, 342, 345, 346, 349, 350, 351, 353, 368, 369, 370, 372.
- The heads reflect decisions: `high` positions 16–328 are owner anchors from 09-06/07;
  `medium`'s head is the owner's 2026-08-21 ordering. The tails reflect filing order:
  every position above ~12000 is as-filed at increments of 100.
- The `critical` band changed shape on 2026-09-12: nine of its eleven ready entries are
  task-414's children, re-prioritised and moved to positions 706–799 by codex that
  morning. `next --why` hands out task-264 first. task-411 (critical, as-filed at 800)
  and task-419 (1300) sit behind the whole durable-execution program. That may be
  intended; it was not recorded as a decision anywhere I found.
- `task_event.mechanical` is 0 on all 460 queue_move events, including the 246 on
  2026-08-21 that applied one approved ordering in bulk (42 log entries that day). The
  schema comment says this flag exists to keep such a reorder out of the activity
  series. Question for auditor 08 / the analytics work (task-372).

**Fix.** None for structure. For the tails: the reorder playbook's "graded effort by
queue depth" (§4) only ever reaches the heads; a periodic "untouched for N days" report
is what would surface the 13.

---

## F7. Age, staleness, tags and categories

**Severity:** P3. **Verified by running.**

Open-task age since filing (days, from 2026-09-12):

| | 0–7 | 8–14 | 15–30 | 31–45 |
|---|---|---|---|---|
| ready (118) | 47 | 2 | 67 | 2 |
| draft (28) | 2 | 0 | 26 | 0 |

Days since last activity: 59 open tasks have had no entry for 22–30 days; 45 more for
15–21. That is 104 of 149 open tasks (70%) untouched for over two weeks.

- **Oldest ready:** task-053-schema-v2-cli, filed 2026-07-29, 45 days. Still there
  because its parent (task-063) is the record the owner answered "close it" on, and
  nobody executed the answer (F3). Next oldest: task-104 and task-106 (30 days), both
  awaiting a human with a device, both at positions 4000+ in `medium`.
- **Ready tasks by filing week, none ever claimed:** week of 08-17: 58; week of
  08-31: 22; week of 09-07: 26. The August burst is the backlog.
- **Categories:** 30 distinct used; `.agentjobs/config.yaml` lists 6. 50 of 149 open
  tasks carry a category outside the configured six. Dead (0 open): performance,
  maintenance, design, deployment, core, cli, acceptance. Singletons with one open task
  and nothing closed: workflow, tooling, product_design, product, process, backend.
- **Tags:** 246 distinct, 104 used exactly once; 23 tasks untagged (11 open). Live and
  growing: `dispatch` (51 open), `frontend` (28), `big-dawg-audit` (25),
  `durable-execution` (6, all this week). Programs tagged and never started:
  `embedding` 12 open / 0 closed, `agent-loops` 6/0, `external-projects` 5/0,
  `voice-input` 4/0, `menu` 6/0. Retired: `codex` (12 closed), `queue-order` (12),
  `epic-066`, `phase-0`, `tasks-2-0`.
- **`effort`:** set on 77 of 149 open tasks, as free text — 39 distinct spellings for
  what is at most five sizes ("half a day", "half day", "a day", "1 day", "small once the
  cause is established"). Not a field anything can sort on.

**Fix.** Categories and tags are what `validate` used to police and no longer does
(F12). Either the config list becomes real (and the 50 get re-categorised) or the config
list is dropped. `effort` should be an enum or nothing.

---

## F8. Closed parents with open children — still true

**Severity:** P3. **Verified by running.** **Status:** Confirms task-018 (open since
2026-08-18) and groom Q2 (task-392 entry 25).

task-078-agent-loops: closed, 6 ready children (task-147 through task-152).
task-080-dispatch-model-profiles: closed, 1 ready child (task-156). task-018's summary
still cites task-060, whose children are all closed now; the defect it names has two
fresh instances and the record does not name them.

---

## F9. Dangling context pointers: 55 on open tasks, 88 on closed — task-411's "eighteen" is three weeks stale

**Severity:** P2. **Verified by running** (each pointer resolved against the working
tree, applying the test's own exemptions: URLs, globs, absolute paths). **Status:**
Confirms task-411; updates its number.

task-411 measured eighteen at `400f3b2a`, before task-380 retired the frozen
per-project task directory. That retirement turned every pointer into that directory
into a dangling one:

```
open tasks:    55 dangling pointers on 35 tasks   (50 of them into the retired directory)
closed tasks:  88 dangling pointers on 60 tasks
```

The directory still exists, empty, and is **not** gitignored (`git status --ignored`
lists nothing under it), so the test's `ignored_by_git` exemption
(`tests/test_task_corpus.py:189`) will not save them. When task-411's ac-1 lands, the
first green run needs ~143 corrections, not 18, and 50 of them are the same shape: a
pointer at another task's record file, written when tasks were files. The right rewrite
is not a path — it is a `related` dependency on that task id, which is what the pointer
meant.

**Fix.** Fold into task-411: (1) the closed-task decision it already names; (2) a
one-off script that turns a pointer at another task's retired record file into a
`related` dependency on that task where none exists, and drops the pointer. Then
re-measure.

---

## F10. `first_claimed_at` is NULL on 195 closed tasks that were claimed

**Severity:** P2 for the analytics work; P3 today. **Verified by running.**
**Status:** Probably new — not searched for by name on the backlog; auditor 02 should
confirm.

```
tasks with first_claimed_at set:                         23
tasks with a `claim` event but first_claimed_at NULL:   197   (195 of them closed)
native claims since the cutover:                         22   → all 22 have it set
```

The importer reconstructed 207 claim events from the log but did not populate the
denormalised column from them. The schema's comment says these columns are "written in
the same transaction as the thing that changes them, so they cannot drift" — true going
forward, false for all of history. task-372 (the analytics API, untouched since filing)
promises cycle-time answers; on this column they would be computed over 5% of the
corpus. `scripts/run_report.py` and anything else measuring claim-to-close is affected
the same way.

**Fix.** A backfill from the earliest `claim` event per task, marked `source:
reconstructed`, before task-372 starts.

---

## F11. The record-quality baseline, regenerated today

**Severity:** P3. **Verified by running** `scripts/corpus_stats.py --tasks-dir` over a
fresh `agentjobs storage export` (417 records). **Status:** Confirms task-396; refutes
one sentence in task-380's decision entry 11.

- **Bare `corpus_stats.py` is worse than task-380 recorded.** Its entry 11 says the
  script "now refuses with 'Not a directory'". It does not: against the retired,
  empty directory it prints `Records: 0 readable, 0 unreadable`, a full table of zeros,
  and exits 0. task-396 (low, position 700, untouched) describes the defect correctly
  and is the fix.

Today against the 2026-08-27 baseline in `docs/task-corpus-audit.md`:

| Measure | 2026-08-27 | 2026-09-12 |
|---|---|---|
| Records | 315 | 417 |
| Log entries | 2,549 | 4,205 |
| `question` per 100 entries | 1.2 | 1.5 |
| `answer` per 100 entries | 0.4 | 1.1 |
| Records carrying a question or answer | 24 (8%) | 34 (8%) |
| Summaries over 40 words, all | 209 (66%) | 243 (58%) |
| Summaries over 40 words, era 186– | 111 of 133 (83%) | 144 of 235 (61%) |
| Median summary words, era 186– | 60 | 46 |
| `default_ball_prompt` sweep | 1 | 0 |

The newest era's summaries got shorter; answers caught up with questions. The
`default_ball_prompt` zero is misleading — see F1: 22 drafts carry the placeholder,
and the check is scoped so that it cannot see them.

**`record_check` guards one door of three.** `check_record` has exactly one caller,
`src/agentjobs/mcp/mutation_tools.py:492`. A task created through the REST API (the
browser's create and Report-issue forms) or the CLI is never checked. The six open
records whose summary is their title word-for-word — task-342, 353, 381, 385, 389,
395 — came in through those doors; groom's Q5 named them; they are still that way and
five of them are on the public roadmap (F14).

**Fix.** Call `check_record` from the manager's write path, not from one client. Fix
task-396. Add the `summary == title` case to `record_check` — it is the cheapest
possible test for "was this specified at all".

---

## F12. `agentjobs validate` checks nothing on a SQLite project — task-262's premise moved, and its substance has no checker now

**Severity:** P3. **Verified by running.** **Status:** Refutes task-262 as written;
the underlying facts stand with nothing enforcing them.

```
$ poetry run agentjobs validate
A project's records are rows in its database, so `validate` has no files to read. ...
  The database enforces every consistency rule as a constraint ...
exit 0
```

task-262 (draft) says validate is red with 221 problems: 124 unknown actors, 84 unknown
categories, 13 non-canonical files. The constraints in the `task` table enforce the
state rules; they do not enforce the actor vocabulary or the category list. Today the
log carries actors `finisher` (421 entries), `dispatcher` (284), `system` (82),
`git-backfill`, `Claude`, `jeff`, `Codex`, `human` — none of them in
`.agentjobs/config.yaml`'s three — and 50 open tasks carry categories outside its six
(F7). The 13 non-canonical files are gone with the files. So the number is not 221 and
never will be again; it is "zero, because nobody is counting".

**Fix.** Decide what the config's `actors` and `categories` lists are for. If they are
a vocabulary, `validate` should read the store and report against them; if not, delete
them and the sentence in task-262 that promises otherwise. Close task-262 with that
decision, or re-spec it.

---

## F13. ALLAGENTS.md says `queue check` exits non-zero; it exits 0

**Severity:** P4. **Verified by running.** For auditor 12/13.

`ALLAGENTS.md:31`: "or `agentjobs queue check` exits non-zero". `queue check --help`:
"Exits 0 even when it finds problems ... `--strict` is the CI form".
`docs/api-reference.md:197` has it right. An agent following ALLAGENTS would treat exit
0 as a clean queue.

---

## F14. The public face: `ROADMAP.md` is stale on `main` right now, and reads as a list

**Severity:** P3. **Verified by running** `export_roadmap.py` to scratch and diffing.
**Status:** Confirms task-407; confirms task-413's premise (its fix is on a branch in
review).

- Diff against the store at 11:37 Central: `## High (63)` → `(64)`, task-420 added,
  task-413 marked *in progress*. Eleven lines. The `roadmap` gate stage would be red on
  an unchanged `main`, which is exactly task-407's claim — and task-392's entry 27
  reported the same thing the day the feature shipped.
- To a stranger: 121 bullets under four priority headings; 28 drafts counted in one
  sentence at the bottom; umbrella children listed flat beside their parents; five
  entries whose summary is their title. Nothing says that a third of the backlog is one
  program (dispatch, 51 open) or that a whole program (embedding, 12 tasks) has never
  started. task-413 addresses all of this and is waiting on the owner.

---

## F15. Playbook runs are indistinguishable from ordinary dispatches in the run ledger

**Severity:** P3. **Verified by running.** For auditor 04.

`task_run.playbook` and `playbook_hash` exist in the schema. Both are NULL on all 214
rows. None of the 214 `meta.yaml` files under the runs directory carries a `playbook`
key. The three groom/reorder passes on record (task-288/289/290 in August, task-392 in
September) were run by hand or as ordinary dispatches; `flesh-out` has never run. Whether
`agentjobs playbook run` writes the column I did not test; nothing in the ledger says it
has ever been called.

---

## F16. The write guard refuses a read-only command that names a task path — task-276 confirmed live

**Severity:** P3. **Verified by running.** **Status:** Confirms task-276 (draft, one
log entry since 2026-08-22).

A `git check-ignore` on one retired task-record path, combined with a Python query
against my scratch snapshot, was refused with "an interpreter invoked against a managed
task path would write AgentJobs task records". The directory it named is empty and
retired. The first draft of this very file was refused by the same guard for containing
the path pattern in prose. The brief warned that this cost two auditors their final
minutes last time; it is still there, three weeks later, as a draft with a placeholder
prompt (F1).

---

## Other observations (P4)

- The runbook's pre-flight said to back up the database. Three dated backups with
  manifests exist beside the live file (09-09, 09-11, 09-12). Also beside it: eight
  `local-agentjobs-*.db` / `local-tasks-*.db` files with live `-wal`/`-shm` sidecars.
  Sandbox litter, or something's live state? For auditor 02.
- `agentjobs branches` is clean: one branch in flight (task-413), nothing left behind.
  But `task_branch` has `status: active` on two closed tasks (task-116, task-157) —
  records that outlived their branches, harmless and wrong.
- `agentjobs quotations` reports none across 417 records. Groom's Q3 says task-179 and
  task-349 carry verbatim quotations of a named person in `spec.description`. task-104's
  ac-5 names the owner. I did not adjudicate the detector; auditor 07.
- The `dispatch_result` entries on task-015 ("whatever it did is in its own transcript,
  not here") are the tool admitting its premise failed. task-414 exists because of a
  later instance of the same thing.

---

## What I did not get to

- The other four projects' corpora (fantasy-football, job-hunting, mastercalls,
  product-strategy). Everything here is the `agentjobs` project only.
- The 88 dangling pointers on closed tasks — counted, not enumerated.
- The four playbooks' full text. I read the sections cited and the headings; I did not
  audit reorder.md's anchor rules (§6) against what the September run did.
- Whether the React log view renders the 147 null-body `dispatch_result` entries.
- task-410's own record and the audit dispatch's runs — deliberately, per the brief.
- A read of every completed task's decision entries; five were read in full, the rest
  counted.
- The `task_event.open_delta` series and whether the backfilled/reconstructed events
  reproduce the true open count over time — auditor 02 or 08.
- `agentjobs search` as a CLI. I used the FTS table directly against the snapshot.
- Whether `agentjobs playbook run` populates `task_run.playbook` when it is used.

## Questions for other auditors

- **02 (SQLite):** Why did the importer leave `first_claimed_at` NULL for 195 claimed
  closed tasks (F10)? Are the eight `local-*.db` files beside the live databases
  sandboxes that were never cleaned up, and does anything still hold them open?
- **04 (dispatch/finish):** Does the scripted finish ever touch `acceptance[]` (F4)?
  Should it refuse to close a task whose criteria are all `pending`? Does
  `playbook run` write `task_run.playbook` (F15)?
- **05 (gate):** The `roadmap` stage is red on `main` as of 11:37 Central without any
  commit (F14). Is task-407's proposed fix the right shape, and does `--since-gate`'s
  receipt on `main` currently attest to a stale roadmap?
- **07 (schema/validation):** `validate` is a no-op on a SQLite project (F12). Is that
  the intended end state, or is a store-reading validator still planned? Does
  `quotations` miss the description shape groom's Q3 describes?
- **08 (manager/queue):** All 460 `queue_move` events have `mechanical = 0` (F6). Was
  the flag ever written? Is a band that is a priority the intended design, or a
  placeholder for real bands?
- **11 (frontend):** Is there any surface that lists drafts as decisions waiting on the
  owner (F1)? How does the log view render a `dispatch_result` with a null body?
- **12/13 (docs/context):** ALLAGENTS.md:31's exit-code claim (F13). task-380 entry
  11's sentence about `corpus_stats.py` refusing (F11) — a decision entry that
  misstates what the code does is the kind of drift F5 says is not happening.
