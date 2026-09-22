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

## The register

| # | test | failure | cause | class | status |
|---|---|---|---|---|---|
| 1 | `test_auth_recovery.py::TestSelfHealingNeedsNobody::test_a_store_that_recovers_is_probed_and_the_session_resumed_in_place` | `assert 'nudged' == 'recovered'` | two timelines in one test | clock race | **fixed** (task-518) |
| 2 | `test_execution_controller.py::TestBatchRecovery::test_a_surviving_worker_is_left_alone_and_its_death_is_proved_not_guessed` | `AssertionError: []` -- the controller concluded nothing | Windows pid reuse: a stranger answered a dead supervisor's pid | production defect | **fixed** (task-505) |
| 3 | various dispatch tests, `exit 1` with empty stdout and stderr | a killed process's signature | `_stop_batch` ran `taskkill /T /F` at a recycled pid | production defect | **fixed** (task-505) |
| 4 | `test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too` | times out | a 30s budget sized for a machine running one gate | test premise | open -- **task-325** |
| 5 | `test_dispatch_api.py::...::test_cancelling_a_live_run_stops_it_and_marks_it_cancelled` | `assert 'failed' == 'cancelled'` | the `cancel_requested` guard still races at 32 workers | production defect | open -- **task-370** |
| 6 | `test_dispatch_registration.py::TestRefusals::test_a_session_the_ledger_does_not_hold_is_refused` | the refusal names the ledger rather than the live session | process creation itself failed; the runner never ran | environment, surfaced as a defect | **cause removed** (task-518) |
| 7 | `test_execution_controller.py::TestLaunchCrashWindows::test_a_marked_launch_the_listing_cannot_find_is_unknown_not_absent` | `AssertionError: []` -- the controller decided nothing | the attempt store stamped `admitted_at` on the machine's clock while the controller read another | clock race | **fixed** (task-518) |

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

**The fix.** `agentjobs.dispatch.clock` is the subsystem's one time source, and
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
