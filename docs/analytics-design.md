# Analytics page — design record

**Status: proposed 2026-09-06; §6 shipped, the page has not.** Task-213, the design child
of task-212. The history contract in §6 landed as task-371 on 2026-09-08 (§6.4 says where
each item lives). The API, the page and its entry point — task-372, task-373, task-374 —
are open and unstarted, so §7 onwards is still a proposal.

**Pass two, 2026-09-19 (task-471): §16 onwards.** The first page shipped (task-372,
task-373, task-465) and the owner found it thin. §16 says why, §17 defines the lifecycle
segments, §18 is the metric catalogue, and §19 to §21 are what the page, the store and
the API change. Where a section below is superseded or amended, a line at its head says
by which.

This document decides two things that have to be decided together: **where the history
behind the numbers comes from**, and **what the page says**. They are one design because
each constrains the other — a chart nobody can answer is a wish, and a column nobody
plots is a cost.

It consumes the store contract task-273 settled, and amends it in §6. Everything in §5 is
a measurement taken on this machine on **2026-09-06** against the real 367-task corpus,
not an estimate. The probe that produced those numbers is throwaway and lives in a job
scratch directory; nothing from it was committed.

**What it does not decide.** The page's implementation, the API's implementation, the
storage backend (task-273) or the migration (task-311). Cross-project and per-actor
analytics are excluded by the parent and are not designed for.

---

## 1. The gap

The Dashboard used to carry a five-tile strip — Needs you / In Progress / Blocked /
Completed / Total — computed by `build_dashboard_snapshot` in `src/agentjobs/dashboard.py`
from current task state. Task-294 removed it outright on 2026-09-06 and deliberately left
no replacement, no route and no placeholder, on the grounds that four of the five numbers
are already answered by something that stays and the fifth is analytics rather than a call
to action.

So this task starts from a clean floor: **there is no analytics surface, no entry point to
one, and nothing on the Dashboard pointing at a page that does not exist.**

The strip's real defect was never its position. It was that **a count has no meaning on
its own.** 125 open tasks is a project going under or a project finishing, and which one
it is depends entirely on what the number was last week. The store could not answer that
question at all: task records hold current state, and the log holds entries whose payloads
were written to explain a change to a human rather than to reconstruct one.

Two facts fixed that:

- The owner decided on 2026-09-05 that **authoritative SQLite** replaces canonical YAML
  (task-273 backend, task-311 migration).
- Task-273's design pass drafted `task_event` — one row per state-changing event carrying
  **before and after** for nine axes — and the owner chose, on 2026-09-06, to **mine the
  git history of the task records** as a one-time backfill.

This design is the consumer of that contract, checking it against the questions a person
actually opens the page to ask.

---

## 2. The questions, before the charts

The parent's constraint is that this is not a chart list. Four questions justify the page.
Everything in §8 exists because one of them needs it; anything that answers none of them
is not on the page.

**Q1. Is the backlog growing or shrinking?**
The one the strip could not answer. It needs open-task count as a series, and it needs the
two flows behind it — work arriving and work finishing — because a flat backlog produced by
zero arrivals and zero completions is a stalled project, and a flat backlog produced by ten
of each is a healthy one. The chart must show the level *and* the flows.

**Q2. Is work getting finished faster or slower?**
Throughput (how many tasks close per week) and cycle time (how long a task takes from
creation to close). These move independently and both matter: throughput can rise because
the work got smaller, and cycle time can fall because the hard tasks were abandoned rather
than finished.

**Q3. Is anything aging badly?**
Not a mean. A mean age hides the one task that has been open for four months, and that task
is the entire content of the question. This needs a distribution and a named list.

**Q4. Where is work stuck?**
The ball is the schema's answer to "who acts next", so "stuck" has an exact definition here
rather than a heuristic: work sitting on `human` is waiting for a person, work on
`external`/`dependency` is blocked on another task, work on `external`/`service` is blocked
on a third party. This needs the current holders **and how long each has held**, plus the
same split over time so a rising pile is visible before it is painful.

A fifth question is deliberately **not** on the page: *"what should I do next?"* The
Dashboard's next-action ladder answers that, it answers it well, and the parent's
constraints forbid disturbing it.

*Extended by §16.1 (task-471): Q5 to Q8 — where a task's time goes, whether the machine
is keeping up, whether review is the bottleneck, whether questions get answered. Q2's
cycle time is redefined there (§17); the "what should I do next" exclusion stands.*

---

## 3. Where the history comes from

### 3.1 The three tables the page reads

Everything on the page comes from three of task-273's tables. No query in §5 touches a JSON
column, and none parses a task document.

| Table | What the page takes from it |
|---|---|
| `task_event` | Every series. One row per state-changing event, with `_from`/`_to` for lifecycle, ball, ball_reason, outcome, archived, priority, owner, position and parent, plus `source`, `mechanical` and the generated `open_delta`. |
| `task` | Every "right now" figure: the five counts, the age distribution, the oldest-open list, cycle time (`created_at` → `closed_at`). |
| `task_run` | Dispatch activity, if §8.7 is built. Not required by Q1–Q4. |

`project.history_baseline_at` and `project.history_baseline_kind` are the coverage
boundary; §3.6 says what the page does with them.

### 3.2 Open, closed, and the backlog level

**Open means `lifecycle <> 'closed'`.** Draft, ready and active are all open: a draft is
work that exists and has not been done, which is precisely what a backlog is. This matches
`dashboard.py`'s existing `total`, and §5.4 shows the page's numbers reconciling with the
running server's.

The level is a running sum of `open_delta`, which task-273 generates as `+1` on `create`,
`-1` on the transition **into** `closed`, `+1` on the transition **out of** it, and `0`
otherwise. Three properties follow, and the third is the one that makes the chart
trustworthy:

1. **Reopening is handled by construction.** A reopen is a transition out of `closed`, so
   it adds one to the level on the day it happened, without the query knowing the word.
2. **A close is counted once however many times the record was rewritten.** Only a
   *transition* moves the delta; an edit to a closed task is `0`.
3. **The invariant**: `SUM(open_delta)` over every event equals `COUNT(*)` of open tasks,
   at every instant. Measured on the imported corpus: **125 = 125** (§5.3). This must be a
   test in the backend child, not a hope — it is the single assertion that catches a
   history which has quietly stopped adding up.

### 3.3 Completed is not closed, and unique tasks are not events

Two distinctions the page must not blur, both already settled and both measurable in this
corpus.

**Completed ⊂ closed.** `Dashboard.tsx` counts `outcome == 'completed'` only, deliberately,
so cancelled work is not counted as finished; the parent's constraints keep that rule. The
corpus today: 242 closed tasks — **219 completed, 16 superseded, 5 duplicate, 2 cancelled**.
Reporting 242 as "completed" would overstate delivered work by 10%.

So the throughput chart plots **completed** and the outcome split (§8.4) shows the rest,
because 23 tasks closed without being finished is itself information about the project.

**Unique tasks ≠ completion events.** A reopened task closes twice. The corpus holds **244
close events across 242 distinct tasks** — two reopenings. The API returns both numbers on
every throughput bucket:

- `tasks_completed` — `COUNT(DISTINCT task_id)`, what the chart plots.
- `completion_events` — `COUNT(*)`, shown in the readout when it differs.

They differ by two today and will differ by more later. The chart plots the honest one; the
other is there so a reader who notices the sum not matching has an answer.

### 3.4 Archive

`archived` is a visibility flag, not a lifecycle state, and it is orthogonal to open/closed.
The rules:

- **The backlog level ignores `archived`.** An archived open task is still open; hiding it
  would make the level disagree with the invariant in §3.2 and with the Dashboard.
- **The "right now" panels — aging (§8.5), oldest-open (§8.5), stuck (§8.6) — exclude
  archived tasks**, because those panels are calls to attention and an archived task is
  explicitly not one.
- Every panel that filters says so in its subtitle: *"open, not archived"*.

Two tasks are archived in this corpus, both taking `archived: true` in the single 2026-08-10
schema-migration commit as an initial value. **`archived` has never transitioned in this
project's history** — verified against the git backfill, not assumed. So the archive
dimension has no history to plot and none is being lost; `archive`/`unarchive` events exist
in the schema for the future.

### 3.5 Day buckets and the timezone — and why the obvious answer is wrong

A daily chart needs an answer to "when does a day end", and it needs exactly one, or two
panels will disagree by a day at the edges.

Task-273's offer is `date(ts, :tz)` with the offset from `project.reporting_tz`. **That is
wrong for half of every year, and it is measured rather than argued.** SQLite has no
timezone database; its `date()` modifier takes a *fixed* offset. Central time is `-06:00`
in winter and `-05:00` in summer, so:

| instant (UTC) | true local day (America/Chicago) | `date(ts,'-06:00')` | `date(ts,'-05:00')` |
|---|---|---|---|
| 2026-07-04T05:30Z | 2026-07-04 (CDT) | **2026-07-03** ✗ | 2026-07-04 ✓ |
| 2026-12-24T05:30Z | 2026-12-23 (CST) | 2026-12-23 ✓ | **2026-12-24** ✗ |

Either constant is wrong for roughly half the year, in the late-evening hours — which is
when a good deal of this project's work is recorded.

Three ways out were measured (§5.5):

- **Fixed offset in SQL** — 0.24 ms, wrong half the year. Rejected.
- **A boundary table joined in SQL**, with the API computing each local day's UTC start
  through `zoneinfo` — correct, and **58.8 ms**, 250× the naive form, because the
  correlated lookup runs once per event against an unindexed `VALUES` list. Rejected on
  cost.
- **Bucketing in the API** — SQL returns `(ts, open_delta)` filtered by an indexed range;
  the API folds them into local days with `zoneinfo`. Correct, and **0.91 ms** over the
  whole corpus. **Chosen.**

**Decision: day bucketing happens in the API, using `zoneinfo` and the project's IANA
reporting timezone. SQL filters and returns instants; it never names a day.**

Three things fall out of that, all of them wanted anyway:

- **The API has to build the calendar spine regardless.** Only **26 of 316 days** in this
  corpus have a backlog event — **8%**. A chart plotted from grouped rows alone would draw
  a line through eight per cent of the days and imply the level was undefined between them.
  The API emits every day in the range, carrying the running level forward.
- **`project.reporting_tz` holds an IANA name** (`America/Chicago`), not an offset. §6
  asks task-273 to say so.
- **Week and month buckets get the same treatment**, so "week" means a week in the reporting
  zone rather than `strftime('%W')` over UTC.

**Scaling, and its trigger.** The rows crossing into Python are bounded by the range filter,
not by the corpus: 613 delta events for the whole of this project's history, and 11,860 at
20× scale for a 90-day window (§5.6). If a window ever returns enough events that bucketing
exceeds ~50 ms, the answer is task-273's already-designed `task_state_daily` rollup — a
rebuildable accelerator, never a second authority. At today's 0.91 ms that is roughly 50×
away.

### 3.6 Coverage: what is known, what is reconstructed, and what is not zero

The store distinguishes three kinds of event through `task_event.source`, and the page must
render the distinction rather than flatten it:

- **`native`** — written by a manager verb at the moment of the change. Exact.
- **`reconstructed`** — replayed from the log, or a reconciliation row written at import.
  The timestamp is an upper bound on when the change really happened.
- **`backfilled`** — recovered from the git history of the task records, carrying
  `detail_json.git_commit`. **The timestamp is commit time, not change time** (§4.3).

`project.history_baseline_at` is the instant before which the store makes no claim, and
`history_baseline_kind` says how it was arrived at.

**Three rules for the page, and the first is the one the acceptance criteria are about:**

1. **Unknown is never drawn as zero.** Every series starts at `history_baseline_at`, not at
   the epoch and not at the first event. A range that reaches back before the baseline is
   clipped to it and the panel says so. A backlog line that runs to zero on the left is a
   lie the reader has no way to detect.
2. **Reconstructed spans are marked.** The region left of the last `reconstructed` or
   `backfilled` event is drawn hatched, with a rule at the boundary and a one-line caption:
   *"History before 26 Oct 2025 was reconstructed from the task records' own git history;
   timestamps in that span are commit times."* The reader is told once, on the chart, not
   in a document they will not read.
3. **Coverage is part of the payload, not an afterthought.** The API's `coverage` object
   (§7.3) carries the baseline, its kind, the first native event, and the counts by source,
   so the page never has to infer what it is allowed to claim.

---

## 4. What the existing history can actually support

Task-273 measured log replay alone and found it unfaithful — priority history absent
entirely, 79 tasks with no creation event. The owner chose to mine git as well. This design
**re-measured that choice end to end**, because the analytics page is what the choice was
made for, and because a coverage claim nobody has checked is the most expensive kind of
claim to be wrong about.

### 4.1 The method

The whole corpus was loaded into task-273's schema, and `task_event` was synthesised three
ways at once: log replay with the manager's own semantics applied, a git backfill over
**1,420 commits reaching back to 2025-10-26** (`git log --reverse -p -U0 -M`, **0.4 s**,
11.2 MB of diff), and a reconciliation pass. The replayed final state was then compared
against the current row on eight axes.

### 4.2 The result: 89% of tasks reconstruct exactly

| axis | tasks whose replay disagrees with the current row |
|---|---|
| ball / ball_reason | **1 / 367** |
| position | 2 / 367 |
| parent | 3 / 367 |
| priority | **6 / 367** |
| archived | **0 / 367** |
| outcome | 22 / 367 |
| lifecycle | 29 / 367 |
| **any axis** | **41 / 367 (11%)** |

Against task-273's log-only measurement — priority 337/367 wrong, ball 35/367 — this is a
different quality of history, and it vindicates the owner's answer. Three things did it:

- **Git supplies the starting value the log never wrote.** A priority change is logged as a
  `note` whose `data.fields` says `["priority"]` and never says the value; the replay had
  nothing to start from. Git has the value at file creation for **369 tasks**, which closes
  almost the whole gap.
- **`queue_move` entries carry priority after all.** A band change records `from_band` →
  `band`, so most priority transitions were in the log the whole time. Only **21** of the 38
  git-observed priority transitions were new information after de-duplication.
- **The manager's own semantics recover the ball.** Entries that record a lifecycle without
  the ball it implies — promote, release, claim — are replayed by applying the rule the
  manager applies. Ball disagreement went from 119/367 to **1/367**.

The residual 41 tasks are the 2026-08-10 v1 migration wall: 60 entries whose whole payload
is a `v1_status` marker, and tasks whose close predates any structured record of it.
Import writes one `kind='import'`, `source='reconstructed'` event per such task carrying the
true current values, after which the reconciliation invariant holds exactly.

**What is genuinely thin, stated so no chart over-claims:** priority moved **37 times in
this project's life**, earliest 2026-08-15. A priority-churn chart will look sparse because
the project is sparse, not because the method failed.

### 4.3 The finding that matters most: commit time is not change time

Git records when a change was *committed*. The task record it describes was written
earlier — usually seconds, sometimes hours. Taking a backfilled creation instant at face
value produces an event ordering that is **causally impossible**, and this is not
hypothetical:

> Three tasks' logs record a close at `2025-10-26T01:24Z`. Git first sees those files at
> `2025-10-26T16:21-05:00` — twenty hours later. Replayed literally, the backlog series
> opens at **−3**.

A negative open count is impossible, it is visible on the chart, and no reader could
diagnose it.

**The rule, and it belongs to the import rather than to the page:**

> A backfilled `create` event is timestamped at **the earliest evidence that the task
> existed** — `min(git commit time, the task's own `created` field, its first log entry)` —
> never at commit time alone.

Applying it removed the impossibility (series minimum **1**, maximum 126, ending at the true
125) *and* improved fidelity, taking tasks-with-any-disagreement from 44 to 41. §6 hands
this to task-311 as a required import rule; §12 keeps a page-side floor as defence in depth,
because a store that has been imported once by a version without this rule will still be
read by this page.

### 4.4 What the git backfill is allowed to touch

Backfilling every field git can see **double-counts every close**: the log already records
lifecycle transitions with their true timestamps, and git records the same transitions again
at commit time. Measured: `SUM(open_delta)` came out at **69 against 125 open tasks**, an
error of 45%, and the query plans gave no hint.

> **The backfill is authoritative only for axes the log does not record.** Lifecycle and
> outcome come from the log, always. Priority, parent, category and `archived` come from
> git. `queue_position` takes both — a bulk renumber rewrites it with no per-task log
> entry — de-duplicated against any native move of the same axis within five minutes.

With that rule the invariant holds exactly and 314 backfilled events survive de-duplication
out of 4,773 raw git observations.

### 4.5 The cost of the backfill, and when it must run

One pass: **0.4 s of `git log` and 0.16 s of parsing**, for the whole history. It is a cheap
one-time job, not a project.

**The sequencing constraint that is fatal to get wrong, and belongs to task-311: the
backfill reads the git history of the task files, so it must run — and be verified — before
those files are retired.** Retire first and the evidence is gone permanently. This is also
already recorded on task-273; it is repeated here because this design is the reason the
evidence is worth anything.

---

## 5. Measured evidence

Machine: this one, 2026-09-06. Corpus: the live 367-task project, copied to a scratch
directory. SQLite 3 through Python 3.13. Latencies are the median of 25 runs.

### 5.1 The corpus, and what the page would cost today

| | |
|---|---|
| task records | **367** (8.0 MB of YAML) |
| log entries / operations / runs | 3,555 / 1,760 / 164 |
| parse the whole corpus (libyaml) | **427 ms** |
| `GET /api/projects/agentjobs/tasks` | **790 ms**, `X-Task-Parses: 367`, 7.0 MB body |
| `GET /api/projects/agentjobs/dashboard` | **719–779 ms**, 367 parses |

**An analytics endpoint on today's storage would cost more than either**, because it needs
the log of every task and not just the current row — 427 ms of parsing before any work, plus
the replay, plus a git pass the parent's constraints forbid doing per request. This is the
whole argument for the page waiting on task-273, restated as a number.

The probe's loaded database is **9.7 MB** against 8.0 MB of YAML. The overhead is indexes,
and it buys everything below.

### 5.2 The page's query set

Seventeen queries — the whole page, including panels §8 does not require. **No plan touches
a JSON column, and no plan scans a task document.**

| query | rows | p50 (ms) | plan |
|---|---|---|---|
| backlog series (running sum) | 26 | **0.239** | `SEARCH task_event USING INDEX ix_event_backlog` |
| arrivals vs completions per day | 26 | 0.241 | `SEARCH task_event USING INDEX ix_event_backlog` |
| throughput per week | 6 | 0.223 | `SEARCH task_event USING INDEX ix_event_closed` |
| aging buckets | 3 | 0.129 | `SEARCH task USING INDEX ix_task_activity` |
| oldest open work | 10 | **0.018** | `SEARCH task USING INDEX ix_task_open_age` |
| where work is stuck, with dwell | 4 | 0.281 | `SEARCH task USING INDEX ix_task_ball` |
| holders over time | 20 | 0.456 | `SEARCH task_event USING INDEX ix_event_ball` |
| cycle time per month | 4 | 0.220 | `SEARCH task USING INDEX ix_task_closed_at` |
| cycle-time p50/p90, 90 days | 1 | 0.395 | `SEARCH task USING INDEX ix_task_closed_at` |
| the board on a past date | 3 | 0.674 | `SEARCH task_event USING COVERING INDEX ix_event_task_ts` |
| history coverage | 3 | 1.266 | `SEARCH task_event USING INDEX ix_event_project_ts` |
| the five current counts | 1 | 0.130 | `SEARCH task USING INDEX ix_task_activity` |
| dwell time per ball state | 3 | 0.891 | `SEARCH task_event USING INDEX ix_event_task_ts` |
| reopenings per day | 2 | 0.322 | `SEARCH task_event USING INDEX ix_event_project_ts` |
| outcome split per month | 10 | 0.180 | `SEARCH task USING INDEX ix_task_closed_at` |
| dispatch activity per day | 13 | 0.107 | `SEARCH task_run USING INDEX ix_run_task` |
| backlog by priority band | 63 | 0.527 | `SEARCH task_event USING INDEX ix_event_backlog` |
| **whole page** | | **6.30 ms** | |

Against 719–779 ms for today's dashboard endpoint, the storage half of a page carrying
seventeen panels costs **six milliseconds**.

The two headline queries, in full, are §7.2's contract:

```sql
-- Q1, the backlog level. `open_delta` is a STORED generated column, so this is a
-- sum over one indexed integer and nothing else.
SELECT ts, open_delta
  FROM task_event
 WHERE project_id = :p AND open_delta <> 0 AND ts >= :from
 ORDER BY ts;
-- plus the opening balance, so a windowed chart starts at the right level:
SELECT COALESCE(SUM(open_delta), 0)
  FROM task_event
 WHERE project_id = :p AND open_delta <> 0 AND ts < :from;   -- 0.031 ms at 20x scale
```

```sql
-- Q2, throughput. Unique tasks and completion events, kept apart (§3.3).
SELECT ts, task_id
  FROM task_event
 WHERE project_id = :p AND lifecycle_to = 'closed' AND outcome_to = 'completed'
   AND ts >= :from
 ORDER BY ts;
```

Both return instants; the API buckets them (§3.5).

### 5.3 The invariant, and reconciliation with the dashboard

- `SUM(open_delta)` = **125**; `COUNT(*)` of open tasks = **125**. Holds after import,
  after the git backfill, and after reconciliation.
- The five counts computed from the probe database are **identical** to what the live server
  on 8876 returns from `build_dashboard_snapshot`:

  ```
  probe : total 367  in_progress 1  blocked 0  waiting_for_human 0  awaiting_input 29  completed 219
  server: total 367  in_progress 1  blocked 0  waiting_for_human 0  awaiting_input 29  completed 219
  ```

  That is the parent's sc-6 and this task's sc-7 — *current-state totals reconcile with the
  dashboard* — demonstrated rather than asserted. It must become a test: the backend child
  asserts the analytics `totals` object equals `build_dashboard_snapshot(...)["stats"]` for
  the same corpus, so the two can never drift apart silently.

### 5.4 History shape, which is what the empty states are for

- Event span **2025-10-26 → 2026-09-06**, 316 days.
- **26 of those days (8%) carry a backlog event.** The calendar spine in §3.5 is not
  cosmetic.
- 2,155 replayed/backfilled events + 41 reconciliation events. By source: **1,839 native,
  314 backfilled, 43 reconstructed**.
- 244 close events over 242 distinct tasks; **2 reopenings**.
- Current open work sits at: `agent`/`available` 93, `human`/`spec` 29, `agent`/`answer` 2,
  `agent`/`work` 1.

### 5.5 The timezone measurement

| approach | correct across DST | p50 |
|---|---|---|
| `date(ts, '-06:00')` in SQL | **no** | 0.239 ms |
| boundary table joined in SQL, boundaries from `zoneinfo` | yes | **58.8 ms** |
| **API-side bucketing with `zoneinfo`** | **yes** | **0.906 ms** |

367-day spine, whole corpus, all three producing the same final level of 125.

### 5.6 At twenty times the size

The corpus was synthetically multiplied to **7,340 tasks and 43,920 events** (64 MB
database, `ANALYZE` run) and the same queries re-measured. The invariant still holds
(2,500 = 2,500).

| query | 1× | 20× |
|---|---|---|
| backlog series | 0.24 ms | **4.5 ms** |
| throughput per week | 0.22 ms | 14.0 ms |
| holders over time | 0.46 ms | 18.4 ms |
| the five current counts | 0.13 ms | 13.8 ms |
| history coverage | 1.27 ms | 37.3 ms |
| dwell per ball state | 0.89 ms | 43.1 ms |
| **whole page** | **6.3 ms** | **253 ms** |
| API-side day bucketing | 0.91 ms | 17.4 ms (12,260 delta events) |

253 ms for a seventeen-panel page at twenty times this project's size is acceptable, but
two of the panels degrade worse than linearly and both are fixed by an index. Measured, at
20× scale:

| index | query | before | after |
|---|---|---|---|
| `ON task(project_id, lifecycle, ball, outcome)` | the five counts | 14.6 ms (`SCAN task`) | **1.68 ms** (covering) |
| `ON task_event(project_id, source, ts)` | history coverage | 21.9 ms (`SCAN task_event`) | **6.58 ms** (covering) |

Both are requested in §6. The five-count query is worth the index on its own account: it is
what the Dashboard's own summary would use if it stops counting in Python.

A 90-day window at 20× scale — what the page actually requests — returns 11,860 backlog
delta rows in **3.9 ms**, 13,500 ball rows in 17.9 ms, and 4,640 completion events in
10.9 ms, all through indexed range scans.

---

## 6. What this design asks of task-273

Task-273's constraint is *"coordinate the schema/history contract with task-213 before
freezing it"*, and the owner's answer to its fourth fork was *run task-213 next, then
build*, without a blocking edge. This section is that coordination. **Nothing here is a
redesign; every item is a column, an index, or a rule at import.**

Ordered by what it costs to omit.

### 6.1 Required — the store is wrong without these

**A. Clamp a backfilled creation instant to the earliest evidence.** §4.3. Commit time
postdates the change, and taking it literally makes the backlog series go negative.
Belongs to task-311's import.

**B. Restrict the git backfill to axes the log does not record.** §4.4. Backfilling
lifecycle and outcome double-counts every close — measured at a 45% error in the level with
no symptom in any query plan. Belongs to task-311's import.

**C. `project.reporting_tz` holds an IANA zone name, not an offset.** §3.5. `America/Chicago`
is correct all year and `-06:00` is correct for half of it. The column already exists; this
fixes what goes in it, and the API is what reads it.

**D. Assert the reconciliation invariant in the store's own test suite.** `SUM(open_delta)`
= `COUNT(*)` of open tasks, after import, after backfill, and after a backup/restore round
trip. Task-273 already proposes it; this design depends on it, so it is named here as a
dependency rather than a suggestion.

### 6.2 Requested — cheap now, a migration later

**E. `CREATE INDEX ix_task_counts ON task(project_id, lifecycle, ball, outcome)`.**
Measured 14.6 ms → 1.68 ms at 20× (§5.6). Serves both this page's totals and any future
summary that stops counting in Python.

**F. `CREATE INDEX ix_event_source ON task_event(project_id, source, ts)`.** Measured
21.9 ms → 6.58 ms at 20×. Coverage is computed on every page load, so it is a hot query
despite looking like metadata.

**G. `FOREIGN KEY (project_id, parent_id) … DEFERRABLE INITIALLY DEFERRED`.** Found while
loading the corpus: the parent self-reference makes a bulk insert fail unless children
follow parents, and an importer should not have to topologically sort a task graph that may
legitimately arrive in any order. Deferring costs nothing and the constraint still holds at
commit.

**H. Keep `mechanical` populated at import, not just defined.** The page uses it to keep a
132-file bulk renumber out of "activity" (§8.7). A column that exists but is always `0` is
worse than none, because the query looks correct.

### 6.3 Confirmed as sufficient — no change wanted

- `task_event`'s nine before/after axes answer every question in §2. Nothing on this page
  needs an axis that is not there.
- The generated `open_delta` column carries Q1 entirely and is why the level is a sum over
  one indexed integer.
- **`ball` needs no generated delta column.** Its three-way running sum is a `CASE`
  expression over the partial index `ix_event_ball`, measured at 0.46 ms (18.4 ms at 20×) —
  cheap enough that three more stored columns would be paying maintenance for nothing.
- `source` and the project baseline are exactly what §3.6 needs to distinguish unknown from
  zero.
- The `task_state_daily` rollup stays deferred, with the trigger unchanged: a windowed
  series costing more than ~50 ms. Today it is 0.91 ms.

### 6.4 Where each item landed (task-371, 2026-09-08)

This section was written before task-273 was built. By the time it was folded in, task-273
had shipped and task-311 had cut this project over, so the items are code rather than
edits to a draft. Recorded here, and on task-273 and task-311, so a reader of either can
tell which parts of the history contract came from the analytics consumer without reading
task-371.

| item | where it lives | state when task-371 opened |
|---|---|---|
| A — clamp the backfilled creation | `sqlstore/importer.py`, `_prepare` | implemented, **untested** |
| B — restrict the backfill | `sqlstore/backfill.py`, `BACKFILL_FIELDS` | implemented, **untested** |
| C — `reporting_tz` is a zone name | `sqlstore/reporting_tz.py` + migration `002` | a comment on the column |
| D — the invariant asserted | `tests/test_analytics_contract.py` | held after backup/restore only |
| E — `ix_task_counts` | `001_initial.sql` | done |
| F — `ix_event_source` | `001_initial.sql` | done |
| G — deferred `parent_id` | `001_initial.sql` | done |
| H — `mechanical` populated | `sqlstore/importer.py`, `_bulk_renumber_commits` | defined, always `0` |

Three things are worth carrying forward from doing it.

**A and B were right and unasserted, which is the more expensive half of B.** Nothing in
the suite touched the git backfill at all, so the 45% error §4.4 measured could have come
back in a one-word edit to a tuple. It took a mutation to find the shape that actually
reproduces it, and it is not the one this document's prose suggests: backfilling the
*close* of a task the log already closed costs nothing, because `open_delta` only counts a
close whose `lifecycle_from` is not already `closed`. The damage is at creation — git's
first sight of a file that already reads `lifecycle: closed` seeds the synthesised
creation, a creation is `+1` whatever lifecycle it carries, and the task then counts as
open forever.

**H needed no threshold.** §4.4 already defines a bulk renumber as one that rewrites
`queue_position` *with no per-task log entry*, and the de-duplication pass already computes
that: a git observation within five minutes of a native `queue_move` is dropped, so a
surviving row is by construction a position change nobody logged. Grouping the survivors by
commit says how many tasks one commit did it to, and a commit that did it to more than one
is a renumber — the moved task's own change has already dropped out, and what is left is
the collateral. Measured on the 376-record corpus: **213 of 325 surviving position events,
across ten commits**, of which the two largest renumbered **93 and 87** tasks. The
distribution is bimodal with nothing between 5 and 87, so the cut-off decides nothing
delicate.

**The mark stays on `queue_position` and no other axis.** A commit that changes `priority`
on seven tasks looks identical on every signal — bulk, unlogged, one commit — and is a
grooming pass: seven decisions, which is activity. Widening the rule would drop real work
off the chart to catch nothing, so the corpus in `test_analytics_contract.py` carries such
a commit specifically to fail a widened rule.

**What is not covered, and is left deliberately.** `reporting_tz` is validated at
`ensure_project` and by the trigger, and there is no way to *change* it after a cutover
short of SQL: `agentjobs storage import --reporting-tz` is the only setter. This project
was cut over with `America/Chicago`, so nothing here needs correcting today — but a project
cut over without the flag holds `UTC` and has no way back. §7 is what reads the column, so
the setter belongs with the API that makes the value matter rather than with the schema
that holds it.

---

## 7. The API

### 7.1 One endpoint

```
GET /api/projects/{project_id}/analytics?range=90d
```

**One request for the whole page, not one per panel.** The page is a single screen read on
a phone over Tailscale; seventeen round trips would be seventeen chances to render half a
page, and the panels share a range, a timezone and a coverage statement that must be
identical across all of them. The storage cost of the whole set is 6.3 ms (§5.2), so there
is nothing to save by splitting it.

Mounted in `PROJECT_SCOPED_ROUTERS` like every other task-facing router, so it is served at
both `/api/analytics` (default project) and `/api/projects/{id}/analytics`, per
`api/main.py`. Read-only, `GET` only, no authorization beyond what the project scope already
applies.

`range` ∈ `30d | 90d | 12m | all`, defaulting to `90d`. Nothing else is a parameter: an
arbitrary date range is a control nobody on a phone will use, and it can be added later
without changing a shape.

### 7.2 Why the shape matters more than usual

`npm run generate:api-client` regenerates `frontend/src/api/generated/` from `openapi.json`,
and the `api` stage of `scripts/check.py` compares both against the working tree. So the
response models are the contract in a literal sense: renaming a field is a gate failure
until the client is regenerated. Every model below is a named Pydantic class in
`src/agentjobs/api/models.py` — inline `dict`s generate anonymous TypeScript and are not
acceptable here.

### 7.3 The response

*Extended by §21 (task-471): new series, each with its own `SeriesCoverage`;
`ThroughputPoint` loses its cycle fields there. Everything else below is unchanged.*

```python
class AnalyticsRange(BaseModel):
    key: Literal["30d", "90d", "12m", "all"]
    start: datetime          # inclusive, UTC
    end: datetime            # exclusive, UTC
    bucket: Literal["day", "week", "month"]
    timezone: str            # IANA name the buckets were computed in

class AnalyticsCoverage(BaseModel):
    baseline_at: Optional[datetime]      # None => the store makes no claim at all
    baseline_kind: Literal["native", "reconstructed", "backfilled", "unknown"]
    native_from: Optional[datetime]      # first event written by a verb
    reconstructed_before: Optional[datetime]  # draw the hatch left of this
    events: dict[str, int]               # {"native": 1839, "backfilled": 314, ...}
    complete: bool                       # start >= native_from
    note: Optional[str]                  # one sentence, rendered as the caption

class AnalyticsTotals(BaseModel):
    """Identical, field for field, to build_dashboard_snapshot()["stats"]."""
    total: int
    in_progress: int
    blocked: int
    waiting_for_human: int
    awaiting_input: int
    completed: int
    open: int                # total - closed; the number Q1's series ends on

class BacklogPoint(BaseModel):
    day: date
    open_count: int
    opened: int              # arrivals in this bucket
    closed: int              # departures in this bucket
    estimated: bool          # this bucket contains a reconstructed/backfilled event

class HolderPoint(BaseModel):
    day: date
    agent: int
    human: int
    external: int

class ThroughputPoint(BaseModel):
    bucket: date             # first day of the week/month, in the reporting zone
    tasks_completed: int     # COUNT(DISTINCT task_id)
    completion_events: int   # COUNT(*)
    cancelled: int           # closed with any other outcome
    cycle_p50_days: Optional[float]
    cycle_p90_days: Optional[float]
    sample: int              # tasks behind the percentiles; None-able above

class AgeBucket(BaseModel):
    label: Literal["0-6d", "7-29d", "30-89d", "90d+"]
    tasks: int
    mean_age_days: float

class AgingTask(BaseModel):
    task_id: str
    title: str
    priority: str
    ball: Optional[str]
    ball_reason: Optional[str]
    age_days: float

class StuckGroup(BaseModel):
    ball: str
    ball_reason: str
    tasks: int
    mean_days_held: float
    max_days_held: float
    oldest_task_id: str

class AnalyticsResponse(BaseModel):
    range: AnalyticsRange
    coverage: AnalyticsCoverage
    totals: AnalyticsTotals
    backlog: list[BacklogPoint]        # one entry per day in range, gaps filled
    holders: list[HolderPoint]         # same spine
    throughput: list[ThroughputPoint]
    aging: list[AgeBucket]
    oldest: list[AgingTask]            # ten, open and not archived
    stuck: list[StuckGroup]
```

Notes that are decisions, not commentary:

- **`backlog` carries the level and both flows on one point.** Q1 needs all three and they
  share a spine; three parallel arrays would let a client mis-align them.
- **`estimated` is per bucket, not per response.** It is what makes the hatch land on the
  right days rather than on a guessed prefix.
- **Percentiles are `Optional`.** A week with two completions has no meaningful p90, and
  `null` is the honest value. `sample` is beside them so the page can suppress a line drawn
  from three points.
- **`totals` is the dashboard's own stats object.** Same names, same rules, same function
  where possible — the reconciliation test in §5.3 is what keeps it true.

### 7.4 Errors and edges

- Unknown project → the 404 the scoped routers already produce.
- **A project with no events is not an error.** `200`, `totals` populated from `task`,
  every series an empty list, `coverage.baseline_at` `null` and `complete` `false`. §9.1 is
  what the page draws.
- The queue being broken does not affect this endpoint: nothing here reads the order. It
  should not inherit `dashboard.py`'s `QueueCorruptionError` handling, and should not offer
  the repair command.

---

## 8. The page

Route `/p/:projectId/analytics`, rendered inside the existing project shell. **It is not
framed** — `task-294`'s `h-dvh` frame is the Dashboard's alone, and a page of stacked charts
is a document that may legitimately scroll.

### 8.1 Order, and it is the order of §2

*Superseded by §19.4 (task-471), which places the new panels and moves aging and stuck
below the trends.*

Top to bottom, one column on a phone, two on a wide screen. The order is the priority order
of the questions, so the answer to "is this project OK" is above the fold on a phone.

1. **Summary row** — the five counts, each with its change over the range (§8.2).
2. **Backlog** — level plus flows (§8.3). Q1.
3. **Throughput and cycle time** (§8.4). Q2.
4. **Aging** — distribution and the ten oldest (§8.5). Q3.
5. **Stuck** — current holders and the same split over time (§8.6). Q4.
6. **Coverage footer** — one line saying what the page is allowed to claim (§9.3).

### 8.2 The summary row — what the strip should have been

*Amended by §19.1 (task-471): the delta baseline is `max(range.start, native_from)`, the
tile names the date, and the default range is `30d`. The suppression rule below fires on
`native_from`, not `baseline_at`.*

The five counts the Dashboard shed, **each with a delta against the start of the range**:
*"125 open ▲ 9 in 90 days"*. The delta is the entire reason this page exists; a count
without one is the strip again.

- The arrow's direction is stated in words as well as glyph and colour, because red-up is
  bad for backlog and good for completed, and colour alone cannot carry that.
- Where the range starts before `coverage.baseline_at`, the delta is suppressed and replaced
  by *"no comparison — history begins <date>"*. It is not rendered as ▲125.
- Tapping a count filters the Tasks surface, which already has the filters for all five.

### 8.3 Backlog (Q1)

**One chart, two layers.** A filled area for the open-task level, and beneath it a paired
bar per bucket — arrivals up, completions down from a zero line. The level answers "is it
growing", the bars answer "why".

- The y-axis for the level starts at zero. A truncated axis makes a 3% change look like a
  cliff, and this is the number the project is judged by.
- Buckets follow the range: day for 30d and 90d, week for 12m, week for all.
- **A hatched region left of `coverage.reconstructed_before`**, with the rule and caption
  from §3.6.
- Readout line above the chart, always present: *"6 Sep · 125 open · 2 opened · 1
  completed"*. On a phone that line is how values are read, because there is no hover
  (§10.4).

### 8.4 Throughput and cycle time (Q2)

*Superseded by §19.2 (task-471). Cycle time as `created_at` → `closed_at` measured queue
wait, not pace (§16.1); throughput becomes its own chart at the spine grain (T1) and
where-the-time-goes (S1) takes cycle time's place. The two-axis argument below is
withdrawn.*

Bars for `tasks_completed` per bucket, with a line for `cycle_p50_days` on a second axis and
a lighter band to `cycle_p90_days`.

- Two axes on one chart is normally a mistake; it is right here because the question is
  explicitly about the two together, and each series is labelled at its own axis rather than
  by colour alone.
- `completion_events` appears in the readout only when it differs from `tasks_completed`
  (§3.3), as *"3 completed (4 completion events — one task was reopened)"*.
- `cancelled` is a separate, muted bar segment. Closed-not-completed is not throughput, and
  hiding it entirely would make a month of cancellations look like a quiet month.
- Percentile line suppressed for buckets with `sample < 3`, with the gap left visible rather
  than interpolated.

### 8.5 Aging (Q3)

Two panels side by side on a wide screen, stacked on a phone.

- **Distribution**: four horizontal bars, `0-6d / 7-29d / 30-89d / 90d+`, labelled with
  counts. Horizontal because the labels are words and a vertical axis would rotate them.
- **The ten oldest open tasks**: id, title, age, current holder — each a link to the task.
  This panel is the answer to Q3; the distribution is context for it.
- Both exclude archived tasks and say so (§3.4).

### 8.6 Stuck (Q4)

*Amended by §19.3 (task-471): rows are ordered by who is being waited on — waiting on
you, blocked, with an agent, then the queue, named as the queue — not by count.*

- **Now**: one row per `(ball, ball_reason)` group with a count, the mean days held and the
  worst case, ordered by count. The ball vocabulary is already the app's language, so the
  rows read *"29 waiting on you — spec · longest 34 days"*.
- **Over time**: a stacked area of `agent` / `human` / `external`, from `holders`. A rising
  `human` band is the single most actionable thing on the page and it is invisible in any
  snapshot.
- The three bands are distinguished by fill pattern as well as colour.

### 8.7 Deferred panels

Designed, not built, each with what would trigger it:

- **Backlog by priority band** (measured, 0.53 ms). Trigger: the level moving without the
  bands being obviously the cause.
- **Dispatch activity** — runs per day and agent-hours from `task_run` (0.11 ms). Trigger:
  a question about machine time. Excluded today because the parent forbids per-actor
  analytics and this is one question away from it. *Triggered on 2026-09-19: built as
  R-1 to R-6 in §18.5, per project and never per runner (§16).*
- **The board as it stood on a date** (0.67 ms). Trigger: wanting to explain a step in the
  level rather than see it.
- **Reopenings** (0.32 ms, 2 in this corpus). Trigger: more than a handful. *Now a marker
  on the throughput chart (T1) rather than a panel.*

---

## 9. Empty, thin, and unknown

The parent's constraint: *a two-day-old project must not render as a broken chart*. Three
distinct states, and they must not be conflated — "nothing has happened" and "we do not know
what happened" are different sentences.

### 9.1 No history at all

`coverage.baseline_at` is null and every series is empty — a project initialised today.

The page renders the **summary row with counts and no deltas**, and replaces the four chart
panels with a single bordered block: *"No history yet. Charts appear once this project has
a day of activity."* Not an empty axis, not a flat line at zero, and not a spinner. An empty
chart frame reads as a failure; a sentence reads as a state.

### 9.2 Thin history

Fewer than **14 days** between `coverage.baseline_at` and now.

- Charts render, with the axis running from the baseline rather than from the range start.
  The range control still offers 90d; choosing it shows nine days of data on a nine-day
  axis, not nine days marooned in ninety.
- **Trends are suppressed, values are not.** The summary deltas, the cycle-time percentile
  line and any "faster/slower" wording are hidden below 14 days; the bars and the level are
  drawn, because they are facts.
- A caption on the range control: *"9 days of history"*.

The threshold is a named constant, documented where it lives, so the next person changing it
knows what it is for. Fourteen days is two weekly cycles, which is the shortest span where
"is it growing" is a question rather than noise.

### 9.3 Unknown history

The range reaches back before `coverage.native_from`, which is the normal case for this
project for the next several months.

- The hatch and the boundary rule (§3.6).
- The coverage footer, one line, always present: *"History from 26 Oct 2025. Events before
  6 Aug 2026 were reconstructed from the task records' git history — timestamps in that span
  are commit times, and priority changes before 15 Aug 2026 are not recorded."*
- Where `coverage.complete` is false, the summary deltas say *"since 26 Oct 2025"* rather
  than *"in 90 days"*, so the number is never attributed to a window it does not cover.

**Never** substitute zero for unknown, anywhere, including the axis minimum and including a
`BacklogPoint` the API could have omitted.

---

## 10. Drawing the charts

### 10.1 Inline SVG, no charting dependency

**Decision: the charts are inline SVG with path data computed by pure functions in the
frontend. No charting library is added.**

The parent's constraint is that whatever is proposed *"has to survive the existing gate —
Vitest in jsdom and a Playwright path"*. That was measured against this repository's own
jsdom (30.0.1) rather than assumed:

| capability | jsdom 30.0.1, measured 2026-09-06 |
|---|---|
| `getBoundingClientRect()` | returns all zeros |
| `offsetWidth` / `clientWidth` | `0` |
| `ResizeObserver` | **undefined** |
| `SVGSVGElement.prototype.getBBox` | **undefined** |
| `canvas.getContext('2d')` | returns `null` — *"Not implemented … without installing the canvas npm package"* |

What follows is not a preference:

- **Anything that measures its container renders nothing in the `vitest` stage.** Recharts'
  `ResponsiveContainer`, visx's parent-size hook and Chart.js all size themselves from the
  DOM, and every one of those inputs is zero or absent. They need shims, and a shim that
  makes a chart "work" in a test is exactly the trap ENGINEERING.md names: *do not set up
  the state your test is meant to be checking.*
- **Canvas charting cannot render in this gate at all** without adding the `canvas` npm
  package — a native build, in a repository whose bootstrap is currently 30 seconds.
- **Inline SVG is fully assertable.** The scale and path functions are pure — `(data,
  width, height) => string` — so vitest tests them with no DOM whatsoever, and the rendered
  `d`, `x`, `y` and `aria-label` attributes are strings a component test can read out of
  jsdom without layout. Playwright then checks the parts that need a real browser: that the
  chart is inside the viewport at 390px, that nothing scrolls horizontally, and that the
  readout updates on tap.

The cost is honest and should be stated: axes, ticks, stacking and a readout are perhaps
300–400 lines of code that a library would supply. In exchange the gate stays as it is, the
bundle does not grow, and there is no second layout system to reason about. The trade is
worth taking for **four chart shapes** — area, paired bars, horizontal bars, stacked area.
It would not be worth taking for twenty.

**Reopen trigger:** a genuine need for interactive zoom, brushing, or a chart type outside
that set. At that point the choice is re-made with a measurement, not this argument.

### 10.2 Responsive without measuring

The frontend has no `ResizeObserver` in tests and the page must work from 320px to 1600px.
So charts do not measure themselves:

- Each chart is an `<svg viewBox="0 0 W H">` with `width: 100%` and a height from a Tailwind
  breakpoint class — 160px on a phone, 220px from `sm`, 260px from `lg`. The viewBox
  coordinate system is fixed, so all path maths is width-independent and testable.
- Tick density is chosen from the **bucket count**, which is data, not from pixels: at most
  6 x-labels, chosen by taking every ⌈n/6⌉th bucket.
- Text inside the SVG is a fixed viewBox size; because the viewBox scales, small screens get
  proportionally larger text, which is the right direction.

### 10.3 Colour is never the only channel

Dark theme, using the tokens already in `styles.css` (`--color-dark-bg`, `-surface`,
`-border`, `-text`, `-muted`). Series are distinguished by **fill pattern plus label plus
colour**, never colour alone: the stacked holder chart uses solid / 45° hatch / dotted, and
every series carries an inline label at its right edge rather than only a legend.

### 10.4 Touch is the primary input

This is read on a phone as often as on a desktop, so:

- **No hover-only information.** Every value a reader needs is either printed on the chart
  or in the readout line above it.
- Tapping the plot area selects the nearest bucket and updates the readout. The hit target
  is the full column height, not the data point.
- Charts never scroll horizontally. A range with too many buckets **aggregates** — day to
  week — rather than growing wider. This is what keeps the page's `overflow-x` clean, which
  ENGINEERING.md's responsive rule requires.
- Every chart carries an `aria-label` naming what it shows and its headline value, and a
  visually-hidden table of the underlying series follows it. The table is also how a
  component test reads values without parsing path data.

---

## 11. The entry point

Task-294 removed the strip and created **no route, no link and no placeholder**, leaving the
entry point entirely to this design. The parent's constraint: discoverable but small, and
reclaiming vertical space is the point, so a banner is a failure.

**Decision (2026-09-18, task-465, reversing task-374): Analytics is a `PrimaryNav`
destination, between Tasks and Create.** The Dashboard heading row carries only
`View all N →`, as it did before task-374.

**What was decided first, and why it was reversed.** Task-374 shipped this as one text
link, `Analytics →`, on the Dashboard's "Active tasks" heading row beside `View all N →`.
The argument was that the row already existed and already carried a right-aligned link, so
the entry point cost zero vertical pixels on a page task-294 had just fitted to one screen;
a nav destination was rejected because `NAV_INLINE_MIN_PX` is measured from the row's
contents and another entry drops a band of desktop widths into the burger. The owner
reviewed the merged result the same day and could not find the page. That is the whole
finding: a small text link in a heading row is not an entry point for a surface, and
discoverability outranks the burger band. The rejected alternative became the decision.

**What it cost, measured.** Seven destinations plus API Docs, with the switcher pinned to
its 224px maximum and the attention badge showing, did not fit inside the header's
`max-w-7xl` at the old 24px spacing at any viewport: 17px short, with `API Docs` wrapping
to two lines at 1280. So the inline row's gap went from 24px to 16px, and
`NAV_INLINE_MIN_PX` moved from 1140 to **1220** — the bar last overflows at 1208 with the
tighter gap. The 1140–1219 band now gets the burger. The measurement method and the
numbers are in the constant's docstring in `PrimaryNav.tsx`.

**It moved again, into the actions menu.** Task-345 thinned the bar to navigation only,
sending Dispatch settings, Playbooks and API Docs to the actions menu (task-168). It
proposed keeping Analytics in the bar — a reading surface is somewhere you navigate to
in a way a settings page is not — and the owner, looking at the built bar on 2026-09-20,
moved it into the menu with the rest. So the entry point is now a row under the kebab at
the top right, present at every width and two interactions from any page.

**That is not a return to what this section reversed, and the distinction is the whole
point.** Task-374's placement failed because the link lived *inside another page*, in a
heading row of the Dashboard, where a reader had no reason to look for it. A menu that
is in the global header on every screen is somewhere a reader does look. What this
section established still holds: Analytics needs an entry point of its own at the app's
top level. Which of the two top-level containers holds it — the row or the menu — was
never this section's claim.

The row it left is now four entries, and `NAV_INLINE_MIN_PX` fell from 1256 to **854**.

**Still rejected: an icon.** An unlabelled glyph for a page nobody has seen before is a
guess, and the nav's other entries are words.

**Mobile treatment.** Below the breakpoint the destination is in the burger panel with
the others, one tap away. Nothing on the Dashboard changes at any width, so the
one-screen spec is unaffected.

---

## 12. Rejected alternatives

**Read the git history on each page request.** Explicitly forbidden by the parent, and
`git log -p` over the task directory costs 0.4 s plus 0.16 s of parsing (§4.5) — which is
not catastrophic, and is still the wrong shape: it makes the page depend on a checkout, on
a branch, and on task files continuing to exist after task-311 retires them.

**Compute the history from the log at query time instead of storing events.** The obvious
design, and the one the corpus superficially supports. Rejected on task-273's measurement
and this one: replay alone gets priority wrong for 337 of 367 tasks, and even *with* the git
backfill 41 tasks need a reconciliation row. Replaying per request would also pay the whole
427 ms parse on every load.

**Store the local day on each event.** It would make the naive `GROUP BY` correct and fast.
Rejected because it freezes the reporting timezone into the data: changing it later would
require rewriting every row, and the whole store is one database across projects (task-273's
first fork) which may not share a zone.

**A `task_state_daily` rollup now.** Designed and deferred by task-273; nothing here changes
that. The trigger is a windowed series exceeding ~50 ms and it is currently 0.91 ms.
Building it now would add a second thing that can disagree with the events, to save 49 ms
nobody is waiting on.

**One endpoint per panel.** Rejected in §7.1: the panels share a range, a timezone and a
coverage statement, and splitting them creates the possibility of a page whose panels
disagree about what history exists.

**A charting library.** §10.1, on measured jsdom behaviour rather than on taste.

**Keeping the five counts on the Dashboard as a collapsed line.** Task-294 already rejected
this and the reasoning holds: the three counts a collapsed line would carry are the three
nobody acts on.

---

## 13. Implementation children

*Pass two's children — task-471 to task-474 — are in §23.*

Four children under task-212, sequenced by `dependencies[]`. Each cites its sections here
rather than restating them.

| Task | Title | `needs` | Sections | Priority |
|---|---|---|---|---|
| **task-371** | Fold the analytics history findings into the SQLite contract | — | §6 | critical |
| **task-372** | The analytics API: one endpoint that answers the four questions | task-371 | §3, §5, §7 | high |
| **task-373** | The analytics page and its charts | task-372 | §8, §9, §10 | high |
| **task-374** | The Dashboard's entry point to analytics | task-373 | §11 | high |

**task-371** is small and belongs to the store rather than to this page, but it is a child
here because this design is what discovered every item in it. It carries no `needs` edge
onto task-273's implementation — adding a column or an index is a migration, not a
redesign — which is the shape the owner chose for the task-213/task-273 relationship
generally. It is **critical** while the other three are **high**, and that is deliberate:
items A and B in §6.1 are import rules, and an import that has already run without them
produces a store whose backlog chart is wrong in a way nobody can see, so it has to reach
task-273 and task-311 before they run. The other three cannot start until the storage
exists, and parking three unstartable tasks at the head of the critical band would push
real work down.

**task-372** cannot be finished before task-273's storage exists, and should say so rather
than inventing a shim: the endpoint's whole value is that it does not parse task documents.
Its acceptance includes the reconciliation test in §5.3 and the invariant test in §6.1 D.

**task-373** is the largest and carries §10's chart primitives. It builds the four shapes as
pure functions with their own unit tests **before** any component, because that is the half
jsdom can actually verify.

**task-374** is one link, and is last because a link to a page that does not exist is the
one thing task-294 explicitly refused to leave behind.

---

## 14. Decisions

Each of these is binding on the children above; each has its rejected alternative recorded
in the section named.

1. **Open means `lifecycle <> 'closed'`**, drafts included; the level is a running sum of
   `open_delta`, and reopening is handled by construction. §3.2.
2. **Completed means `outcome == 'completed'`**, not closed; unique tasks and completion
   events are both returned and only the first is plotted. §3.3.
3. **Archived tasks count in the level and are excluded from the attention panels**, which
   say so. §3.4.
4. **Day buckets are computed in the API with `zoneinfo`**, from an IANA zone on the
   project; SQL never names a day. Measured: correct across DST at 0.91 ms, against a
   correct-but-58.8 ms SQL alternative and a fast-but-wrong one. §3.5, §5.5.
5. **The API fills the calendar spine.** 8% of days carry an event; a chart drawn from
   grouped rows alone would misrepresent the other 92%. §3.5.
6. **Unknown history is never rendered as zero.** Series start at the coverage baseline,
   reconstructed spans are hatched, and the page states its own limits in one line. §3.6,
   §9.3.
7. **The git backfill is authoritative only for axes the log does not record**, and a
   backfilled creation instant is clamped to the earliest evidence. Both were found by
   measurement — the first as a 45% error in the level, the second as a negative backlog.
   §4.3, §4.4.
8. **One endpoint for the whole page.** §7.1.
9. **`AnalyticsTotals` is the dashboard's stats object**, kept identical by a test. §5.3,
   §7.3.
10. **Inline SVG, no charting dependency**, on measured jsdom behaviour. §10.1.
11. **The entry point is a text link on an existing Dashboard heading row**, not a nav
    destination and not an icon. §11. *Reversed 2026-09-18 by task-465: Analytics is a
    `PrimaryNav` destination; §11 records why.*

*Continued in §22 (task-471), decisions 12 to 24.*

---

## 15. Relationship to other work

- **task-212** — the parent. This design completes its "design" step and creates its
  implementation children. Its sc-1 was satisfied by task-294 removing the strip; its sc-2
  through sc-6 are carried by children 2–4.
- **task-273** — the storage backend. §6 is the coordination its constraints require, and
  the owner's fourth fork chose *"run task-213 next, then build"* without a blocking edge.
  Everything in §6.1 should be folded in before its implementation starts.
- **task-311** — the migration. Items A, B and H in §6 are import rules and belong to it,
  as does the sequencing constraint in §4.5: the git backfill must run and be verified
  **before** the task files are retired.
- **task-294** — removed the strip, bounded the Dashboard to one viewport, and left the
  entry point to this design. §11 must not undo its work; its
  `frontend/e2e/dashboard-one-screen.spec.ts` is the check.
- **docs/task-selection-design.md** — the model for this document's shape, and the source of
  the queue semantics §6.2 H refers to.
- **docs/performance.md** — the measurement tooling. The endpoint figures in §5.1 use the
  `X-Task-Parses` instrumentation documented there.

---

# Pass two — the metric catalogue, and where each number comes from

**Status: proposed 2026-09-19, task-471, the design child of the second round under
task-212.** §1 to §15 above are the first pass and stand except where a section says it
is superseded here. Everything from §16 down was measured on this machine on
**2026-09-19** against a copy of the live `agentjobs` store (476 tasks, 2,522 events,
259 runs) and the 212 finish records under the home directory's `.agentjobs/finishes`.
The probes are throwaway and live in a job scratch directory; nothing from them is
committed.

**Binding on every section below: no series is split by runner.** The owner decided on
2026-09-19, in answer to the supervisor's question on task-212, that run and finish
metrics are not split by runner (Claude versus Codex). Every series here is per project,
and the parent's out-of-scope line on per-actor scoring stands. A runner dimension stored
and hidden behind a toggle was the rejected alternative: it is per-actor scoring with an
extra click.

---

## 16. Why the first page is thin

The owner's ask, paraphrased from task-212 on 2026-09-19: everything one might want to
know as a trend, to see whether the process is getting better or worse — how long a task
takes, how long a finish takes, and so on. The throughput chart made no sense to him; the
backlog chart did something useful.

The first pass answered four questions chosen before anybody had seen a chart. Looking at
the shipped page against the store it reads, five things are wrong, and four of them are
about the data rather than the drawing.

**16.1 Cycle time measures the wrong thing.** §8.4's cycle time is `created_at` →
`closed_at`. On a backlog worked from a queue, that is mostly how long a task sat
*unclaimed*, which is a prioritisation decision and not how long work takes. Measured on
the 78 tasks completed since the SQLite cutover on 7 Sep: creation to first claim is
**p50 2.2 h, p90 281 h**; first claim to close is **p50 0.7 h**. The median-cycle line
swung between 0.2 and 13.5 days from week to week depending on which old tasks got
picked up, and nothing on the chart said so. That line is the reason the throughput
panel reads as nonsense: it was never about pace.

**16.2 Throughput is two charts forced into one.** Completed bars on an axis that one
116-task week stretched to 200, sharing the plot with a p50 line on a second axis and a
p50–p90 band that renders as a blob. §8.4 argued two axes were right "because the question
is explicitly about the two together"; with 16.1 the two are not even about the same
thing.

**16.3 The summary deltas compare against a date before the project existed.** *"149
open, 145 more since 22 Jun"* is the total restated. On the 90d default the range start
lies inside the reconstructed span, and §8.2's suppression rule fires only when the range
reaches back past `coverage.baseline_at` — which is the *backfilled* baseline of
2025-10-25, not the first native event of 2026-09-07. Every tile did this.

**16.4 "Stuck" is mostly the queue.** The first row of §8.6 read *113 tasks with an
agent, available, longest 33 days*. Ready-and-unclaimed is not stuck; it is the backlog
waiting its turn. Listing it first buried the rows that are stuck.

**16.5 Nothing on the page is about the machine.** No runs, no finishes, no gates, no
review latency, no questions, no usage-limit pauses. Those are the numbers that say
whether the *process* is improving, because task counts move with how much was filed.

### 16.1 The questions, extended

§2's four questions stay. Four more justify the panels added below, and each entry in the
catalogue (§18) names the question it serves.

**Q5. Where does a task's time go?** Not "how long does a task take" — that number is
dominated by queue wait — but which *segment* of its life is growing: waiting to be
claimed, being worked, waiting for review, being finished.

**Q6. Is the machine keeping up?** Runs per day and the hours they consume, how they end,
how many it takes to complete a task, how long a finish and its gate take, and how much
of the week was lost to usage limits.

**Q7. Is review the bottleneck?** How long work sits in review, how often it is approved
first time, and what is sitting there now.

**Q8. Are questions getting answered?** How many are open, and how long an answer takes.

### 16.2 What is recorded, and since when

Every series below states its coverage the way §3.6 does, and the boundaries differ by
source. Measured on 2026-09-19:

| source | rows | native from | what came before |
|---|---|---|---|
| `task_event` | 2,522 (472 native, 1,604 reconstructed, 446 backfilled) | **2026-09-07T19:06Z** | replayed from the log (handoff, claim and close rows carry the log entry's own timestamp) and backfilled from git (priority, parent, position only — §4.4) |
| `task_run` | 259 | 2026-09-07T19:06Z | **171 rows imported at the cutover carry the import instant as `started_at`** (§20.5) |
| finish records on disk | 212 directories | 2026-08-23 (the scripted finish shipped, task-241) | nothing — before that a person merged by hand and no record was written |
| `execution.db` · `run_attempt` | 53 | 2026-09-13 (durable execution) | `task_run` |
| `execution.db` · `dispatch_queue` | 2 | 2026-09-18 (the machine queue) | nothing — a refused dispatch was refused, not queued |
| `execution.db` · `auth_incident` / `auth_waiter` | 4 / 9 | 2026-09-18 (task-463) | nothing |
| `log_entry` questions | 80, 53 with a threaded answer | whole history | the log is the record; nothing is reconstructed |

Two consequences shape everything below. **Native task history is twelve days old**, so
every "in 90 days" wording on the page is currently a claim about reconstructed rows and
the tiles have to say so. And **the finish records predate native events by a fortnight**,
so once imported they are the longest exact series the page has.

---

## 17. The lifecycle segments, defined exactly

### 17.1 One rule: dwell time by ball holder

The ball is the schema's answer to *who acts next* (§2, Q4), and every instant between a
task's creation and its close is on exactly one holder. So the segments are **the time
the ball spent with each holder**, grouped into five names, and they partition a task's
open life exactly by construction — there is no residual and nothing to reconcile.

| segment | holder | reads |
|---|---|---|
| **queue** | `agent` / `available` — ready and unclaimed, whether before the first claim or after a release | |
| **work** | `agent` with any other reason — `work`, `revise`, `answer`, `redirect`, `hold` | |
| **review** | `human` / `review` | |
| **waiting** | `human` with any other reason — `spec`, `decision`, `input`, `approval` — and `external` / `dependency`, `external` / `service` | |
| **finish** | carved out of *work*: the span from the last approval to the close (§17.3) | |

Every definition reads the same columns of `task_event`, and nothing else: `task_id`,
`ts`, `kind`, `source`, `ball_from`, `ball_reason_from`, `ball_to`, `ball_reason_to`,
`lifecycle_from`, `lifecycle_to`. A row is a segment boundary when the holder changes —
`ball_to IS NOT ball_from OR ball_reason_to IS NOT ball_reason_from` — or when `kind` is
`create`, `claim`, `close`, `reopen` or `import`. `create` starts the clock with the ball
the task was born holding; `close` stops it with `ball_to` null.

**Total** is the sum of the five, which is `created_at` → `closed_at` for a task that was
never reopened and the sum of its open intervals otherwise (§17.4).

This is the definition because it is the only one whose parts add up. The alternatives
each fail a reader in a way the chart cannot show:

- *work = first claim → first review handoff* (the first-pass phrasing) leaves a task's
  second review round, its wait on a decision and its finish all unaccounted for, and a
  task with no review handoff has no end to its work at all.
- *work = total − queue − review − finish* (a residual) credits the agent with the day
  the task sat on `human/input` waiting for an answer. The owner asked for the work
  number specifically; a residual is not it.
- *four segments, folding waits into work or review* mis-states whichever one absorbs
  them. **Five is the smallest number that keeps every holder honest**, and *waiting* is
  the segment that tells the owner the process is blocked on him somewhere other than
  review.

### 17.2 Approval, defined

**An approval is an event whose ball leaves `human`/`review` for `agent`/`work`, or a
`close` whose ball was `human`/`review`.** Measured on this corpus, the ball leaves review
for exactly five places, and only two of them mean *yes*:

| `ball_to` / `ball_reason_to` | rows (native + reconstructed) | meaning |
|---|---|---|
| `agent` / `work` | 124 | **approval** — the Approve button, which then starts a finish |
| *close* (`ball_to` null) | 16 | **approval** — approve-and-close in one act |
| `agent` / `revise` | 28 | a second round: the work needs changing |
| `agent` / `answer` | 7 | a question back to the agent |
| `agent` / `redirect` | 1 | the ask changed |

A handful of rows move the ball to another `human` reason, release it to the queue, or
are an edit that happened to move it; none of those is an approval and none is a round
trip.

The actor column is deliberately **not** in the definition. A human is who normally
approves, but the event is what the finisher acts on, and an approval routed through an
API write on the owner's behalf is still an approval. What the definition does read is
the *destination*: leaving review for `revise` or `answer` is a round trip, not an
approval, and it is what makes the first-time approval rate (§18, R3) a real number.

### 17.3 The finish segment

**For a task with at least one approval, finish is the span from the last approval to the
close**, and it is subtracted from *work* (the holder after an approval is `agent`/`work`
until the finisher closes). Measured: 22 of the 78 completed tasks have one, **p50 5.4
min**, which is the scripted finish's own duration plus the seconds around it.

**For a task with no review handoff — an autonomous merge, 51 of the 78 — there is no
approval event, and `task_event` alone cannot say when the finish began.** The finisher
closes the task and the row before it is the claim. So:

- **From `task_event` alone, such a task's finish is zero and its finish time is inside
  *work*.** That is a true statement about what the store records today, and the segment
  is reported that way with the task counted in the readout: *"51 of 78 merged without a
  review; their finish time is inside work"*.
- **Once the `finish` table exists (§20), the finish segment of an unreviewed task is the
  span of the finish row that closed it** — the row with this `task_id`, `merged = 1`,
  whose `finished_at` is within a minute of the close — subtracted from *work* the same
  way. Coverage for that refinement is the finish table's, from 2026-08-23, which is older
  than native events, so nothing in the native span is left estimated by it.

A task closed by hand — actor other than the finisher, 11 of the 78 since the cutover —
has no finish row and no finish segment, which is correct: no finish happened.

### 17.4 The edge cases, decided

Each of these was found in the corpus rather than imagined, and each is a test case for
the API child.

**No review handoff.** §17.3. Review is zero, not null: the task genuinely waited no time
for review. It is a sample in every segment's percentile, and the readout says how many
of the bucket's tasks it describes.

**A second review round-trip.** Review is the **sum of every visit** to `human`/`review`
(the corpus holds tasks with 2, 3 and 6 rounds). Time on `agent`/`revise` or
`agent`/`answer` between visits is *work*. `review_rounds` — the number of entries into
`human`/`review` — is counted per task and drives R3; it is not a segment.

**A reopen.** The interval between a `close` and its `reopen` is on no holder and counts
in **no segment and not in total**: a task reopened a week later was not being worked for
that week. Total is the sum of its open intervals. The task is one sample with one total,
attributed to the bucket of its *last* close, and the throughput chart marks it (T1).
Three reopens exist in the corpus.

**Claimed more than once.** A second `claim` after a `release` starts a second *queue* →
*work* transition and needs no rule: the ball moved through `agent`/`available` in between
and the dwell rule already counts it. A second `claim` with the ball already on
`agent`/`work` (task-393: reopen, handoff to `agent`/`work`, then claim) moves nothing and
contributes nothing. Two tasks in the corpus have two claims.

**A `release`.** Ball to `agent`/`available`; queue time resumes. Nothing special.

**Draft time.** A draft's ball is `human`/`spec` or `human`/`input`, so drafting is
*waiting*, not *queue*: the task is waiting on a person to say what it is. Queue begins at
promotion, when the ball first lands on `agent`/`available`.

**Reconstructed rows.** The segments read every row regardless of `source`, for two
reasons. The git backfill is restricted to priority, parent, position and archived
(§4.4), so **no backfilled row is ever a segment boundary**. And a reconstructed
`claim`, `handoff` or `close` carries the log entry's own timestamp, which was written at
the moment of the change — it is exact in practice and *reconstructed* only in
provenance. A task is `estimated` if any of its boundary rows is non-native, and a bucket
is `estimated` if any task in it is, so the hatch still lands honestly; 27 of the 78
completed tasks are.

**The one reconstructed row that is not exact is the `import` reconciliation row** —
57 in this corpus, 22 of them carrying `lifecycle_to = 'closed'` at the *import* instant.
**A task whose last close is an `import` row is excluded from every segment sample**,
because its close time is unknown; it still counts in throughput. Twenty completed tasks
are excluded this way, the latest closed on 2026-08-13, none inside the native span.

**A creation with no ball.** 118 reconstructed `create` rows carry no `ball_to` — the
log entry they were replayed from predates the ball. Total starts at the creation; the
segment is taken as *queue* when `lifecycle_to` is `ready`, `active` or null and
*waiting* when it is `draft`, which is what 328 of the 336 creations that do carry a ball
say, and the task is `estimated`. Every such row is pre-cutover.

**Cancelled and superseded tasks** are not in the segment sample — the question is where
a *completed* task's time went — but their count is on the throughput chart (T1).

**A task still open** contributes nothing to the closed-in-bucket series; its current
dwell is what the stuck panel (§19.3) and the in-review list (R1) show.

### 17.5 The invariant, and what the corpus says

`queue + work + waiting + review + finish = total` for every task, to the second. Measured
on all 78 completed-since-cutover tasks: **zero tasks disagree and no segment is
negative.** The API child asserts this on every task it emits, the way §3.2's invariant is
asserted, because it is the one check that catches a boundary rule quietly changed.

What the segments say today, in hours, over the 78 tasks — an illustration of the shape,
not a number to quote:

| segment | p50 | p90 | tasks with any |
|---|---|---|---|
| queue | 2.18 | 281 | 77 |
| work | 0.65 | 2.4 | 78 |
| waiting | 0 | 2.0 | 30 |
| review | 0 | 0.5 | 27 |
| finish | 0 | 0.12 | 22 |
| **total** | **8.4** | **300** | 78 |

The p50 of *review* and *finish* is zero because most tasks in the span merged
autonomously, which is a fact about the fortnight and precisely what a stack of medians
should show. It is also why the readout carries, per segment, the median *among the tasks
that have it* and their count (§19.2).

### 17.6 The query and its plan

One query serves the whole panel: every boundary row of every task closed in the window,
driven from the task table so the history side is an indexed lookup per task.

```sql
SELECT e.task_id, e.ts, e.kind, e.actor, e.source,
       e.lifecycle_from, e.lifecycle_to,
       e.ball_from, e.ball_reason_from, e.ball_to, e.ball_reason_to
  FROM task AS t
  JOIN task_event AS e ON e.project_id = t.project_id AND e.task_id = t.task_id
 WHERE t.project_id = :p AND t.closed_at IS NOT NULL
   AND t.closed_at >= :from AND t.closed_at < :to
   AND (e.kind IN ('create', 'claim', 'close', 'reopen', 'import')
        OR e.ball_to IS NOT e.ball_from OR e.ball_reason_to IS NOT e.ball_reason_from)
 ORDER BY e.task_id, e.ts;
```

```
SEARCH t USING INDEX ix_task_closed_at (project_id=? AND closed_at>? AND closed_at<?)
SEARCH e USING INDEX ix_event_task_ts (project_id=? AND task_id=?)
USE TEMP B-TREE FOR ORDER BY
```

**1.07 ms for 455 rows** over the cutover fortnight; **3.4 ms for 1,521 rows** over the
whole history. The fold into segments is Python, like the day bucketing in §3.5, and for
the same reason: the rules in §17.4 are a state machine over a task's rows, and SQL window
functions were measured at 4.2 ms with a full scan for the simpler review-pairing case
(§18, R2) against 0.13 ms for two indexed range reads.

---

## 18. The metric catalogue

One entry per metric. Each states the **question** it serves (§2, §16.1), the
**definition**, the **source** table and columns, the **bucket**, the **statistic**, the
**chart shape** — one of §10.1's four unless argued — and the **coverage** rule: what the
series reads as before its source existed. Where a number is quoted it was measured on
2026-09-19 and is an illustration.

The owner's note on task-212 named five things he wants; each maps to an entry by name:

| owner's item | catalogue entry |
|---|---|
| task total time | **S2** total time, and the stack **S1** it is the height of |
| dispatch to review handoff | **S3** time to first review |
| time in full gates per task | **G4** gate minutes per completed task |
| completed per day | **T1** throughput, at day grain on 30d and 90d (§19.2) |
| added | **B1** backlog, the arrivals bar and the readout's *added this week* |
| open | **B1** backlog, the level |

### 18.1 Where the time goes (Q5)

**S1 — Where the time goes.**
*Question.* Which part of a task's life is growing.
*Definition.* §17: per completed task, hours in queue, work, waiting, review and finish.
*Source.* `task_event`, the columns in §17.1; the `finish` table for unreviewed tasks
once it exists (§17.3).
*Bucket.* Week, by the task's last close, in the reporting zone. Week on every range: a
day rarely closes three tasks, and the percentile needs a sample (§8.4's
`PERCENTILE_MIN_SAMPLE`, kept).
*Statistic.* p50 per segment over the bucket's completed tasks, **p90 on tap**, `sample`
per bucket, and per segment the count of tasks with a nonzero value and the p50 among
them.
*Shape.* **Stacked bars** — the `stackedBars` primitive that already draws cancellations
on throughput. Stacked *area* was rejected: weekly buckets are discrete and an area
implies a level between them. The stack's height is the sum of five medians, which is
not the median total; S2 supplies that in the readout and the caption says so once.
*Coverage.* Tasks whose boundary rows are all native are exact; a bucket holding any
reconstructed task is `estimated` and hatched. The series is drawn over the whole range
because reconstructed handoffs carry their log timestamps (§17.4). Tasks whose close is an
`import` row are excluded and the readout counts them.

**S2 — Total time per completed task.**
*Question.* How long a task takes, all in.
*Definition.* §17.1's total: the sum of the task's open intervals.
*Source, bucket, coverage.* As S1.
*Statistic.* p50 and p90.
*Shape.* Not drawn; in S1's readout as *"total p50 8.4 h · p90 300 h · 22 tasks"*. This
is the first pass's cycle time, correctly attributed, and it is deliberately not a line on
its own: the swing the owner objected to is real and lives in *queue*, where S1 shows it.

**S3 — Time to first review.**
*Question.* The owner's *dispatch to review handoff*: how long from an agent picking a task
up to that agent saying it is done.
*Definition.* First `claim` → first entry into `human`/`review` (`ball_to = 'human' AND
ball_reason_to = 'review'`), per task that has both. **Not the same as the work segment**:
this includes any wait on a decision or an answer in between, because the owner's
question is about elapsed time from dispatch to handoff.
*Source.* `task_event`: `kind`, `ts`, `ball_to`, `ball_reason_to`.
*Bucket.* Week, by the review entry.
*Statistic.* p50, p90, sample.
*Shape.* A line with p90 on tap, on the S1 panel's readout rather than its own chart:
one number per week and the same axis as the stack.
*Coverage.* As S1. A task with no review handoff has no value and is not in the sample.

**S4 — Runs per completed task.**
*Question.* How many dispatches it takes to finish something — retries, parks, resumes.
*Definition.* `COUNT(task_run.run_id)` per task completed in the bucket, including zero
for a task nobody dispatched (6 of 78 today: a decision recorded by hand).
*Source.* `task` (`closed_at`, `outcome`) left-joined to `task_run` (`task_id`).
*Bucket.* Week, by close.
*Statistic.* Mean and the distribution's mode in the readout (*"1.4 runs per task; 52 of
78 took one"*).
*Shape.* Bars.
*Coverage.* `task_run` is native from 2026-09-07; before that the count under-reports
(§20.5) and the bucket is `estimated`.

### 18.2 Throughput and backlog (Q1, Q2)

**T1 — Throughput.** *Supersedes the throughput half of §8.4.*
*Question.* How much is getting finished.
*Definition.* §3.3, unchanged: `tasks_completed` as distinct tasks, `completion_events`,
`cancelled` as closed-with-any-other-outcome, and now **`reopened`** — tasks reopened in
the bucket — as a marker rather than a bar.
*Source.* `task_event` via `SQL_CLOSE_EVENTS`, plus `kind = 'reopen'` rows in range from
`ix_event_project_ts`.
*Bucket.* **The spine grain: day on 30d and 90d, week on 12m and all.** The coarser
grain existed only so the cycle-time percentile had a sample (`bucket_for`'s docstring);
with cycle time gone from this chart, completed-per-day is the owner's ask and the bars
are honest at any count.
*Statistic.* Counts.
*Shape.* Stacked bars, completed with cancelled on top, own axis, own chart. Reopenings
as a small marker above the bar with the count in the readout.
*Coverage.* As today.

**B1 — Backlog.** *§8.3, kept, with one addition to the readout.*
*Definition.* Level and flows, unchanged. The readout gains **net flow per bucket**:
`opened − closed`, signed, so the week's answer to Q1 is a number and not a slope
estimated by eye.
*Everything else.* As §8.3.

### 18.3 Finishes (Q6)

All from the `finish` and `finish_step` tables §20 specifies. Coverage for every F entry:
**native from the store child's merge; imported rows from 2026-08-23**, marked
`source = 'imported'`. Before 2026-08-23 nothing existed to record, and the series says
*"finishes are recorded from 23 Aug 2026"* rather than drawing zero.

**F1 — Finishes per week by outcome.**
*Question.* Is the scripted finish landing work, and when it does not, why.
*Definition.* Count of `finish` rows by `started_at` bucket, split by `outcome`:
`finished`, `escalated`, `declined`, `interrupted`. The readout names the escalation
reasons (`gate_failed` 48, `rebase_conflict` 11, `base_moved` 9 in the corpus).
*Source.* `finish`: `started_at`, `outcome`, `reason`.
*Bucket.* Week.
*Statistic.* Counts.
*Shape.* Stacked bars, `finished` at the bottom.

**F2 — Finish duration.**
*Question.* How long an approval takes to become a merged, restarted, verified change.
*Definition.* `finish.seconds` for rows with `outcome = 'finished'`; escalated rows
separately in the readout, because a finish that stopped at the gate is measuring the
gate. Today: finished **p50 4.8 min, p90 8.3 min**.
*Source.* `finish`: `started_at`, `seconds`, `outcome`.
*Bucket.* Week.
*Statistic.* p50 and p90, sample.
*Shape.* Line with p90 on tap, own axis; blank below `PERCENTILE_MIN_SAMPLE`.

**F3 — Where a finish spends its time.**
*Question.* Whether it is the gate (it is), and whether anything else is growing.
*Definition.* p50 seconds per `finish_step.step` over the bucket's finishes: `preflight`,
`runway`, `rebase`, `gate`, `catch_up`, `merge`, `rebuild`, `restart`, `verify`, `close`,
`worktree`, `branch`. Skipped steps (`skipped = 1`) are excluded from their step's sample.
*Source.* `finish_step` joined to `finish` on `finish_id`.
*Bucket.* Week.
*Statistic.* p50 per step.
*Shape.* Not a chart. **On tap of an F2 bucket**, the readout lists the steps with a
nonzero median; today that is `gate` 253 s and everything else under two seconds.

**F4 — Runway wait.**
*Question.* Whether finishes are queueing behind each other for the repository's one merge
runway (task-223).
*Definition.* `finish_step.seconds` where `step = 'runway'`; the count with any wait and
the p90 among them. Today 14 of 161 waited, max 359 s.
*Source, bucket.* As F3.
*Shape.* In F1's readout: *"2 of 9 waited for the runway, longest 6 min"*. A chart of a
value that is zero 91% of the time would be decoration.

**F5 — Finishes per completed task.**
*Question.* How often a task needs more than one finish — the cost of a red gate or a
moved base.
*Definition.* `COUNT(finish.finish_id)` per completed task, by the task's close bucket.
Today: 43 of 78 took one, 19 took two, two took five.
*Source.* `task` left-joined to `finish` on `task_id` (`ix_finish_task`).
*Bucket.* Week. *Statistic.* Mean; distribution in the readout. *Shape.* Bars, beside S4.

### 18.4 Gates (Q6)

From `gate_run` and `gate_stage` (§20). Coverage: **finisher gates from 2026-08-23 by
import; agent-side and manual gates only from the store child's merge**, and the series
says which. A gate row with `origin = 'run'` before that date does not exist, so *"time
in gates per task"* is a finisher-only figure for the imported span and the readout says
so.

**G1 — Gate duration.**
*Question.* Whether the gate is getting slower.
*Definition.* `gate_run.seconds` for `scope = 'full'` and `passed = 1`, by `started_at`.
A red gate stops early and measures nothing about cost. Over every gate the finish
records hold, green or red, **p50 4.0 min, p90 6.1 min**.
*Source.* `gate_run`: `started_at`, `seconds`, `scope`, `passed`, `origin`.
*Bucket.* Week. *Statistic.* p50, p90, sample. *Shape.* Line with p90 on tap, sharing
F2's panel and axis (both are minutes; the gate is most of a finish).

**G2 — The stage split.**
*Definition.* p50 `gate_stage.seconds` per `stage` over the bucket's full green gates.
Today `pytest` 125 s and `e2e` 107 s are the gate; the other eight stages sum to under
20 s.
*Source.* `gate_stage` joined to `gate_run`.
*Shape.* On tap of a G1 bucket, in the readout — the same treatment as F3.

**G3 — Green rate.**
*Question.* How often a gate is red, which is how often a finish escalates for that
reason.
*Definition.* `passed` over full gates per bucket, with `failed_stage` counted in the
readout.
*Source, bucket.* As G1. *Statistic.* Fraction and counts. *Shape.* In F1's readout
beside the escalation reasons; it is the same fact from the other side.

**G4 — Gate minutes per completed task.** *The owner's "time in full gates per task".*
*Definition.* `SUM(gate_run.seconds)` over gates with this `task_id` and `scope = 'full'`,
whatever `origin` and whether green or red, per task completed in the bucket. Today
**p50 4.4 min, p90 6.9 min**, 8 of 78 with no gate at all (hand closes and decisions).
*Source.* `task` left-joined to `gate_run` on `task_id` (`ix_gate_task`).
*Bucket.* Week, by close. *Statistic.* p50, p90. *Shape.* Bars, beside S4 and F5 in one
"cost per completed task" panel.
*Coverage.* Under-reports before agent-side gates are recorded (§20.3), and says so.

### 18.5 Runs (Q6)

From `task_run`. Coverage: **native from 2026-09-07T19:06Z**. The 171 rows imported at
the cutover carry the import instant as `started_at` (§16.2) and would draw 177 runs on
7 Sep; **until §20.5 re-stamps them, the series starts at the first native dispatch and
the imported rows are excluded**, with the caption *"runs are recorded from 7 Sep 2026"*.

**R-1 — Runs per day.**
*Definition.* `COUNT(*)` of `task_run` by `started_at` bucket, `trigger` in the readout
(`manual` 188, `child` 48, `auto` 23 today).
*Source.* `task_run`: `started_at`, `trigger`. Needs `ix_run_started` (§20.5); today's
plan is `SEARCH USING INDEX ix_run_task (project_id=?)` with a sort, which is a scan of
the project's runs.
*Bucket.* The spine grain. *Statistic.* Count. *Shape.* Bars.

**R-2 — Agent-hours per day.**
*Definition.* `SUM(duration_seconds) / 3600` by `started_at` bucket. A run is attributed
whole to the bucket it started in; splitting a run across midnight was rejected as
precision the question does not need.
*Source.* `task_run`: `started_at`, `duration_seconds`. *Shape.* Bars, the same panel as
R-1 on a second axis — the one case where §8.4's two-axis argument holds, because
runs-and-their-hours is one question.

**R-3 — Run outcome mix.**
*Definition.* `COUNT(*)` by `outcome` per week: `completed`, `interrupted`, `cancelled`,
`finished_without_handoff`, and null for runs still in the air (in the readout, not the
bar).
*Source.* `task_run`: `started_at`, `outcome`. *Shape.* Stacked bars, `completed` at the
bottom.

**R-4 — Run duration.**
*Definition.* `duration_seconds` for ended runs; today **p50 27 min, p90 97 min**.
*Statistic.* p50, p90, sample. *Shape.* Line with p90 on tap, own axis.

**R-5 — Schedule-to-start latency.**
*Question.* How long an admitted dispatch waits before its session is up.
*Definition.* `launched_at − admitted_at` per `run_attempt`. Today it is a near-constant
**2.2 s** — the launcher — because the queue only shipped on 2026-09-18 and a dispatch
that could not start was refused rather than queued. **The number that will matter is
`dispatch_queue.claimed_at − queued_at`**, the time a queued dispatch waited for a slot,
and it is the same metric once a queue exists: the readout reports both, and the chart
plots the queue wait when any row has one.
*Source.* `execution.db`: `run_attempt` (`project_id`, `admitted_at`, `launched_at`) and
`dispatch_queue` (`project_id`, `queued_at`, `claimed_at`, `status`), read through
`execution_store_for(home).read(...)`, never a second connection composed by path.
*Bucket.* Week. *Statistic.* p50, p90, and the count that waited at all. *Shape.* In
R-1's readout until a week has three queued dispatches; then a line.
*Coverage.* `run_attempt` from 2026-09-13, `dispatch_queue` from 2026-09-18. Machine-level
tables filtered by `project_id`.

**R-6 — Hours paused on usage limits.**
*Question.* How much of the week the machine was stopped by a quota.
*Definition.* Per project, the sum over `auth_waiter` rows with this `project_id` and an
incident of `kind = 'usage_limit'` of `updated_at − stall_at` where `status =
'recovered'`, by the bucket of `stall_at`. This is **run-hours lost**, not wall-clock:
three runs stalled for the same 50-minute reset count 2.5 hours, which is what it cost.
Today: 6.9 run-hours across five waiters in one day.
*Source.* `execution.db`: `auth_waiter` (`project_id`, `stall_at`, `status`, `updated_at`)
joined to `auth_incident` (`kind`). Both plan as a scan of a table with single-digit
rows; an index is not requested until it has hundreds.
*Bucket.* Week. *Shape.* Bars, on R-2's panel as a muted segment above the agent-hours
bar — paused hours are machine hours that produced nothing.
*Coverage.* From 2026-09-18 (task-463). Before that the series is absent, not zero.

### 18.6 Review (Q7)

**R1 — In review now.**
*Definition.* Open, unarchived tasks with `ball = 'human' AND ball_reason = 'review'`,
each with the hours since the ball landed there — `MAX(ts)` of the task's holder-changing
rows, the same rule as §8.6's *days held*.
*Source.* `task` and `task_event` via the in-review variant of `SQL_BALL_SINCE`. Plan:
`SEARCH t USING INDEX sqlite_autoindex_task_1`, `SEARCH e USING INDEX ix_event_task_ts`,
0.16 ms.
*Shape.* A list, id, title and wait, each a link — this is the one panel that is a call
to action, and it is the reason the review section is above the machine sections (§19.4).

**R2 — Approval latency.**
*Definition.* Per exit from `human`/`review` in the bucket, the hours since the matching
entry; **all exits**, not only approvals, because a revise request is also the owner
answering. Today **p50 4 min, p90 5.1 h** over 34 visits.
*Source.* `task_event`: every entry into and exit from `human`/`review` for the project
with `ts < :to` — one range read of 367 rows in 0.85 ms via `ix_event_task_ts`, paired
in Python per task. The lower bound is deliberately absent: an exit in the window may
have its entry before it. A window-function pairing in SQL was measured at 4.2 ms with a
full scan and rejected.
*Bucket.* Week, by exit. *Statistic.* p50, p90, sample. *Shape.* Line with p90 on tap.
*Coverage.* Reconstructed rows carry log timestamps and are included, flagged
`estimated` (§17.4).

**R3 — First-time approval rate.**
*Definition.* Of tasks approved in the bucket (§17.2), the fraction with `review_rounds
= 1`. Today **14 of 24**.
*Source.* The same rows as R2. *Bucket.* Week. *Statistic.* Fraction, with both counts.
*Shape.* In R2's readout — a rate of a small count drawn as a line would be noise.

### 18.7 Questions (Q8)

**Q-1 — Open questions now.**
*Definition.* `log_entry` rows of `type = 'question'` on open tasks with no `answer`
entry whose `re` names them. **An answer is an entry of `type = 'answer'`**: the
`handoff` entry the UI writes beside it also carries `re`, and counting both would count
every answer twice. Today 2.
*Source.* `log_entry` (`type`, `re`, `ts`, `task_id`) and `task` (`lifecycle`). Plan:
`ix_log_type_ts` then `ix_log_thread`, 0.13 ms.
*Shape.* A count in the review panel's header, linking to the task list filtered to
open questions if that filter exists; a list of the questions if it does not.

**Q-2 — Time to answer.**
*Definition.* `MIN(answer.ts) − question.ts` per question asked in the bucket that has
an answer; unanswered questions are counted, not averaged. Today **p50 16 min, p90 30 h**
over 53.
*Source.* As Q-1, `ix_log_type_ts` range on `ts` with a correlated `ix_log_thread` probe,
0.09 ms.
*Bucket.* Week, by the question. *Statistic.* p50, p90, sample, unanswered count.
*Shape.* Line with p90 on tap, on R2's panel: both are "how long did a person take".

---

## 19. The page, second version

### 19.1 The default range and the delta baseline

**The delta baseline is `max(range.start, coverage.native_from)`, and the tile names the
date it compares against.** Never `baseline_at`: that is the backfilled floor of October
2025 and comparing against it is what produced *"145 more since 22 Jun"*. A tile reads
*"149 open ▲ 4 since 7 Sep"* until native history is older than the range, and *"in 30
days"* after. §8.2's suppression rule is amended to fire on `native_from` rather than
`baseline_at`; §9.3's *"since <date>"* wording moves with it.

**The default range becomes `30d`** (from `90d`). Not because 30 days sits inside native
coverage — nothing offered does, for another eighteen days — but because the page is now
mostly machine series at day grain, and thirty bars fit a phone without aggregating where
ninety do not (§10.4's rule is aggregate rather than scroll, and week-grain runs-per-day
would answer a different question). The week-grain series get four or five buckets at
30d, which is thin, and `90d` is one tap away; the range control's caption says how many
days of native history there are (§9.2), so the reader can see when a longer range
becomes worth choosing.

The `estimated` flag and the hatch are unchanged. The tiles are the only place that
compared against a reconstructed instant without saying so.

### 19.2 Throughput and cycle time become two charts

*Supersedes §8.4.*

- **Throughput (T1)** keeps the bars and loses the line, the band and the second axis.
  It moves to the spine grain, so on 30d it is completed-per-day. Its readout carries
  `completion_events` when it differs, `cancelled`, and reopenings.
- **Where the time goes (S1)** takes cycle time's place, directly beneath throughput,
  as a stack of five medians per week. Its readout line reads *"w/c 14 Sep · queue 1.1 h ·
  work 0.6 h · waiting 0 · review 0 · finish 0 · total p50 4.8 h · 22 tasks"* and on tap
  expands each segment to *p90* and *median among the n that had it*. Buckets under
  `PERCENTILE_MIN_SAMPLE` are left blank, not interpolated.
- No axis label sits inside the plot area. The first page's cycle-time axis did; the
  geometry's `PADDING.left` is widened for the hour labels rather than the labels moved
  in.

### 19.3 Stuck, reordered

*Amends §8.6.* Rows are ordered by **who is being waited on**, not by count:

1. `human` / any reason — *waiting on you* — most-held first.
2. `external` / `dependency`, `external` / `service` — *blocked*.
3. `agent` / `work`, `revise`, `answer`, `redirect`, `hold` — *with an agent*, which is
   not stuck but is where a parked run shows up.
4. `agent` / `available` — **the queue**, last, labelled *"ready, unclaimed"*, with its
   count and longest wait. It is the backlog waiting its turn and it says so.

The over-time chart is unchanged. The `human` band is still the one that matters.

### 19.4 Order, and why each panel sits where it does

*Supersedes §8.1.* One column on a phone, two on a wide screen; the order is still "is
this project OK" first, and the calls to action above the machine.

1. **Summary row** — the five counts with deltas against the native baseline (§19.1).
   First because it was always first; now it is honest.
2. **Backlog (B1)** — Q1, the chart the owner said works. Unchanged.
3. **Throughput (T1)** — Q2, its own chart. Beneath backlog because the two share a
   day spine and the eye reads flows then completions.
4. **Where the time goes (S1, S2, S3)** — Q5. Beneath throughput because it is what
   cycle time was, and the reader who wanted "faster or slower" finds it where that
   answer used to be, now split into the part that is the queue and the part that is
   the work.
5. **Review (R1, R2, R3, Q-1, Q-2)** — Q7 and Q8. Above the machine sections because
   its first panel is a list of things waiting on the person reading it, and a call to
   action belongs above a report. Questions share the panel: both are "how long did a
   person take".
6. **Finishes and gates (F1–F5, G1–G4)** — Q6. The scripted finish is the project's
   delivery mechanism, and its duration and escalation rate are the most direct
   "getting better or worse" the machine has.
7. **Runs (R-1 to R-6)** — Q6. Machine capacity. Last of the trends because it is the
   input the others are outputs of, and because its coverage is the youngest.
8. **Cost per completed task (S4, F5, G4)** — three small bar charts side by side:
   runs, finishes and gate minutes it took to complete a task. After runs because each
   is a ratio over the panels above it.
9. **Aging** — Q3, unchanged (§8.5).
10. **Stuck** — Q4, reordered (§19.3). Aging and stuck move to the bottom: both are
    "right now" lists rather than trends, and the owner's ask was trends.
11. **Coverage footer** — now one line **per source family**: task history, finishes,
    gates, runs, execution journal. One sentence cannot honestly cover five baselines.

### 19.5 Shapes

Every panel above draws with §10.1's four shapes plus the stacked bar that already exists
in `analyticsGeometry.ts` (`stackedBars`, used since task-373 for cancellations). No new
shape and no library. The "p90 on tap" treatment replaces the p50–p90 band everywhere: a
band was the blob the owner could not read, and §10.4 already makes tap the primary
input.

---

## 20. What the store child (task-472) must add

Written the way §6 was written for task-273: columns, indexes and rules, nothing
architectural. The tables below were created in the scratch copy of the live store, the
212 finish records were imported into them, and every query in §18 and §21 was planned
and timed against the result. The DDL is what was measured; the migration is the store
child's to write in `src/agentjobs/sqlstore/migrations/`.

### 20.1 The four tables

```sql
CREATE TABLE finish (
  project_id        TEXT NOT NULL,
  finish_id         TEXT NOT NULL,                -- fin_xxxxxxxx, from meta.yaml
  task_id           TEXT NOT NULL,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,                         -- NULL while running or interrupted
  seconds           REAL,
  outcome           TEXT NOT NULL CHECK (outcome IN
                      ('finished', 'escalated', 'declined', 'interrupted', 'running')),
  reason            TEXT,                         -- gate_failed, rebase_conflict, base_moved, ...
  stopped_at        TEXT,                         -- the step an escalation stopped at
  merged            INTEGER NOT NULL DEFAULT 0 CHECK (merged IN (0, 1)),
  merge_commit      TEXT,
  run_id            TEXT,                         -- the run that ran `finish --posture-release`
  dispatched_run_id TEXT,                         -- the follow-on run an escalation started
  authority         TEXT,
  source            TEXT NOT NULL DEFAULT 'native' CHECK (source IN ('native', 'imported')),
  PRIMARY KEY (project_id, finish_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
CREATE INDEX ix_finish_started ON finish(project_id, started_at);
CREATE INDEX ix_finish_task    ON finish(project_id, task_id, started_at DESC);

CREATE TABLE finish_step (
  project_id  TEXT NOT NULL,
  finish_id   TEXT NOT NULL,
  seq         INTEGER NOT NULL,                   -- order within the finish
  step        TEXT NOT NULL,                      -- preflight, runway, rebase, gate, catch_up,
                                                  -- merge, rebuild, restart, verify, close,
                                                  -- worktree, branch, unexpected
  ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
  skipped     INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0, 1)),
  seconds     REAL NOT NULL DEFAULT 0,
  detail      TEXT,
  ts          TEXT NOT NULL,
  PRIMARY KEY (project_id, finish_id, seq),
  FOREIGN KEY (project_id, finish_id) REFERENCES finish(project_id, finish_id) ON DELETE CASCADE
);

CREATE TABLE gate_run (
  project_id   TEXT NOT NULL,
  gate_id      TEXT NOT NULL,                     -- <finish_id>:g<n> for a finisher gate;
                                                  -- <run_id>:g<n> for an agent gate;
                                                  -- man_<uuid> by hand
  origin       TEXT NOT NULL CHECK (origin IN ('finish', 'run', 'manual')),
  finish_id    TEXT,
  run_id       TEXT,
  task_id      TEXT,                              -- NULL for a manual gate with no run
  scope        TEXT NOT NULL CHECK (scope IN ('full', 'partial', 'since_gate', 'concurrent')),
  tree         TEXT,                              -- gate_scope.tree_fingerprint
  checkout     TEXT,                              -- the path the gate ran in
  branch       TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,                              -- NULL: killed mid-gate (9 of 214 imported)
  seconds      REAL,
  passed       INTEGER CHECK (passed IN (0, 1)),
  failed_stage TEXT,
  stages_run   INTEGER,
  stages_total INTEGER,
  source       TEXT NOT NULL DEFAULT 'native' CHECK (source IN ('native', 'imported')),
  PRIMARY KEY (project_id, gate_id)
);
CREATE INDEX ix_gate_started ON gate_run(project_id, started_at);
CREATE INDEX ix_gate_task    ON gate_run(project_id, task_id, started_at) WHERE task_id IS NOT NULL;

CREATE TABLE gate_stage (
  project_id  TEXT NOT NULL,
  gate_id     TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  stage       TEXT NOT NULL,                      -- black, ruff, mypy, api, icons, oxlint,
                                                  -- pytest, vitest, build, e2e, roadmap
  seconds     REAL,                               -- NULL for the stage that failed
  passed      INTEGER CHECK (passed IN (0, 1)),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (project_id, gate_id, seq),
  FOREIGN KEY (project_id, gate_id) REFERENCES gate_run(project_id, gate_id) ON DELETE CASCADE
);
CREATE INDEX ix_gate_stage_name ON gate_stage(project_id, stage, started_at);
```

Two shapes are deliberate. **`gate_run` has no foreign key to `finish` or `task_run`**,
because an agent-side gate in a worktree must be writable when the run it belongs to is
not yet a row in this store (§20.3), and a manual gate belongs to nothing. And **there is
no `runner` column anywhere**; `task_run` already carries one for the dispatch UI, and
adding it here would be the toggle §16 rejected.

### 20.2 The plans these indexes serve

Measured on the seeded scratch store, 212 finishes, 1,384 steps, 214 gates, 1,410 stages:

| query (§18 entry) | rows | p50 (ms) | plan |
|---|---|---|---|
| finishes in range (F1, F2) | 212 | 0.30 | `SEARCH finish USING INDEX ix_finish_started` |
| finish steps in range (F3, F4) | 1,384 | 1.28 | `ix_finish_started` → `SEARCH s USING INDEX sqlite_autoindex_finish_step_1` |
| gate runs in range (G1, G3) | 214 | 0.30 | `SEARCH gate_run USING INDEX ix_gate_started` |
| gate stages in range (G2) | 1,410 | 1.36 | `ix_gate_started` → `SEARCH s USING INDEX sqlite_autoindex_gate_stage_1` |
| gate seconds per completed task (G4) | 193 | 0.46 | `ix_task_closed_at` → `SEARCH g USING INDEX ix_gate_task` LEFT-JOIN |
| finishes per completed task (F5) | 193 | 0.38 | `ix_task_closed_at` → `SEARCH f USING INDEX ix_finish_task` LEFT-JOIN |
| finish coverage | 1 | 0.11 | `SEARCH finish USING INDEX ix_finish_task (project_id=?)` |

Every one is an indexed range read; none touches a JSON column or a task document. The
step and stage queries return the whole detail set and fold in Python, like every series
before them; they grow with the number of finishes in the window and would be the first
to earn a `GROUP BY` in SQL, at the same ~50 ms trigger §3.5 sets.

### 20.3 The writers

**The finisher** writes a `finish` row when it opens its directory (`outcome =
'running'`), a `finish_step` row at the moment it appends each `finish_step` line to
`phases.jsonl`, and updates the `finish` row where it writes `meta.yaml`'s
`finished_at`. The files stay: they are the human-readable log and `finish_status.py`
reads them. Same moment, same process, one more `INSERT`; the write is inside the
finisher's existing store access, so no new connection.

**The gate** (`scripts/check.py`) writes a `gate_run` row at `gate_started`, a
`gate_stage` row at each `gate_stage_finished`, and updates the `gate_run` row at
`gate_finished` — with `passed`, `seconds`, and `failed_stage` when there is one, and
**a partial run is a row with `scope = 'partial'`**, not an omitted row. It writes
through the same swallow-everything wrapper `record_phase` uses: a gate that could not
write a row runs, and the row is a gap in a chart rather than a lost gate.

**The agent-side gate is the row nobody writes today.** A gate run in a worktree before
handoff records itself only in `phases.jsonl` when the session is a dispatched run — 0 of
214 imported gates have `origin = 'run'`, because the import reads finish directories and
not run directories — and in the checkout's own receipt, which the next gate overwrites.
So *time in gates per task* (G4) is finisher-only until this lands. What the gate should
write:

- `origin = 'run'` with `run_id` from `AGENTJOBS_RUN_ID` and `task_id` from the run's
  registration, when the environment names a live run (`phases.current_run()` already
  decides this); `origin = 'manual'`, `run_id` null, `task_id` from the branch name when
  it matches `<type>/task-<nnn>-…`, otherwise null.
- `checkout` as the absolute path of `ROOT`, `branch` from `git rev-parse
  --abbrev-ref HEAD`, `tree` as the fingerprint the receipt already computes.
- **Into the served project's store, through `store_factory.task_manager_for`** — the
  safety rail in ENGINEERING.md — never a path composed from the checkout. A worktree's
  gate imports the worktree's own `agentjobs` package, which resolves the same machine
  home and the same database file; the migration is in the database, not in the code
  reading it, so a worktree behind `main` reads the tables and simply lacks the writer.
- Which project: the one whose `.agentjobs/config.yaml` the checkout carries. The gate
  runs in this repository, so it is `agentjobs`; a gate in a checkout with no config
  writes nothing.

**What it costs**, measured: **one `gate_run` and ten `gate_stage` rows commit in 5.6 ms**
on the scratch store, against a gate that takes four minutes. Opening the store from a
cold process is expected to be the larger cost — it is the same import the CLI pays —
and was not measured here; task-472 measures it in the gate's own timing table and
records it, with under one second as its acceptance line.

**Run directories are not imported.** The 289 run directories under `.agentjobs/runs`
hold `phases.jsonl` with agent-side gate records back to 2026-08-19, and importing them
would give G4 its history. It is not asked for here: a run's phase file also carries
records from other processes (§dispatch/phases.py's docstring, task-249), a gate in it may
have been written into the wrong run's directory before that fix, and the value is one
metric's backfill. If G4's coverage note turns out to matter, that import is its own
small task with the task-249 caveat as its first test.

### 20.4 The one-time import

`agentjobs storage import-finishes` (or a flag on the existing import), reading every
`fin_*` directory under the home's `finishes/`, **idempotent on `finish_id`**, reporting
what it skipped and why. Measured: **212 directories parse and load in 211 ms**, so it
is not a job that needs progress output. The rules, each found in the corpus:

- `meta.yaml` → `finish`, `source = 'imported'`. `merged` is absent on 138 of 212
  records (it was added later); derive it as `outcome = 'finished' AND merge_commit IS
  NOT NULL` when the key is missing.
- **`outcome: running` with no `finished_at` and a `started_at` older than 24 hours is
  imported as `interrupted`** — 11 such records, the oldest from 2026-08-25, each a finish
  whose process died. A `running` record younger than that is skipped and reported: the
  native writer owns it.
- Each `finish_step` line → one `finish_step` row, `seq` in file order.
- Each `gate_started` line opens a `gate_run` with `gate_id = <finish_id>:g<n>`, `origin
  = 'finish'`, `task_id` from the finish; `gate_stage_finished` lines become `gate_stage`
  rows; `gate_finished` closes the run, and a `failed_stage` gets a `gate_stage` row with
  `passed = 0` and `seconds` null. A `gate_started` with no `gate_finished` (9 of 214)
  stays open with `finished_at` null.
- Unknown `kind` lines (`finish_gate_attempt`, `finish_gate_receipt`, `finish_merged`,
  `finish_catch_up`, `finish_gate_retry`, `gate_stage_browser_gone`) are skipped without
  comment; a torn line is counted and reported.
- A finish whose `task_id` is not in the `task` table is refused and reported, not
  inserted with the foreign key off. Zero in this corpus.

Run it against this machine's store as part of task-472's delivery and record the counts
on that task, the way §6.4 recorded where each item landed.

### 20.5 Requested of `task_run`, found on the way

**A. Re-stamp the 171 rows imported at the cutover, and stop stamping.** `_record_run`
in `sqlstore/store.py` writes `started_at` as `_now()` — the instant the `dispatch`
entry is written — and `ended_at` the same way on the `dispatch_result`. For a native
dispatch that is the launch to within a second. For the cutover import it was the import:
171 rows carry `2026-09-07T19:03:31Z` within one minute, `ended_at` a tenth of a
millisecond later, and a `duration_seconds` that is nonetheless correct because it came
from the payload. Two changes: the writer takes `started_at` and `ended_at` from the
payload when the entry carries them and falls back to the clock only when it does not;
and a one-pass re-stamp keyed on `run_id` from the run ledger on disk, which has the true
`started_at` and `finished_at` for every one of them (289 `meta.yaml` files, back to
2026-08-19). That fixes R-1 to R-4's history back to the first dispatch. Until it runs,
§18.5's exclusion applies.

**B. `CREATE INDEX ix_run_started ON task_run(project_id, started_at)`.** R-1 to R-4
filter runs by `started_at` range; today's plan is `SEARCH USING INDEX ix_run_task
(project_id=?)` followed by a sort over every run the project has. 0.36 ms either way at
259 rows, so this is asked for on the plan and not on the time; `ix_run_live` is partial
on `ended_at IS NULL` and cannot serve it.

### 20.6 Not asked for

- No rollup tables. The whole second set of series costs under 10 ms of storage at this
  corpus (§21.4), and §3.5's trigger stands.
- No change to `phases.jsonl` or `meta.yaml`. The files are the log; the rows are the
  index.
- No `task_event` change. The segments read what is there.

### 20.7 Where each item landed (task-472, 2026-09-19)

| item | where it lives | note |
|---|---|---|
| the four tables and their indexes (§20.1) | migration `005_finish_and_gate_history.sql` | DDL as written above, plus `source` on `finish` and `gate_run` |
| the plans (§20.2) | checked on a copy of the live store after the import | every §18 query plans as the indexed range read the table says |
| the finisher writes rows (§20.3) | `dispatch.finish.FinishDirectory` → `history.FinishHistory` | over the service: the finish holds a remote manager, and only the server opens the database (task-273) |
| the gate writes rows (§20.3) | `scripts/check.py` → `history.GateHistory`, `PUT /history/gates/{id}` | origin from the inherited run or finish id; project from the clone the checkout is a worktree of; `patient=False` so a dead service costs one refused request |
| a partial run is a row marked `partial` | `history.SCOPES` | the gate's `necessity` is stored as `since_gate` |
| the one-time import (§20.4) | `agentjobs storage import-finishes --project <id>` | idempotent on finish id; reports `exists`, `unknown_task`, `still_running`, `no meta.yaml`, another project's, torn lines |
| §20.5 A, re-stamp | migration `005`, from the `dispatch` and `dispatch_result` entries' `ts` | the run ledger on disk was not needed: the entries in the store carry the same instants. `_record_run` now stamps from the entry, not the clock. **§18.5's exclusion is dead code once this lands** |
| §20.5 B, `ix_run_started` | migration `005` | |
| what the gate row costs | printed under the gate's timing table as `history` | measured on task-472: two writes of a red partial run in under 0.05 s; one write is one loopback `PUT` |

Two things differ from the text above. **The finisher has no store access of its own**
-- `agentjobs finish` runs with `task_manager_for`, which outside the server is the
service -- so the rows go through two `PUT` routes and the manager verbs behind them
rather than "one more `INSERT`". And the gate's `checkout` and `branch` on imported rows
come from the preflight step's detail (`<branch> at <sha> in <path>`), which 172 of the
219 imported gates carry; the gate's own phase lines never held them.

---

## 21. The API, second set

*Extends §7.3.* The same endpoint, the same `range`, the same rule that every model is a
named Pydantic class. One new convention: **a per-series coverage**, because the sources
now have five different baselines and one `coverage` object cannot carry them.

### 21.1 Per-series coverage

```python
class SeriesCoverage(BaseModel):
    """What this series can honestly claim, independent of the page-level coverage."""
    recorded_from: Optional[datetime]   # first row of any source; None => no rows at all
    native_from: Optional[datetime]     # first row written as it happened
    complete: bool                      # range.start >= native_from
    note: Optional[str]                 # "finishes are recorded from 23 Aug 2026", or None
```

Every series below carries one. `estimated` stays per bucket (§7.3), and a bucket is
estimated when any input row is non-native — `task_event.source`, `finish.source`,
`gate_run.source`, or a `task_run` row §20.5 A has not yet re-stamped.

### 21.2 The models

```python
class SegmentPoint(BaseModel):                   # S1, S2, S3
    bucket: date
    sample: int                                  # completed tasks in the bucket, in the sample
    excluded: int                                # closed by an import row (§17.4)
    unreviewed: int                              # tasks with no review handoff (§17.3)
    queue_p50_hours: Optional[float];  queue_p90_hours: Optional[float]
    work_p50_hours: Optional[float];   work_p90_hours: Optional[float]
    waiting_p50_hours: Optional[float]; waiting_p90_hours: Optional[float]
    review_p50_hours: Optional[float]; review_p90_hours: Optional[float]
    finish_p50_hours: Optional[float]; finish_p90_hours: Optional[float]
    total_p50_hours: Optional[float];  total_p90_hours: Optional[float]
    first_review_p50_hours: Optional[float]      # S3
    first_review_p90_hours: Optional[float]
    first_review_sample: int
    among: dict[str, SegmentAmong]               # per segment: tasks with a nonzero value
    estimated: bool

class SegmentAmong(BaseModel):
    tasks: int
    p50_hours: Optional[float]

class ThroughputPoint(BaseModel):                # T1 — replaces §7.3's, cycle fields removed
    bucket: date
    tasks_completed: int
    completion_events: int
    cancelled: int
    reopened: int
    estimated: bool

class CostPerTaskPoint(BaseModel):               # S4, F5, G4
    bucket: date
    sample: int
    runs_mean: Optional[float];     runs_mode: Optional[int]
    finishes_mean: Optional[float]
    gate_minutes_p50: Optional[float]; gate_minutes_p90: Optional[float]
    without_gate: int
    estimated: bool

class FinishPoint(BaseModel):                    # F1–F4
    bucket: date
    finished: int; escalated: int; declined: int; interrupted: int
    reasons: dict[str, int]                      # escalation reasons
    duration_p50_min: Optional[float]; duration_p90_min: Optional[float]; sample: int
    steps_p50_s: dict[str, float]                # F3, nonzero medians only
    runway_waited: int; runway_p90_s: Optional[float]
    estimated: bool

class GatePoint(BaseModel):                      # G1–G3
    bucket: date
    full: int; passed: int
    failed_stages: dict[str, int]
    duration_p50_min: Optional[float]; duration_p90_min: Optional[float]; sample: int
    stages_p50_s: dict[str, float]               # G2
    origins: dict[str, int]                      # finish / run / manual
    estimated: bool

class RunPoint(BaseModel):                       # R-1 to R-4
    bucket: date
    runs: int
    triggers: dict[str, int]
    agent_hours: float
    outcomes: dict[str, int]
    in_flight: int
    duration_p50_min: Optional[float]; duration_p90_min: Optional[float]; sample: int
    estimated: bool

class MachinePoint(BaseModel):                   # R-5, R-6, from execution.db
    bucket: date
    admitted: int
    start_latency_p50_s: Optional[float]; start_latency_p90_s: Optional[float]
    queued: int; queue_wait_p50_s: Optional[float]; queue_wait_p90_s: Optional[float]
    paused_run_hours: float; paused_waiters: int

class ReviewPoint(BaseModel):                    # R2, R3, Q-2
    bucket: date
    exits: int; approvals: int
    wait_p50_hours: Optional[float]; wait_p90_hours: Optional[float]
    first_time_approvals: int                    # approvals with review_rounds == 1
    questions: int; answered: int
    answer_p50_hours: Optional[float]; answer_p90_hours: Optional[float]
    estimated: bool

class InReview(BaseModel):                       # R1
    task_id: str; title: str; hours_waiting: float

class OpenQuestion(BaseModel):                   # Q-1
    task_id: str; entry_id: int; hours_open: float

class AnalyticsResponse(BaseModel):              # §7.3's, extended
    ...                                          # range, coverage, totals, backlog, holders,
                                                 # aging, oldest, stuck: unchanged
    throughput: list[ThroughputPoint]            # shape changed: no cycle fields
    segments: list[SegmentPoint];      segments_coverage: SeriesCoverage
    cost_per_task: list[CostPerTaskPoint]; cost_coverage: SeriesCoverage
    finishes: list[FinishPoint];       finishes_coverage: SeriesCoverage
    gates: list[GatePoint];            gates_coverage: SeriesCoverage
    runs: list[RunPoint];              runs_coverage: SeriesCoverage
    machine: list[MachinePoint];       machine_coverage: SeriesCoverage
    review: list[ReviewPoint];         review_coverage: SeriesCoverage
    in_review: list[InReview]
    open_questions: list[OpenQuestion]
```

Decisions in the shape:

- **`ThroughputPoint` loses its cycle fields** rather than keeping them deprecated. The
  generated client is regenerated by the same task, and a field that means the wrong
  thing (§16.1) is worse than a field that is gone.
- **Segment percentiles are per segment on one point**, not five parallel arrays, for
  §7.3's reason: a client cannot misalign them.
- **`MachinePoint` is its own series** because its source is another database with its
  own baseline; folding it into `RunPoint` would give one series two coverages.
- **`dict[str, int]` for the small vocabularies** (reasons, stages, outcomes, triggers)
  rather than a model per vocabulary: the keys are the store's own strings, a new gate
  stage or escalation reason must not be a client regeneration, and the page renders
  them as a list.
- **Hours for task-scale durations, minutes for finishes and gates, seconds for
  latencies.** Each unit is in the field name so a reader of the JSON never guesses.

### 21.3 The queries, and their plans

Recorded against the copy of the live store on 2026-09-19; the `QUERIES` map in
`analytics.py` gains each, and `tests/test_analytics_api.py::TestQueryPlans` walks them
as it does today. The execution-journal queries run through
`execution_store_for(home).read(...)` and are planned against that file.

| name | serves | rows | p50 (ms) | plan |
|---|---|---|---|---|
| segment events | S1–S3 | 455 | 1.07 | `SEARCH t USING INDEX ix_task_closed_at` → `SEARCH e USING INDEX ix_event_task_ts` |
| review transitions | R2, R3 | 367 | 0.85 | `SEARCH task_event USING INDEX ix_event_task_ts (project_id=?)` |
| in review now | R1 | 1 | 0.16 | `SEARCH t USING INDEX sqlite_autoindex_task_1` → `SEARCH e USING INDEX ix_event_task_ts` |
| runs in range | R-1–R-4 | 259 | 0.35 | `SEARCH task_run USING INDEX ix_run_started` (§20.5 B; `ix_run_task` + sort without it) |
| runs per completed task | S4 | 78 | 0.17 | `ix_task_closed_at` → `SEARCH r USING INDEX ix_run_task` LEFT-JOIN |
| questions in range | Q-2 | 26 | 0.09 | `SEARCH q USING INDEX ix_log_type_ts` → correlated `SEARCH a USING INDEX ix_log_thread` |
| open questions | Q-1 | 2 | 0.13 | `ix_log_type_ts` → `ix_log_thread` → `sqlite_autoindex_task_1` |
| finishes, steps, gates, stages, per-task | F1–F5, G1–G4 | | 0.30–1.36 | §20.2 |
| schedule-to-start (`execution.db`) | R-5 | 50 | 0.05 | `SEARCH run_attempt USING INDEX ix_attempt_admitted` |
| dispatch queue waits (`execution.db`) | R-5 | 2 | 0.01 | `SCAN dispatch_queue` — single-digit rows, see R-5 |
| usage-limit pauses (`execution.db`) | R-6 | 5 | 0.02 | `SCAN w` → `SEARCH i USING INDEX sqlite_autoindex_auth_incident_1` |

The two scans are of machine-level tables with fewer rows than the plan line has
characters; `test_the_query_is_answered_by_an_index` should exempt them by name with
that reason rather than be weakened.

### 21.4 What the second set costs

Summing the medians above and §20.2: **about 8 ms of storage for every new series at this
corpus**, on top of §5.2's 6.3 ms. The Python folds — segments over 455 rows, review
pairing over 367, step and stage medians over 2,800 — are the same order. The
`X-Task-Parses` header stays at zero; nothing here reads a task document, and task-473
verifies that over HTTP the way task-372 did.

### 21.5 Tests the API child carries

- §17.5's invariant on every emitted task: the five segments sum to total within a second.
- The seeded corpus covers each §17.4 case by name: no review handoff, two round trips,
  a reopen, two claims, a release, an import close, a draft promoted late.
- A series whose source is younger than the range reports `recorded_from` and draws
  nothing before it — asserted on the payload, not on the page.
- §5.3's reconciliation test is unchanged; `totals` did not move.
- No field, model or query names a runner. A test greps the models for `runner` and
  `agent` as field names and fails on either.

### 21.6 Where each item landed (task-473, 2026-09-19)

| item | where it lives | note |
|---|---|---|
| the models (§21.2) | `api/models.py`, from `SeriesCoverage` to `OpenQuestion` | named as written, with one field added: `SeriesCoverage.bucket`, the grain the series is aggregated at, so a client never infers which series are weekly and which follow the spine |
| the segments (§17) | `analytics.fold_segments` and `settle_finish`; `AnalyticsProjection.segment_tasks` exposes the per-task fold | the invariant holds on all 296 completed tasks in the live copy, and `total` agrees with an independent sum of open intervals on every one |
| the finish segment of an approved task (§17.3) | `settle_finish` | measured as the *work accrued since the last approval*, which is the span from that approval to the close whenever the holder stayed `agent`/`work`, and stays inside *work* when it did not -- so the five segments always partition the total. The design's phrasing would let a wait after an approval push *work* negative |
| the finish segment of an unreviewed task (§17.3) | `settle_finish`, from the merged `finish` row whose `finished_at` is within a minute of the close | 107 of 129 finished rows on the live copy match under that rule; the rest belong to tasks closed by hand later or reopened, and stay inside *work* |
| the queries and their plans (§21.3) | `analytics.QUERIES` gains eighteen names, `EXECUTION_QUERIES` the four journal queries, `UNINDEXED_QUERIES` the two scans with their reason | every task-store query plans as §21.3's table says on the live copy; `tests/test_analytics_second_set.py::TestQueryPlans` refuses a `SCAN` on any of them and exempts the two journal scans by name |
| the machine series (§18.5) | `AnalyticsProjection.machine`, over `execution_store_for(home).read(...)` | a journal that cannot be read costs that series a caption, not the page a 500 |
| `ThroughputPoint` (§21.2) | gains `reopened` and `estimated`; **keeps its cycle fields and its grain for now** | the page still draws the cycle line from them, and removing them here would have made task-474's page change a prerequisite of this one; task-474 removes the fields and moves the grain when it removes the consumer. Recorded as a decision on task-473 |
| what it costs (§21.4) | measured on the live copy, 2026-09-19, after the §20.4 import | 32 statements on the task store per request; the projection builds in **35 to 38 ms** on every range (task-372 measured the first set alone at 16 to 33 ms over HTTP); over HTTP **53 to 62 ms** wall-clock, **25 to 61 KB** of body, `X-Task-Parses: 0` on every range |

One thing the verification found that is not this task's: **the live store had not had
`agentjobs storage import-finishes --project agentjobs` run when this was measured** --
`finish` held four native rows and `gate_run` thirteen, all from the afternoon of
2026-09-19 -- so the finish and gate series on the dashboard start there until the
import runs. The import was run on the scratch copy for the numbers above (218
finishes, 220 gates, from 2026-08-23) and left the live store untouched.

---

## 22. Decisions, pass two

Numbered on from §14. Each binds task-472, task-473 and task-474; each has its rejected
alternative in the section named.

12. **No series is split by runner.** Owner's decision, 2026-09-19. §16.
13. **Segments are dwell time by ball holder, five of them, partitioning a task's open
    life exactly.** Rejected: work as first-claim-to-first-review, work as a residual,
    four segments. §17.1.
14. **An approval is the ball leaving `human`/`review` for `agent`/`work`, or a close from
    review.** Destination, not actor. §17.2.
15. **The finish segment is last approval to close; an unreviewed task's finish comes
    from the `finish` table once it exists and is zero before that, counted in the
    readout.** §17.3.
16. **A closed interval before a reopen is in no segment; a task closed by an `import`
    row is in no segment sample.** §17.4.
17. **Segments read reconstructed rows and flag them; backfilled rows are never
    boundaries.** §17.4.
18. **Throughput and cycle time are two charts; throughput moves to the spine grain;
    where-the-time-goes is a stack of per-segment medians with p90 and the
    among-those-that-had-it median on tap.** Rejected: means (they swing with the one old
    task, which is the complaint), a stacked area, a p50–p90 band. §18.1, §19.2.
19. **The delta baseline is `max(range.start, native_from)` and the tile names the
    date; the default range is `30d`.** §19.1.
20. **Stuck lists waiting-on-you, then blocked, then with-an-agent, then the queue,
    named as the queue.** §19.3.
21. **Finishes and gates become rows — `finish`, `finish_step`, `gate_run`,
    `gate_stage` — written at the moment the files are, imported once from disk, with
    the agent-side gate writing its own row through `store_factory`.** Rejected: parsing
    the finish directories per request (178 ms today and growing by every finish); a
    `runner` column. §20.
22. **Every new series carries its own coverage.** One page-level coverage cannot
    describe five baselines. §21.1.
23. **`execution.db` is read through `execution_store_for`, filtered by `project_id`,
    and its series are their own model.** §18.5, §21.2.
24. **Usage-limit pauses are run-hours lost per project, not machine wall-clock.** §18.5.

---

## 23. Implementation children, pass two

Four children under task-212, chained by `needs`, filed on 2026-09-19 before this design
was written and pointed at it by name.

| task | title | `needs` | sections |
|---|---|---|---|
| **task-471** | this document | — | §16–§23 |
| **task-472** | finish and gate history into the store | task-471 | §20 |
| **task-473** | the analytics API, second set | task-472 | §17, §18, §21 |
| **task-474** | the page, second version | task-473 | §18, §19 |

Task-472 carries §20.5 A and B as well as the four tables: the re-stamp is what makes the
run series honest before 7 Sep, and it is a one-pass fix over a ledger that already has
the answer. Task-473 should not build R-1 to R-4 over the un-re-stamped rows and then
exclude them; if task-472 has landed, the exclusion in §18.5 is dead code.

*Task-472 landed on 2026-09-19 with both halves of §20.5 (§20.7). The exclusion in §18.5
is dead code from that migration on.*

---

## 24. Where each item landed (task-474, 2026-09-19)

The page section of this pass, on the endpoint task-473 shipped. Written the way §21.6
was: one row per thing §19 asked for, and what it cost if the answer differed.

| item | where it lives | note |
|---|---|---|
| the order (§19.4) | `Analytics.tsx` | eleven panels plus the coverage footer, asserted as a list of `aria-label`s in both the component test and the browser test -- a list rather than a spot check, because a panel that silently stops rendering is the failure a spot check misses |
| throughput and cycle time become two charts (§19.2) | `AnalyticsCharts.ThroughputChart`, rebuilt | the percentile line, the p50-to-p90 band and the second axis are gone; `ThroughputPoint` lost `cycle_p50_days`, `cycle_p90_days` and `sample` from the API, and `THROUGHPUT_BUCKET` is now `SPINE_BUCKET`. Task-473's first decision left both to this task, since this is where the consumer was |
| where the time goes (§18.1, §19.2) | `AnalyticsPanels.SegmentsChart` | five medians as a stacked bar per week, five fill patterns as well as five colours; a bucket under `PERCENTILE_MIN_SAMPLE` is outlined and left empty rather than drawn as five noughts, and the caption says once that the stack's height is the sum of five medians and not the median total |
| p90 on tap (§19.5) | every readout in `analyticsSecondSet.ts` | the readout always describes the *selected* bucket, so the tap §10.4 already made primary is what reaches the 90th percentile. No band anywhere on the page |
| no axis label inside a plot (§19.2) | `SeriesName`, and `VALUE_PADDING` / `COUNT_PADDING` | the name sits in the top margin; the margin was widened rather than the label moved in. Measured in Chromium against the rendered rectangles, not against the attributes |
| finishes and gates (§18.3, §18.4) | `FinishChart` and `DurationChart` | F1 as a stack by outcome; F2 and G1 as two lines on **one** axis, because both are minutes and the gap between them is the part of a finish that is not the gate. F3, F4, G2 and G3 are readout clauses -- a value that is zero nine weeks in ten, or a rate over single digits, is decoration as a chart |
| runs (§18.5) | `RunsChart`, `RunOutcomeChart`, `groupedStacks` | R-1 and R-2 side by side in each bucket on two axes, R-6 stacked above the hours. The one case §8.4's argument against two axes does not cover: neither series is an overlay on the other, so there is nothing to misattribute |
| review and questions (§18.6, §18.7) | `ReviewChart`, and two lists in `Analytics.tsx` | above the machine sections, because its first panel is a list of things waiting on the reader |
| cost per completed task (§18.1 S4, §18.3 F5, §18.4 G4) | `CostPerTaskPanel` | three small charts rather than one with three series: runs, finishes and minutes do not compare, and one axis would invite the comparison. They share a selection, so a tap moves all three |
| the delta baseline (§19.1) | `deltaBaseline` and `summaryTiles` | `max(range.start, native_from)`, never `baseline_at`; every sum is taken from that day forward, and the tile names the date |
| the default range (§19.1) | `DEFAULT_ANALYTICS_RANGE`, and the API's `DEFAULT_RANGE` | one constant each side, both `30d`, so the page and the endpoint cannot open on different windows |
| stuck, reordered (§19.3) | `orderStuck`, `stuckRowPhrase` | four bands by who is waited on; count still breaks a tie inside a band. The queue is last and says *"ready, unclaimed -- the backlog waiting its turn"* |
| per-series coverage (§21.1, §19.4) | `seriesCaption`, the panel caption, the footer list | one line per source family in the footer and one caption per panel. The sentence is the API's own; the page never derives one from three nullable fields |
| a series younger than the range (§9.3) | nothing, deliberately | the endpoint already starts such a series at its source, so the page draws what it is given and says where it starts. Padding it back to the range with zeros is the substitution §9.3 forbids, and the browser test asserts the bucket count rather than the absence of a zero |

### 24.1 One thing the design asked for that cannot be given literally

§19.4's acceptance says *a tile on the default range never shows a delta equal to its
total*. Against the old baseline that was always the defect it describes. Against
`native_from` it is **sometimes simply true**: on the review sandbox every blocked task
became blocked inside the window, so `blocked 3` really did rise by three.

Suppressing a true delta to satisfy the sentence would be the page lying to look
correct. What is enforced instead is the readable half: a rise equal to the whole count
is **labelled** -- *"▲ 3 more in 30 days — all of them"* -- so a reader can tell the two
apart at a glance, and both tests assert the *silent* form is impossible rather than the
arithmetic. The baseline rule itself is asserted directly and separately.

### 24.2 The review sandbox

`scripts/analytics_sandbox.py`, on its own port, with two projects and no connection to
the live corpus:

*   **sandbox-deep** -- nine months, five sources with five baselines, a reconstructed
    span older than 45 days, weeks under the percentile minimum, a reopened task, a day
    the machine spent paused on a usage limit, and a stuck panel whose largest group is
    the queue. The reconstructed cutoff is 45 days rather than 60 so **both** halves of
    §19.1 are on one project: at 30d native history covers the window and the tiles read
    *"in 30 days"*; at 90d it does not, and they name the date.
*   **sandbox-thin** -- four days old. Trends suppressed, values drawn, and most of the
    second set with no rows at all, so every "nothing recorded yet" sentence is on screen
    beside a project where the same panel is full.

The comparison is the fixture. A page built against nine months of everything renders a
flat line at zero for the thin project and looks healthy doing it; the only way to see
that is to have both on one server.
