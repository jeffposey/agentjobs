# Durable dispatch harness (task-419)

`test_durable_replay.py` is the end-to-end proof for design section 9a of
[`docs/agent-dispatch-design.md`](../../docs/agent-dispatch-design.md). It runs the
production dispatcher, the durable controller, the poller's session follower, auth
recovery and the scripted finish against recorded incidents. Each crash is a real
process death, and a fresh coordinator recovers from it.

```bash
poetry run pytest tests/dispatch/test_durable_replay.py            # the harness
AGENTJOBS_REAL_DRIVER_CONTRACT=1 poetry run pytest tests/dispatch -k RealDriver   # opt-in
agentjobs execution failures --since 7                              # the same classes, over real runs
```

## What is simulated, and what is real

| Real | Simulated |
| --- | --- |
| Task stores (SQLite), the execution journal, the incident tables | The Claude CLI: a script answering `agents --json --all`, `stop`, `-p`, `--bg --resume` and a launch, with rows shaped like Claude Code 2.1.270's |
| `dispatch_task`, `Controller`, `poll_live_sessions`, `auth_recovery.tick`, `finish_task`, `walk_epic`, `DispatchLedger` | The credential store behind the probe: a file saying `ready` or the refusal text |
| Git repositories, branches, worktrees and merges | The agent's own work: the test hands off, closes or goes idle as the agent would |
| Child interpreters dying with `os._exit(9)` at a named line | The clock: `FakeClock`, injected into every runner, the controller and auth recovery |

Isolated real-driver evidence is only `TestTheRealDriverContract`. It is opt-in and
read-only: it lists the installed CLI's sessions and compares their fields with the fake's.
It launches, stops and probes nothing, and touches no credential. Fault injection never
runs against a real driver or a real authentication store.

**Determinism.** Nothing sleeps. The fake clock starts two hours ahead of the wall clock
and only moves when a test moves it, so a real timestamp written by the code under test
is always in its past, however loaded the machine. No assertion depends on how long
anything took.

## Fixture provenance

Fixtures are in `fixtures/`, with times in seconds relative to the incident's first
event. Each event names the log entries that established it. Session ids, run ids,
credential paths and people's words are removed.

| Fixture | Source entries | What it establishes | Deliberately not established |
| --- | --- | --- | --- |
| `task-224.yaml` `self_heal` | task-224 entries 6, 7, 30 | 2026-08-20: a login expiry healed in about two minutes, and nobody logged in | Why the refresh failed |
| `task-224.yaml` `dead_store` | task-224 entries 6, 8, 10 | 2026-08-21: a message sent into the dead session failed. Only a login cleared it, and the session needed a nudge afterwards | Whether any other process wrote the credential |
| `task-224.yaml` `spend_limit_probe` | task-224 entries 47, 48 | Six probe answers counted as healthy were all spend-limit refusals | — |
| `task-410.yaml` | task-410 entries 6–17 | A big-dawg autonomous dispatch parked on a login expiry one second in. An answer was buffered, the run was cancelled with no recorded requester, and it resumed on the default runner | **Who cancelled it.** The harness plays an explicit Stop and an administrative stand-down as separate branches, and claims neither happened historically |

Entry 29 of task-224 counted a second self-heal. Entry 30 withdrew that claim, so the
fixture carries only the one that stood.

## Capability matrix

What each driver's own store can prove about a launch its coordinator lost track of.
`TestCapabilities` checks these rows against `dispatch.controller.CAPABILITIES`, so a
change to either one fails the suite.

| Driver | Mode | Correlation | Absence proves "never started" |
| --- | --- | --- | --- |
| claude | session | `session_name` | no |
| codex | session | `none` | no |
| any | batch | `worker_receipt` | no |

A driver with correlation `none` gets no correlation from the fake, even though the fake
CLI could list the session. `test_a_driver_without_correlation_is_effect_unknown_even_when_the_fake_could_list_it`
proves the controller reports `effect_unknown` in that case, escalates once and never
relaunches.

## Guarantee matrix

| Guarantee (§9a row, task-414 owner bar) | Proved by | Human actions asserted |
| --- | --- | --- |
| A self-healing auth store needs nobody, and the run reaches review (a1, o1, o4) | `TestTask224::test_a_store_that_heals_itself…` | 0, and 0 notifications |
| A dead auth store: one notification, one login, no Answer, no Dispatch (a1, o4) | `TestTask224::test_a_dead_store_pages_once…` | 1 login, 1 notification |
| Billing is not success (a1) | `TestTask224::test_a_spend_limit_answer…` | — |
| task-410 without Stop: one continuation that carries the answer and the granted policy clause; a later retry runs on the recorded runner, group and posture, and is followed to its end (a1, a4, o3) | `TestTask410::test_without_a_stop…` | 0 |
| Explicit Stop suppresses every automatic continuation across restarts (a3) | `TestTask410::test_an_explicit_stop…` | the Stop itself only |
| Administrative stand-down keeps the answer and the approval, and merges once (a3) | `TestTask410::test_an_administrative_stand_down…` | 1 approval: the review `auto` requires |
| Launch: before intent, after intent, effect with a lost result, before the result commits (a2) | `TestLaunchBoundaries` | — |
| Resume message: dying between intent and effect, and after the effect (a2) | `TestNudgeBoundaries` | 1 escalation, only for the unprovable one |
| Outbox: dying before the task write, and after the write but before the acknowledgement (a2) | `TestOutboxBoundaries` | — |
| A handoff the API committed but never imported (a2) | `test_a_handoff_the_api_never_imported…` | — |
| Dying after `git merge` and before its receipt: the poll tick resumes it and it merges once (a2, o2) | `test_a_death_after_git_merge…` | 1 approval: the review `auto` requires |
| A five-hour usage limit parks on the service and resumes once after the reset (o5) | `test_a_usage_limit_parks…` | 0 |
| Envelope drift, budget reset, two-project collision, version mismatch, stale controller, parent grounding (a4) | `TestRegressions` | — |
| A missing capability surfaces as `effect_unknown`, and unknown ownership survives later admissions (a5) | `TestCapabilities` | 1 escalation |
| The failure rollup counts every injected class (a7) | `test_the_failure_rollup_reports…` | per class |

After every boundary, `assert_no_duplicate_effects` checks the same things: at most one
live session per task, no operation id written twice, and at most one dispatch and one
result entry per run. `test_the_invariants_would_catch_a_duplicate_write` shows the check
failing on a duplicate.

## Defects this harness found

All are fixed on task-419's branch, and each has a test that fails without its fix:

- An `effect_unknown` launch lost its ownership as soon as any other dispatch ran on the
  machine (`journal.attempt_evidence` ignored the launch marker).
- A signal still pending across a retry wedged every later replay of the execution with
  `ActivityConflict`, so the retried attempt was never followed.
- A flaky test's id was lost from the finish record because pytest colours the summary
  line.
- A walk whose supervisor had died stayed refused as "already being walked" whenever an
  unrelated process inherited the dead supervisor's pid. The walk now records the holder's
  creation-time identity. This is the **suspected** cause of the harness's one red, under
  a contended gate on 2026-09-13; that run printed nothing about how its walk ended. The
  supervisor child now prints it, so a recurrence names its cause.
