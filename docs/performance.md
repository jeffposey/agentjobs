# Measuring performance

Three questions, three tools:

| Question | Tool | Where |
| --- | --- | --- |
| How long does the product take to answer? | `scripts/bench.py` | [below](#producing-a-beforeafter-pair) |
| What does the repository gate cost? | `scripts/check.py` | [What the gate costs](#what-the-gate-costs) |
| Where does dispatched agent time go? | `scripts/run_report.py` | [Where agent time goes](#where-agent-time-goes) |

The rules derived from all three — quote a command and a date rather than a bare count,
state a before/after pair, prefer parse counts to wall clock — are in
[ENGINEERING.md](../ENGINEERING.md#testing). This file is the working detail and the
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
API
------------------------------------------------------------------------------
  surface                                   p50        p95   parses   srv ms
  GET /tasks                           3786.2ms   3801.0ms      476   3694.0
```

- **p50 / p95** — median and 95th-percentile wall time from the client, over the
  configured iterations, after one discarded warmup request.
- **parses** — how many task files the server read and parsed from disk to answer.
  Read from the `X-Task-Parses` response header.
- **srv ms** — time spent inside the application, from the `X-Response-Time-Ms`
  header. A large gap between this and p50 points at transport or client overhead
  rather than at the server.

**The parse count is the more useful number.** Wall-clock time depends on the machine,
what else it is running, and the weather; the parse count does not. A request that
parses a 119-file corpus 476 times is doing four times too much work on any hardware,
and a change that drops it to 119 has demonstrably fixed something. Prefer to write
assertions against parse counts and treat timings as corroboration.

## The two headers

Every API response carries them, not just benchmark runs. They are the way to
attribute a slow request without attaching a profiler:

| Header | Meaning |
| --- | --- |
| `X-Response-Time-Ms` | Wall time inside the application. |
| `X-Task-Parses` | Task files read and parsed from disk while serving the request. |

The parse counter is also available to tests through
`agentjobs.instrumentation.count_task_parses()`, which is how a test asserts that one
request never parses the same file twice.

## Choosing a corpus

The benchmark never runs against your live project. It copies task files into a
temporary project, serves that on its own port, and deletes it afterwards, so a run
cannot write to the real backlog and is unaffected by whatever a long-running server
happens to hold in memory.

```bash
poetry run python scripts/bench.py --corpus real        # a copy of tasks/agentjobs (default)
poetry run python scripts/bench.py --corpus synthetic --tasks 200
```

The synthetic corpus is generated at a size you choose, with realistically sized
records — prose, a multi-entry log, acceptance criteria, a dependency. Use it whenever
a number needs to stay stable over time: a threshold tuned against today's backlog
becomes a failing test when the backlog grows, through no fault of the code.

**Two runs are only comparable if they measured the same corpus.** Every report states
the file count and total bytes in its header, and `--compare` warns when the two do
not match.

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

---

## What the gate costs

The rules for *running* the gate are in
[ENGINEERING.md §Testing](../ENGINEERING.md#testing). The stage table and the measurement
history live here — kept because a performance claim is only checkable if the run that
produced it is on the record, and here rather than there because a session that is about
to commit does not need it, and because the four always-loaded files have a byte budget
(`tests/test_context_budget.py`) that a table of numbers is a poor use of.

### The stages, and what each cost

One green `poetry run python scripts/check.py` on this machine, 2026-08-21, with nothing
else competing for it. Read the bottom of your own run rather than quoting these; the
gate prints the same table every time, which is the whole point of printing it.

| # | Stage | What it checks | Cost |
|---|---|---|---|
| 1 | `black` | Python formatting | 0.6s |
| 2 | `ruff` | Python lint | 0.1s |
| 3 | `mypy` | Python types | 1.5s |
| 4 | `api` | `openapi.json` and the generated client both match the app | 4.2s |
| 5 | `icons` | the committed PWA icons match `assets/app-icon.svg` | 2.8s |
| 6 | `oxlint` | frontend lint | 0.6s |
| 7 | `pytest` | the Python suite, across every core | 52.1s |
| 8 | `vitest` | the jsdom component tests | 5.2s |
| 9 | `build` | `tsc --noEmit` and the production bundle | 3.7s |
| 10 | `e2e` | the Playwright suite against a live server | 25.0s |
| | | | **95.8s** |

MyPy is the one stage whose cost moves for a reason unrelated to load: under two seconds
against a warm cache, about nineteen seconds on the first run after a checkout.

### The three figures, and which to quote

| Figure | What it is | Measured |
| --- | --- | --- |
| **95.8s** | one green `scripts/check.py` on this machine with nothing else competing | 2026-08-21 |
| **~155s** | the median full passing gate a *dispatched* session actually paid — 125s, 141s, 155s, 157s, 174s, from the phase records | to 2026-08-23 |
| **342s / 361s / 384s** | three concurrent parallel gates, from `run_4063f1c0` | 2026-08-23 |

95.8s is a quiet-machine best case and 155s is the working figure. Quote whichever the
question calls for, and say which.

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

### Why the cheap stages run first

Task-189 moved `api`, `icons` and `oxlint` above `pytest`. Together they cost 8.2s, and a
session working task-188 paid four and a half minutes twice to reach one of them.
Everything above the pytest line now costs 9.8s together.

The argument used to be stated as "seconds before minutes", and task-233 took the minutes
away. The ordering stays regardless: it costs nothing, and the gap it exploits reappears
the moment a slow stage is added.

### How the gate degrades under contention

| Concurrent gates | Serial suite (historical) | Parallel suite |
|---|---|---|
| 1 | 365s | 96s |
| 2 | 388s | not measured |
| 3 | not measured | ~360s |
| 4 | 411s | not measured |
| 6 | 444s | not measured |

The serial column is kept only as history: it does not describe the gate as it now runs.
Two conclusions follow from the parallel column:

- **The parallel gate does not degrade as gently as the serial one did**, because
  `-n auto` asks for all 32 cores and three gates are dividing the same machine. The
  absolute number is still no worse than the serial gate ever was.
- **A run's summed gate time can exceed its own duration, and that is not a bug.**
  `run_report.py` reports what the phase records say; overlapping gates make the
  percentage a sum, not a share of a timeline. The report flags it when it happens.

#### What is actually scarce, measured (task-339, 2026-09-05)

Task-233 assumed cores. It is memory. Sampling `\Memory\Available MBytes` and the
`python` process set every twelve seconds through the runs below, on a 64 GB machine:

| | Peak `python` processes | Peak `python` working set | Lowest free memory | Mean CPU |
|---|---|---|---|---|
| One gate | 175 | 9.3 GB | 923 MB | 29% |
| Two gates | 282 | 15.9 GB | **159 MB** | 43% |

**159 MB free of 64 GB, at 43% CPU.** The machine is paging, not queueing for cores,
which is why the degradation is worse than the 2x that dividing 32 cores between two
gates would predict — and why the fix is a cap on the *total* number of xdist workers
alive on the machine rather than a fairness scheme. `-n <cores / active gates>` gives
exactly that: N gates at 32/N workers each is 32 workers however many gates there are,
so the machine-wide footprint of the pytest stage stays what a single gate costs.

Two things fall out that are not about speed:

- **Contention makes the suite flaky, not merely slow.** In the two-gate before-arm,
  `tests/test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too`
  failed with "pid 3393900 survived the timeout" — a timing assertion losing to a paging
  machine. A red gate costs a whole extra launch, so this is part of the six-to-nine.
- **One gate alone is already close to the edge here** (923 MB free at `-n auto`), with a
  browser session holding several gigabytes. Lowering the single-gate worker count is a
  separate lever and belongs to task-268, which owns `check.py`'s stage internals.

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
[ENGINEERING.md §One gate per handoff](../ENGINEERING.md#one-gate-per-handoff), and the
two that the prose alone would not have caught now announce themselves: an unqualified
gate over a tree this run already has a green gate for prints `ALREADY GREEN` with the
moment it passed, and both the run that fails to earn a receipt and the `--since-gate`
that goes looking for one name the paths that blocked it.

### `--since-gate` is kept for the reasoning, not the saving

It was worth much more when task-221 wrote it: the full gate was six minutes then, and a
rebase that brought in a single task YAML cost all six to re-establish something that
could not have changed. The gate is now about a minute, so the same case saves under a
minute.

It stays because the reasoning is the durable part — the gate should be able to say what
a change cannot reach — and once `pytest` is cheap, the same machinery is what makes it
safe to add an expensive stage later. Its four properties are rules and live in
ENGINEERING.md.

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
