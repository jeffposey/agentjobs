# Analytics page — design record

**Status: proposed 2026-09-06; §6 shipped, the page has not.** Task-213, the design child
of task-212. The history contract in §6 landed as task-371 on 2026-09-08 (§6.4 says where
each item lives). The API, the page and its entry point — task-372, task-373, task-374 —
are open and unstarted, so §7 onwards is still a proposal.

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

Top to bottom, one column on a phone, two on a wide screen. The order is the priority order
of the questions, so the answer to "is this project OK" is above the fold on a phone.

1. **Summary row** — the five counts, each with its change over the range (§8.2).
2. **Backlog** — level plus flows (§8.3). Q1.
3. **Throughput and cycle time** (§8.4). Q2.
4. **Aging** — distribution and the ten oldest (§8.5). Q3.
5. **Stuck** — current holders and the same split over time (§8.6). Q4.
6. **Coverage footer** — one line saying what the page is allowed to claim (§9.3).

### 8.2 The summary row — what the strip should have been

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
  analytics and this is one question away from it.
- **The board as it stood on a date** (0.67 ms). Trigger: wanting to explain a step in the
  level rather than see it.
- **Reopenings** (0.32 ms, 2 in this corpus). Trigger: more than a handful.

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

**Decision: one text link, `Analytics →`, on the Dashboard's existing "Active tasks"
heading row, on the right, where `View all N →` already sits.**

Concretely, that row is `Dashboard.tsx`'s `<div className="flex items-baseline
justify-between gap-4 …">` holding the `Active tasks (n)` heading and the `View all n →`
link. The two links become a `flex items-baseline gap-3` group on the right, `Analytics →`
first so the existing link keeps the edge position a reader's thumb already knows.

Why that and not the obvious alternative:

- **It costs zero vertical pixels.** The heading row already exists and already carries a
  right-aligned link. `dashboard-one-screen.spec.ts` asserts the Dashboard never scrolls;
  an entry point that adds a row would put that at risk and would be relitigating the space
  task-294 just reclaimed.
- **It is at the point of removal**, which is what the parent's sc-2 asks for.
- On a phone the heading row is already visible without scrolling, so the link is reachable
  in the place it matters most.

**Rejected: a `PrimaryNav` destination.** It is the more discoverable option and it costs
nothing on the Dashboard, which is genuinely attractive. Against it: `NAV_INLINE_MIN_PX` is
**1140px**, measured, and the constant is a function of the row's contents — task-338's
34px badge moved it from 1090. Another destination moves it again, which drops every desktop
window between 1140 and the new value into the burger. Paying that for a page opened weekly
is the wrong trade. **Reopen trigger:** the page becoming something opened from the Tasks or
Runs surfaces rather than from the Dashboard.

**Rejected: an icon.** An unlabelled glyph for a page nobody has seen before is a guess. The
word costs about sixty pixels of a row that has room.

**Mobile treatment.** The link is the same link — it is a text link on a row that is already
in the layout at every width. At the narrowest widths the heading row wraps, which is
existing behaviour for `View all N →` and is acceptable for a link that is not a call to
action. The implementing child verifies at 320px, 390px and 768px that the Dashboard still
does not scroll, using the existing one-screen spec rather than a new mechanism.

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
    destination and not an icon. §11.

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
