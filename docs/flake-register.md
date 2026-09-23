# The flake register

Every test in this repository that has been seen to fail for a reason other than the code
being wrong, with what it was, how to reproduce it, and what was done. One page, because
before task-518 this lived in four closed tasks, two open ones and several people's
memory, and a flake nobody can find is a flake that gets rediscovered.

**A flake costs a merge, not a minute.** Inside a scripted finish a red gate costs the
whole stage, then its one permitted retry, then an escalation and a person -- and it
merges nothing. Task-147 paid that twice in one evening on a single timing assertion, on
a branch whose diff touched no Python.

## Where this lives, and who keeps it

`docs/flake-register.md`, linked from [the docs index](index.md). **There is one, and
this is it.** Before task-518 the same handful of failures lived in task-325, task-370,
task-505 and two of its neighbours, and were rediscovered from scratch about once a
fortnight; a fifth scattered task is the failure mode this page exists to stop.

**Whoever sees a flake adds it**, at the moment they see it, whether or not they are going
to fix it -- including a dispatched agent whose gate went red on a test its own branch does
not touch. Whoever fixes one updates its entry rather than closing a task quietly. Nobody
owns the page.

**The work is task-524.** Every open row here is a child of that epic, and a new row
that needs fixing gets a child there rather than a task of its own. When a scripted
finish goes red at pytest twice, its escalation names this page and hands over the rows
ready to paste -- nodeid, assertion text, how many gates were running and both gate logs
(task-526). Number them after the last row here and fill in the cause if you know it.

## How to use this page

- **Adding an entry** is the job of whoever sees the failure, at the moment they see it,
  even if they are not going to fix it. An entry with `status: open` and a copied
  traceback is worth more than a fix six weeks later with no record of what it was.
- **An entry needs a reproduction somebody else can run.** "Fails sometimes" is not one.
  Name the load: how many gates, which `-n`, which files. `scripts/flake_probe.py`
  (task-505) re-runs a set of files N times under a stated number of held slots and keeps
  every red run's output.
- **An entry is resolved when its cause is named**, not when a run comes back green. A
  flake passes most of the time by definition, so a green run is the null result.
- **No retry decorators, no rerun plugins, no `flaky` markers.** They convert a visible
  flake into an invisible one and make the gate report green for a suite that is still
  racing. The finisher's own one retry is a different thing: it is recorded as
  `flaky_test` and counted, and `agentjobs execution failures --since 14` reads the count.
  A retry that fails **the same way** -- same nodeid, same assertion text -- is recorded
  as `deterministic_in_context` instead, because chance does not repeat itself verbatim:
  the branch or the machine is the cause, and that is the first thing to rule out before
  adding a row here (task-526).

## The register

| # | test | failure | cause | class | status |
|---|---|---|---|---|---|
| 1 | `test_auth_recovery.py::TestSelfHealingNeedsNobody::test_a_store_that_recovers_is_probed_and_the_session_resumed_in_place` | `assert 'nudged' == 'recovered'` | two timelines in one test | clock race | **fixed** (task-518) |
| 2 | `test_execution_controller.py::TestBatchRecovery::test_a_surviving_worker_is_left_alone_and_its_death_is_proved_not_guessed` | `AssertionError: []` -- the controller concluded nothing | Windows pid reuse: a stranger answered a dead supervisor's pid | production defect | **fixed** (task-505); task-454 confirmed the 2026-09-18 red was this -- the test ran under 8s, so its 60s wait never expired -- and made it tick until the conclusion it asserts on |
| 3 | various dispatch tests, `exit 1` with empty stdout and stderr | a killed process's signature | `_stop_batch` ran `taskkill /T /F` at a recycled pid | production defect | **fixed** (task-505) |
| 4 | `test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too` | times out | a 30s budget sized for a machine running one gate | test premise | open -- **task-325**, and task-243, the same test filed a week earlier; whoever takes one closes the other |
| 5 | `test_dispatch_api.py::...::test_cancelling_a_live_run_stops_it_and_marks_it_cancelled` | `assert 'failed' == 'cancelled'` | the `cancel_requested` guard still races at 32 workers | production defect | open -- **task-370** |
| 6 | `test_dispatch_registration.py::TestRefusals::test_a_session_the_ledger_does_not_hold_is_refused` | the refusal names the ledger rather than the live session | process creation itself failed; the runner never ran | environment, surfaced as a defect | **cause removed** (task-518) |
| 7 | `test_execution_controller.py::TestLaunchCrashWindows::test_a_marked_launch_the_listing_cannot_find_is_unknown_not_absent` | `AssertionError: []` -- the controller decided nothing | the attempt store stamped `admitted_at` on the machine's clock while the controller read another | clock race | **fixed** (task-518) |
| 8 | `test_dispatch_journal.py::TestACancelLandingMidPoll::test_the_cancel_wins_and_the_poll_writes_nothing` | `the poller never reached its conclusion` | the poller arrived late, not never: a ten-second wall-clock budget for a thread that spawns a subprocess | test premise | **fixed** (task-522) |
| 9 | `test_task_queued_status.py::TestTheLabel::test_a_waiting_dispatch_reads_queued_and_a_task_without_one_still_reads_ready` | `assert 'In progress (claude)' == 'Queued'` | a controller tick between the runner naming a session and recording its dispatch stopped a live launch; the fixture's concurrent poll was where it showed | production defect | **fixed** (task-522) |
| 10 | `test_execution_controller.py::TestLaunchCrashWindows::test_a_fresh_process_performs_the_recovery` | `assert 'never launched' in '\n'` -- an empty report | `attempt_evidence` asks a bare `process_alive` with no start-time guard, so a reused pid keeps a dead launcher's attempt owned | production defect | open -- **task-489** |
| 11 | `test_dispatch_api.py::TestDispatchRuns::test_a_finished_run_reports_its_outcome_and_its_captured_output` | `sqlite3.ProgrammingError: Cannot operate on a closed database` | a supervisor thread outlives its test and writes through a store the fixture has closed | teardown lifetime | open -- **task-497** |
| 12 | whichever test an xdist worker happens to be running (`test_auto_dispatch.py` and `test_dispatch_api.py` seen) | `Windows fatal exception: access violation`, `worker 'gwN' crashed` | `_classify_batch_exit` reads SQLite from a background thread while the fixture closes the database | teardown lifetime | open -- **task-438** |
| 13 | `test_epic_supervision.py::TestTwoWalkersOfOneEpic::test_a_childs_run_started_by_another_process_on_this_authorisation_is_adopted[already-closed]` | `assert 1 == 0` -- the sibling-dispatch subprocess exited 1 with **empty stdout and empty stderr** | not named. Entry 3's signature exactly, on a test entry 3 does not cover; seen at four gates on this machine and green alone | environment, or entry 3's cause not fully removed | open -- see below |
| 14 | `test_dispatch_poller.py::test_the_tick_takes_back_an_ask_whose_reason_has_been_resolved` | `AssertionError: []` -- the tick took nothing back | not named; the process-global sweep throttle is the first suspect | unknown | open -- **task-546**; seen by finish `fin_8f638f51` and by task-522 |
| 15 | `dispatch/test_durable_replay.py::TestRegressions::test_two_projects_with_one_task_id_share_nothing_but_the_machine_slots` | `exactly one remaining slot was awarded`, `assert 3 == 2` -- a second `task-001 recoverable` launch | not named. Reproduces on `main` df398c13 under load: 1 of 48 runs with 12 copies at once, 30 of 30 green run alone (task-525, 2026-09-23) | production defect, until shown otherwise | open -- **task-549**; seen by finish `fin_8f638f51` |
| 16 | `frontend/e2e/capture-draft.spec.ts:223` › a rebuild still reloads a tab where nobody is typing | `page.waitForFunction: Timeout 20000ms exceeded` at line 233 -- the idle tab never reloaded | not named | unknown | open -- seen by finish `fin_8f638f51` |
| 17 | `test_dispatch_atomic_yaml.py::TestTheDocumentIsNeverHalfWritten::test_a_reader_never_sees_a_partial_document` | `461 of 1153 reads saw a document without run_id` | not named. Seen once in about ten full runs on 2026-09-23 (task-525's measurements) | production defect, until shown otherwise | open -- **task-550**, needs a reproduction |

**14-16 observed** 2026-09-23 about 21:50 UTC in task-526's finish `fin_8f638f51` on
`b6be1fd9`, a branch touching only the finisher's classification, the failure rollup and
docs. Attempt 1 (`scripts/check.py`, `-n 13, sharing this machine with 3 gates`): 14 and
15 red, 5681 passed; log `~/.agentjobs/finishes/fin_8f638f51/gate.log`. Its retry
(`--from pytest`, `-n 26, alone on this machine`): pytest all green, then 16 red in `e2e`;
log `gate-retry-1.log` beside it. 14 and 15 passed run alone on the same commit. Three
different tests over two attempts, none repeating: the flake signature.

### 13. A sibling dispatch that exits 1 saying nothing

**What it looked like.** `assert 1 == 0` inside `TestTwoWalkersOfOneEpic.sibling`, which
dispatches a child from another interpreter with `subprocess.run(..., timeout=180)`. The
`CompletedProcess` carried `returncode=1`, `stdout=''` and `stderr=''`. A process that
fails *for a reason* says something on one of those streams; one that says nothing on
either was killed, or never got far enough to speak.

That is **entry 3's signature**, on a test entry 3 does not list. Entry 3's cause -- a
`taskkill /T /F` aimed at a recycled pid -- was removed in task-505, so either this is a
second producer of the same shape or that removal is incomplete. It is deliberately left
unnamed here rather than attributed to entry 3 on a resemblance.

**Observed** 2026-09-22 16:29-16:52 local, in task-523's pre-handoff gate, on a branch
whose diff touches neither `epic.py` nor dispatch. The gate said *"Sharing this machine
with 3 gates, so pytest runs at -n 10 rather than -n auto"*; 5566 passed, this one and
one unrelated corpus failure red. Both parameters of the same test passed on the
immediate re-run, alone, in the same worktree and the same interpreter.

**Reproduction.** Not reduced. The load is the thing to reproduce: four concurrent gates
on this machine, `-n 10`. `scripts/flake_probe.py` over `tests/test_epic_supervision.py`
under held slots is the tool for it. Until somebody does that, the entry's value is the
signature: **an exit 1 with nothing on either stream is not a test failure, it is a
killed or stillborn process**, and reading it as a defect in the code under test costs an
afternoon.

### 1. The two timelines in `test_auth_recovery` (clock race, fixed)

**What it looked like.** `assert 'nudged' == 'recovered'`. Three failures on 2026-09-21,
two of them inside task-147's scripted finish, which stopped that merge. It passed alone
in 3.6 seconds every time anybody looked.

**The cause, exactly.** The harness stamped its simulated moments from `datetime.now()`
at the point each line of the test ran:

```
machine.reply_on_wake(offset_seconds=124)   # reply stamped at T_reply + 124s, + 126s
machine.poll(); machine.tick(0); machine.tick(61)
machine.tick(122)                           # the resume, at T_tick + 122s
machine.tick(130)                           # expects: recovered
```

Recovery needs a real reply *after* the resume was sent, so it needs
`T_reply + 126 > T_tick + 122` -- that is, fewer than **four seconds** of wall clock
between two lines of the test. The three calls between them spawn subprocesses, and
subprocess creation on this machine was measured at 0.064s to 16.7s for identical argv
depending on what else was running. The margin was whatever the machine had left.

`auth_recovery.py` looked converted -- it took a `clock=` parameter and a `monotonic=`
parameter -- but four `_parse(row[...]) or utcnow()` fallbacks called the wall clock, and
so did forty-three call sites elsewhere in the subsystem. A partly injected clock is
worse than none, because it looks finished.

**The fix.** `agentjobs.clock` is the process's one time source, and
`tests/skipping_clock.py` is a clock a test owns that advances itself when every
registered waiter is blocked. The harness has one origin; every simulated moment is an
offset from it, and no amount of wall clock can pass between two of them.

**Reproduction, before the fix:** a full `-n auto` run of the suite on a machine with
other work on it. Observed 2026-09-21 at 17:51-18:04 local, 5485 passed and this one
failed. Alone it passes.

**Reproduction, after:** the same, plus `scripts/threshold_probe.py`, which shows the
test still goes red when the production threshold it covers is taken away.

### The deterministic pair for entry 1

A flake passes most of the time by definition, so a green run proves nothing. What proves
this one is naming the mechanism and then producing it on demand.

**The mechanism is a margin of four seconds.** Recovery needs the session's reply to be
later than the resume that asked for it. The reply was stamped from one `datetime.now()`,
in `reply_on_wake`; the resume from another, four simulated seconds earlier, in
`tick(122)`. Whatever wall clock passed between those two lines of the test came straight
out of the margin, and the lines between them spawn subprocesses.

**So spend it.** Six real seconds inserted at that point, on `05b4569b`:

| tree | runs | result |
|---|---|---|
| `main`, six seconds inserted | 5 | **5 red**, `AssertionError: assert 'nudged' == 'recovered'` |
| `main`, unmodified | 3 | 3 green |
| this work's branch, same six seconds | 5 | **5 green** |

The failure text is the one the register's entry 1 quotes and the one task-147's finish
recorded twice, which is what makes this a reproduction of the reported defect rather than
a new test that happens to fail. The control arm matters as much: unmodified `main` is
green, so the six seconds are the cause and not the harness.

`tests/test_auth_recovery.py::TestSelfHealingNeedsNobody::test_the_recovery_survives_wall_clock_passing_mid_scenario`
is that reproduction, kept. It costs the suite six seconds and it is the only assertion in
that file that would have caught the defect; every other one passes on the broken code
whenever the machine happens to be quiet.

### 6. A refusal that names the wrong thing (environment, cause removed)

**What it looked like.** The test asks for the refusal you get when you claim a session
the ledger does not hold, and asserts the message names the session that *is* live, so
the answer is actionable. Under load it instead got the refusal for "the session ledger
could not be read at all", naming a path under `C:\Users\...`.

**The cause.** `DispatchRunner.ledger()` shells out to the runner, and under enough load
Windows fails to create the process -- `0xC0000142`, seen in this repository's own shell
output on the same machine the same day. `ledger()` reported that as "could not read the
session ledger", which conflates *the runner said no* with *the machine could not start
the runner*. They are different facts and only the first is evidence about a session.

**What was done.** `DispatchRunner._run_listing` now separates them, and a spawn that
failed for want of machine resources -- an `OSError` from `CreateProcess`, or the exit code
`0xC0000142` the Windows loader gives when it cannot initialise a process -- is tried once
more after a quarter of a second before being reported. Once, not until it works: a machine
that cannot start two processes a quarter of a second apart has a problem worth surfacing.
A runner that *ran* and exited non-zero is not retried, because that is its own answer.

This is a production improvement rather than a test convenience. A poller tick that
concluded "could not read the ledger" because `CreateProcess` momentarily failed is a
false negative about somebody's live run, not only about a test.

**Reproduction:** `TestAMachineThatCouldNotStartTheRunner` in
`tests/test_dispatch_registration.py` -- four tests that make the spawn fail in each of
the two ways, check it recovers, check a doubly-refused spawn says so, and check that a
runner which answered badly is not retried.

**Honestly stated limit:** the *original* failure has no deterministic reproduction. It was
seen once, in task-518's spec, and the cause above is read off the failure text plus the
same Windows error appearing in this repository's own shell output on the day the task was
worked. What is proved is that the two facts are now distinguished and that the transient
one recovers; what is not proved is that this was the only way that test could produce that
message. Catch it again and add the evidence here rather than assuming.

### 7. A third clock, found by measuring (clock race, fixed)

**What it looked like.** `AssertionError: []` -- the controller's tick produced no lines
where the test expected it to declare a launch `unknown`. Two reds in ten runs of the six
flake-prone files at `-n 16` with two gates holding slots, both the same test. It also
appears in task-505's own corpus, from a gate run on 2026-09-20, so it predates this work.

**The cause.** `ExecutionStore` defaulted its clock to `datetime.now()`. An attempt's
`admitted_at` is stamped there, by the child process that performs the launch; the
controller subtracts it from its own now to decide whether the launch has missed its
600-second reconcile deadline. Two clocks, so the subtraction silently loses however long
the child took to start -- between 0.06s and 16.7s on this machine -- and `advance(601)`
bought 601 seconds minus that. Under load it was not 600.

Worth stating plainly: **this is the same defect as entry 1, one layer down, and the work
that fixed entry 1 made it reproducible.** Pointing the dispatch subsystem at one clock
turned a race that needed a busy machine into one that needed only a slow child, which is
how ten runs found it twice where a gate had found it once in a fortnight.

**Reproduction:** `scripts/flake_probe.py runs --times 10 --slots 2`, which keeps every red
run's full output under `flake-probe-reds/`.

### The rate arm, with its own limits stated

The deterministic pair above is the proof; this is the context, and it is reported with
what it cannot show. `scripts/flake_probe.py runs --times 10 --slots 2`, which re-runs the
six flake-prone files with gates holding slots against them, on 2026-09-21 evening:

| tree | condition | result |
|---|---|---|
| after the one clock, before the store was on it | 10 runs, `-n 16`, 2 gates | **2 red**, both entry 7 |
| after entry 7's fix, before entry 7's test was corrected | 3 runs, `-n 8`, 4 gates | **3 red**, all the same test, deterministic |
| final tree | 10 runs, `-n 8`, 4 gates | **0 red**, 204s to 240s |

**Do not read 0/10 as "the flakes are gone".** A 13% event -- which is roughly what these
were over gate runs -- misses ten times in six tries out of ten, and task-505 recorded
exactly that: 0/10 on a tree where the defect was present. The rate arm is worth running
because a *red* in it is information and because it is where entry 7 was found; its green
is not the evidence. The evidence is a named cause per entry and a reproduction that fails
before the fix and passes after it.

**The middle row is the one worth keeping.** Three reds in three runs is not a flake rate,
it is a broken test, and it only became reproducible *because* the clocks were made to
agree. Making a race deterministic is a good outcome, and it is what a register is for:
that run would otherwise have read as "the flake is still there".

### 4 and 5, still open, with what is known

Neither is this task's to fix -- each has its own -- but a register with only the solved
ones in it is a trophy cabinet.

**4. `test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too`
(task-325).** Times out rather than failing an assertion. Its own comment says its
30-second budget is "large enough that a loaded machine cannot exhaust it", which was
written for a machine running one gate; this one runs two or three. Not a clock race: the
thing being waited for is a real process group dying, which is the one category
`agentjobs.clock` deliberately leaves alone. **Reproduction:** run the file while two other
gates hold slots. Task-325 has to settle whether the grandchild genuinely survives or the
check is simply slow, and only the second of those is a budget question.

**5. `test_dispatch_api.py::...::test_cancelling_a_live_run_stops_it_and_marks_it_cancelled`
(task-370).** `assert 'failed' == 'cancelled'` -- the race `runner.py`'s `cancel_requested`
guard was written to close, still open at 32 workers. A production defect rather than a
test premise: a cancelled run reported as failed is wrong on somebody's dashboard, not only
in a suite. **Reproduction:** the file at `-n 32`; it cost task-268's scripted finish a
whole gate.

### 8 and 9, seen on 2026-09-22 (fixed -- see below)

Added by the task-147 run whose gate they stopped, under the rule above: whoever sees a
flake adds it, at the moment they see it, whether or not they are going to fix it. Task-522
is the fix. Neither is reachable from that branch's diff, which touches models, the store
mapper, `update_task`, `dispatch/checks.py`, one CLI command, one route and a migration.

**What makes these two a pair worth reading together.** One scripted finish, `fin_ce2a7482`,
red at `pytest` twice on the same commit -- and **a different test each time**, each once,
out of 5553 that passed. That is the signature: a deterministic defect does not move. The
pytest stage took 641s and 625s against the 52s `docs/performance.md` records for a machine
to itself, so the load was roughly an order of magnitude, with three agent sessions live.

**8. `test_dispatch_journal.py::TestACancelLandingMidPoll::test_the_cancel_wins_and_the_poll_writes_nothing`.**
Fails at `tests/test_dispatch_journal.py:164`:

```
assert reached.wait(10), "the poller never reached its conclusion"
```

The poller thread shells out to the fake CLI, and entry 2 above already records subprocess
creation on this machine at **0.064s to 16.7s for identical argv** depending on what else
is running. A ten-second budget is therefore not a property of the code under test. Same
class as entry 4, and the same question has to be settled first: whether the poller reaches
its conclusion late or not at all. A bigger number answers neither.

**9. `test_task_queued_status.py::TestTheLabel::test_a_waiting_dispatch_reads_queued_and_a_task_without_one_still_reads_ready`.**
`assert 'In progress (claude)' == 'Queued'` at line 95. This one names its own mechanism in
the captured stdout, which is why it needed no investigation:

```
Dispatch poll journal: imported 3 task-log entries
Dispatch poll exe_bc7ff479fe8085fa: launch_reconcile: stopped unfollowable session b55b0000
```

`fill_the_machine` holds the ceiling with runs whose sessions do not really exist, and
`launch_reconcile` is entitled to judge exactly such a session unfollowable and stop it.
That frees a slot, so the dispatch that was supposed to queue is admitted, claims the task,
and the label reads `In progress (claude)`. Whether the reconcile lands before or after the
assertion is a race decided by load.

**A test premise, not a production defect.** Stopping an unfollowable session is the
behaviour we want; the fixture is what assumes it will not happen. `fill_the_machine` lives
in `tests/test_start_pause.py` and is imported by more than this file, so every caller
inherits the premise.

**Reproduction for both:** `poetry run python scripts/flake_probe.py runs --times 20
--slots 2 --files tests/test_dispatch_journal.py tests/test_task_queued_status.py`. Stated
rather than run: the two files pass 42 of 42 in 95s alone, so the rate is what has to be
measured, and measuring it honestly means a quiet machine that was not available. Task-522
owns the pair of rates. **What is evidence here is the two gate logs**, kept at
`~/.agentjobs/finishes/fin_ce2a7482/gate.log` and `gate-retry-1.log`.

**Related, and not the same.** Task-513 budgets the run ceiling so concurrent tasks stop
tripling the gate. That lowers the rate both of these fire at. It does not make either
premise true.

### 8 and 9, resolved 2026-09-23 (task-522)

The text above is what was known when they were filed. It is kept as written. One
sentence in it turned out to be wrong, and that is corrected below rather than edited
out.

**8 arrived late, not never.** The kept `gate.log` settles it. Its warnings summary holds
a `PytestUnhandledThreadExceptionWarning` for `Thread-263 (poll_live_sessions)`: the
traceback runs `follow_session` -> `poll_session` -> `_finish_session` ->
`claim_conclusion` -> the test's own `held`, and ends in *"the test never released the
poller"*. So the poller that "never reached its conclusion" did reach it, after the ten
seconds had run out, and then waited out ten more seconds for a release the failed test
would never send. That makes it a budget question. As entry 4 says, a bigger number does
not answer a budget question.

**The fix for 8 waits on facts.** "Never reached" can only be observed as the poller's
thread ending without arriving, so the test now waits for one of two things: the
conclusion, or the end of the thread (`reached_or_finished`). The test also always
releases and joins the poller. The test itself sets no bound. The bound comes from the
code under test: every subprocess the poll runs has the runner's own 60-second timeout.
`skipping_clock` was the tool the spec suggested, but it does not fit: nothing on this
path reads a clock, and the ordering is enforced by events.

| arm | result |
|---|---|
| old test, poller made 12s late (`poll_session` sleeps first) | **red**, `the poller never reached its conclusion`, the text this entry quotes |
| new test, same 12s | green |
| new test, hold removed, so the poll concludes before the cancel | **red** (`cancel.stopped` is False), and it fails fast rather than hanging |

Worth knowing: breaking the journal's compare-and-set (the early return removed and
`won` forced true) does **not** turn this test red. The outbox's shared
`result_operation_id` and the run's meta each refuse a second ending as well. That is
defence in depth, and it means this test is not what guards the compare-and-set on its
own.

**9 was a production defect; the "test premise" paragraph above is wrong.** The runner
writes `session_id` to the run's meta the moment the session exists, and writes
`dispatch_entry_id` only after `_record_dispatch` returns. `perform_launch_reconcile` sent
any record carrying a `session_id` straight to `_adopt_session` without asking whether the
launcher was still alive. So a tick that landed between those two writes found "a session
with no dispatch entry" and stopped a launch that was still in progress. The server's
lifespan poll runs beside the dispatches it serves, so this could happen on the live
dashboard, not only under `served`. The fixture was a faithful witness.

**The fix for 9 is in the controller.** While the admitting process is alive and the
launch is younger than `launch_observation_seconds` (120), such a run counts as a launch
in progress and the tick waits. A launcher that never records anything is still stopped
once that window has passed. This rule already existed one branch further down, for a
launch with no session id yet, and now it covers both.

| arm | result |
|---|---|
| controller tick run from inside `_record_dispatch`, without the guard | **red**, `launch_reconcile: stopped unfollowable session b55b0000`, the line this entry quotes |
| the same, with the guard | green |
| launcher stuck 121s inside that window, with the guard | still stopped |

Both of these are kept as tests: `TestLaunchCrashWindows::test_a_tick_inside_a_live_launch_stops_nothing`
and its neighbour in `tests/test_execution_controller.py`.

**The other callers of `fill_the_machine`.** Only `test_task_queued_status.py` polls
while the machine fills, because its `served` fixture starts the app lifespan. The tests
in `test_start_pause.py` tick on the test thread after the fill has finished, when every
run already has its dispatch entry. Its one `TestClient` is used without `with`, so no
lifespan starts there. With the guard, no poll can empty the fixture's machine: each run
it holds is either mid-launch with a live launcher, or recorded and followable.

**The rate pair, with its limits.** `scripts/flake_probe.py runs --times 10 --slots 2`
over the two files, on 2026-09-23 between 16:43 and 16:48 local:

| tree | condition | result |
|---|---|---|
| `main`'s controller and both test files | 10 runs, `-n 13`, 2 gates holding slots | 0 red, 9.6s to 15.5s a run |
| this work | 10 runs, `-n 8`, 3 gates holding slots (a third gate started between the arms) | 0 red, 11.1s to 16.2s a run |

**A null pair, as expected.** Both failures needed roughly ten times the machine's
normal load (the gate's pytest stage took 641s against 52s). The probe holds slots and
does not create that load. The evidence is the two deterministic pairs above. The rate
pair is recorded because the spec asked for it and because a red result in it would have
meant something.

### 14. The retraction sweep that swept nothing (open, task-546)

Seen twice on 2026-09-23, independently: in finish `fin_8f638f51` (above, with 15 and 16),
and in task-522's own run. In the second it failed with `AssertionError: []` while
task-522 was running `pytest -n 8` over eight
dispatch files on 2026-09-23 (397 passed). It passed alone four times, on that branch
and on `main`, and that branch's diff does not reach it. The first suspect is not
confirmed: `_last_retraction_sweep` is process-global, and this test, unlike its
neighbour, does not reset it. The test sets the interval to zero, so the sweep skips only
if the stamp reads *later* than this test's `now`: a thread still polling, or a stamp
taken from a different clock. **Reproduction:** not reduced yet.

### 10, 11 and 12, filed before this page existed, open

Three tasks that predate the register and were missed when it was written on 2026-09-21.
Added 2026-09-22 by the review of task-518. Each is a child of task-524, the epic that
holds every open row here.

**10. `test_execution_controller.py::TestLaunchCrashWindows::test_a_fresh_process_performs_the_recovery`
(task-489).** Seen 2026-09-19 in task-482's handoff gate with three gates running: the
test crashes a launch before its marker is written, runs a controller tick in a fresh
subprocess, and expects the report to name the attempt as admitted but never launched.
The report came back empty. `attempt_evidence`'s never-launched branch asks a bare
`process_alive(holder_pid)`, which answers whether *some* process holds that number, and
under `-n auto` the crashed launcher's pid is routinely reissued before the tick. The
store's own holder check already compares process start times for exactly this reason;
this branch does not. **A production rule, not a test premise**: a run whose launcher died
keeps its task and its slot until a person notices. **Reproduction:** construct the state
directly, a live pid whose process started after the attempt was admitted, rather than
loading the machine and hoping.

**11. `test_dispatch_api.py::TestDispatchRuns::test_a_finished_run_reports_its_outcome_and_its_captured_output`
(task-497).** Seen 2026-09-20 in task-175's scripted finish, green on the retry. The
traceback ends in `connection.write()` executing `BEGIN IMMEDIATE` on a closed handle,
reached from `manager.handoff` on a `dispatch-run_*` thread. A lifetime question, not a
timing one: something closed the writer while a handoff was still in flight through it,
and the leading candidate is a supervisor thread outliving the test that started it. The
same message appears in task-438's logs from the same thread name, so 11 and 12 are very
likely one cause with two faces. **Reproduction:** `tests/test_dispatch_api.py` alone
under `-n 8`, repeatedly; task-438 measured one worker crash in nine runs on a branch and
none in nine on `main`, so the rate is low and the instrument has to be stated.

**12. An xdist worker crash, on whatever test it was running (task-438).** `Windows fatal
exception: access violation` with `_classify_batch_exit` -> `manager.get_task` ->
`store.load_task` on the current thread, during a `served` fixture's TestClient teardown.
Seen 2026-09-06, 09-07, 09-13 (three finishes) and 09-18; the finish logs are listed on
the task. A native crash takes the worker and every test still queued on it, so the test
id in the report is not the culprit. **Reproduction:** as for 11, and grep
`~/.agentjobs/finishes/*/gate*.log` for `access violation`.

## Proving a converted test still catches its threshold

Three of the entries above were fixed by giving the code a clock a test owns, so a
ninety-second window is waited out in no time. **The way that goes wrong is invisible in a
diff**: shortening the window gets you the same green test in the same second, and the test
stops covering the rule. `scripts/threshold_probe.py` is the check -- it removes a
production threshold and asserts the test notices.

Its run on 2026-09-21, after the conversions:

| case | constant removed | verdict |
|---|---|---|
| `standdown-window-unbounded` | `STAND_DOWN_CONFIRM_SECONDS` -> unbounded | caught (the test stops returning, which is what an unbounded wait is) |
| `auth-probe-period` | the auth policy's 60s probe period | caught |
| `auth-notify-after` | the 300s before anybody is paged | caught |
| `auth-nudge-lease` | `NUDGE_LEASE_SECONDS` | caught |
| `auth-confirm-seconds` | `CONFIRM_SECONDS` | caught |

**Two cases were written and reported MISSED, and that is the tool working.** Both were
removed from the probe and recorded in its `NOT_COVERED` note rather than adjusted until
they passed:

- `test_a_busy_session_stands_down_and_the_approval_merges` stayed green with the
  stand-down window set to zero. The session it stands down is observed stopped on the
  first poll, so the window is never consulted: that test covers the transfer, not the
  window. Its neighbour covers the window.
- `test_client_retry.py` monkeypatches the backoff to zero and asserts on the *number of
  attempts* -- deliberately, so it means the same thing on every machine. It covers the
  retry loop rather than the constant, and a probe that replaces the constant is answered
  by the fixture before the test runs.

**Tests that have no threshold at all are not a gap.** `test_show_task_not_found` and the
two MCP startup probes each assert an *absence*, and were slow only because they waited out
a retry budget meant for riding through a restart. There was never a wait they asserted on,
so there is nothing to take away.
