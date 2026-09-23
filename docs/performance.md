# Measuring performance

Four questions, four tools:

| Question | Tool | Where |
| --- | --- | --- |
| How long does the product take to answer? | `scripts/bench.py` | [below](#producing-a-beforeafter-pair) |
| How long does a restart leave the port dead? | `scripts/bench_startup.py` | [What a restart costs](#what-a-restart-costs) |
| What does the repository gate cost? | `scripts/check.py` | [What the gate costs](#what-the-gate-costs) |
| Where does dispatched agent time go? | `scripts/run_report.py` | [Where agent time goes](#where-agent-time-goes) |

The rules derived from all four — quote a command and a date rather than a bare count,
state a before/after pair, prefer parse counts to wall clock — are in
[ENGINEERING.md](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md#testing). This file is the working detail and the
measurement history behind them.

`scripts/bench.py` measures how long AgentJobs takes to answer, on three surfaces: the
REST API, the CLI, and the browser interaction of opening a task. It exists so that a
change which claims to make the product faster can say by how much.

```bash
poetry run python scripts/bench.py
```

## Producing a before/after pair

This is the normal use. Measure, change something, measure again:

```bash
poetry run python scripts/bench.py --json before.json
```

```bash
poetry run python scripts/bench.py --json after.json --compare before.json
```

The comparison prints each surface's median before and after, the factor between
them, and the change in task files parsed. Paste that table into the task record —
the performance work in the backlog is reviewed on recorded numbers, and "feels
faster" is not evidence.

## Reading the report

```
  corpus      synthetic: 112 tasks served (imported from 112 files, 1,209,921 bytes)

API
------------------------------------------------------------------------------
  surface                                   p50        p95   parses   srv ms
  GET /tasks                           3786.2ms   3801.0ms      476   3694.0
```

The `GET /tasks` row is a **2026-07 figure from the file era**, kept because it is the
one that makes the parse column legible: 476 parses of a 119-file corpus. Nothing
resembling it can be produced today — see
[Figures from before the cutover](#figures-from-before-the-cutover).

- **corpus** — how many task rows the server held, and the files they were imported
  from. Rows, because a file count says nothing about what was served (task-408).
- **p50 / p95** — median and 95th-percentile wall time from the client, over the
  configured iterations, after one discarded warmup request.
- **parses** — how many task files the server read and parsed from disk to answer.
  Read from the `X-Task-Parses` response header, and zero on every ordinary request.
- **srv ms** — time spent inside the application, from the `X-Response-Time-Ms`
  header. A large gap between this and p50 points at transport or client overhead
  rather than at the server.

**The parse count is now a regression alarm rather than a dial.** Records are rows, so
no request parses a task file and the honest reading of the column is that anything other
than zero is a request that has quietly started reading a directory again. Only an import
legitimately parses files. It is kept for exactly that: an assertion that costs nothing
and holds a property the whole migration was for.

What it used to be is worth knowing, because the reasoning still applies to any future
counter. Wall-clock time depends on the machine, what else it is running, and the
weather; a count of work done does not. A request that parsed a 119-file corpus 476 times
was doing four times too much work on any hardware, and the change that dropped it to 119
had demonstrably fixed something no timing could have proved. Prefer an assertion on work
done over an assertion on elapsed time wherever you can construct one.

## The three headers

Every API response carries them, not just benchmark runs. They are the way to
attribute a slow request without attaching a profiler:

| Header | Meaning |
| --- | --- |
| `X-Response-Time-Ms` | Wall time inside the application. |
| `X-Task-Parses` | Task files read and parsed from disk while serving the request. |
| `X-Corpus-Loads` | Times every task in the project was loaded from the store. |

Both counters are available to tests through
`agentjobs.instrumentation.count_task_parses()`, which is how a test asserts that one
request never parses the same file twice and never loads the corpus twice.

**`X-Corpus-Loads` exists because the parse count stopped being able to see this class
of defect** (task-485). Parses went to zero when records became rows, and a request
could then load the whole corpus nine times while every counter the application had read
zero — which is exactly what `GET /dashboard` was doing. One is the expected value for a
request that needs the corpus at all; zero for `/revision`, which answers from a counter
and must never load anything.

## Choosing a corpus

The benchmark never runs against your live project. It writes task files into a
temporary project, **imports them into a database named from that directory**, serves
that on its own port, and deletes it afterwards — so a run cannot write to the real
backlog and is unaffected by whatever a long-running server happens to hold in memory.

```bash
poetry run python scripts/bench.py --corpus synthetic --tasks 200
poetry run python scripts/bench.py --corpus real --source <a directory of task YAML>
```

`--corpus real` copied this repository's own tracked records until task-380 retired them,
so it now needs a directory naming: `agentjobs storage export <dir>` writes one. Both
modes take the same road in — the YAML is an import *source*, never a backlog, because
records have been rows since task-402.

**A run proves the corpus is served before it times anything** (task-408). It asserts
that the running server lists tasks and that the sample task's detail endpoint answers
200, and exits non-zero if either fails. That is not decoration: between the cutover and
task-408 the benchmark wrote its YAML into a directory nothing read, and every run timed
an empty store while printing a corpus line that said 112 files.

The synthetic corpus is generated at a size you choose, with realistically sized
records — prose, a multi-entry log, acceptance criteria, a dependency. Use it whenever
a number needs to stay stable over time: a threshold tuned against today's backlog
becomes a failing test when the backlog grows, through no fault of the code.

**Two runs are only comparable if they measured the same corpus.** Every report states
the rows the server held as well as the file count and total bytes it imported them
from, and `--compare` warns when the two do not match — or when the baseline has no row
count at all, which marks it as one of the empty-store runs below.

### Figures from before the cutover

**No `scripts/bench.py` figure recorded before 2026-09-19 is comparable with one
recorded after it**, and they are not comparable with each other either. Three eras, and
the middle one is the trap:

| When | What a run measured |
| --- | --- |
| Before 2026-09-07 | A real corpus, read from task YAML by the file backend. Sound in their own terms; they measure a backend that no longer exists. |
| 2026-09-07 to 2026-09-19 | **An empty store.** The file backend was gone (task-402) and the benchmark was still seeding a directory, so the server held nothing however large the corpus line said. Zero parses on every surface and a 404 from the detail endpoint were the only signs, and neither stopped the table printing. |
| From 2026-09-19 | The seeded rows, asserted to be served before any timing (task-408). |

Two consequences worth stating plainly, because a JSON baseline from any era still
loads:

- A `--compare` against a baseline with no `tasks` key in its corpus block is comparing
  against an empty store. The tool says so; do not quote the factor.
- The figures recorded elsewhere in this document under **What the gate costs** and
  **Where agent time goes** come from `scripts/check.py` and `scripts/run_report.py`,
  which read the suite and the run records. Neither seeds a corpus, so neither is
  affected by any of this.

The first post-fix run, for the record — this checkout, 112 synthetic tasks, 5
iterations, 2026-09-19:

```
  corpus      synthetic: 112 tasks served (imported from 112 files, 1,209,921 bytes)
  GET /dashboard                                    372.8ms    583.3ms        0    340.9
  GET /tasks                                        217.3ms    353.4ms        0    346.3
  GET /tasks/{id}/detail                            157.4ms    186.4ms        0     92.8
  click task row -> detail rendered (warm app)      470.0ms    526.0ms
  cold load -> task detail rendered                1383.0ms   1411.0ms
```

**It is a baseline, not a regression.** The numbers are far larger than anything the
middle era produced, and no factor should be computed from that: the middle era was
serving nothing. The empty-store run task-408 was filed on is the illustration — five
files, one iteration, `GET /dashboard` at 12.5ms, `0 parses` everywhere and the detail
endpoint 404ing. A run that answers for no records is fast, and that is all such a
figure says.

### One load per request (task-485, 2026-09-19)

`GET /dashboard` took 1,755ms against this repository's 488 records, and **nothing in it
was a slow query**. `build_dashboard_snapshot` asks the corpus six separate questions
and `dependency_facts` three more; each called `storage.list_tasks()`, which assembles
488 `Task` aggregates with 5,545 log entries in about 160ms. Eight of the nine loads
were the same corpus, and no counter in the application could see it.

`agentjobs.corpus` is the fix. It is **a memo with a lifetime, not a cache**: inside an
open scope the first load is kept and the rest are answered from it, and outside one
nothing is kept at all. There is no expiry to tune and no staleness window, because the
memo cannot outlive the block that opened it. A write inside a scope discards it, so a
handler that saves a task and then reads the corpus reads the corpus it just changed.

**The scope is one HTTP request, and it is opened in one place** — the API middleware.
That is not incidental. The previous snapshot was scoped to a *CLI invocation*, and an
epic walk is a single invocation that runs for as long as the epic takes: it saw every
child exactly as it was when the walk began, forever. `tests/test_dispatch_epic.py`
carries that epitaph. Nothing in the dispatch family opens a scope, and nothing should.

Measured on the same 488-task corpus, isolated bench server, 5 iterations, p50:

| surface | before | after | change | loads |
| --- | --- | --- | --- | --- |
| `GET /dashboard` | 1755.0ms | 225.2ms | 7.8x | 9 -> 1 |
| `GET /tasks/next` | 629.0ms | 181.0ms | 3.5x | 7 -> 1 |
| `GET /tasks` | 918.1ms | 416.5ms | 2.2x | — |
| `GET /tasks/{id}/detail` | 658.0ms | 210.2ms | 3.1x | — |
| `GET /search?q=the` | 952.5ms | 642.7ms | 1.5x | — |
| `GET /revision` | 6.3ms | 6.6ms | — | 0 |

The last three rows were not the task's subject. They are the same defect: every read
surface asked the corpus more than one question.

### `/revision` was never slow; it was waiting (task-485)

The poll every connected client runs every 15 seconds was recorded at 220ms against a
19ms close-out figure. **On an idle server it answers in 7ms**, and it loads nothing —
one aggregate query over the `task` table. The 220ms was measured on a server that was
also serving the dashboard.

Head-of-line blocking, demonstrated rather than assumed. The same server, `/revision`
sampled alone and then with three clients fetching `/dashboard` in a loop:

| | idle | behind 3x `/dashboard` |
| --- | --- | --- |
| before | 8ms | 6,038ms |
| after | 7ms | 733ms |

The improvement is 8.2x, which is the dashboard's own 7.8x: `/revision`'s latency is
whatever work is queued in front of it. **The route handlers are `async def` and do
their work synchronously, so a slow request holds the event loop and every other request
waits.** Shortening the blocking work fixed the symptom in proportion; the structure is
untouched and a concurrent dashboard still costs the poll most of a second. Whether the
read routes should run in the threadpool instead is task-492.

## The browser measurement

The browser leg drives the packaged React app with Playwright and times a click on a
task row until the task's specification region is visible. Click to *rendered*, not
click to response — a fast endpoint behind a component that paints nothing until every
field arrives still feels slow, and only the rendered timing notices.

It reports two figures:

- **warm app** — the app is already open and a row is clicked. This is the interaction
  users complain about.
- **cold load** — a fresh navigation to the list, which also pays for the bundle and
  the first list fetch.

It needs the frontend built and a browser installed:

```bash
cd frontend && npm install && npm run build && npx playwright install chromium
```

Use `--skip-browser` without them, and `--skip-cli` to skip the CLI section. A run of
either kind still prints everything else.

## Other options

| Flag | Effect |
| --- | --- |
| `--iterations N` | Timed iterations per surface (default 10). |
| `--port N` | Port for the benchmark's own server. The default is **derived from this checkout's filesystem path**, not a fixed number, so two worktrees benchmarking at once do not collide (task-187). `--help` prints the value for the checkout you are in. |
| `--source PATH` | Where the real corpus is copied from. |

The benchmark is deliberately **not** part of `scripts/check.py`. It starts servers and
a browser and takes minutes; the repository gate has to stay fast enough that people
actually run it.

### The gate's own browser budget

**The benchmark stayed out of the gate and the browser went unguarded for a month, so
the gate now carries a budget of its own** (task-487). `frontend/e2e/perf-budget.spec.ts`
runs inside the existing `e2e` stage — no new stage, because that stage has already
built the bundle, started the packaged app and opened a browser — and times the same
interaction against the same stop condition this benchmark uses, so a figure from either
is the same measurement.

It is **not** a second benchmark. It is an order-of-magnitude catastrophe check, set to
catch task-135's 263ms becoming 2.6 seconds and explicitly not to catch it becoming
320ms: a wall-clock assertion on a machine that routinely runs three gates at once
cannot be held tighter than that without becoming the flaky test somebody disables. It
prints its median on a pass as well as a failure, so drift shows up in gate output
rather than only at the moment it breaks.

Two things about what it can and cannot see:

- **The corpus is sixty tasks, seeded by the spec.** That is enough for a
  per-interaction collapse and not enough for a per-record one — the class task-131
  fixed, where a single request walked the corpus — which at sixty records would cost a
  few milliseconds and hide inside the threshold.
- **So the spec asserts a byte count alongside the clock.** Bytes per task in the list
  response are exact — the same fixtures produce the same payload on every machine and
  under any load — so that assertion is held *close* rather than loose, and it is where
  the per-record class is caught. It currently guards task-484's listing projection:
  601 bytes per row against a ceiling of 1,500, where a list answering with whole
  records again measures 3,735.

**It costs the `e2e` stage about 5.5 seconds** — 1.7s seeding the sixty fixtures, 1.9s
timing the clicks, 0.4s on the payload, 1.5s closing the fixtures again, measured on
2026-09-19. The stage's own before/after totals on that day were 125.0s and 146.7s, and
**that difference is not the cost**: both runs lost a browser to task-404's flake and
the retry took 5.5s in one and 35.8s in the other, which swamps everything this spec
does. The per-hook figures the spec prints are the number to quote.

Quote it for what it is: a guard against a collapse, not evidence the interaction is
good. Playwright's synthetic click proves the render, and ENGINEERING.md's verification
section says what it does not prove.

### The gate's own server budget (task-486)

The browser budget above guards the render. **`tests/test_performance_budgets.py` guards
what the server sends and what it asks the database**, and it does it because the
budgets that were there guarded neither: on 2026-09-19 `GET /tasks` was 2.3 seconds and
10,355,121 bytes on the real backlog with every assertion in that file green. An
endpoint that runs one query and serialises ten megabytes parses zero task files, which
is all the file could see.

Two counters were added beside the parse tripwire, both chosen to mean the same thing on
every machine:

| Counter | Held to | Why it is durable |
| --- | --- | --- |
| Response bytes **per record** | a ceiling per endpoint | exact, identical under any load, and the units the defect happened in |
| SQL statements **per request** | a small constant, *and* the same number at two corpus sizes | a fan-out is a query per record, and the two-size check fails on the shape of the code rather than on how big the backlog got |

Measured 2026-09-20 against a generated corpus of 480 records, which is where
`CORPUS_SIZE` now sits — this repository's own backlog was 479 that day:

| Endpoint | Bytes | Per record | Statements |
| --- | --- | --- | --- |
| `GET /tasks` | 376,731 | 785 | 5 |
| `GET /dashboard` | 5,226,014 | 10,888 | 14 |
| `GET /search?q=` | 5,174,703 | 10,781 | 17 |
| `GET /tasks/{id}/detail` | 10,822 | — | 32 |
| `GET /tasks/next` | 10,633 | — | 11 |
| `GET /revision` | 63 | — | 1 |

Every statement count above is **the same at 60 records and at 480**, which is the
assertion worth having; the ceilings alone would be met by a route running one query per
record on a small enough corpus.

**Two of those payloads are the defect, not the target.** `/dashboard` and `/search`
still answer with whole records — spec prose, acceptance criteria and the complete log
of every task — which is 5.2 MB each here and the shape task-484 took out of `/tasks`.
They are recorded and held where they stand rather than fixed, because task-486 budgeted
endpoints and did not own them.

**It costs 5.2 seconds**, from 9.4s to 14.6s for the module run alone and serially,
measured on 2026-09-20 — and it is eight times the corpus and fourteen more tests for
that. Most of the 5.2s is one payment: the corpus generator's `yaml.safe_dump` is 6ms a
record and the module now caches it, and caches a built database to copy rather than
re-importing 480 records per test.

**At the stage it does not show up at all.** `check.py --only pytest` on the same
checkout and the same environment, immediately before and after the commit: **185.0s for
5,162 tests and 175.0s for 5,176**. The stage got *faster* with more tests in it, which
is the point — both runs shared the machine with two other gates at `-n 16`, and that
contention moves a run by more than this module does. Quote the 5.2s, which is
attributable; the stage pair is only evidence that nothing stage-sized happened.

Three things the budgets do **not** catch, established by deliberately breaking the code
and watching what stayed green:

- **The five-second catastrophe check notices nothing.** With `_assemble` rewritten into
  a query per record — 3,840 statements for one whole-corpus read — that read took
  **0.14s** against its 5-second budget, 36x under, at eight times the corpus size it
  was written for. It is there to catch a lost index, and the note telling you not to
  tighten it is still right.
- **The parse tripwire notices nothing**, by construction. Neither regression touched a
  file.
- **The byte budgets and the statement budgets catch different things and neither
  subsumes the other.** Returning whole records from `GET /tasks` again trips both; an
  N+1 underneath trips only the statements, because the bytes on the wire do not change
  when the server works harder to produce them.


---

## What the gate costs

The rules for *running* the gate are in
[ENGINEERING.md §Testing](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md#testing). The stage table and the measurement
history live here — kept because a performance claim is only checkable if the run that
produced it is on the record, and here rather than there because a session that is about
to commit does not need it, and because the four always-loaded files have a byte budget
(`tests/test_context_budget.py`) that a table of numbers is a poor use of.

### The stages, and what each cost

Green unqualified `scripts/check.py` runs on this machine with nothing else competing for
them, in a worktree with warm caches. Two dates, because the shape has changed and the
column on the left is what most of this file's reasoning was built on. Read the bottom of
your own run rather than quoting either; the gate prints this table every time, which is
the whole point of printing it.

| # | Stage | What it checks | 2026-08-21 | 2026-09-06 |
|---|---|---|---|---|
| 1 | `black` | Python formatting | 0.6s | 1.0 / 0.6s |
| 2 | `ruff` | Python lint | 0.1s | 0.1 / 0.1s |
| 3 | `mypy` | Python types | 1.5s | 1.6 / 1.6s |
| 4 | `api` | `openapi.json` and the generated client both match the app | 4.2s | 2.4 / 2.5s |
| 5 | `icons` | the committed PWA icons match `assets/app-icon.svg` | 2.8s | 1.2 / 1.2s |
| 6 | `oxlint` | frontend lint | 0.6s | 0.4 / 0.4s |
| 7 | `pytest` | the Python suite, across every core | 52.1s | 79.3 / 89.1s |
| 8 | `vitest` | the jsdom component tests | 5.2s | 28.4 / 7.2s |
| 9 | `build` | `tsc --noEmit` and the production bundle | 3.7s | 4.5 / 4.3s |
| 10 | `e2e` | the Playwright suite against a live server | 25.0s | 135.3 / 138.8s |
| | | *the same stage on 2026-09-22, one worker then four* | | *307s then 107s* |
| | | | **95.8s** | **254.3 / 245.8s** |

A `roadmap` stage sat between `icons` and `oxlint` from 2026-09-11 to 2026-09-13. It
checked the roadmap files against the task store rather than the tree, so a task closing
anywhere turned every branch red; task-413 took out the listing half and task-427 the
page half. The roadmap playbook runs that audit now.

**On 2026-09-06 the gate was bounded by `e2e`, not by `pytest`** — 138.8s against 89.1s —
and every argument in this file written before that assumed otherwise. The task-268 spec's
critical-path arithmetic, "max(pytest 52s, api+build+e2e 33s) ≈ 52s", read
max(89s, 146s) ≈ 146s, which is where the concurrent runner below lands.

**Task-369 took that back**, from a median 307s to 107s on 2026-09-22 — by which date the
one-worker stage had reached 307s on its own, not the 138.8s in the column above. The
stage moved twice in a fortnight and in opposite directions, so read
[the Playwright suite runs on four workers](#the-playwright-suite-runs-on-four-workers-task-369-2026-09-22)
before quoting either number.

Four of the moves have named causes. `api` and `icons` fell because task-268 stopped
routing their Python halves through `npm run` and a nested `poetry run` — measured on
this machine, three interleaved reps against `main`'s own `check.py` in one worktree, the
pair together went 6.5 / 6.4 / 7.0s to 3.6 / 3.6 / 4.3s. `pytest` and `e2e` rose because
the suites did: 2538 tests to 4152, and the Playwright suite to 107 tests on one worker.
Neither per-test cost had moved *by that date*. The Playwright one moved afterwards, from
1.30s to 1.78s over the next fortnight, which task-369 measured and this file had no
reason to go looking for.

`vitest`'s 28.4s in the first run and 7.2s in the second is Vite's dependency
pre-bundling, paid once per worktree. It is the reason two runs are quoted rather than one.

MyPy's cost moves for a reason unrelated to load, and not the one this file used to give:
**a fresh worktree pays about 14s for each of its first two `mypy .` runs and about 1.5s
thereafter, and copying the main clone's `.mypy_cache` in does not change that** —
task-268 measured 15 / 14 / 2 / 1s with no cache against 14 / 14 / 1 / 2s with one copied
in, and 30s for a variant that copied a warm cache to a fresh directory. The cost is
first-touch I/O over 62 MB of small files, not analysis: `mypy -v` reports 1288 of 1289
metadata entries fresh in the copied-cache run. The one entry it never finds is
`tests/test_mcp_server.py`, and one stale module in that graph makes mypy deserialise 798
SCCs it would otherwise skip.

### The Playwright suite runs on four workers (task-369, 2026-09-22)

**First, what the re-measurement found, because it is a bigger number than the fix.**
Task-268 measured `e2e` at 135.3 / 138.8s over **107 tests** on 2026-09-06. Fifteen days
later, same machine, same one worker, same uncontended conditions: **318 / 307 / 305s over
172 tests.** Both halves moved. The suite grew 61%, and the cost of a test grew with it —
1.30s each in September's measurement against 1.78s now — so "the suite grew" is only half
the account, and the sentence in the stage table above saying neither per-test cost moved
was true when it was written and is not true now.

Where the extra half-second is, as far as one breakdown shows: **two files hold 29% of the
suite's measured test time** — `dashboard-one-screen.spec.ts` at 40.7s and
`attention-badge.spec.ts` at 36.3s, out of 263.6s. The second is slow by construction: its
largest test sets a 120-second budget because it has to sit through two revision polls 15
seconds apart, which is the only way to prove a badge refreshes without a reload. Tests
that *wait* cost what they wait for, and the suite has been acquiring them.

#### Before and after

Three runs of each arm, uncontended, in `worktrees/agentjobs-369`, on the same commit
apart from the change itself. Wall clock is `npx playwright test` end to end; the summed
column is what the tests themselves measured, which is the work the arms share.

| Arm | Run 1 | Run 2 | Run 3 | Median | Summed test time |
|---|---|---|---|---|---|
| `workers: 1` (before) | 318s | 307s | 305s | **307s** | 282 / 267 / 264s |
| `workers: 4` (after) | 103s | 107s | 108s | **107s** | 245 / 271 / 265s |

**2.9×, and 200s off the gate's critical path.** All 172 tests ran in all six runs; the
summed test time is unchanged between the arms, which is the check that the saving is
scheduling and not tests quietly not running.

#### Why four, and where the floor is

The suite parallelises **by file** — `fullyParallel: false` stays, because several specs
build fixtures in a `beforeAll` and read them across the tests below, and a few assert on
an ordering the tests above them established. So the longest file is a floor no worker
count can go under: 40.7s, `dashboard-one-screen.spec.ts`. Against 264s of total test time
that gives `264/4 = 66s` at four workers and `44s` at six, after which the floor binds and
nothing is left to win. Four is chosen there rather than six for two reasons that are not
about the arithmetic: this machine routinely runs three gates at once, and the port block
(`frontend/e2e/ports.ts`) divides the gate's historic 20000–29999 into 2500 blocks at a
width of four — the same range, so a concurrent `scripts/bench.py` still cannot collide
with a gate, at the cost of taking two checkouts' collision odds from 1 in 10000 to 1 in
2500.

#### One server per worker, and what that had to isolate

Every spec shared one server and one temporary project, and the deferral in task-268
named three hazards. The answer to all three is the same: **each worker starts its own
`run_server.py`**, which already builds a throwaway project and a throwaway
`AGENTJOBS_HOME` per process. A second server is therefore a second database, a second
queue, a second dispatch configuration and a second run ledger, so `dispatch.spec.ts` and
`live-runs.spec.ts` — the two the deferral expected to have to quarantine — run in
parallel with everything else. **Nothing is quarantined.** The alternative considered and
rejected was one server serving a project per worker: it would have meant threading a
project id through roughly 170 hard-coded `_local` references and would still have left
those two specs sharing one machine.

The per-file audit that decides what needs isolating is in
[`frontend/e2e/README.md`](https://github.com/jeffposey/agentjobs/blob/main/frontend/e2e/README.md),
and `frontend/src/test/e2eInventory.test.ts` fails when it stops describing the directory.

**Three things the audit did not predict and running it did**, all of them latent defects
the shared project had been hiding rather than regressions:

- **The built bundle is shared too.** `capture-draft.spec.ts` rewrites `sw.js` and
  `build-info.json` to make a tab believe the app was rebuilt under it. Out of the
  checkout, that write reaches every worker's service worker, and three other browsers
  reload mid-interaction — a neighbour failing for a reason with no trace of itself in it.
  `run_server.py` now copies the bundle per server; it is 700 KB and nine files.
- **A spec was relying on the corpus its neighbours left.** `tasks-shell.spec.ts` skipped
  its own central assertion when the task list was shorter than one screen, which on a
  shared project it never was. Isolation made it short, and the test stopped running
  without failing — the failure mode a conditional skip always has. It now files the rows
  it needs and asserts the condition instead of skipping on it.
- **A locator was ambiguous and had never met the state that showed it.**
  `promote-draft.spec.ts` clicked `/^View all/` on the Dashboard; the drafts panel's link
  starts the same way and renders only when nothing is claimable, which on a shared
  project was never true by the time that spec ran.

#### What it costs: the browser death gets more chances

The failure [task-404](#a-browser-that-was-gone-before-its-test-started-task-404)
describes — a Chromium terminated between two tests, seen as `browser.newContext: Target
page, context or browser has been closed` with no source location — **appeared in four of
six parallel runs and none of three serial ones.** Small samples, and task-404's own
corpus is a per-run rate measured with one browser, so this does not establish that four
browsers make it four times as likely. It is the obvious hypothesis, and the practical
point stands either way: the gate's narrow retry is now load-bearing where before it was
insurance. It retries once, and only when *every* failure on the run is that shape, so a
run that draws two deaths in two passes is still red. Nothing here changes that rule;
raising the retry would hide the browser deaths an application could cause, which is the
whole reason the rule is narrow.

The `webServer` start timeout went from 30s to 90s in the same change. Four interpreters
importing the application at once, on a machine that may be running three gates, is not
the thing 30 seconds was comfortable for.

#### No whole-gate figure is quoted for this change, and that is deliberate

The arms above are `npx playwright test` measured directly, six uncontended runs. **A
whole-gate re-cut was attempted on 2026-09-22 and thrown away**, because the machine would
not hold still: the attempt's `pytest` stage took 1522.8s against the 79.3 / 89.1s in the
table above, having asked for `-n auto` — 32 workers — because `gate_slots` correctly saw
one gate *at the moment pytest started*. Three more gates began during the twenty-five
minutes that followed, one at 10:38, one at 10:53 and one at 11:02, and a
stall-detection timing assertion in `test_dispatch_registration.py` failed under the
starvation. That is the documented limitation of counting slots once
([why the count is taken when pytest starts](#how-the-gate-degrades-under-contention)),
not a new defect, and the failing test is untouched by this change and passes three times
in isolation on the same branch.

The lesson is the one this file keeps relearning: **a stage figure is only comparable to
another stage figure measured the same way on a machine in the same state.** The e2e arms
are quotable because both were taken uncontended, back to back, on one tree. Nothing about
the whole gate was, so nothing about the whole gate is claimed here. Re-cut the full table
on a quiet machine before quoting a new total.

**Interaction with `--concurrent`, unmeasured.** That flag already runs `pytest` beside
`e2e`; with four browsers and four servers under `e2e` the two are contending for more
than they were, and every figure in
[Running the stages concurrently](#running-the-stages-concurrently-task-268-2026-09-06)
predates this change. The flag is off by default and writes no receipt, so nothing that
gates a branch depends on those numbers — but they should be re-cut before it is promoted.

### The three figures, and which to quote

| Figure | What it is | Measured |
| --- | --- | --- |
| **95.8s** | one green `scripts/check.py` on this machine with nothing else competing | 2026-08-21 |
| **254.3s / 245.8s** | the same thing, two runs, after five weeks of suite growth | 2026-09-06 |
| **~155s** | the median full passing gate a *dispatched* session actually paid — 125s, 141s, 155s, 157s, 174s, from the phase records | to 2026-08-23 |
| **342s / 361s / 384s** | three concurrent parallel gates, from `run_4063f1c0` | 2026-08-23 |

~250s is the current quiet-machine best case; 95.8s is what most of the reasoning below
was measured against and is kept so those arguments can be read. The dispatched median is
five weeks old and is now certainly low. Quote whichever the question calls for, and say
which and when.

The three-way contention figure was got by accident rather than by a benchmark — one
session started a gate at 03:27:10, another at 03:27:50 and a third at 03:29:03 with the
first two still running — so treat it as one observation and not as a curve.

### Why pytest went from 326.5s to 52.1s (task-233)

Two changes, both of them arrangements of how pytest is *invoked* rather than reductions
in what it checks. The same tests run; the pass/fail counts were compared on the same
commit before and after.

| Configuration | Wall clock | Result |
|---|---|---|
| serial, with coverage — what the gate ran until task-233 | 540.1s | all passed |
| serial, no coverage | 342.6s | all passed |
| `-n auto` across 32 cores, no coverage | **42.5s / 45.7s / 43.6s** | all passed |
| `-n auto --dist loadfile` | 54.9s | 2538 passed |

Three consecutive `-n auto` runs are quoted because one green parallel run proves nothing
about a suite's parallel-safety. The suite is safe because `tests/conftest.py` already
gives every test its own project registry and its own Claude home and stubs the
reachability probe, and because nothing in it binds a fixed port — the four places that
open a socket ask the kernel for port 0.

One thing had to be fixed: a `parametrize` whose cases came out of a `frozenset`, which
each xdist worker iterated in its own hash order, so the workers disagreed about what the
test IDs were and the run aborted during collection.

Coverage is off by default and available with `--coverage`. It cost between 60 and 200
seconds depending on what else the machine was doing, and wrote an HTML report that
nothing reads before a commit.

### The suite barely sleeps, and knowing that changed the fix (task-518)

Task-518 was filed against a `--durations=15` list totalling **477s across fifteen tests**
out of 5,537, on a pytest stage of **1146.2s**, and it read that list as "the suite sleeps
through a third of the gate". It does not. The measurement that settles it is worth
keeping, because it is the one figure in this file that means the same thing on a loaded
machine and a quiet one.

**Every `time.sleep` in the suite, totalled.** A pytest plugin wrapped `time.sleep` in
every worker, recorded the seconds and the call site, and wrote one JSON file per process
for aggregation. Whole suite, `-n auto`, 2026-09-21, 5,485 tests passing:

```
TOTAL SLEPT 113.6s across 63 call sites
    35.1s x3511   test_approval_standdown.py <- finish.py <- ledger.py:597   (the run-lock wait)
    15.8s x6      compat.py   <- client.py:1071                              (the retry budget)
    15.8s x6      remote_manager.py <- client.py:1071                        (the retry budget)
     9.0s x180    a child process's own startup wait
     8.8s x882    test_durable_replay.py <- finish.py <- ledger.py:597       (the run-lock wait)
```

**113.6 seconds, against a wall clock of 824.9s and thousands of seconds of worker time.**
Two call sites -- one lock wait and one client retry budget -- are 60% of it. There is no
third of a gate spent sleeping anywhere in this suite, and a plan to remove sleeping would
have had 113.6s to find.

**Where the time actually goes is process creation**, and the top of the durations list is
made of tests that spawn a lot of short-lived interpreters. `TestTask224`'s three tests
spawn **120 between them** -- one `<runner> agents --json` and one `<runner> logs` per
poll, plus twenty auth probes -- which is the production polling contract rather than a
test artefact.

**And process creation on this machine is not a stable quantity.** The same three tests,
same commit, same interpreter, three times in twenty minutes:

| run | seconds |
|---|---|
| inside a 150-test serial run | 172s |
| alone, class only | 254s |
| alone, class only, minutes later | **35s** |

Per-spawn cost for identical argv ranged from **0.064s to 16.7s**. The machine was not
idle -- 97 python/node/claude/git processes and 64% CPU while this was measured, none of
them a gate. **So a pytest total is only comparable with another taken under the same
contention**, which this machine does not offer on demand, and the 477s the task was filed
against is a reading through that noise. The sleep total is the half that reproduces.

#### What that changed, and the before/after pair

The fix followed the measurement rather than the spec. Two things were worth doing and a
third was not:

**1. The client's retry budget, where a test was proving a service was absent.** One
`TaskClient` call against a port nothing listens on costs **29.8s on this machine**: seven
attempts, each a refused connection Windows takes **2.05s** to return here, plus the 15.75s
of `RETRY_BACKOFF_SECONDS`. That patience is for riding through a restart, and a test whose
subject is an absent service has nothing to ride through. Measured before and after, same
machine, same evening:

| test | before | after |
|---|---|---|
| `test_cli.py::test_show_task_not_found` | 30.5s | **2.4s** |
| `test_mcp_server.py::...::test_startup_against_a_missing_service_fails_on_stderr` | 32.3s | **4.5s** |
| `test_mcp_server.py::...::test_unreachable_service_says_so_and_does_not_start_one` | 23.1s | **1.2s** |

**2. The stand-down window, which was a real wait and is now a real threshold.**
`test_approval_standdown.py` used to set `STAND_DOWN_CONFIRM_SECONDS` and
`STAND_DOWN_POLL_SECONDS` to zero, which is a test of a window that closes immediately.
With the clock installed the window keeps its production ninety seconds and costs nothing:

| | before | after |
|---|---|---|
| `test_a_busy_session_stands_down_and_the_approval_merges` | 51.4s | **3.9s** |
| `test_an_idle_session_not_yet_polled_is_settled_rather_than_waited_for` | 44.6s | **4.8s** |
| `test_a_session_stuck_on_an_expired_login_is_still_taken_over` | 50.4s | **4.1s** |
| the whole file, serial | 131.4s | **38.0s** |

`test_finish_durable.py` is the same change and the same shape: its three slowest were
42.3 / 41.9 / 38.9s and its slowest is now **6.0s**, 27 tests in 98s, with no threshold
shortened anywhere.

**3. Time-skipping makes a wait free. It does not make a poll free.** Worth knowing before
reaching for it. Running that file's fourth test at the full production cadence -- a
ninety-second window polled every two seconds -- is 45 real `poll_session` calls, two
subprocesses each, and it went from about 20s to **135s under a contended gate**, making it
the slowest test in the suite. The window is the threshold under test and keeps its value;
the cadence is coarsened to 30s in that one test, which is three polls and proves the same
thing. `scripts/threshold_probe.py` checks that claim by removing the window and watching
the test stop returning.

#### Whole-suite figures, with their contention stated

All `-n auto` in one worktree on 2026-09-21 evening, on a machine with the owner's ordinary
desktop load and no other gate running:

| when | result | wall |
|---|---|---|
| before any change, 17:51 | 5,485 passed, **1 failed** (the auth-recovery flake) | 824.9s |
| after the one clock, 18:15 | 5,486 passed | 465.4s |
| after the lock waits and the stand-down, 19:32 | 5,491 passed, 1 failed (a second clock, found and fixed) | 482.5s |
| after the execution store joined the clock, 21:03 | 5,509 passed | 484.5s |

**Do not read 824.9s to 465.4s as the size of the change.** Some of it is the machine being
quieter at 18:15 than at 17:51, and this file's own rule is that a wall-clock figure across
a gap like that is an anecdote. The per-test pairs above were taken back to back and are
the defensible half; the whole-suite column is here because leaving it out would be worse.

The gate's own figures, both green across all ten stages, on the same machine with the
owner's ordinary desktop load and no other gate running. Two runs rather than one, because
the spread between them *is* the point this section keeps making:

| stage | 20:29-20:41 | 22:24-22:38 |
|---|---|---|
| `black` | 2.2 | 0.9 |
| `ruff` | 0.4 | 0.6 |
| `mypy` | 2.2 | 20.4 |
| `api` | 6.8 | 5.4 |
| `icons` | 1.6 | 1.5 |
| `oxlint` | 2.0 | 0.5 |
| **`pytest`** | **475.0** | **530.3** |
| `vitest` | 38.1 | 36.2 |
| `build` | 9.0 | 7.5 |
| `e2e` | 189.0 | 180.1 |
| **total** | **726.2** | **783.6** |

Nothing between those two runs made the suite slower -- the second has *more* tests in it
and its slowest file had just been made three times faster. The 55 seconds are the machine,
and `mypy`'s 2.2 to 20.4 in the same pair is the same thing said in a stage small enough to
see it whole.

**1146.2s to 475-530s for the pytest stage**, and the same caveat applies to that pair as
to every other in this section: the before figure was taken on a different evening, and
this machine's process-creation cost was measured varying by a factor of 260 between
identical spawns on the day both readings were taken. What is not subject to that caveat is
the per-test table above, which was measured back to back.

**The gate is bounded by `pytest` again** -- 475s against `e2e`'s 189s -- which is the
reverse of where task-268 left it, 89.1s against 138.8s. That is where the next round has
to look, and this round says where: nothing here touched the thing that actually costs the
stage its time, which is how many short-lived processes the dispatch tests start. The
figure to attack is 120 interpreter spawns across three tests, not any number of seconds.

### What the slowest tests actually are (task-268)

Every proposal about this suite up to now has been arithmetic over its total — task-268's
own spec argues "2538 tests at 342s serial is 135ms/test, and 52s at 32 workers against an
11s ideal says the tail is the cost" — and there was no per-test measurement anywhere in
the repository to check one against. `--durations=15` is now on the gate's pytest stage
unconditionally, so every run prints them.

The first one, 2026-09-06, 4152 passed in 78.1s at `-n auto`:

| Seconds | Test |
|---|---|
| 20.52 | `test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too` |
| 14.48 | `test_validate.py::TestRealCorpus::test_no_task_file_is_unloadable_or_points_at_nothing` |
| 13.43 | `test_models_v2.py::TestAgreesWithTheLinkMLSchema::test_the_linkml_cross_check_can_actually_fail` |
| 13.02 | `test_migrate_schema.py::TestTheRealCorpus::test_every_real_task_converts_loads_and_loses_nothing` |
| 12.18 | `test_validate.py::TestRealCorpus::test_the_tolerated_drift_is_only_taxonomy_and_serialization` |
| 9.17 | `test_dispatch_finish.py::TestAfterTheMerge::test_a_retry_after_an_escalation_finishes_its_own_merge` |
| 7.42 | `test_task_corpus.py::test_agentjobs_task_ids_and_relationships_are_not_dangling` |
| 7.18 | `test_principals.py::test_a_served_process_answers_the_same_as_the_pure_rule` |

**The inference was right and it is now a measurement.** The single slowest test is 20.5s
against a 78.1s stage — 26% of the stage's wall clock in one test on one worker — and the
top five are 73.6s of worker time between them. At 32 workers a stage cannot finish before
its longest test, so the floor here is set by `TestProcessGroup`, not by the 4152 tests.

Two shapes account for almost all of it, and both are worth knowing before anyone proposes
a fix: **real subprocess timeouts** (the top entry waits out a process-group kill) and
**whole-corpus loads** (four of the eight parse every tracked task file under `tasks/`,
so they grew with the backlog while the backlog was files; since the cutover that
directory is frozen, and the ones that read it skip once it is retired). Neither is wasted work; both are candidates
for being made cheaper, and neither is this task.

This also settles proposal 9 in the negative for now, as its own text said it would:
receipt-backed incremental test selection attacks the *bulk*, and the bulk is not where
the time is.

#### `--dist worksteal`: measured, not adopted (task-268)

The obvious follow-on, since a tail is exactly what work-stealing is for. Six runs in one
worktree, arms interleaved so machine drift falls on both, `-n auto` throughout, same
4157 tests passing in every one:

| Rep | default (`--dist load`) | `--dist worksteal` |
|---|---|---|
| 1 | 76.5s | 69.0s |
| 2 | 77.2s | **112.9s** |
| 3 | 79.7s | 73.9s |

**Not adopted.** Two of the three worksteal runs beat every default run, and the third is
45% worse than any of them — so the spread *within* the worksteal arm, 69s to 113s, is
larger than the gap between the arms, and three runs cannot tell a 6% effect from a
neighbouring gate. The default arm's own spread is 3.2s, which is the contrast that
settles it: whatever produced the 113s was not the default's to suffer that evening.

Reopen it with more reps on a machine with no dispatched neighbour, or once the tail
itself is shorter — the thing worksteal cannot fix is that no arrangement of 4157 tests
finishes before the slowest one, and that test is 20.5s.

### Why the cheap stages run first

Task-189 moved `api`, `icons` and `oxlint` above `pytest`. Together they cost 8.2s, and a
session working task-188 paid four and a half minutes twice to reach one of them.
Everything above the pytest line cost 9.8s together then and **6.4s on 2026-09-06**,
against a gate that has meanwhile gone from 95.8s to about 250s.

The argument used to be stated as "seconds before minutes", and task-233 took the minutes
away. The ordering stays regardless: it costs nothing, and the gap it exploits reappears
the moment a slow stage is added.

### Why `-n` is passed by the gate

`pytest`'s `addopts` is empty and xdist's `-n` is passed by `scripts/check.py` rather than
configured globally, so a hand-run `pytest` is serial. That is deliberate: xdist costs
more than it saves on a small selection, and its interleaved output is the wrong trade
when you are reading one failure.

What a hand-run `pytest -n auto` *means* is no longer xdist's business either (task-536):
`tests/conftest.py` implements `pytest_xdist_auto_num_workers`, so it resolves through
the same budget the gate uses and takes a gate slot while it runs. A serial hand-run
still takes nothing and waits for nothing.

### How the gate degrades under contention

| Concurrent gates | Serial suite (historical) | Parallel suite | Parallel suite, 2026-09-06 |
|---|---|---|---|
| 1 | 365s | 96s | 250s |
| 2 | 388s | not measured | 326–404s |
| 3 | not measured | ~360s | not re-measured |
| 4 | 411s | not measured | not measured |
| 6 | 444s | not measured | not measured |

The right-hand column is task-268's re-cut and is the one to quote; the middle one is
five weeks and 1600 tests old, and most of the reasoning below rests on it.
The serial column is kept only as history: it does not describe the gate as it now runs.
Two conclusions follow from the parallel column:

- **The parallel gate does not degrade as gently as the serial one did**, because
  `-n auto` asks for all 32 cores and three gates are dividing the same machine. The
  absolute number is still no worse than the serial gate ever was.
- **A run's summed gate time can exceed its own duration, and that is not a bug.**
  `run_report.py` reports what the phase records say; overlapping gates make the
  percentage a sum, not a share of a timeline. The report flags it when it happens.

#### The core budget, before and after (task-339, 2026-09-05)

Two checkouts of the same commit in `worktrees/agentjobs-339-a` and `-b`, each running
the unqualified gate; for the paired arms both were started at the same moment and the
per-stage figures come from each gate's own printed table. Three runs per arm, and the
paired arms were run adjacent in time so machine drift is least able to explain the gap:

```
python scripts/check.py            # in each worktree, concurrently for the paired arms
```

| Arm | Window (UTC) | `-n` | pytest stage, seconds | Whole gate, seconds |
|---|---|---|---|---|
| One gate | 22:16–22:23 | auto (32) | 489.6 / 299.0 / 268.4 | 750.5 / 473.4 / 409.8 |
| Two gates | 22:31–23:07 | auto (32) each | 648.2 / 553.3 / 520.0 / 519.9 / 465.1 / 466.8 | 666.1 / 818.3 / 727.2 / 727.3 / 666.2 / 666.4 |
| Two gates | 23:09–23:44 | **16 each** | 465.2 / 465.5 / 445.5 / 443.5 / 461.6 / 460.5 | 654.6 / 651.6 / 613.2 / 619.1 / 624.1 / 624.2 |
| One gate | 23:45–00:00 | auto (32) | 246.7 / 263.1 / 249.7 | 401.6 / 403.3 / 388.2 |
| Two gates, **control** | 23:57–00:21 | auto (32) each | 432.9 / 399.8 / 448.0 / 459.9 | 606.4 / 609.0 / 643.2 / 645.5 |

Six figures per paired arm because both gates in a pair are a measurement.

**Read the control row, not the first two.** The naive before/after — rows 2 and 3 —
shows the paired pytest stage falling 529s to 457s, and **that reading is wrong**: the
one-gate rows either side of it fell by more (352s to 253s), so the machine simply got
quieter over the evening and the apparent gain is drift. This is the failure this file
exists to prevent, and it was nearly written down as a 14% win. The control arm is the
same paired gates forced back to `-n auto` an hour later, on the machine as it then was.

Against the control, the budget is:

| | pytest stage, mean | Whole gate, mean | Lowest free memory |
|---|---|---|---|
| Two gates, `-n auto` | 435.2s | 626.8s | **6 MB** |
| Two gates, `-n 16` | 456.9s (+5.0%) | 632.1s (+0.8%) | 1452 MB |

**So this is a reliability change, not a speed-up, and it is priced accordingly.** It
costs 5% of the pytest stage and nothing distinguishable on the whole gate — 626.8s
against 632.1s, well inside either arm's own spread — and it buys back the machine:

| Arm | Peak `python` processes | Peak working set | Lowest free memory | Mean CPU |
|---|---|---|---|---|
| One gate, `-n auto` | 175 | 9.3 GB | 923 MB | 29% |
| Two gates, `-n auto` (busy machine) | 282 | 15.9 GB | 159 MB | 39% |
| Two gates, `-n auto` (quiet machine) | 213 | 10.7 GB | **6 MB** | 32% |
| Two gates, `-n 16` | **146** | **6.4 GB** | **1452 MB** | 33% |

**Six megabytes free of 64 GB**, twice, an hour apart, at a third of the CPU. Two gates
under the budget cost the machine less than one gate did without it, which is the
property the rule was chosen for rather than a surprise: N gates at `32/N` workers is 32
workers whatever N is. Sampling is every twelve seconds, so the peaks are floors.

The 5% is worth it because the failure it removes is not gradual. A red gate costs a
whole extra launch, and one of the six unbudgeted paired gates went red on
`TestProcessGroup`'s timeout assertion — a timing test losing to a paging machine, not a
defect in the code under it. One observation is not a flake rate, and it is not claimed
as one; what is claimed is that a machine held at single-digit megabytes has no headroom
for the third dispatched run this machine is configured to allow.

**Not measured: three concurrent gates**, which is `limits.max_concurrent_runs` and where
the budget should matter most — 96 workers unbudgeted against 30 budgeted. Two was
measured because two arms of three runs each was already two hours of machine time. If
the third slot is ever the case in question, measure it rather than extrapolating this.

**What was rejected.** Lowering `limits.max_concurrent_runs` to 1 was a real candidate,
since the cost being minimised is time to review of one task rather than machine
throughput — and the numbers do not support it: a lone gate's pytest stage is ~250s and a
budgeted paired one ~457s, so two tasks in parallel still reach review sooner than two in
sequence. It is also machine-level configuration in `~/.agentjobs/dispatch.yaml`, not
anything in this repository. Capping the *single*-gate worker count is a different lever
with its own evidence above — 923 MB free at `-n auto` with no neighbour at all — and
belongs to task-268.

One correction to the row above it while we are here: **task-233 assumed the scarce
resource was cores**, and said so — "`-n auto` asks for every core, so two gates now
compete for the same 32". At 32% CPU and six megabytes of memory, it is not cores. That
mattered for the shape of the fix: a fairness scheme dividing *cores* would have been
guesswork, whereas capping the machine-wide worker count is the thing that bounds the
memory, and the two happen to be the same arithmetic.

The red test above is `tests/test_dispatch_runner.py::TestProcessGroup::
test_the_timeout_kills_the_grandchild_too`, failing with "pid 3393900 survived the
timeout" — worth naming because a reader who meets it should suspect the machine before
the code.

#### The 2.9x spread was the budget, not contention (task-513, 2026-09-21)

The row above says three concurrent gates were "not measured", and task-513 was filed on
what happened when five were. Its evidence was five gate runs over one evening as
worktrees accumulated: **225.4s, 268.9s, 314.2s, 358.7s, 663.5s** — a 2.9x spread on
identical work, monotonic with the number of live worktrees, and read at the time as
five agents oversubscribing a machine nothing budgets.

**Both readings predict that table exactly.** The other one is the section above: the
Nth concurrent gate is handed `32/N` workers *on purpose*, so a gate beside four others
is not being starved, it is being given a fifth of the machine. One experiment separates
them — throttle a gate's worker count and give it no neighbours at all:

```bash
poetry run python scripts/gate_cost.py curve --gates 1,2,3,4,5
```

`scripts/gate_cost.py` holds synthetic `gate_slots` files, so `active()` answers N
without N gates existing, then drives this checkout's own `scripts/check.py --only
pytest` and reports the figure from the gate's own timing table. Every arm's full output
is kept, and each row records the slot count seen at both ends so a real gate arriving
mid-arm is visible rather than averaged in.

| Gates believed | `-n` | pytest stage | vs arm 1 | Real neighbour? |
|---|---|---|---|---|
| 1 | auto (32) | 507.5s | 1.00x | no |
| 2 | 16 | 546.1s | 1.08x | no |
| 3 | 8 | 996.8s | 1.96x | **yes, one** |
| 4 | 6 | 1112.0s | 2.19x | **yes, one** |
| 5 | 6 | **1520.6s** | **3.00x** | no |

**Read rows 1, 2 and 5: those three had the machine to themselves.** A lone gate
throttled to what a fifth gate would be handed runs 3.0x a lone gate at `-n auto` — so
the division reproduces the whole spread the task was filed about, and beats it, with no
contention anywhere in the measurement. Rows 3 and 4 picked up a real neighbour gate
from another dispatched run and are kept only because dropping a sample you did not like
is how a table stops being evidence.

Two things a reader needs before quoting this:

- **The suite is nearly flat from 32 workers to 16** — 8% for half the workers — so
  `-n auto` was over-provisioned and the steep part of the curve is below about ten.
  That is the useful shape: the first neighbour is close to free and the fourth is not.
- **Run-to-run noise at `-n 6` is large.** 1112.0s and 1520.6s are the same worker count
  on the same evening, 37% apart, and the slower one is the arm with *no* neighbour. A
  single arm at this end of the curve does not resolve anything finer than the noise,
  which is why the claim above rests on the gap between `-n auto` and `-n 6` rather than
  on either figure.

**Which suite these five arms measured.** They were taken on 2026-09-21, before task-518
landed one clock over the dispatch subsystem and made this stage's slowest file about
three times faster. The absolute seconds above are therefore a suite that no longer
exists, and arm 1's 507.5s should not be compared with a figure taken today — the
lone-gate `-n auto` pair to compare against is task-518's own 475.0s and 530.3s,
[above](#whole-suite-figures-with-their-contention-stated). **The ratios survive that**,
because every arm ran the same suite as every other arm and nothing task-518 changed
touches how workers are divided: the shape of the curve, not its height, is what this
section claims. Re-running the curve on the current suite would cost about ninety minutes
of gate and would be worth it only if someone wants to argue the *shape* moved.

**What this settles for admission.** `limits.max_concurrent_runs` counts runs and nothing
weighs what they consume — which is what task-513 set out to fix — but the contended
resource the evidence is actually about is already budgeted one layer down, by the thing
that knows when it is being consumed rather than by a prediction made minutes to hours
earlier at dispatch. The baseline also moved: the same stage measured 257.4 / 288.4s on
2026-09-20 and 507.5s here, on a machine whose largest consumer is not AgentJobs at all
(chrome held 12.2 GB across 64 processes against python's 8.8 GB, with 7.2 GB free of
64 GB). A weighted admission budget would be predicting one variable it cannot see from
another it does not control.

#### Two gates, thirteen workers each, and a queue (task-536, 2026-09-22)

*The capacity of two is superseded by
[one gate at a time](#one-gate-at-a-time-measured-with-real-gates-task-534-2026-09-22),
which measured real concurrent gates. The reserve and the heartbeat below still stand.*

The sections above are the evidence this change rests on; it adds no measurement of its
own, which is deliberate and is why task-534 follows it. What it does is stop the
regime those sections describe from recurring: on 2026-09-22 three gates overlapped on
this machine, each handed a third of it, and one of them took **45 minutes** and went red.

Four changes, in `scripts/gate_slots.py` and `scripts/check.py`, plus a hook in
`tests/conftest.py`:

| | Before | After |
|---|---|---|
| Gates running pytest at once | unbounded | **2**, a third queues |
| A lone gate's `-n` | `auto` (32) | **26** |
| A paired gate's `-n` | 16 | **13** |
| A third gate's `-n` | 10 | queued, or 13 if it waits out 40 minutes |
| Cores never given to pytest | 0 | **6** |
| A slot's life without a heartbeat | 30 minutes | **5 minutes**, touched every 60s |
| A hand-run `pytest -n auto` | 32, invisible | budgeted, and takes a slot |

**Why two at thirteen and not one at thirty or three at ten.** The owner asked for one of
those two; neither is right on the curve
[above](#the-29x-spread-was-the-budget-not-contention-task-513-2026-09-21). The suite is
nearly flat from 32 workers to 16 — 8% for half the machine — and steep below about ten,
with `-n 6` at 3.0x a lone run.

- **Three at ten** sits at the knee. Each gate is 1.5 to 2x its lone cost, and it is the
  regime that produced the 45-minute gate.
- **One at thirty** gives the best per-gate time and the deepest queue: with
  `limits.max_concurrent_runs` at three, the third run waits two whole gates.
- **Two at thirteen** stays on the flat part, so each gate costs close to what it costs
  alone, and the third run waits one gate. Memory is inside what task-339 measured for
  two at sixteen — 6.4 GB peak, 1452 MB lowest free — because 2 × 13 is fewer workers
  than 2 × 16.

**Why the heartbeat is the load-bearing part.** `STALE_SECONDS` was thirty minutes and
the gates that caused this ran forty-five, so each one *disappeared from the count while
it was still running*. The next gate then measured a quieter machine than it had, took a
larger share, and made everything slower — a pileup that feeds itself, and one that no
amount of tuning the division would have fixed. A daemon thread now touches the slot
every 60 seconds and staleness drops to five minutes, so liveness is the heartbeat and
nothing else: a killed gate frees its slot within five minutes with no pid check and no
sweeper, and a live one is never mistaken for a corpse however long it runs.

**Why a hand-run pytest is in scope.** `gate_slots` only ever saw `scripts/check.py`. An
agent running `pytest -n auto` by hand took all 32 cores and was invisible to every gate
on the machine while doing it, which is a third gate by another name. The
`pytest_xdist_auto_num_workers` hook in `tests/conftest.py` resolves that count through
the same budget and takes a slot the same way; a serial `pytest -k one_test` takes
nothing and waits for nothing, and the pytest *inside* a gate recognises the slot its
gate is already holding through `AGENTJOBS_GATE_SLOT`.

**What task-534 should judge this against.** Whether two at thirteen beats three at ten
and one at twenty-six on time-to-review across a real day of dispatched runs, which is
the cost being minimised and not per-gate seconds. If a different shape wins there, the
numbers change and the mechanism stays.

**Outside this repository.** `limits.max_concurrent_runs` in `~/.agentjobs/dispatch.yaml`
still admits three runs, and should: a run is not a gate, and the third run's *work*
proceeds while only its gate queues. Its comment is the owner's to update — the
recommended text is on task-536.

#### One gate at a time, measured with real gates (task-534, 2026-09-22)

Task-536 chose two gates at thirteen workers from task-513's curve, which throttled a
*lone* gate with synthetic slots. That curve says 16 workers cost 8% over 32 when nothing
else is running. It could not say what a real neighbour costs, because a synthetic slot
uses no memory, starts no browser and runs none of the gate's other stages. This section
measures that directly.

**The experiment.** Three detached worktrees at one commit (`worktrees/agentjobs-534-a`,
`-b` and `-c`, at 446a0599), each bootstrapped and pre-warmed. Each arm is a wave of `K`
whole unqualified gates started together, one per worktree, and timed until the last one
finishes. The arms were interleaved A, B, C three times, so machine drift lands on every
arm rather than on one:

```
python scripts/gate_shape.py arm --label A --rep N --worktree <a>
python scripts/gate_shape.py arm --label B --rep N --worktree <a> --worktree <b>
python scripts/gate_shape.py arm --label C --rep N --worktree <a> --worktree <b> --worktree <c>
python scripts/gate_shape.py report
```

`gate_shape.py` sets `AGENTJOBS_GATE_CAPACITY` to `K` for the arm, so arm C runs three
gates at `26 // 3 = 8` rather than queueing its third. It reads each gate's width from the
gate's own output rather than assuming it. Memory is sampled every five seconds;
`typeperf` sampled CPU, disk, file-system operations, context switches and Defender from
part-way through rep 1 onwards.

| Arm | Shape | Wave, rep 1 / 2 / 3 | Gates per hour, mean (range) | One gate's pytest stage | Red gates |
|---|---|---|---|---|---|
| A | 1 at `-n 26` | 707 / 972 / 866\* s | **4.40** (3.70–5.09) | 521–669 s | 0 of 2\* |
| B | 2 at `-n 13` | 1587 / 1648 / 1559 s | **4.51** (4.37–4.62) | 1358–1398 s | 2 of 6 |
| C | 3 at `-n 8` | 2431 / 2321 / 2183 s | **4.68** (4.44–4.95) | 2170–2175 s | 2 of 9 |

\* A rep 3 overlapped a hand-run test session, so its time is shown but left out of the
mean, and its red, an e2e timeout, is not counted.

**Throughput is a tie.** The contended shapes average up to 6% more gates an hour, which
is inside the lone arm's own spread. The reason is visible in the counters: the machine's
aggregate rate barely moves with the shape.

| Window | CPU | File-control ops/s | Context switches/s | Processes | Defender (cores) |
|---|---|---|---|---|---|
| A rep 2, one gate | 24.9% | 55,800 | 84,200 | 661 | 0.64 |
| B rep 2, two gates | 26.5% | 66,600 | 97,300 | 672 | 0.75 |
| C rep 2, three gates | 26.9% | 66,600 | 91,200 | 676 | 0.78 |
| B rep 3, two gates | 25.5% | 73,500 | 87,400 | 668 | 0.74 |
| C rep 3, three gates | 25.8% | 65,500 | 86,800 | 673 | 0.78 |

**So a neighbour costs far more than the width does.** A paired gate's pytest stage ran
**2.7x** a lone one, and three together **4.2x**, where task-513's lone curve predicted
+8% for halving the workers. Neither cores nor memory is the bound. CPU never averaged
above 27% busy and the processor queue stayed near zero. The lowest free memory in any arm
was 5.9 GB. Disk latency was 0.26 ms. A single gate already drives the machine at about
the rate three do, so the binding resource is something shared and roughly serial that
none of these counters saturates. The file-system path is the leading candidate: 56,000
to 74,000 file-control operations a second, with Defender inspecting them. That is an
inference, not a measurement. Confirming it needs a run with Defender exclusions on the
worktrees, which is a machine security setting and was not changed here.

**With throughput tied, latency decides.** Take a convoy of three, an epic walk filling
every run slot, with a lone gate at about 14 minutes:

| Capacity | Finish times | Mean time to review | First result |
|---|---|---|---|
| **1** | 14, 28, 42 min | **28 min** | **14 min** |
| 2 | 27, 27, 41 min | 31 min | 27 min |
| 3 | 38, 38, 38 min | 38 min | 38 min |

Every red in the measurement came from a contended shape: a vitest region lookup, two
pytest timing tests and an e2e reload. Four reds are too few to state a rate, and they are
not claimed as one. They are evidence in the same direction, not the basis of the
decision.

**What changed.** `gate_slots.CAPACITY` went from 2 to 1. `DIVISION_CEILING = 2` keeps a
gate that finds a neighbour anyway at the paired share. That covers a gate that waited out
the timeout and a gate that cannot count its neighbours, and without it such a gate would
run at 26 beside another 26, the shape that drove this machine to 6 MB free on
2026-09-05. `QUEUE_TIMEOUT_SECONDS` went from 40 to 60 minutes, because the third gate of
a convoy now waits two whole gates, up to about 32 minutes at the slowest lone figure
measured.

**What was rejected.**

- *Keep two at thirteen (task-536).* It ties on throughput and loses on latency: 31
  minutes mean against 28, and the first result at 27 minutes rather than 14.
- *Three at eight.* This was the owner's question, and it is today's behaviour whenever
  the cap is lifted. It ties on throughput, and every gate in a convoy waits for the
  slowest, 38 minutes for all three.
- *A weighted budget at gate start*, as this task's description sketched. The budget
  would divide a resource the measurement shows does not divide: the machine's aggregate
  rate is the same at one gate as at three. A queue is the budget that matches that.

**Two confounders from the task, and what happened to them.** The fixed cost outside
pytest (black, ruff, mypy, api, icons, oxlint, vitest and build) was paid by every
concurrent gate, as predicted, and is inside every wave above. The e2e stage ran on four
Playwright workers throughout (task-369 had landed), so a gate in e2e was not using only
one core. That was the state measured, and a later change to e2e is a reason to re-run
`gate_shape.py` rather than to reuse these numbers.

**The run ceiling.** `limits.max_concurrent_runs` should stay at three, which is the
recommendation; it lives outside this repository. `run_report.py --overlap` put runs at
6.1 : 1 work to gate over the last week (75 runs), so runs rarely collide except in a
convoy. There a fourth run's gate would wait three gates, about 42 to 48 minutes, close to
the queue timeout. So a ceiling above three should come with a longer timeout, not on its
own.

**What the ledger says about overlap.** `run_report.py --overlap` measures how often
dispatched gates really run together:

| Window | Runs that gated | Work : gate | Gate time beside a neighbour | Most at once |
|---|---|---|---|---|
| 7 days | 75 | 6.1 : 1 | 42.2% | 3 |
| 14 days | 111 | 6.4 : 1 | 37.6% | 3 |
| 30 days | 176 | 5.2 : 1 | 29.1% | 3 |

On average gates rarely overlap, but the overlap clusters in epic walks. On 2026-09-20,
17 of the fortnight's 28 overlap episodes were siblings gating together, and three-gate
convoys ran for up to 29.7 minutes. The ledger undercounts this: it does not record gates
run by a scripted finish, which queue on the merge runway right behind the children that
were just gating.

#### Cores that follow the gates: assessed, not built (task-537, 2026-09-23)

The owner asked whether a lone gate should get the whole budget and a second should take
half of it from the first. The queue above already gives a lone gate everything the
reserve leaves; the question is the *take*. Three ways to take, and why none ships:

- **Fewer workers for a running gate is impossible.** pytest-xdist fixes its worker count
  at launch, so a gate at 26 cannot become 13. The only gates a worker count can be
  chosen for are gates whose pytest has not started, which is exactly what the capacity
  already decides.
- **Shrinking a running gate's CPU affinity was not measured, because nothing it serves
  still happens.** With a capacity of one, a second gate beside the first exists only
  after a 60-minute queue timeout or when the slot directory cannot be read. Even then it
  would put 26 workers on 13 cores with unchanged memory, and cores were not what bound:
  CPU sat at 25 to 27% busy in every task-534 arm, with the processor queue near zero.
- **Below-normal priority for agent pytest was not built, for the same reason.** Priority
  only reorders a contended run queue, and task-534 found none. It does nothing for
  memory, gate against gate, or the file-system path that is the leading suspect. The
  owner's reserve of six cores stays the protection. Revisit if a gate is ever measured
  with the CPU busy.

#### Running the stages concurrently (task-268, 2026-09-06)

`scripts/check.py --concurrent` runs each stage as soon as `check.DEPENDENCIES` allows —
`api` before `vitest` and `build`, `build` before `e2e`, everything else free — capturing
each stage's output and printing it whole when the stage ends. **It is off by default, it
says `EXPERIMENTAL RUN` at both ends, and it writes no gate receipt.** What follows is the
evidence ac-4 asked for before that could change; it is not an argument that it should
change yet.

Two worktrees of the same commit, `worktrees/agentjobs-268` and `-b`, both bootstrapped and
both with warm caches, each running the unqualified gate. Paired arms start at the same
moment. **A third dispatched gate (`worktrees/agentjobs-363`) came and went during the
run**, which is the normal case on this machine and is why each row names the gate count
its own budget line reported.

| Arm | Gates seen | pytest `-n` | A | B |
|---|---|---|---|---|
| Alone, serial | 1 | auto (32) | 254.3s / 245.8s | — |
| Alone, `--concurrent` | 1 | 28 | **165.1s / 166.0s** | — |
| Paired, serial | 2 | 16 | 325.4s / 375.7s | 352.8s / 403.6s |
| Paired, `--concurrent`, rep 1 | 3 | 6 | **206.4s** | **201.2s** |
| Paired, `--concurrent`, rep 2 | 2 | 12 | **235.4s** | **234.6s** |
| Paired, `--concurrent`, rep 3 | 3 | 6 | **211.2s** | **211.6s** |

Every one of the six contended concurrent gates exited 0. The concurrent figures are wall
clock; the same runs' summed stage costs — what they would have paid serially — are 397s
to 491s, and the gate prints both.

- **Alone it is a 35% cut**, 165s against 250s, and the wall clock is exactly
  `api + build + e2e`: the run is bounded by Playwright end to end.
- **Paired it is about 45%**, ~215s against ~365s. There the binding constraint moves to
  `pytest`, because `gate_slots` divides the machine between gates and `CONCURRENT_RESERVE`
  takes four more off the top: two gates give `32/2 − 4 = 12` workers, three give `6`.
- **The reserve is doing real work and is also the main thing left to tune.** At three
  gates a concurrent run's suite gets six workers where a serial one would get ten, and
  rep 1 and rep 3 are what that costs: pytest at 206s and 211s becomes the critical path
  and `e2e` finishes underneath it with time to spare. A reserve that shrank as the gate
  count rose would probably recover most of that. Not changed here, because the number
  that would justify it has not been measured.

**Why this is still a flag.** Three green contended pairs is what the task asked for and
it is what there is; it is not a flake rate, it is one evening on one machine, and the two
stages with their own timeouts — Playwright's 30s server start and 30s per test — were
never close enough to failing for anyone to know how much margin is left. Promoting it
should also come with the reserve question above settled, since the paired case is the
one that matters and it is the case the reserve is worst at.

### How many gates a run launches (task-339)

Task-233 made one gate cost 96s and the per-task gate bill did not fall, because the
number nobody had counted was **how many times a run launches it**.

`poetry run python scripts/run_report.py --since 14 --list`, 2026-09-05, completed runs
with gate records:

| Task | Run | Gates launched | Gate time |
|---|---|---|---|
| task-333 | 88m | 7 (3 failed) | 46.0m |
| task-330 | 104m | 6 (3 failed) | 33.9m |
| task-336 | 66m | 8 (2 failed, 2 abandoned) | 48.4m |
| task-328 | 60m | 8 (5 failed) | 21.9m |
| task-296 | 47m | 8 (3 failed) | 21.4m |
| task-321 | 80m | 8 (5 failed) | 20.3m |
| task-337 | 73m | 3 (1 failed) | 15.0m |
| task-320 | 47m | 2 | 14.4m |
| task-244 | 67m | 1 (1 failed) | 12.1m |

**Six to nine launches per run, half of them red, 20 to 46 minutes of gate per task.**
task-336's row reads 8 and 48.4m only since task-339 taught the report to count
**abandoned** gates — a `gate_started` with no finish, which is a gate the session was
killed inside or walked away from. It used to read 6 and 31.1m, and the two it dropped
were the two longest single blocks in the run.

The worked example, from `~/.agentjobs/runs/run_f401cd88/phases.jsonl`: task-336 added a
tab indicator to the React app, **finished the code 25 minutes in**, and reached review at
65. In between it ran a full gate on a stage it already knew was red, abandoned a second
mid-`e2e`, ran a third green, ran a fourth green over identical code (chained by the agent
as "wait for gate 3, then run the final gate"), then rebased and found `--since-gate`
could not narrow anything because two untracked sandbox files had stopped gate 4 writing a
receipt — and paid a sixth full gate for it.

Every one of those is addressed by the sequence in
[ENGINEERING.md §One gate per handoff](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md#one-gate-per-handoff), and the
two that the prose alone would not have caught now announce themselves: an unqualified
gate over a tree this run already has a green gate for prints `ALREADY GREEN` with the
moment it passed, and both the run that fails to earn a receipt and the `--since-gate`
that goes looking for one name the paths that blocked it.

### A browser that was gone before its test started (task-404)

`e2e` runs every spec against one Chromium process, and on this machine that process is
sometimes terminated between two tests. The next test then fails with
`browser.newContext: Target page, context or browser has been closed` before any line of
it runs. It was seen on at least thirteen gate runs between 2026-08-23 and 2026-09-13,
always costing exactly one test. At least once it stopped a scripted finish, on a branch
that could not have caused it.

**What kills the process is not known.** Task-404's record has what was ruled out and the
evidence for each: it is not a native crash (WER records those here and has none for
Chromium), not a console control event, not memory, and not this repository's test suite.

So the gate reads Playwright's JSON report before believing a red `e2e`:

```
BROWSER GONE, NOT A TEST FAILURE: 1 test never started.
...
Re-running only that test, once.
> npm run test:e2e -- --last-failed
RETRIED AND PASSED: the 1 test whose browser was gone passed on a second run, ...
```

- **It is not a retry of a failing test.** It is a retry of a test that never started. A
  failure qualifies only if every error on it is that message with *no source location*:
  Playwright's own `context` fixture raised it before the test body began. A browser that
  dies while a test holds a page fails at `page.something` with a line of the spec on it,
  and stays red. That is the case the application could have caused, and hiding it would
  be worse than the flake.
- **Nothing is retried beside a real failure**, beside an error outside any test, or when
  more than two browsers died in one run, since that is a condition rather than a flake.
  The banner still says which failures were which.
- **The retry has to run exactly those tests and pass them.** A `--last-failed` that ran
  nothing exits 0 and proves nothing, so it is refused.
- Playwright's own `retries` stays `0`, because a blanket retry is precisely the case the
  rule above excludes.

Both directions were checked for real on 2026-09-13, with a throwaway spec that killed its
own worker's browser from outside during the full suite: killed between tests, the gate
printed the banner, re-ran one test in 4.8s and passed the stage; killed inside a test
body, the stage failed with no banner and no retry. The rule is `scripts/e2e_failures.py`,
and `tests/test_e2e_failures.py` holds it to both directions without a browser. A retry
is recorded in a dispatched run's phases as `gate_stage_browser_gone`, so the rate stays
measurable.

### `--since-gate` is kept for the reasoning, not the saving

It was worth much more when task-221 wrote it: the full gate was six minutes then, and a
rebase that brought in a single task YAML cost all six to re-establish something that
could not have changed. The gate is now about a minute, so the same case saves under a
minute.

It stays because the reasoning is the durable part — the gate should be able to say what
a change cannot reach — and once `pytest` is cheap, the same machinery is what makes it
safe to add an expensive stage later. Its four properties are rules and live in
ENGINEERING.md.

### The pre-review gate stays full

Task-386 asked whether the gate an agent runs before handing off for review could be
narrowed to "the stages the change can reach". On a project with `finish=on` the merge is
protected by the finish's own full gate, so the pre-review gate is not what admits the
code to `main` — it only keeps a reviewer from being handed something that does not run.
The proposal was measured against the ledger and **rejected**. What follows is why, so it
is not re-proposed from the same intuition.

The corpus, read from `~/.agentjobs/runs/*/phases.jsonl` and this project's own task log,
2026-09-09. It covers the 70 runs of 197 that carry phase records; `phases.jsonl` arrived
partway through the ledger, so these are counts of what was recorded.

| | Gates | Time |
|---|---|---|
| Pre-review gates — the last full gate before a review handoff | 39, across 26 tasks | 3.27h, mean 302s |
| Earlier full gates in the same stretch (iteration) | 57, 40 of them red | 3.15h |
| Full gates that never answered a review at all | 116 | 7.80h |
| Scripted-finish gates | 125 | 9.45h |

**The premise did not survive the count.** The task was prompted by one run whose
pre-review gate was thrown away by a change request twelve minutes later. Across the whole
ledger that happened **4 times in 39** — 30 handoffs were approved and 5 answered a
question. The gate time a change request discarded is **1138s in total, 0.32h**. There is
no pot of money there.

**Both halves of the proposed rule are unsound in this repository.** The rule was "a
Python change runs black, ruff, mypy and pytest; a frontend change runs oxlint, vitest,
build and e2e". Neither holds:

- **`e2e` is not a frontend stage.** `frontend/e2e/run_server.py` starts
  `agentjobs.api.main:app` — the Python application — behind Playwright, and the specs
  drive dispatch, queue moves and task creation through it. A Python change reaches `e2e`
  directly.
- **`pytest` reads the frontend.** `tests/test_pwa_contract.py` reads
  `frontend/src/service-worker.js`; `tests/test_identity_problem_headlines.py` reads
  `frontend/src/components/identityProblem.ts`. A frontend change reaches `pytest`.

**And the saving is concentrated in exactly the stage the rule gets wrong.** Pricing the
proposed table against every branch `main` has merged — 214 of them, each diffed against
its own merge base, costed with the mean each stage took across every `gate_stage_finished`
record in the ledger: `pytest` 213.6s, `e2e` 114.0s, `vitest` 19.0s, `mypy` 9.6s, `api`
7.5s, `build` 6.6s, `icons` 3.4s, `black` 1.4s, `oxlint` 1.0s, `ruff` 0.3s — 376s for all
ten. This is a model over historical diffs, not a replay.

| Selection | Mean per branch | Total | Branches narrowed |
|---|---|---|---|
| Full gate every time (today's rule) | 376s | 22.37h | — |
| `gate_scope`'s table as it stands | 367s | 21.79h | 13 of 214 (6%) |
| The richer table task-386 proposed | 309s | 18.37h | 99 of 214 (46%) |

The richer table saves 4.00h, or 18%. **2.41h of that 4.00h is `e2e` skipped on a
Python-only branch** — 76 branches at 114s each, 81% of everything the Python half of the
rule saves. And `e2e` is the second most common stage to stop a red gate. Every red
`gate_finished` records the stage that stopped it, so this is read rather than inferred —
of the 161 red gates in the ledger, 60 stopped in `pytest` (37%) and **34 stopped in
`e2e`** (21%), ahead of `black` at 32 and `mypy` at 16.

So the trade on offer was: give up the stage that stops a fifth of all red gates, on the
changes most able to cause them, to save 67 seconds of a 302-second gate. The remaining
115 branches touch both Python and the frontend, and the rule narrows nothing for them.

**`gate_scope`'s existing table already is the reachable-stage rule, computed correctly.**
Its three entries — task records, `docs/`, `*.md` — are the classifications this
repository can actually defend, and its docstring already records that `frontend/*` was
considered and dropped. It narrows 6% of branches for 0.59h. That is the honest size of
the win, and it does not justify a new entry point: `--since-gate` needs a receipt, a
fresh worktree has none, and every dispatched run works in a fresh worktree.

**Where the time actually is.** 116 full gates never answered a review, and 57 more ran
inside a review stretch that already ended in one — nearly 11 hours against the 3.27h all
pre-review gates cost together. Those are gates run to iterate, which step 1 of the
sequence already forbids. The pre-review gate is not the expensive habit; running the full
gate instead of `--only <stage>` while fixing a known-red stage is.

**Reopen this if** `e2e` stops exercising the Python application, or a classification that
skips it becomes provable rather than assumed, or the finish's gate stops being the thing
that admits code to `main`. Until then step 3 of
[One gate per handoff](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md#one-gate-per-handoff)
is unqualified, and on a project with `finish=off` it is the only gate the work ever gets.

---

## What a restart costs

`scripts/bench_startup.py` measures getting a server *up*, which `bench.py` does not:
its corpus is the run history under the AgentJobs home, not the task store. Startup
housekeeping walks that history, so the cost grows with every dispatched run and no
amount of seeded tasks exercises it.

```bash
poetry run python scripts/bench_startup.py phases      # where the startup path's time goes
poetry run python scripts/bench_startup.py handover    # how long the port is dead
```

Both seed their own throwaway home, modelled by default on this machine's ledger as it
stood on 2026-09-20 — 330 run directories, 274 sessions already reaped, 31 still to
attempt and 20 of those permanently doomed. `phases --home ~/.agentjobs` measures the
real one instead; it reads, and skips the one step that would write.

**`handover` is the instrument the defect was found with.** It starts a server, stops
it, starts another, and watches which process owns the listening socket across the
handover — because connectability alone cannot tell a successor that has bound from a
predecessor that has not let go, and during the early part of a restart it is the *old*
process still answering. What it prints is the window in which nobody was listening.

A before/after pair is two runs of the same command from two checkouts, since what is
being changed is this repository's own startup path:

```bash
git worktree add ../worktrees/agentjobs-before <commit-before>
cp scripts/bench_startup.py ../worktrees/agentjobs-before/scripts/
python scripts/bench_startup.py handover    # from each checkout, same flags
```

The copy is deliberate: a benchmark has to be newer than the code it measures, so the
script guards what it reaches for in the application and falls back to the older shape.
Comparing two different scripts would compare the scripts.

### Startup blocked the bind, and a doomed reap was eternal (task-503)

Measured 2026-09-20, before `660b474c` and after, against the real home
(`phases --home ~/.agentjobs`, 332 run directories):

| step | before | after |
| --- | ---: | ---: |
| `import agentjobs.api.main` | 1.90s | 1.32s |
| `_verify_served_source()` | 0.00s | 0.00s |
| `capture_source_identity()` | 0.08s | 0.03s |
| `live_contract_digest()` | 0.91s | 0.96s |
| `list_runs()` | 0.24s | 0.23s |
| `_wakeable_run_ids()` | **39.18s** | **1.33s** |
| total | 42.32s | 3.88s |

`_wakeable_run_ids` asked `newest_session_run` once per task and each of those re-read
every run directory on the machine: 330 runs, 185 distinct tasks. The rule moved into
`newest_session_runs`, which answers every pair in one pass over records the caller
already holds. It keeps the same five runs.

And the end to end, from `handover` on the seeded default, two runs of each arm:

| | before | after |
| --- | ---: | ---: |
| launch to listening, cold | 43.7s / 45.1s | 6.2s / 5.7s |
| **dead port across a restart** | **74.2s / 48.5s** | **3.7s / 3.6s** |

Two changes produce that. Session reaping moved behind the bind — it spawns one session
manager per unreaped run, and nothing about serving a request depends on it. And a reap
the session manager refuses because it has no job by that id is now recorded as
`reap_settled` rather than `reap_blocked`, which was never a skip condition: 20 of this
machine's 31 attempts were permanently doomed, the oldest since 2026-09-10, and the set
only grew. Every other refusal still says `reap_blocked` and is still retried, because a
refusal meaning "this session still owns something worth keeping" is the half of `reap`
worth having.

**The two arms are not the same scale of machine load, and the spread above says so.**
The before arm's two dead-port figures differ by 26s because a 40-second start is long
enough to overlap whatever else this machine is running; the after arm's differ by
0.1s. That is the shape of the finding rather than noise in it — a cost that large is
also a cost that varies.

**A methodology note worth keeping**: sampling the socket's owner with `netstat` ten
times a second made the thing being timed slower. A 43.7s cold start had not finished
in 420s. The script now probes connectability cheaply and asks who owns the socket only
when that answer can have changed.

---

## Where agent time goes

`scripts/run_report.py` reads the run ledger in `~/.agentjobs/runs/` and prints total
time, runs per task, the length distribution, and — for runs dispatched since task-233 —
how much of each run was the gate and how much of that was gate runs that failed.

```bash
poetry run python scripts/run_report.py --per-task     # every task, worst first
poetry run python scripts/run_report.py --since 7      # the last week
poetry run python scripts/run_report.py --task task-233
poetry run python scripts/run_report.py --epics        # per epic: wall, work, idle
```

**A cycle-time claim is a before/after or it is an anecdote**, so `--split` prints the
table twice either side of a moment — give it the timestamp of the merge whose effect you
are claiming. Pair it with `--driver`: a window that introduced a second runner is not
comparable to one that had only the first, and the newcomer's startup failures land as
very short runs that move every percentile.

```bash
poetry run python scripts/run_report.py --driver claude --split 2026-08-21T23:24:52+00:00
```

Read the **median** task, not the mean. Both are printed, and at these sample sizes one
feature build moves the mean by a factor and the median not at all — the task-233
baseline's own mean fell from 56.7m to 40.7m on the removal of a single epic.

### Where epic time goes, and the baseline before concurrent walks (task-223)

Runs and finishes were each measured on their own, so **the gap between one child of an
epic closing and the next starting was in no table anywhere** — which is exactly the
quantity a concurrent walk removes. `--epics` groups runs and finishes under the parent of
their task and reports it.

```bash
poetry run python scripts/run_report.py --epics
poetry run python scripts/run_report.py --epic task-269    # one epic, span by span
```

Four numbers, and the distinction between the last two is the whole honesty of the table:

| Column | What it is |
|---|---|
| `wall` | time inside a **sitting** — what somebody watching this epic actually waited |
| `work` | the **sum** of child spans, ignoring overlap |
| `idle` | wall with no child running: a slot free with eligible work waiting |
| `x` | `work / wall`. **1.00 is serial**, whatever the slot count was |

A gap longer than `--gap-ceiling` (default 60m) ends a sitting and is reported as
`paused` instead of as idle. Without that split the first cut of this read **92% idle**,
because task-160's children span five days and most of that is a person asleep. The two
kinds of gap do not overlap on any epic recorded so far: turnaround is a poll interval
plus a dispatch, and the other kind is overnight.

`work` is the one figure here that is deliberately a sum rather than a union, and it is
the one that makes a concurrency claim checkable: running two children at once reduces the
wall clock *and* the union together, so a report of unions alone would hide the whole
improvement.

**The baseline, 2026-08-27, before any walk ran children concurrently** — this machine's
entire ledger:

```
  epic                             kids  sit  peak     wall     work     idle   idle%      x
  task-160-dispatch-phase-two         7    5     2   642.8m   535.0m   111.3m   17.3%   0.83
  task-081-task-selection-ranking     3    1     1   167.5m    79.5m    88.0m   52.5%   0.47
  task-280                            1    1     1    27.9m    21.8m     6.0m   21.6%   0.78
  task-211                            6    5     1   117.3m    37.2m    80.1m   68.3%   0.32
  task-269                            8    5     2   331.1m   332.9m    22.2m    6.7%   1.01

  epics                 5
  time in sittings      21.4h  (a gap over 60m ends one)
  child work            16.8h summed, so parallelism 0.78x  <- 1.00 is serial
  turnaround idle       5.1h (24% of it)
  paused between        167.4h
  ran serially          3 of 5 (peak concurrency 1)
```

**Read `x`, not `idle%`, for the after half.** Removing turnaround is the smaller of the
two savings; the larger is children overlapping, and only `x` shows it. The `peak 2` on
task-160 and task-269 is a child's scripted finish overlapping another child's run, not a
walk running two children — every walk in this window was serial by construction.

**The honest expectation is nearer 2x than 4x on a four-wide fan-out**, and it is worth
writing down so nobody quotes the older figure. Task-223's own brief cited +9% at two
concurrent gates and +16% at four; those are from the serial-pytest era and are now wrong.
Since task-233 the gate asks for every core, and [How the gate degrades under
contention](#how-the-gate-degrades-under-contention) measures three concurrent gates at
roughly 360s each against 96s solo. The gate is also only about a sixth of an instrumented
run, so most of what parallelises is the agent session rather than the gate — which is the
argument for this change and against grinding the gate again.

### Where the numbers come from

The gate reports itself: `scripts/check.py` appends a `gate_started` and a `gate_finished`
record to `phases.jsonl` in the run directory whenever it runs inside a dispatched run,
and writes nothing at all when it does not. Dispatch puts `AGENTJOBS_RUN_ID` and
`AGENTJOBS_RUN_DIR` in the session's environment, so anything downstream of the agent
inherits them and can add a phase with
`agentjobs.dispatch.phases.record_phase_from_env`.

It also reads `~/.agentjobs/finishes/`, where a **scripted finish** (task-241) writes
itself down. A finish is not a run — no agent, no session, no tokens — and it exists to
remove the follow-on run this report was built to measure, so it is counted in its own
block rather than folded in. Without that the saving would show up only as runs-per-task
falling, with nothing to attribute it to.

**Do not measure a run by grepping `transcript.log`.** It is a raw TTY capture, so a line
appears in it as many times as the terminal repainted it and every count derived from it
is an artefact of that. Task-233 is the incident; phase records exist so the question does
not have to be asked that way again.
