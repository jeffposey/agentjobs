# 04 — Dispatch, finish, and the merge runway

Big Dawg Audit II, night of 2026-09-11. Auditor 04. Read-only in the main clone at
`096f33ae`; nothing written to the live store, the ledger, or the tree. All times UTC unless
marked.

**Method.** I read `finish.py`, `ledger.py` (locks and runway), `finish_status.py`,
`record_commit.py`, `gate_slots.py`, the approve route, the CLI `finish` command, the
machine's `dispatch.yaml`, the restart launcher, and the 145 finish records under
`~/.agentjobs/finishes/`. I ran two throwaway probes, the focused finish tests, and the
roadmap check. Three areas were read by delegated sub-audits whose probe scripts and
file:line evidence I relay below with their own verified/inferred labels: guards and the
tool-level write hook, the epic walk, and registration/poller/ledger. I did not re-run
their probes; where a label says "delegated" that is the provenance.

**One line on what is sound.** Every stop the finish makes *itself* writes the right
sentence: across 145 records, all 49 escalations carry either "Nothing was merged" or
"The merge is done" correctly, including the three that stopped after merging. The
failures below are what happens when something *else* stops the finish, and what the
runway and the walk do around it.

---

## F1 — P1 — A run that finishes itself is killed at its shell tool's ten-minute ceiling, mid-delivery

**Label: New** (adjacent to task-322 and task-414, which describe resuming; neither names
this cause).

**Evidence.**
- `runner.py:239-244` (`AUTOMATIC_CLAUSE`) tells an autonomous run to *run*
  `agentjobs finish <task> --posture-release` from the project root. `epic.py:697` says
  every epic child lands "inside their own `agentjobs finish`". ALLAGENTS.md says the
  same. Nothing says to detach it or background it; `cli.py:3058-3130` runs the whole
  sequence in the foreground of that process.
- The sequence is rebase + full gate (231–451 s measured under contention) + runway wait
  (up to 3600 s) + merge + rebuild + restart + verify (up to 120 s). The harness's shell
  tool has a hard ceiling of ten minutes and a default of two.
- **10 of 145 finish records on disk have `outcome: running` and no `finished_at`.**
  Read-only scan of `meta.yaml` (script inline in my session). The two that died *after
  the merge*: `fin_d11a8f5e` (task-321, started 16:52:12, last write `restart.log` at
  17:02:10 — 9 min 58 s) and `fin_2f382546` (task-329, started 04:01:03, last write at
  04:09:58, no `verify` step; `phases.jsonl` ends after `restart ok`). Two more died
  under two minutes in (`fin_4e711482` task-239 at 1:58, `fin_7e343a88` task-337 at
  1:47), consistent with the two-minute default. Three more died at 7–8 minutes in the
  pytest stage.
- The run that started `fin_2f382546` says so in its own transcript
  (`~/.agentjobs/runs/run_43c2a909/transcript.log`): the finish merged successfully and
  the agent's shell call hit its ten-minute ceiling while the finish was in delivery.
  That sentence is the agent's, paraphrased.
- What the record looked like meanwhile: the task-329 log has the finisher's "Merged …
  as `e26a460b` … this task stays open until it is verified" entry at 04:09:50 and then
  nothing from that finish. The ball was `agent/work` on a live run, so the run retried
  at 04:12:09 (`fin_506d9094`), which **re-ran the full gate for eight minutes on a
  branch already in `main`**, returned the earlier sha through `previous_merge_commit`
  (`finish.py:1115-1128`), and wrote a *second* "Merged … as `e26a460b`" entry at
  04:19:50 before closing. The record now states one merge twice.
- `finish_status.py:397-428`: for a run finishing itself the finish takes no lock
  (`finish.py:1954`), so the lock holder is the live run, `holder_is_working` is true,
  and the dashboard reports the dead finish as `running` until the run ends, then
  `interrupted` with no merge commit — while the record says merged.
- `docs/agent-dispatch-design.md:2070-2076` promises "the task is never left reading
  `agent` with nothing dispatched" and that a stop always writes the dispositive
  sentence. Neither holds for an externally killed finish.

**Verified by running** (the on-disk scan and the transcript grep); the ceiling itself is
the harness's documented limit, not something I reproduced tonight.

**Consequences tonight.** Any autonomous child of an epic that queues on the runway for
more than a few minutes, or whose gate runs long under contention, will be killed after
the merge and before the close. The retry path recovers it at the cost of a second gate,
a duplicate merge entry, a `running` finish record nobody reconciles, and (inferred, see
F6) a gate-slot file that throttles every following gate for up to 30 minutes.

**Fix.** `--posture-release` should do what the approve route does: write the marker,
`spawn_finish` itself detached, print the finish id, and return immediately; the run then
waits on the *record* (the ball moving, or `finish_step: merge`/`close` entries), which
is what ALLAGENTS.md already says the signal is. Until then, `AUTOMATIC_CLAUSE` must tell
the run to start the command in the background and wait on the record, not on the
process. Reconcile the ten `running` records the way `finish_status` already infers
interruption, and have a retry that finds a prior merge *not* rewrite the merge entry.

---

## F2 — P2 — Two finishes can both hold the merge runway after reclaiming one stale lock

**Label: New.**

**Evidence.** `ledger.py:522-527`: when a contender judges a lock stale it releases it
with `RunLock(task_id=…, path=path, run_id=holder.run_id).release()` — no `kind`, no
`finish_id`. `RunLock.release` (`:407-435`) refuses to delete a lock that names a
*different* run or finish, but a runway holder's `run_id` is always empty and the
reclaiming `RunLock` carries no `finish_id`, so neither guard fires and the delete is
unconditional. Interleaving, which the 1 s poll in `acquire_runway_lock` (`:560-628`)
permits: B reads the stale holder; A reclaims and takes the runway; B's release deletes
A's fresh lock; B takes the runway. Probe
`C:/Users/jpose/.claude/jobs/0987ec11/tmp/runway_race.py` on a throwaway home printed:

```
A holds : LockHolder(pid=350260, kind='runway', finish_id='fin_A', …)
after B's reclaim, file exists: False
B holds : LockHolder(pid=350260, kind='runway', finish_id='fin_B', …)
```

Both then rebase, gate and merge against a base the other is moving — the exact property
`Runway` exists to guarantee (`finish.py:1543-1567`). The same hole applies to a per-task
finish lock (`KIND_FINISH`, empty `run_id`).

**What would have caught it.** `tests/test_dispatch_finish.py:2202`
(`test_releasing_does_not_delete_a_runway_somebody_else_reclaimed`) tests the *holder's
own* release, which does carry a `finish_id`; the reclaim path is untested.

**Precondition.** A stale runway lock (a finish whose pid is gone — F1 makes that
routine) and two finishes polling the same second, which is the epic's normal landing
pattern. The window is milliseconds, so I rate it P2 rather than P1.

**Verified by running** (the interleaving is scripted, not raced).

**Fix.** Reclaim by conditional delete: re-read the holder and unlink only if its full
text equals what was judged stale. Or pass `kind` and `finish_id` from the judged holder
into the reclaiming `RunLock` so the existing guards apply.

---

## F3 — P2 — An escalation after the restart is written through the server it just restarted, and is unguarded

**Label: New** (a road to task-340's failure that task-390 did not close).

**Evidence.**
- `cli.py:3107`: the finish's manager is `task_manager_for(project)`, which for a project
  on SQLite is `RemoteTaskManager` over HTTP (`store_factory.py:245-257`). The machine's
  `dispatch.yaml` sets `api_base` and `finish.verify_base` both to `127.0.0.1:8876`, and
  `finish.restart` restarts that same server (the launcher runs `agentjobs restart
  --port 8876`, which `taskkill /F` the old pid without `/T`, so the finish survives — a
  point in its favour).
- `finish.py:1298-1310` (`restart_failed`) and `:1432-1441` (`not_live`) raise
  `Escalate` when the server did not come back. The handler at `:2004-2010` calls
  `escalate_on_record` — `add_log_entry`, `get_task`, `handoff` over HTTP — with **no
  try/except**; only the later `park_for_human` is guarded (`:2054-2062`). The client
  retries connection failures for 15.75 s total (`client.py:56`, backoff 0.25…8 s) then
  raises `ServiceUnavailable` (`:897`).
- So: server down after the restart → escalation write raises out of the `except
  Escalate` block → `finally` releases the locks → the CLI exits with a traceback. On
  disk `meta.yaml` says `escalated` (written at `:1996` before the write). On the record:
  "Merged … stays open until it is verified", ball `agent/work` from the approval
  (`api/routes/tasks.py:781-786`), no run dispatched, no human park. Open task, ball on
  nobody, `main` moved.
- `mark_branch_merged`'s docstring (`finish.py:1514-1530`) states that the manager is
  remote whenever the project is on SQLite, so this is known and deliberate;
  `dispatch_manager_for` (`store_factory.py:261-300`) exists precisely so the dispatch
  family does not depend on the server, and the finish is the one dispatch-family verb
  that does not use it.

**Inferred from reading.** Not reproduced: it needs a restart that leaves 8876 down, and I
was forbidden that server. One `not_live` escalation exists on disk (task-337,
2026-09-05) and it succeeded in writing, so the server was up in that instance.

**Fix.** Build the finish's manager with `dispatch_manager_for` (local SQLite, WAL) as
the rest of the dispatch family does; or guard `escalate_on_record` like
`park_for_human`, fall back to a local manager, and record `escalation_write_failed` in
meta so `finish_status` can show it.

---

## F4 — P1 tonight, already filed — `ROADMAP.md` is stale on `main` right now, so every finish gate tonight goes red at `roadmap`

**Label: Confirms task-407** (fresh evidence on today's tree).

**Evidence.** `poetry run python scripts/export_roadmap.py ROADMAP.md --check` at
03:42 UTC printed "ROADMAP.md is stale; run … and commit the result." The `roadmap`
stage (`scripts/check.py:348-351`) runs exactly that check inside the finish's gate, in
the branch's worktree, against the live store. The roadmap projects id, title, summary,
lifecycle and relations of every open task (`roadmap.py:116-178`), so any task filed or
closed anywhere since `096f33ae` — which happened during this audit's setup — turns the
stage red for a branch that never touched it. The finish then escalates `gate_failed` and
dispatches a session to "fix" a red gate nothing on the branch caused.

**Verified by running.**

**Fix (task-407's, restated).** The finish should regenerate `ROADMAP.md` on the rebased
branch before gating, or the stage should not run inside a finish. Tonight: regenerate and
commit `ROADMAP.md` on `main` before approving anything.

---

## F5 — P2 — The gate is the finish's dominant stop, and two known flakes account for most of it

**Label: Confirms task-325, task-348, task-370.** Contradicts the reading that task-390
closed task-370.

**Evidence.** Of 145 finishes: 78 finished, 32 stopped at a red gate (30 `gate_failed`,
2 `catch_up_gate_failed`), 9 `base_moved`, 5 `rebase_conflict`, 7 declined
`no_active_branch`, 2 `unexpected_error` (both task-388's datetime bug, since fixed),
1 `not_live`, 10 dead (F1). Scanning the 32 red gate logs (read-only, ANSI stripped):

| Failing test named in the gate log | red finishes |
|---|---|
| `TestProcessGroup::test_the_timeout_kills_the_grandchild_too` (task-325) | 12 as the FAILED line; the name appears in 21 of 32 logs |
| `TestDispatchRuns::test_cancelling_a_live_run_stops_it_and_marks_it_cancelled` (task-370) | 5 |
| `TestRealCorpus::test_the_tolerated_drift_is_only_taxonomy_and_serialization` | 2 |

The task-370 test failed a finish gate on **2026-09-07 22:59, the finish of the branch
carrying task-390's fix**, and on task-388's finish the same evening. So task-390 did not
make it pass under load; the finish for task-390 itself had to be retried. Each red gate
costs a dispatched run (task-348's premise).

**Verified by running** (the scan).

**Fix.** task-348's re-rebase-and-rerun-once is the right shape, but only for a stage
whose failure names a test on the flake list; land task-325 first. Keep task-370 open.

---

## F6 — P3 — A gate that exceeds its timeout orphans the gate's children and leaves no log

**Label: New.**

**Evidence.** `run_command` (`finish.py:360-395`) is `subprocess.run(timeout=…)` and
writes `gate.log` only after it returns. `gate_timeout_seconds` defaults to 3600
(`config.py:646`). On expiry `subprocess.run` kills the direct child (the `check.py`
interpreter) and raises `TimeoutExpired`; on Windows the pytest workers, node and
Playwright it spawned are not in a job object and keep running. Nothing in `finish.py`
or `tests/test_dispatch_finish.py` names `TimeoutExpired`; `_guarded_sequence`
(`:2267-2296`) turns it into an `unexpected_error` escalation with no `gate.log` to point
at. `gate_slots.py:128-145` holds a slot file for the gate's lifetime and clears it by age
only (`STALE_SECONDS = 1800`, `:63`), so a killed gate — by timeout here, by F1's ceiling
more often — throttles every following gate's worker count for up to 30 minutes.

**Inferred from reading.** No slot litter was on disk when I looked (03:40 UTC).

**Fix.** Run the gate under the same process-group kill the runner uses
(`TestProcessGroup` exists for that code), write the log on the timeout path, and have
`gate_slots.hold` also drop a slot whose pid is gone.

---

## F7 — P2 — Approve on a task with no recorded branch does nothing and tells nobody; a live task is in that state tonight

**Label: Confirms task-355** (fresh evidence).

**Evidence.** `agentjobs branches --project agentjobs` at 03:43 UTC:
`docs/task-413-roadmap-phases 0.0d – no task lists this branch` beside a worktree at
`C:/projects/worktrees/agentjobs-413`; task-413 is active, claimed interactively
(`run_8a84f426`, `origin: claimed`). `api/routes/tasks.py:718-724`: approve hands the
ball to `agent/work`, spawns the finish, and returns without auto-dispatch. The finish
declines `no_active_branch` (`finish.py:807-811`) and a decline writes nothing to the
record (`:1988-2003`). Result: an approved task reading `agent/work` with no run, no
finish and no note. Seven of the 145 finish records on disk are exactly this decline.

**Verified by running** (the branches report); the approve path is read.

**Fix.** The approve route should refuse, or fall through to the handback path, when
`active_branches(task)` is empty — the finish already computes that in preflight.

---

## F8 — P3 — The finish's own comments describe a base that no longer moves the way they say

**Label: New** (documentation drift from task-402).

**Evidence.** `record_commit.py` since task-402: "a record is a row now … nothing to
commit"; `commit_task_record` always returns "nothing to commit". `finish.py` still calls
it five times and still explains `catch_up` (`:1002-1030`), `contains_commit` (`:1223`),
`verify_live` (`:1373`), `CATCH_UP_ROUNDS` (`:154-155`) and the runway announcement
(`:1594`) in terms of "the finisher commits the task record to the base" and "other
sessions committing task records". None of that happens; a `base_moved` today is a code
merge, and `catch_up`'s only remaining absorbable case is prose. The `Runway.take`
comment says its note is "deliberately not committed" for a reason that is gone.

**Inferred from reading.** Cosmetic in effect; misleading to the next reader of the
`base_moved` refusal, whose message (`:1157-1166`) still tells them the catch-up handles
record commits.

---

## F9 — The epic walk (delegated sub-audit; probes at `…/tmp/probe_walk.py`; `tests/test_dispatch_epic.py` 58 passed in 7.3 s)

**F9.1 — P1 — A restarted walk reports live children as a deadlock and hands the parent
to a human.** `epic.py:870` builds the frontier from claimable tasks, which excludes an
active child; in-flight children live only in memory (`:777`); so a walk restarted while
a child is working returns `no_eligible_child` (`:934-977`), and the CLI hands the parent
to `human/decision` and exits 1 (`cli.py:1726-1743`) while the child keeps running.
*Verified by running (delegated probe A).* **Confirms task-418.** Fix: adopt any active
child with a live run carrying this epic's marker into `in_flight` before computing the
frontier.

**F9.2 — P2 — The bounded retry is effectively unreachable.** `DIED` requires terminal
run status *and* `ball is AGENT` (`epic.py:1039-1052`), but every settle path writes the
terminal status then a `human/decision` handoff (`runner.py:2870-2883, 2943-2949,
3210-3240`), so the walk sees `PARKED` unless its 15 s poll lands between two writes —
and then retries a child the poller is parking. The only retry test (`:593`) scripts a
state the poller never produces. ALLAGENTS.md's "enforced rather than promised" is not
what the code does. *Inferred from reading.* **New.**

**F9.3 — P2 — Stopping does not spend a dispatched epic's authorisation.**
`resolve_epic_authorization` (`epic.py:273-286`) reads the newest `dispatch` entry's
`caused_by`, which a handoff cannot move; with the parent's ball on `human` the walk still
started children (`started: ['task-002']`). The design doc says the opposite
(`agent-dispatch-design.md` ~745). *Verified by running (delegated probes B, C).* **New.**

**F9.4 — P2 — A spawn failure escapes the walk uncaught.** `DispatchRunError`
(`runner.py:1147`) is neither `DispatchRefused` nor `DispatchError`; `walk_epic` catches
only the former (`epic.py:889-911`), the CLI only the latter (`cli.py:1698-1704`): a
traceback, no progress entry, no handoff, in-flight children unwatched, and the attempt
already spent because the marker is written before the spawn (`guards.py:1005` vs
`:1036`). *Inferred from reading.* **New.**

**F9.5 — P3 — Cooldown eats the one retry.** `cooldown_seconds` (60, `config.py:804`)
binds trigger `CHILD`; a child that dies inside a minute gets `BudgetCapError`, which the
walk treats as `COULD_NOT_START_CHILD` and grounds. *Inferred.* **New.**

**F9.6 — P4 — Grounding rule matches the doc** (closed-not-completed and parked
`human/review` both ground; in-flight siblings are watched down, `:787-798, 856`);
`--dry-run` starts and writes nothing (`cli.py:1677-1688`); exit codes as documented.
The walk's own supervising run holds one of the `max_concurrent_runs=3` slots, so an
epic gets two children in the air, and **a scripted finish registers no run at all**, so
task-352's "a finish is a run: count it" is not reflected in the walk's slot arithmetic
(`guards.py:912-930`). *Inferred.* Question for the frontend auditor, below.

---

## F10 — Run registration, poller, ledger (delegated sub-audit; probes at `…/tmp/race_demo.py`; poller/registration/atomic_yaml tests 61 passed)

**The 3 a.m. answer first.** A session that parks on a permission prompt is on the record
as `human/input` within about 10 s (`poller.py:45`; `runner.py:2694-2737`). Nobody is
woken: the poller is started with no managers (`api/main.py:162`), builds
`dispatch_manager_for` with `webhook_manager=None` (`poller.py:174`,
`store_factory.py:265`), and `TaskManager._fire` is a no-op without one
(`manager.py:2814-2816`). So parked, auth-stalled, stalled and finished-without-handoff
all fire no `task.handoff` webhook. *Inferred from reading.* **Confirms task-265**;
task-417's "deduplicated wake" has no wake to deduplicate yet.

**F10.1 — P2 — Lost updates on `meta.yaml`.** Two processes doing
`RunDirectory.update_meta` (`runner.py:1077-1088`, read-merge-`write_yaml_atomically`)
against a throwaway home: n=50 → 49 lost; n=200 → 197 lost; both exit 0. There is no lock
on the run directory (`atomic_yaml.py:41-47` says so). Writers on one file: poller,
`cancel()`, handback route, phases. *Verified by running (delegated).* **Confirms
task-264.**

**F10.2 — P2 — Cancel and the poller can both write terminal results.**
`ledger.cancel → _stop` runs `claude stop` (seconds) then `_conclude` writes `cancelled`
unconditionally (`:1607-1631`); a poll landing in that window sees `STOPPED` and writes
`finished` plus its own `dispatch_result` (`runner.py:2904-2932`); `_finish_session` never
checks `cancel_requested`, unlike `_finish_batch` (`:3189-3195`). *Inferred.* **Confirms
task-264** (task-390 removed the torn read, not this).

**F10.3 — P2 — `read_run` opens `meta.yaml` without share-delete and treats a refused
read as a live run.** `ledger.py:936` uses `read_text`; `OSError` → `status: unknown` →
`is_live` (`:855-857`); `atomic_yaml.py:96-105` documents exactly this failure. Every 10 s
the poller's scan can hold off a writer or resurrect a finished run as live, which counts
against `max_concurrent_runs`. Same pattern at `guards.py:611`, `phases.py:108`,
`wake.py:202`. *Inferred.* **New.**

**F10.4 — P2 — A run parked once is never un-parked.** `_park_session` keys on
`status == "parked"` (`runner.py:2707`) and nothing writes `running` back (only `stalled`
is reset, `:2790`), so a second permission prompt in the same run is silent — task-296's
regression on the second park. *Inferred.* **New.**

**F10.5 — P2 — task-389 still true.** `handle_from_record` returns `None` without an
int `dispatch_entry_id` or a `session_id` (`poller.py:92-96`); the id is written by a
second `update_meta` after launch (`runner.py:2158, 3075`). Live ledger: 10 of 213 session
runs lack both; four were cancelled by hand. *Verified by running (ledger read).*
**Confirms task-389.**

**F10.6 — P3 — task-197 still true, and wider:** `poller.py:183` resolves the project
default with no `group=`, and `poll_session` branches on the *resolution's* driver
(`runner.py:2445`), so a codex run polled under a claude default is settled
`interrupted`. Unreachable today only because every enabled member is claude. *Inferred.*
**Confirms task-197.**

**F10.7 — P3 — A session can be registered against two tasks at once**
(`registration.py:285-286` scans per task; `_already_dispatched` keys only on the env
var), yielding two run records, two locks, two slots for one session. *Inferred.* **New.**

**F10.8 — P3 — `finished_without_handoff` waits an hour from `started_at`, not from
idle** (`runner.py:2862-2865`), and `run_report.py:189-192` reports that settlement as
work time. *Inferred.* **Confirms task-265.** Its totals do match the ledger (213 runs).

**F10.9 — P4 — Live ledger:** 213 runs, none non-terminal older than 24 h; `task-398.lock`
names `run_82c8e3ab`, cancelled since 02:13 — cancel does not release the lock, and it
waits for the next acquire or the startup sweep (`ledger.py:1031`). `atomic_yaml` is
atomic on Windows; a killed writer leaves a `.tmp` nobody sweeps (none present).
*Verified by running.*

---

## F11 — Guards and the classifier (delegated sub-audit; probe at `…/tmp/probe_guard.py`; guard and hook tests 272 passed)

Scope note: `dispatch/guards.py` is the dispatch-*precondition* chain; the tool-level
guard is `plugins/agentjobs/hooks/task_write_guard.py`; the allow-list is
`runner.py:394-426`.

**F11.1 — P1 — The write guard protects an empty directory; the real store has none.**
`agentjobs storage status`: `rows=416 files=0`; `managed_directories()` still returns the
retired `tasks/agentjobs` (`task_write_guard.py:194-227`). `sqlite3 <db> 'delete from
tasks'`, a Python one-liner doing the same, and `curl -X POST …/log` are all **allowed**.
*Verified by running.* **New** (the inverse of task-129).

**F11.2 — P2 — False positives beyond task-276:** writer verb anywhere + task path
anywhere in a comment (`:346-368`); `tee /dev/null` (`:118`); heredoc *bodies* scanned as
commands (`:157`) — the delegated probe was itself refused that way; `Write` with the
path in `content` denied while `Edit` with it in `new_string` is allowed (`:427-429`).
*Verified by running.* **Confirms task-276**, with the `Edit`/`Write` asymmetry new.

**F11.3 — P2 — False negatives needing no obfuscation:** `f=<path>; echo x > $f`;
`python -c "open('…' + '.yaml','w')"`; `C:/Python313/python.exe -c …` (`.exe` not in
`INTERPRETERS`, `:126-144`); `poetry run python -c …`, `uv run`, `npx`; PowerShell
`[IO.File]::WriteAllText`; `yq -i` (in `READ_ONLY`, `:180`); `vim`; `git apply`; `Write`
to `<path>.tmp`. All **allowed**. *Verified by running.* **New.**

**F11.4 — P1 — Nothing blocks `git add -A`, `git push`, `git merge`, `git checkout`, or
API writes, and the allow-list removes the classifier from three of them.** All allowed
by the hook (git subcommands matter only with a managed path, `:364-367`);
`settings_json` emits `allow` only (`runner.py:492-517`); `ALLOW_PREFIXES`
(`:394-405`) still holds `git add`, `git commit`, `git merge`, `npm run`; and
`tests/test_dispatch_runner.py:308-319` **asserts** `Bash(git merge:*)` is present.
*Verified by running (probe) and reading.* **Confirms task-245**; that task must name the
test to invert.

**F11.5 — P2 — `.claude/settings.local.json:17-22` re-grants `git merge` and
`python -c '*'` to every session in the shared clone**, dispatched runs included (they
start with `cwd=project_root`, `runner.py:1511, 1907, 2085`); it is hidden by the user's
global gitignore. *Inferred.* **New.**

**F11.6 — P3 — The installed hook is the pre-`d2ef033b` snapshot** (installed
2026-08-17 at `c30ca8c0`; 49 differing lines). *Verified.* **Confirms task-271.**

**F11.7 — P3 — `.mcp.json` pre-approval unchanged** (`runner.py:453-490, 603-614`).
*Inferred.* **Confirms task-266.**

**F11.8 — P4 — Order:** hook (deny is final) → `--settings` allow rules (classifier
skipped) → classifier under `--permission-mode auto` (`runner.py:609-614`); the
three-consecutive-blocks park is a single-run observation in prose (`:436-446, 578-584`),
not anything counted here. `dispatch_task` refuses `CLOSED` and `AGENT/HOLD` only
(`guards.py:835-845`), not `DRAFT` or `ball=human`. **Confirms task-259.**

---

## F12 — P4 — Smaller observations

- **The runbook names a command that does not exist.** `PLAN.md` says "Search it for your
  system — `agentjobs search`"; the CLI has no `search` (`agentjobs --help`). I used
  `agentjobs list` and grep. Worth fixing before the next audit.
- **A retry that finds a prior merge rewrites the merge entry** (F1's task-329: two
  "Merged … as `e26a460b`" entries). `merge` returns the earlier sha (`finish.py:1197`),
  and `record_merge` runs unconditionally (`:2362`).
- **`_merge_commit_of` parses the sha out of a step's prose** (`:2229-2234`,
  `detail.split()[-1]`); a wording change to the merge step silently loses the "merge is
  done" sentence on every escalation.
- **The runway's one-hour timeout** (`RUNWAY_TIMEOUT_SECONDS`, `ledger.py:74`) escalates
  `runway_busy` rather than reporting success — a queued finish does queue. Good. But the
  queue note is only written once and only to the *queued* task; the task holding the
  runway is never told others are waiting.

---

## Focused tests

`poetry run pytest tests/test_dispatch_finish.py tests/test_finish_status.py -q -x`:
**153 passed in 105.9 s** (2026-09-12 03:45 UTC). The three delegated runs (guards/hooks
272, epic 58, poller/registration/atomic 61) all passed. Green, and none of F1, F2, F3
or F6 has a test that would have gone red.

---

## What I did not get to

- **Reproducing F3** with a sandbox server on 8904 and a restart that leaves it down.
- **`runner.py`** (3308 lines) beyond the clauses cited; `codex_app_server.py`,
  `interactive.py`, `transcript.py`, `session_env.py`, `address.py`, `budget.py` in full;
  `docs/codex-dispatch*.md` at all; `branch_report.py` only through its CLI output.
- **Racing F2 for real** with two processes rather than a scripted interleaving.
- **The epic walk against a real dispatch.** Every walk finding is a probe or a read.
- **Whether the frontend renders the ten `running` finishes**, and what the slot board
  counts.
- **Re-running the delegated probes myself.** I relayed their labels as given.
- **task-264's spec as a whole** — I confirmed two of its races and did not assess the
  journal design it proposes.

## Questions for other auditors

- **05 (gate):** The roadmap stage is red on `main` right now (F4). How often has it been
  the *only* red stage in a finish? And does `gate_slots` ever see the litter F6 predicts?
- **05 (gate):** `test_the_timeout_kills_the_grandchild_too` is named in 21 of the 32 red
  finish gates. Is it still flaking under today's slot budget, or did task-339 fix it
  without task-325 being closed?
- **02 (SQLite):** F3's fix is to have the finish open the database locally while the
  server is also open on it. Does the WAL/`busy_timeout` argument in
  `store_factory.py:293-300` actually hold for a writer that lives for ten minutes?
- **03 (authorization):** A run finishing itself writes `close_task` and
  `update_task(branches=…)` on its own task over HTTP with its run credential; a human's
  `agentjobs finish` from a shell presents none. Which principal does the server think
  closed the 78 finished tasks, and does the capability table allow both?
- **08 (manager/queue):** When a local `dispatch_manager_for` writer (the poller, the
  walk) changes a task, does the server's read path see it immediately, or is there a
  cache the two-process model bypasses?
- **09 (frontend):** How do the ten `running` finish records render on their task pages
  today? Does the slot board count a finish (task-352, marked completed) when the ledger
  has no run for it (F9.6)?
- **11 (docs):** `agent-dispatch-design.md:2070-2076` and ALLAGENTS.md's "Retries are
  bounded … enforced rather than promised" are both contradicted above (F1, F9.2, F9.3).
