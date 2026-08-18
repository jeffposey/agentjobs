# Measuring performance

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
| `--port N` | Port for the benchmark's own server (default 18950). |
| `--source PATH` | Where the real corpus is copied from. |

The benchmark is deliberately **not** part of `scripts/check.py`. It starts servers and
a browser and takes minutes; the repository gate has to stay fast enough that people
actually run it.
