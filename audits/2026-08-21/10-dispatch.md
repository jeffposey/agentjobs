# 10 — Dispatch subsystem

Auditor 10, Big Dawg Audit (task-242), night of 2026-08-21. Read-only. Nothing was
dispatched, cancelled, claimed or committed; the only write is this file.

## What was examined, and how

Read in full: every module in `src/agentjobs/dispatch/` (14 files, 8,794 lines —
`runner`, `ledger`, `guards`, `poller`, `wake`, `auto`, `phases`, `auth`, `address`,
`record_commit`, `config`, `scaffold`, `finish`, `__init__`), `docs/agent-dispatch-design.md`
(2,178 lines), `scripts/run_report.py`, the dispatch parts of `api/routes/status.py`,
`api/routes/dispatch.py`, `api/routes/tasks.py`, `api/main.py`, `cli.py`, the phase
plumbing in `scripts/check.py`, `manager.record_dispatch*`, `models_v2.DispatchData`,
`actors.RESERVED`, and the names of every test in `tests/test_dispatch_*.py` and
`tests/test_auto_dispatch.py` (about 330 tests).

Read on this machine: `~/.agentjobs/dispatch.yaml`, `projects.yaml`, all 61 run directories
under `~/.agentjobs/runs/` (every `meta.yaml`, the one `phases.jsonl`, several `stdout.log`),
`~/.claude/daemon.log`, `~/.claude/jobs/<id>/state.json`, and the two launcher scripts under
`C:/ai/shared/launchers/`. Ran read-only GETs against the live server on 8876
(`/api/version`, `/api/dispatch/runs`). Cross-checked every `dispatch`/`dispatch_result`
entry across the agentjobs, job-hunting and mastercalls task corpora.

A note on the brief: it lists `scaffold.py` as "dispatch prompt scaffolding". It is not —
`scaffold.py` is the commented example `dispatch.yaml` behind `agentjobs dispatch example`.
The prompt stubs live in `runner.py:115-191` (`PROMPT_STUB`, `SUPERVISOR_STUB`) and
`wake.py:39-53` (`WAKE_STUB`). Those are what I audited for item 5. Likewise `auth.py` does
not authenticate anything; it detects a session killed by an expired login (item 6 below).

## One run's lifecycle, traced through the code

1. **Request.** `POST /api/projects/{p}/tasks/{id}/dispatch` with `{"user": "Jeff Posey"}`
   (`api/routes/status.py:583-665`) or `agentjobs dispatch run` (`cli.py:984-1049`). The HTTP
   path passes the listening socket's address down (`serving_api_base`, `status.py:567-580`);
   the CLI passes none and is probed (`guards.py:237-275`).
2. **Gates.** `guards.dispatch_task` (`guards.py:615-805`): task exists → not closed → not
   `agent/hold` → the four config gates plus group selection (`config.py:1098-1152`) → either
   the stored newest entry is a configured human's (`assert_human_clocked`, `guards.py:441-479`)
   or the `user` is a configured human and the spec has a description → runner actor known →
   directory scan of `~/.agentjobs/runs/*/meta.yaml` for a live run on this task and for the
   machine ceiling (`guards.py:707-722`) → clean tree outside `tasks/` → address probe (CLI
   only) → per-task `O_CREAT|O_EXCL` lock (`ledger.py:278-337`) → for the `user` path, write the
   human's `note`, re-read it from disk, assert it again → claim (`_claim_or_verify`,
   `guards.py:850-872`).
3. **Spawn.** `DispatchRunner.start` → `_assert_spawnable` (sentinel, clean tree again) →
   `_start_session` (`runner.py:1200-1300`): compose argv from the runner template plus posture
   flags (`posture_flags`, `runner.py:360-446`; the `--settings` blob carries the allow-list and
   the project's `.mcp.json` server names), decide cold start vs `--resume` (`_plan_wake`,
   `runner.py:1157-1198`, `wake.py`), write `meta.yaml` with `status: starting`, run
   `claude --bg --remote-control ... <prompt>` synchronously with a 120 s timeout, scrape the
   short session id from stdout (`capture_session_id`, `runner.py:1302-1314`), append the
   `dispatch` log entry (`manager.record_dispatch`, `manager.py:2048-2109`), set `status: running`.
   The lock file is then re-written to name the run (`lock.adopt`, `guards.py:803`).
4. **Follow.** `poll_sessions_forever` (`poller.py:200-238`), started in the server's lifespan
   (`main.py:148`), calls `poll_live_sessions` in a thread every 10 s. For each live session run
   it rebuilds a handle from disk (`_handle_from`, `poller.py:69-107`), re-resolves the dispatch
   gates, and calls `poll_session` (`runner.py:1474-1520`): `claude agents --json --cwd <root>`
   → classify → copy `claude logs <id>` into `transcript.log` → auth-stall check → park
   (`waiting/blocked`), settle (`idle`), cancel (`stopped`), or conclude `interrupted` (gone).
5. **Settle.** `_settle_finished_session` (`runner.py:1633-1670`): if a `handoff`/`transition`
   entry newer than the dispatch entry exists → `completed`, `claude stop <id>`; otherwise wait
   out `session_stale_seconds` (3600) → `finished_without_handoff`, ball → human/decision.
   `_finish_session` writes `dispatch_result`, stamps `finished_at`, releases the lock, and
   commits the task file with `git commit --only` (`record_commit.py`).
6. **Later.** Server restart → `reconcile` (`ledger.py:744-812`) re-attaches sessions still in
   `claude agents --json --all`, concludes the rest, sweeps locks; then `reap_finished`
   (`ledger.py:855-913`) runs `claude rm` on finished sessions except the newest one per open
   task, which `wake.find_wake_target` may later resume.

Observed corpus: 61 runs, **all session mode, zero batch**; 26 distinct tasks across two
projects; 50 `completed`, 8 `interrupted`, 3 `cancelled`, 1 `finished_without_handoff`,
0 `failed`/`timeout`/`crashed`. Every `dispatch` entry has a `dispatch_result`; one run has
two (finding 4). `~/.agentjobs/finishes/` does not exist (finding 17).

---

## Findings

### P1-1 — Phase records (and the runner's `env:`) do not reach most sessions: the `--bg` worker inherits the daemon's environment, not the launcher's

`runner._environment` (`runner.py:1027-1053`) puts `AGENTJOBS_RUN_ID`/`AGENTJOBS_RUN_DIR` and the
runner's `env:` into the environment of the `claude --bg` *launcher* process. But `--bg` hands
the session to a persistent background daemon, and the daemon spawns the worker. The worker's
environment is therefore whatever the **daemon** had when it was started — by whichever launch
happened to find no daemon running.

Evidence, all from this machine:

- `~/.claude/daemon.log`: the daemon that shut down at `2026-08-22T03:19:29Z` had
  `uptime=41679s`, i.e. it started at **2026-08-21T15:44:50Z**, seven and a half hours *before*
  the instrumentation merged (`735c267`, 2026-08-21T23:22Z). It spawned every post-instrumentation
  session except one: `0fa21703`, `795aaa29`, `1d565f9c`, `55e8c4ae`, `2d28ad2d`, `7d9101ec`
  (= runs `run_92d3e98c`, `run_1994a874`, `run_b06b6084`, `run_f0accc73`, `run_e62535f2`,
  `run_b440fdb5`). **None of those six run directories has a `phases.jsonl`.**
- The one run that does, `run_d813a110`, is the one whose launcher printed "Starting background
  service…" (`stdout.log`) — the daemon log shows `daemon start` at `04:27:57Z` immediately
  followed by `bg spawned d12b034c`. Its two `gate_finished` records carry its own run id because
  it *was* the launcher that started the daemon.
- `run_b440fdb5` is task-241's working run; its own task log reports running the full gate
  ("`scripts/check.py`, unqualified, green: 130.8s", task-241.yaml:505-506). No phase record
  exists for it anywhere.
- Across all 61 runs only 12 `stdout.log`s contain "Starting background service" — in steady
  state the daemon is already up, so the launcher's environment is discarded.
- `~/.claude/jobs/d12b034c/state.json` records `respawnFlags` (argv) and no environment, which
  is consistent with the daemon, not the launcher, owning the worker's process environment.

Consequences:

1. `scripts/run_report.py`'s gate lines — the reason task-233 exists — are absent for 6 of the
   7 instrumented runs, and the report prints "No phase records yet" identically for
   "uninstrumented", "instrumented but the env never arrived" and "instrumented and nothing ran
   the gate" (`run_report.py:329-334`). Nothing on disk distinguishes them.
2. The inverse is worse and merely un-observed so far: when a *dispatched* launch is the one that
   starts the daemon, every later session that daemon spawns — other tasks' runs, Jeff's own
   `spawn-session` children — inherits **that run's** ids, and their gate records are filed
   under the wrong run. The guard in `_environment` ("granted, never inherited") protects
   against the dispatcher's own ambient env, not against this.
3. `docs/agent-dispatch-design.md:434-436` and `config.py` tell operators to put secrets in a
   runner's `env:` because argv is logged. In steady state a runner's `env:` is **not delivered**
   to the session at all; a runner that depended on it would work exactly once per daemon
   lifetime.
4. `tests/test_dispatch_runner.py::TestTheRunIsMeasurable` (three tests) and
   `test_a_run_identity_is_granted_and_never_inherited` pass against a `sys.executable` fake
   runner, which inherits `Popen` env directly. They cannot see this. Same failure shape the
   module's own docstring records for `resolve_executable` ("every unit test passed against
   fake runners").

Fix: stop relying on process-environment inheritance through `--bg`. Verify first whether the
installed CLI offers a per-session environment or a `--env`-style flag for `--bg` (read-only:
`claude --help`, `claude agents --help`); if it does not, have the gate derive its run from the
ledger — `live_runs(home)` filtered to the project whose checkout it is running in, attributing
to the single live session run when there is exactly one and writing `run_id: ambiguous`
otherwise — and record `daemon_started: true/false` in `meta.yaml` from the launcher banner so
`run_report` can at least say *why* a run has no records. Re-measure task-233's table after.

### P1-2 — The human-clocked rule is not structural: a dispatched agent can dispatch, as Jeff, with one unauthenticated POST

The design's central safety claim (`agent-dispatch-design.md:87-101`, `:225-229`) is that
agent-starts-agent is "structurally impossible rather than merely capped", and that this
survives the one-click path because "agents do not have browsers". The deployed system does not
enforce that claim.

- `status.py:638` passes `payload.user` straight into `DispatchRequest.authorized_by`.
  `guards.assert_authorizer_is_human` (`guards.py:332-361`) checks only that the id is a
  configured actor with `kind: human`. No route in `src/agentjobs/api/` carries any
  authentication (grep for `HTTPBearer|APIKey|Authorization|api_key|authenticat|Security(`
  matches only the word in docstrings and the `conflicting_authorization` code).
- Every dispatched agent is told the server's address in its first sentence (`PROMPT_STUB`,
  `runner.py:122-123`; on this machine `http://127.0.0.1:8876`) and runs under `--permission-mode
  auto`, whose classifier decides each command on content. `curl -X POST
  http://127.0.0.1:8876/api/projects/agentjobs/tasks/task-NNN/dispatch -d '{"user":"Jeff Posey"}'`
  is one HTTP call; it writes "Jeff Posey authorised a dispatch of this task from the task page"
  onto the record (`compose_authorization_body`, `guards.py:364-386`), re-reads it, and passes
  `assert_human_clocked`. The entry is indistinguishable from a click.
- The budget caps (`auto.py:114-163`) bind `DispatchTrigger.AUTO` only (D3). An agent's forged
  click is `MANUAL` and uncapped. The only bounds are `max_concurrent_runs` (3 here), one live
  run per task, and the account's quota. D3's premise — "a human clicking Dispatch repeatedly is
  a decision, not a malfunction" — assumes the server can tell a human's click from an agent's,
  and it cannot.
- The same door is open to any tailnet peer (auditor 12 owns that framing); this finding is about
  the *inside* attacker the brief names.

Honest statement of the boundary: on a single-user machine where the agent runs as the user,
no local secret is unreadable by the agent, so a token cannot make this "structural" either.
What can: (1) bind the per-task and a new machine-wide dispatches-per-hour cap to **every**
trigger, with the log naming which cap tripped — the cost is that Jeff's fifth manual dispatch of
one task in a day is refused and has to be done with `--caused-by` naming a hand-written note;
(2) rewrite §2 and the `guards.py` docstring to say the loop is bounded by the ceiling and the
caps, not made impossible; (3) consider refusing `user`-authorised dispatches from a request whose
client is loopback while a run on this machine is live, as a heuristic named as one.

### P2-3 — Gate 2 ("a repository cannot choose what executes") is false for `.mcp.json`

`mcpjson_server_names` (`runner.py:296-328`) reads the **dispatched project's own** `.mcp.json`
and pre-approves every server in it via `enabledMcpjsonServers` in `--settings`
(`runner.py:331-357`, `:434-446`). That file is repository content. Claude Code's "New MCP
server found in this project" prompt is precisely the consent gate that stops a cloned
repository from running a command; dispatch bypasses it for whatever the repository declares.
For a supervisor run, `supervisor_allow_rules` (`runner.py:268-293`) additionally pre-approves
every tool of every such server (`mcp__<name>`), classifier not consulted.

The design (`agent-dispatch-design.md:1244-1254`) states "a project cannot execute a command
that was not written into `~/.agentjobs/dispatch.yaml` by hand", and rejected
`enableAllProjectMcpServers` as "a much broader grant" (`:792-795`). Per project, the grant is
the same; only the cross-project scope differs. The same family, lower severity because it is
inherent to running a project's gate at all: `Bash(npm run:*)` and `poetry run pytest:*` are
pre-approved, and what `npm run build` executes is defined by the repository.

Today every dispatch-enabled project here is Jeff's own, so this is latent. Fix: name the
permitted MCP servers per project in `dispatch.yaml` (machine-local, hand-written, the same
place runners live) and intersect with `.mcp.json`; refuse on a name the machine has not
approved, the way an unknown runner is refused.

### P2-4 — Cancel and the poller race to write contradictory terminal entries — observed

`ledger._stop` writes `cancel_requested` before stopping (`ledger.py:672-684`), and
`_finish_batch` honours it (`runner.py:1943-1949`). `_finish_session` does not: its only guard
is a re-read of `status` (`runner.py:1702`). The poller's tick and the cancel handler both do
unlocked read-modify-writes of `meta.yaml` (`RunDirectory.update_meta`, `ledger.write_status`)
and both call `record_dispatch_result`.

Evidence: task-107, run `run_a6deb292`. Log entry 9 at `2026-08-19T21:48:04Z` by `dispatcher`:
`dispatch_result` `cancelled` "stopped session 7e5fc33c". Entry 11 at `21:48:15Z`, same actor,
same `re: 7`: `dispatch_result` `interrupted` "The session is no longer in the ledger". The run's
final `meta.yaml` reads `status: finished`, `outcome: interrupted`, `cancel_requested: true` —
the poller's write landed last and overwrote the cancellation. Both guards (`daa97ac`,
2026-08-18; `841f84b`, 2026-08-18) pre-date the event, so this is the current code's window:
the poller read meta before the cancel wrote it, spent ~10 s in `claude agents --json` and
`claude logs`, and concluded on stale state. One occurrence in 61 runs; it recurs whenever a
human cancels within a poll interval, which is the normal time to cancel.

Fix: check `cancel_requested` in `_finish_session` exactly as `_finish_batch` does; and make
`record_dispatch_result` refuse a second terminal entry for the same `run_id` (the manager can
see the log; the per-task storage lock is already held there). Consider also a per-run-directory
lock for meta writes.

### P2-5 — Every per-task mechanism is keyed by task id alone, not `(project, task)`

The run lock is `~/.agentjobs/runs/.locks/<task_id>.lock` (`ledger.py:305`), the live-run scan
compares `run.task_id == task.id` across all projects (`guards.py:708-709`),
`newest_session_run` filters only on `task_id` (`wake.py:120-138`), and `_wakeable_run_ids`
keys on task id (`ledger.py:902`). Task ids are per project; agentjobs uses bare `task-NNN`.

Checked: today no id appears in more than one project (0 collisions across agentjobs,
job-hunting and mastercalls; job-hunting's ids carry slugs) and no ledger task id appears under
two projects. So this is latent — but the failure when it fires is not a refusal: a dispatch of
agentjobs `task-224` whose newest session run is job-hunting's `task-224` would
**`--resume` another project's conversation** with a wake prompt asserting "this is the same
session you were already running on task-224". `test_another_tasks_session_is_never_borrowed`
covers two tasks in one project only.

Fix: include `project_id` in the lock filename and in all three lookups.

### P2-6 — `_ball_moved` counts the dispatcher's own handoffs, so a parked or auth-stalled session that never hands off is recorded `completed`

`_ball_moved` (`runner.py:1672-1679`) returns true for any `handoff`/`transition` entry newer
than the dispatch entry. `_park_session` and `_park_auth_stall` write a `handoff` under actor
`dispatcher` (`runner.py:1560-1576`, `:1613-1623`). After the human answers and the session later
goes idle without ever handing off, `_settle_finished_session` sees the dispatcher's own entry
and records `completed` with reap — the run_a1e35ca5 class (task-224) through a second door.

Fix: exclude `reserved_actors()` (`dispatcher`, `finisher`) from the `_ball_moved` scan.

### P2-7 — Dispatcher-written handoffs never fire webhooks

`poll_live_sessions` constructs `TaskManager(TaskStorage(project.tasks_dir()))` with no webhook
manager (`poller.py:163`), and `main.py:148` passes no `managers`. The same `handoff` through the
API goes through `dependencies.manager_for`, which attaches one (`dependencies.py:226-228`).
So the four handoffs that most need a notification — parked on a permission prompt, stopped on
an expired login, finished without handing off, cancelled/interrupted — emit no `task.handoff`
event, while an ordinary agent handoff does. ENGINEERING.md names that webhook as the
notification extension point.

Fix: build the poller's managers through `dependencies.manager_for(project)` or pass the
server's managers into `poll_sessions_forever`.

### P2-8 — A draft task, and a task whose ball is with the human, can be dispatched; the wake then quotes the agent its own review request as "what the human said"

`dispatch_task` refuses only `closed` and `agent/hold` (`guards.py:660-670`). `_claim_or_verify`
(`guards.py:850-872`) claims a `ready` task and otherwise passes any task whose owner is unset —
which includes `draft` (owner is `None`). A draft cannot be claimed by the agent it is handed to.
Nothing checks `ball` on the manual path either, so a task sitting at `human/review` is
dispatchable; `_plan_wake` then builds `WAKE_STUB` from `task.ball_prompt` (`runner.py:1188-1197`,
`wake.py:39-53`): "A human has moved the ball back to you. What they said:" followed by the
agent's own handoff text. No test in `test_dispatch_guards.py` constructs a `draft` or a
`human`-ball task (the file's lifecycle fixtures are all `READY`).

Fix: refuse `lifecycle is DRAFT` by name; refuse `ball is HUMAN` unless `caused_by`/`user` is
accompanied by a ball move, or make the one-click path write a `handoff` to `agent` rather than
a `note`; and have `build_wake_prompt` say whose entry it is quoting.

### P2-9 — The machine-wide ceiling is a directory scan, not a lock; two concurrent dispatches of different tasks both pass at N−1

`guards.py:707-722` counts live run directories and compares with `max_concurrent_runs`; the run
directory is created later, after the claim, `git status`, and (CLI) an HTTP probe. The per-task
lock is atomic; the ceiling has no primitive at all, and the docstring on the per-task case
already admits "the scan reads a directory and can lose a race with itself". With three sources
of dispatch on this machine (the server, `agentjobs dispatch run`, a finish's escalation) and a
ceiling of 3, a fourth run is admitted by a second request arriving inside that window.
`test_the_machine_limit_refuses_and_does_not_enqueue` exercises the sequential case only.

Fix: take a machine-wide `O_EXCL` lock around scan-plus-directory-creation, or create the run
directory first and back it out on refusal.

### P2-10 — The merge gate has no mechanical enforcement inside a dispatched run: `git merge` is pre-approved regardless of task state

`ALLOW_PREFIXES` (`runner.py:233-244`) pre-approves `git merge`, `git commit`, `git add`
in both shells, classifier bypassed. The design justifies this because "the merge is gated on a
human approval recorded on the task" (`agent-dispatch-design.md:728-736`) — but the allow rule
reads nothing from the task. A worker that decides it is done can `git merge` its branch into
`main` in the shared clone with no approval recorded and no prompt. The only thing between a
dispatched run and an unapproved merge is that the model read ALLAGENTS.md. Note also that
`PowerShell(git add:*)` pre-approves `git add -A`, the exact command ALLAGENTS.md forbids.

Fix (pick one): drop `git merge` from the list and accept classifier friction; or, once
`finish.enabled` is on for a project, treat the scripted finish as the only merge path and
remove the pre-approval for that project; or a `pre-merge-commit`/`pre-commit` hook in the main
clone that refuses a merge into `main` unless the task's record carries an approval (hooks are
bypassable, but not silently).

### P2-11 — `run_report.py` reports settlement time, not work time, for sessions

A session run's `finished_at` is written when the poller *settles* it (`runner.py:1720-1722`),
not when the agent stopped. A `completed` run is settled within one poll interval, but a run
with an unmoved ball waits out `session_stale_seconds` first (`runner.py:1651-1654`): every
`finished_without_handoff` run is ≥ 60 minutes by construction. `run_9f5385a4` (task-233) shows
63.0 minutes. `summary()` folds those into total time, p90 and runs-per-task; `per_task` ranks
on them. Task-233's baseline table ("21.1 hours") was built from these numbers.

Fix: record `idle_at` the first time a poll observes `idle`, and report work time from it;
keep `finished_at` as the bookkeeping time it is.

### P3-12 — A session launch that times out or prints an unexpected banner leaves an orphan session nothing tracks

`_start_session` runs the launcher with `timeout=120` (`runner.py:1233-1247`); on
`TimeoutExpired` or a missing id it marks the run `failed` and raises (`:1248-1267`), and
`dispatch_task` releases the lock (`guards.py:792-795`). The session may nevertheless be running:
it is then absent from `live_runs`, never polled, never reaped, does not count against the
ceiling, and the task stays claimed by an agent the ledger says is not running.

The id capture is more fragile than its docstring says. Tested against a real `stdout.log`:
line 0, `backgrounded · ESC[36md12b034cESC[39m`, does **not** match `\b([0-9a-f]{8})\b`
(the escape's `m` abuts the id, no word boundary); the match comes from line 2, the
`claude attach d12b034c` help line. A cosmetic change to that help text orphans every launch.

Fix: strip ANSI before matching (`strip_ansi` already exists three hundred lines down); on any
launch failure, list `agents --json --cwd` and adopt a session that appeared since the launch
began, recording it rather than abandoning it.

### P3-13 — Disabling dispatch orphans the bookkeeping of runs already live

`poll_live_sessions` re-runs `assert_dispatch_permitted` per run per tick and skips the run on
any refusal (`poller.py:171-178`), by design so that switching dispatch off does not conclude live
work. The consequence is that a project toggled off (or the sentinel) while a run is live
leaves that run never settled, never reaped, its lock and its concurrency slot held until the
project is re-enabled or the server restarts — and `reconcile` then only re-attaches it. The poller
needs the config for `session_stale_seconds` and the runner's executable, not for permission.

Fix: load the config without the gates for *following* a run; only *starting* one needs them.

### P3-14 — `reconcile` lists with `--all`, so a stopped session is "re-attached" and then mis-described

`session_ledger()` passes `--all` (`ledger.py:646`), which the runner's own docstring says
includes stopped sessions (`runner.py:1345-1354`). `reconcile` therefore reports "session still
running; re-attached" for a session someone stopped by hand while the server was down; the next
poll, using the active-only listing, concludes it `interrupted` with the body "no longer in the
ledger", which is the wrong story. Outcome is acceptable, the record is misleading. Fix: use the
active view in `reconcile`, or classify the row's `status` before re-attaching.

### P3-15 — `main.py` and the design say the poller reaps; it stops

`_finish_session` calls `stop_session` (`runner.py:1725-1726`, "stop and not rm"). `main.py:77`
("the poller below reaps each session as it settles it") and
`agent-dispatch-design.md:1719-1720` ("Reaping happens … as the poller settles each session")
are both false; `reap_finished` runs at startup and on demand only. Evidence: every finished
run's `meta.yaml` carries `reaped: true` except the two newest, and all of their mtimes cluster
at the last server start (`/api/version started_at 04:36:42Z`; `meta.yaml` mtimes 23:36 local).
Sessions accumulate in `claude agents --all` between restarts. Fix the text, or call
`reap_finished` from the poller after a settle (it already honours the wake-keep rule).

### P3-16 — Batch mode has zero production exercise

61 of 61 runs are session mode. Every claim about batch — the 1800 s timeout, `CTRL_BREAK_EVENT`
to a process group, `taskkill /T`, tail inlining on failure, the supervisor thread's total
`except` — is test-only (`TestBatchOutcomes`, `TestProcessGroup`, fake runners). Not a defect;
stated so the design's "verified" language about batch is read as "unit-tested".

### P3-17 — The scripted finish has never run on this machine, yet the process docs present it as the normal path

`~/.agentjobs/finishes/` does not exist; `dispatch.yaml` has no `finish:` block for any project,
so `finish_is_offered` is false and Approve falls through to `maybe_auto_dispatch`, which is also
off (`auto_dispatch: false`). ENGINEERING.md ("Steps 3 to 6 may already have happened before you
read them") and ALLAGENTS.md ("Steps 6 and 7 may be done before you wake up") describe the
finish as what an approval does. On this machine an approval does nothing automatic. The only
end-to-end evidence for `finish.py` is task-241's sandbox (`task-241.yaml:476-493`). Auditor 1
and 2 should weigh this when judging those sections as load-bearing.

### P3-18 — Test decoration, and the tests that set up what they claim to verify

Applying ENGINEERING.md's question to the dispatch suite:

- `TestTheRunIsMeasurable` (3 tests) and `test_a_run_identity_is_granted_and_never_inherited`
  would not have caught P1-1; they test `Popen` inheritance with a fake runner, and the mechanism
  that fails is the daemon hop they cannot reach.
- `test_a_concurrent_double_dispatch_starts_exactly_one_process` proves the per-task lock;
  nothing tests two concurrent dispatches of *different* tasks against the ceiling (P2-9).
- No test cancels a session run while a poll is in flight (P2-4); `TestCancel` cancels with no
  poller running.
- No test dispatches a `draft` or a `human`-ball task (P2-8).
- `test_another_tasks_session_is_never_borrowed` uses one project (P2-5).
- `test_it_captures_the_id_the_cli_assigned` passes a clean banner; the real banner has escape
  codes on the id (P3-12).
- `test_the_seed_list_covers_the_boring_commands` and `test_pushing_is_not_pre_approved` assert
  the list's contents, which is decoration: the list has never caused an incident, and what
  would is a pre-approved command doing something the task did not authorise (P2-10).

### P4-19 — Smaller observations

- `_conclude` writes `status: cancelled` for every outcome it concludes, including `interrupted`
  (`ledger.py:933-938`); `CLI status` and `run_report` show `outcome or status`, so it reads
  correctly, but the meta file lies about how the run ended.
- The dispatch endpoint is `async def` and runs the launcher synchronously: the event loop is
  blocked for the launch. Measured from 61 runs (dispatch-entry timestamp minus `started_at`):
  median 0.8 s, max 1.9 s. Tolerable; worth knowing the server freezes for every Dispatch click.
- `reap_finished` reaped `run_b440fdb5`'s session (`7d9101ec`) at 04:36Z while `run_d813a110`,
  which had `--resume`d that very conversation, was live (04:27–04:39Z). The resumed run
  completed, so `claude rm` of the predecessor did not kill the successor's transcript on this
  CLI version. Nothing in `_wakeable_run_ids` protects that case; it works by accident of how
  `--resume` forks. Worth a test against the real CLI before relying on it.
- `PROMPT_STUB` says `../worktrees/<repo>-<nnn>` (`runner.py:119`); ALLAGENTS.md says
  `../worktrees/aj-<nnn>` (`ALLAGENTS.md:142,265`). Two conventions, both "the" convention.
- The design doc's status line still reads "Nothing here is implemented"
  (`agent-dispatch-design.md:3-4`) above 2,000 lines describing shipped code. Auditor 2's
  territory; noted because a reader of §2 takes "structurally impossible" at face value.
- `/runs/{id}/output` and `/tail` serve the full session transcript (everything the agent read
  or printed) to any caller who can reach the API. Auditor 12's.
- `guards.py:496-532` still carries the "task-072 will replace this" note for `live_runs`;
  task-072 landed and the ledger has its own `live_runs`. Two readers of the same directories.
- `agentjobs restart` kills by pid without `/T` (`cli.py:513`), so a detached finish process
  survives the restart it triggers. Checked because the finish docstring's safety argument
  depends on it; it holds.

---

## Per-item coverage of the brief

1. **guards.py — allowed, blocked, what a motivated agent can still do.** Enforcement exists
   only in argv flags Claude Code honours: `--permission-mode auto` (classifier), an allow-list
   that *bypasses* the classifier for ten prefixes in two shells, `--tools` for read-only,
   `bypassPermissions` for autonomous. Nothing in AgentJobs constrains a run after spawn. A
   motivated agent can: dispatch other tasks as Jeff (P1-2), merge without approval (P2-10),
   `git add -A`, `git push` subject only to the classifier's opinion, read and write anywhere on
   the machine as Jeff (the design concedes "a worktree is not a sandbox"), and call every
   AgentJobs MCP tool without review when supervising. The guards are advice with a classifier
   behind them; the classifier memory's "coin flip" characterisation is the correct model.
2. **Ledger integrity.** Start/end/kill paths all write; a crash between `RunDirectory.create`
   and the launch leaves `status: starting` counted live until the next restart (reconcile
   concludes it). Observed defects: P2-4, P3-12, P3-14, P3-15, P4 status/outcome disagreement.
   `run_report` misleads as in P2-11 and P1-1; `read_gates` correctly ignores a `gate_started`
   with no finish.
3. **Poller races / ceiling.** P2-4, P2-9, P3-13. Exactly one poller exists (the server's); a
   second server on 8765 would add one — checked, nothing listens on 8765 now.
4. **wake/resume.** Decision is `resume_sessions` + newest session run for the task id not live,
   not reaped, still listed by `agents --json --all`. Wrong-session risk: P2-5. Missing worktree:
   no mechanical check; `WAKE_STUB`'s escalation clause is the only mitigation, and the finish's
   `worktree_missing` escalation writes the fact into the prompt the woken session receives.
   Examined `wake_argv`, stdin delivery, `BALL_PROMPT_LIMIT`: nothing found.
5. **Prompt selection / injection.** Predicate verified: `open_child_ids` →
   `manager.get_subtasks` (direct children, `manager.py:491-501`) filtered by `is_open`
   (`lifecycle is not CLOSED`), the same rule `_open_children` uses for claimability, read once
   for both the stub and the MCP grant. Interpolation: `str.format` on fixed stubs with ids and
   paths only; `substitute_argv` uses `re.sub` with a callable so braces and backslashes in the
   prompt are inert; argv is a list, no shell. The one place task content enters a prompt is
   `WAKE_STUB`'s `ball_prompt` (up to 4,000 chars, verbatim) — human- or finisher-written,
   settable by any API caller (auditor 12). No escaping, none needed for the process boundary;
   the model boundary is P1-2/12's problem.
6. **auth.py + address.py.** Nothing authenticates; `auth.py` is stall detection and reads the
   session's JSONL transcript with the tail/session-id/real-reply guards its docstring claims —
   examined, nothing found beyond the `since` check being the only thing separating a resumed
   session's old stall from a new one. `address.py`: the observed address comes from
   `scope["server"]` (correct behind the tsnet proxy), the probe bypasses `HTTP_PROXY`, and the
   fallback is the one string it says. Examined, nothing found. What a tailnet peer can invoke:
   every dispatch route including `/enable`, `/dispatch` with `user`, `/cancel`, transcripts.
7. **phases.jsonl.** P1-1. Otherwise the plumbing is as described: `record_phase_from_env`
   is a no-op without the env (`phases.py:84-106`); `check.py:41,85-93` scrubs the pair from its
   children so nested `check.main` tests do not append phantom records; `finish.py:562` sets the
   pair to the finish id so a finish's gate lands in the finish directory. "What breaks
   silently": nothing breaks, nothing is written, and the report cannot tell that apart from a
   run that never ran the gate.

## What I did not get to

- I did not read `tests/test_dispatch_*.py` bodies beyond their names; coverage claims in
  P3-18 rest on names and on grep (`draft`, `Lifecycle.`), not on reading the assertions.
- I did not verify whether `claude --bg` accepts a per-session environment (the fix for P1-1
  depends on it); that needs `claude agents --help` on the installed 2.1.238 and I did not run it.
- I did not test whether `--resume` of a session under one cwd works from another project
  root (P2-5's worst case); stated as what the code would attempt, not as observed.
- I did not audit `docs/agent-loops-design.md` or the `acceptance[].check` schema the design's
  §2a (D5, bounded autonomy) depends on; §2a says "nothing here is implemented" and I took it
  at its word.
- I did not measure poller cost (two subprocesses per live run per 10 s) or the transcript
  endpoint's cost; the docstrings' "negligible" is unverified.
- `docs/agent-workflow.md`'s parent-task protocol, which the supervisor stub points at, was
  not read; a test asserts the anchor exists.
- I did not look at the React side of dispatch (the button's disabled states, the run list);
  auditor 9.

## Questions for other auditors

- **12 (security):** P1-2 and P2-3 are yours from the outside. The specific question: on a
  machine where the agent runs as the user, is there *any* credential the browser can hold that
  a dispatched agent cannot read? If not, the honest design statement is "bounded by caps", and
  the caps must bind manual triggers.
- **8 (MCP):** is `task_log_append` with `actor: "Jeff Posey"` refused when called from an agent
  session? If not, an agent can satisfy `assert_human_clocked` for the CLI path too (write a
  human note, then `agentjobs dispatch run`), which is P1-2 through a second door.
- **4 (storage):** `meta.yaml` is read-modify-written without a lock by three parties (P2-4).
  Does the storage layer's `O_EXCL` lock helper have a shape the ledger could reuse, or is a
  second locking convention (which `ledger.py:278-302` argues against) unavoidable?
- **11 (gate):** `check.py` records phases from env; given P1-1, would you accept the gate
  deriving its run from the ledger instead? It couples the gate to `agentjobs.dispatch`
  at import time, which `record_phase` already does behind a try/except.
- **2 (docs):** `agent-dispatch-design.md` header (P4), the poller-reaps claim (P3-15), the
  "agents do not have browsers" argument (P1-2), the secrets-in-env advice (P1-1) and §2a's
  "nothing implemented" are the five lines in that document I would flag first.
- **1 (context):** ENGINEERING.md and ALLAGENTS.md both describe the scripted finish as what
  Approve does; on this machine it is configured off (P3-17). Static prose describing a
  dynamic setting that is off.
- **5 (queue):** `_claim_or_verify` passes a `draft` through to dispatch (P2-8). Does the
  queue's claimability rule have a single function the guards should be calling instead of
  re-deriving "ready → claim, else check owner"?
