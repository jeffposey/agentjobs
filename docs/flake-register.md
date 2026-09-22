# The flake register

Every test in this repository that has been seen to fail for a reason other than the code
being wrong, with what it was, how to reproduce it, and what was done. One page, because
before task-518 this lived in four closed tasks, two open ones and several people's
memory, and a flake nobody can find is a flake that gets rediscovered.

**A flake costs a merge, not a minute.** Inside a scripted finish a red gate costs the
whole stage, then its one permitted retry, then an escalation and a person -- and it
merges nothing. Task-147 paid that twice in one evening on a single timing assertion, on
a branch whose diff touched no Python.

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
| 5 | `test_dispatch_ledger.py::...::test_cancelling_a_live_run_stops_it_and_marks_it_cancelled` | `assert 'failed' == 'cancelled'` | the `cancel_requested` guard still races at 32 workers | production defect | open -- **task-370** |
| 6 | `test_dispatch_registration.py::TestRefusals::test_a_session_the_ledger_does_not_hold_is_refused` | the refusal names the ledger rather than the live session | process creation itself failed; the runner never ran | environment, surfaced as a defect | **cause removed** (task-518) |

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

### 6. A refusal that names the wrong thing (environment, mitigated)

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
