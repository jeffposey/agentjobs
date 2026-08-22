# 05 — Queue system

**Auditor 5, Big Dawg Audit (task-242).** Read-only; main clone at `17fdfd0`
(the commit the 8876 server reports as `source_commit`). Reproductions ran in
temp directories under the session scratchpad using the repo's own library —
nothing under `tasks/` was touched.

**Scope:** `src/agentjobs/queue.py`, the queue verbs and selection in
`manager.py`, `storage.py`'s lock discipline, `validation._check_queue`,
`docs/task-selection-design.md`, and the `next` / `queue` surfaces in the CLI,
REST (`routes/tasks.py`, `routes/queue.py`, `routes/status.py`), Python client
and MCP (`read_tools.py`, `mutation_tools.py`). The live queue was read through
`agentjobs queue check|list`, `agentjobs next --why`, and `GET` against 8876.

## Findings at a glance

| # | Sev | Title |
|---|---|---|
| F1 | P2 | Reopen/reband by generic patch computes its position under the queue lock and writes it outside — a concurrent create or move duplicates the number (Auditor 2's question: **yes**) |
| F2 | P2 | `next` answers "nothing claimable" over a corpus whose every open task is unloadable — integrity is only checked when there is a winner. Three of five registered projects are in that state right now |
| F3 | P2 | Three live projects have never had positions assigned (17 open tasks invisible to `next`, the listing and the dashboard); nothing in registration or startup runs the baseline |
| F4 | P3 | `queue check` / `queue repair` do not cover the closed half of rule 6; `check` reports "sound" over a file the loader rejects |
| F5 | P3 | `--why`, the MCP explanation and the React panel print skipped tasks' positions without their band; across bands the number is meaningless by the module's own rule |
| F6 | P3 | The top of a band is the hot spot and halving exhausts in five inserts; live `high` is four top-placements from a 57-file rewrite |
| F7 | P3 | `queue repair` has no dry run; `migrate` and `migrate-schema` do |
| F8 | P4 | A replayed `priority` patch recomputes (and can rebalance) before it discovers it is a replay |
| F9 | P4 | Two reads per `next --why` / `task_next`, no snapshot: winner and explanation can come from different corpora |
| F10 | P4 | Repair names the losers of a contested position, not the winner |
| F11 | P4 | Design doc drift: `assert_queue_integrity(candidates)` in §9, and `reopen` listed as a lock holder it only half is |

---

## F1 — P2 — The generic patch's rejoin is not under the lock it claims (Auditor 2's question)

**Question as posed:** `manager.py:1055-1063` computes a reopen/reband position
under `queue_lock()` and writes it later in `_mutate` (`:1018`) outside it —
can a concurrent create or move produce a duplicate position?

**Answer: yes, and both forms reproduce deterministically.**

**Code path.** `update_task` (`manager.py:949-1018`) calls
`_rejoining_the_queue` (`:1020-1067`) *before* building the mutator. That
function takes `self.storage.queue_lock()` at `:1055`, calls `_place(...)`,
and **releases the lock on the way out** with only an integer in hand. The
write happens in `_mutate` at `:1018`, under the *task's own* lock
(`storage.mutate_task`, `storage.py:440-470`) — which is not the queue lock.
Between `:1055`'s exit and `:1018`'s entry, the band on disk still shows the
number as free. Every other position-assigning verb holds the queue lock
across the write: `create_task` (`:805` → `_create_unlocked` → `save_task`),
`move` (`:1649` → `_write_place`), `reprioritize` (`:1744` → `_write_place`).
`update_task` is the one that does not.

**Reproduction** (scratchpad `race_repro.py`; the interleaving is forced by
wrapping `_rejoining_the_queue` so that a `create_task` in the same band runs
after it returns and before `_mutate` is reached — exactly the window a second
process would land in):

```
created task-001 100 task-002 200
closed task-001 position now None
  rejoin planned position 300 | concurrent create got 300
reopened task-001 at 300
check_queue after scenario 1: ["band 'medium' position 300 is claimed by task-001, task-003"]
get_next_task refused: QueueCorruptionError -- ... band 'medium' position 300 is claimed by task-001, task-003
  reband planned position 100 | concurrent create in high got 100
check_queue after scenario 2: ["band 'high' position 100 is claimed by task-002, task-004"]
```

Scenario 1 is a reopen (`lifecycle` patched `closed → ready`); scenario 2 is a
band change (`priority` patched `medium → high`). A concurrent `move --bottom`
into the same band hits the same window by the same arithmetic
(`plan_insertion` BOTTOM, `queue.py:283-300`).

**Reachability.** The reband form is reachable from outside the process:
`priority` is an allowlisted field on `TaskUpdateRequest`
(`api/models.py:349-357`) and on MCP `task_update_content`
(`mutation_tools.py:98`, `:239`), so `PATCH /api/tasks/{id}` with
`{"priority": ...}` from the tailnet, the Python client's `update_content`, or
an agent over MCP all enter this path; so will the browser edit task-230 is
specifying. Locks are files (`storage.py:361-366`), so the race is
cross-process, not just cross-thread. The reopen form is manager-only today
(`lifecycle` is not in the request model; `dispatch/finish.py:935` patches
`branches` only).

**Blast radius.** Loud, not silent: `next` refuses with the repair command,
the dashboard shows `queue_broken`, and `repair` resolves it deterministically
(shown above). That is why this is P2 rather than P1. It still takes down
`agentjobs next`, `/tasks/next`, `task_next` and the dashboard next-action
for the whole project until somebody runs a repair, and auto-dispatch
(design §14) consumes `get_next_task` with no human to run it.

**Fix.** Hold the queue lock across the write. The lock order
`.queue → task lock` (`storage.py:429-431`) already permits it; `move` and
`reprioritize` are the template. Concretely: when `_rejoining_the_queue` finds
work to do, perform the whole `update_task` under `queue_lock()` — or route
the intercepted patch through `_write_place` the way `reprioritize` does, and
compute the position inside the same `with`. Then add the missing test:
`TestTheQueueLockHolds` (`tests/test_queue_verbs.py:185-270`) races
create-vs-create and create-vs-move only; it never races a `priority` patch,
and `test_a_priority_patch_is_intercepted_and_keeps_the_queue_valid` (`:705`)
is serial. A threaded create-vs-`update_task(priority=...)` test would have
caught this.

**Secondary defect in the same function.** `_rejoining_the_queue` reads
`existing = self.get_task(task_id)` at `:1046` *before* taking the lock and
through the snapshot cache, then derives `band`/`reopening`/`rebanding` from
it. A stale `existing` can make the rebanding decision on a priority the task
no longer has. Same fix covers it if the read moves inside the lock and goes
through `load_task_uncached`.

---

## F2 — P2 — "Nothing is claimable" is the wrong answer over an all-broken corpus

**Evidence (live, 2026-08-22):**

```
GET /api/projects/job-hunting/tasks/next          -> null            HTTP 200
GET /api/projects/job-hunting/tasks/next/explain  -> {"task":null,"band":null,"queue_position":null,"empty_bands_above":[],"skipped":[]}  HTTP 200
GET /api/projects/job-hunting/dashboard           -> next_action: 'nothing_claimable', queue_broken: None
GET /api/projects/job-hunting/queue               -> problems: 4 x "is open in band 'high' with no queue_position"
GET /api/projects/job-hunting/tasks/broken        -> 4 files, reason "lifecycle 'ready' is open, so queue_position is required"
```

Same shape for `product-strategy` (6 problems, `next` → null 200) and
`fantasy-football` (7). So `GET /queue` says the queue is broken in four
places while `/tasks/next`, `/next/explain` and the dashboard say the backlog
is simply empty — two surfaces, two incompatible facts, same corpus, same
second.

**Why.** `get_next_task` (`manager.py:565-593`) builds `candidates` from
`storage.list_tasks()` — loadable tasks only, and rule 6 makes every open task
without a position unloadable (`models_v2.py:818-822`). With no candidates it
returns `None` at `:589` **before** reaching `assert_queue_integrity` at
`:591`. The integrity scope (`bands_at_or_above(winning_rank)`, `queue.py:95`)
needs a winner to exist. `explain_next` (`:595-657`) has the same structure,
so `skipped` — which the design promises lists "every open task with the rule
that excluded it" when nothing is claimable — is empty, because the unloadable
tasks are not in `tasks` either. `dashboard.py:131-138` only sets
`queue_broken` on a raised `QueueCorruptionError`, which never raises here.

Design §8: refuse on "an open task in a checked band with no
`queue_position`". The answer "nothing is claimable" is a claim about *every*
band, so every band is the checked set in that case; the code checks none.

**What catches it today.** Only MCP: `_explain_no_work`
(`read_tools.py:538-583`) reads `/tasks/broken` and appends "N task file(s)
could not be read … claimable work may be hidden inside them". CLI `next`
prints `Nothing is claimable right now.` and exits 0 (`cli.py:1647`); REST and
the dashboard say nothing.

**Fix.** In `get_next_task` and `explain_next`, when `candidates` is empty run
`assert_queue_integrity()` with no band filter before returning `None` (the
`_queue_places` raw-read path at `:1847-1864` already surfaces the unloadable
files for exactly this reason). Then `next` 409s, the CLI exits 1 with the
repair command, and the dashboard's `queue_broken` banner appears. A test:
"a corpus where every open task lacks a position refuses rather than reporting
nothing claimable" — `TestABrokenQueueRefusesToAnswer` (`test_queue_verbs.py:388`)
has the duplicate and missing cases but every one of them keeps at least one
loadable candidate, which is the precondition the bug hides behind.

---

## F3 — P2 — Three registered projects have never been given positions

**Evidence** (raw YAML read via PyYAML, not through the loader):

```
C:\projects\job-hunting\tasks\job-hunting:         files=21 open=4 open_with_position=0 schema_versions={2}
C:\projects\product-strategy\tasks\product-strategy: files=7  open=6 open_with_position=0 schema_versions={2}
fantasy-football (via API):                        broken=7, loadable=10 of which open=0
```

Every open task in those projects is schema v2 and carries no
`queue_position`, so every one fails rule 6 at load, appears only in
`/tasks/broken`, and is absent from `next`, `queue list`, the task list and
the dashboard (F2 is what makes that absence look like emptiness). Last task
write on each: job-hunting 2026-08-20, product-strategy 2026-08-17,
fantasy-football 2026-08-16 — i.e. within days of task-204 landing
(`e6188d2`), and all three were left behind.

**Why.** The baseline migration (`queue.migrate_queue_positions`,
`queue.py:711`) and `repair_queue` (`manager.py:1891`) both exist and both
would fix this in one command each, but nothing invokes either for a project
that is merely *registered*: `agentjobs init`, the registry, and server
startup do not check for unpositioned open tasks. The validator
(`validation.py:118-215`) would report all 17 — and nothing runs `validate`
against those repositories.

**Fix, in order of cost.** (a) Today: `agentjobs queue repair` (or the
migration with `--write`) in each of the three projects — after reviewing its
ASSIGNED block, since F7 means there is no preview. (b) Durable: a
registry-wide "unpositioned open tasks" probe at server start or in
`agentjobs doctor`-style output, and a dashboard banner fed by F2's fix. (c)
The honest long-term answer is that a v2 file written without a position is a
file written by something that bypassed `TaskStorage` *or* by a pre-task-204
build — worth Auditor 4 confirming which (see questions below).

---

## F4 — P3 — `check`/`repair` cover three of rule 6's four conditions

`find_queue_problems` (`queue.py:480-520`) reports `missing`, `not-positive`
and `duplicate` — over **open** places only (`_queue_places`,
`manager.py:1847-1864`, appends raw records only `if record.is_open`). The
fourth condition, *a closed task carrying a position*, is rejected by the
model (`models_v2.py:813-817`) and reported by `validation._check_queue`
(`queue-on-closed`, `validation.py:171-180`) but is invisible to
`manager.check_queue` and untouched by `repair_queue`.

**Reproduction** (`check_coverage.py`, case 1 — closed task hand-edited to
`queue_position: 150`):

```
case1 load_errors: ['(root): Value error, a closed task must not have a queue_position (got 150) ...']
case1 check_queue: SOUND
case1 repair: Positions assigned: 0 | Bands rebalanced: 0 | Could not be repaired: 0
case1 load_errors after repair: 1
```

`agentjobs queue check` prints "The queue is sound" while the loader refuses
the file and `/tasks/broken` lists it. Selection is not affected (a closed
task is not in line), which is why this is P3 — but the CLI docstring says
`check` "covers the same rules" as `validate` (`cli.py:1561-1562`), and it
does not. Fix: have `_queue_places` carry closed raw records with a position
as a fourth `QueueProblem` kind, and let repair strip it (`_write_raw_position`
already knows how to rewrite a raw mapping).

---

## F5 — P3 — Skipped entries have no band, so `--why` prints numbers that cannot be compared

Live output:

```
task-214  [high/62]
Ahead of it, and why each was skipped (2):
    100  task-233        not ready (active, held by agent)
     31  task-211        has 6 open children
```

task-233 is `critical/100`; task-211 is `high/31`. Printed as bare numbers
under a `high/62` winner, 100 reads as *behind* 62 and 31, when it is the
first thing in the corpus. `queue.py:6-9` states the rule the output breaks:
the number "carries no meaning across bands". `SkippedTask`
(`manager.py:104-112`) has `task`, `queue_position`, `reason` and no band;
design §9's JSON example has the same omission; the CLI (`cli.py:1663-1666`),
the MCP sentence (`read_tools.py:451-461`) and the React panel
(`NextExplanation.tsx:65`, "(position) — reason") all inherit it. Fix: add
`band` to `SkippedTask.as_dict()`, the `NextExplanationResponse` model, the
MCP output schema (`read_tools.py:405-448`, `additionalProperties: False`, so
it must be declared) and render `band/position` everywhere.

---

## F6 — P3 — The top of a band exhausts in five inserts and then rewrites the band

`_fill(0, first, 1)` (`queue.py:270-281`) halves toward zero: from a fresh
band the sequence is 50, 25, 12, 6, 3, 1 and the seventh top-placement has
`step < 1`, so `_place` (`manager.py:1506-1539`) rebalances **the whole band**
upward and retries. Measured (`check_coverage.py`, case 4; `apply_position`
writes counted):

```
top-insert 5: got 1;   renumber writes this call=0; band size=9
top-insert 6: got 200; renumber writes this call=9; band size=10 min=200 max=1200
```

Live `high` band head: `31, 62, 93, 124, 155, 171, 187 …` (the listing shows
the halving history already). The next top-placements get 15, 7, 3, 1 — and
the fifth rewrites all 57 open `high` files, on `main`, in one operation.
Design §4 says "about six insertions fit between one original pair", which is
true per gap; the point is that *the* gap everyone uses is the one above the
first task, because "do this first" is the common opinion. Also note the
rebalance is always upward (`plan_rebalance`, `queue.py:379`), so after it the
band starts at ~12,300 and the next top-insert gets ~6,150; compaction
(`queue compact`) is manual and nobody is prompted to run it.

Not a correctness defect — the renumber is direction-safe (`plan_renumber`,
`:333-376`, tests at `test_queue_verbs.py:272-386`). It is a cost surprise on
the hottest path. Options, cheapest first: (a) reserve headroom — when TOP
lands below some floor (say 16), renumber only by inserting a *larger* leading
gap, i.e. a one-pass upward rebalance with `base` chosen so the head sits at
`QUEUE_STEP`; (b) have `queue list` print a warning when any band's minimum
gap is under, say, 8; (c) leave it and accept a periodic 57-file commit.

---

## F7 — P3 — `repair` writes on first contact; there is no preview

`queue_repair` (`cli.py:1577-1596`) calls `manager.repair_queue()` and prints
the report afterwards; `POST /queue/repair` likewise. `migrate_queue_positions`
takes `write=False` by default and `migrate-schema` has `--apply`; repair — the
one of the three that *guesses* (who keeps a contested number) — is the one
you cannot preview. The report is reviewable only after the files are
rewritten, and for F3's projects the first thing anyone will run is repair.
Fix: `--dry-run` that returns the same `QueueRepairReport` with `written:
False`, the shape `QueueMigrationReport` already has.

---

## F8 — P4 — A replayed priority patch does work before it knows it is a replay

`update_task` computes `rejoin` at `manager.py:979` — taking the queue lock,
reading the band uncached, and possibly triggering a rebalance that writes
every file in the band — and only inside the mutator at `:983` does
`replay_or_conflict` discover the `operation_id` was already applied. `move`
and `reprioritize` check replay *first*, under the lock (`:1654`, `:1748`).
Harmless to the queue (the rebalance is order-preserving) but it is a retry
rewriting N files to change nothing, and the replay contract says nothing is
written twice.

## F9 — P4 — `next --why` and `task_next` are two reads of a moving corpus

CLI `next_task` (`cli.py:1643-1645`) calls `get_next_task` then
`explain_next`, each parsing the corpus, with no `corpus_snapshot()` scope;
MCP `task_next` (`read_tools.py:478-482`) makes two HTTP requests. A claim
landing between them yields a `task` that the `queue.skipped` list then
describes as "not ready (active…)" — or vice versa. Cheap fix: have the CLI
call `explain_next` once and derive the winner from it (it already returns
`task`, `band`, `queue_position`); for MCP, add the winner's record to the
explain response or accept the window.

## F10 — P4 — Repair names what it moved, not what it kept

`repair_queue` (`manager.py:1911-1921`) keeps the earliest-created claimant of
a shared number and strips the rest; the report's ASSIGNED block lists the
stripped tasks and their new numbers. The guess — *which* task kept the
contested place — is not stated, only derivable. One line per contested
position ("300 in medium: kept task-001, moved task-003") would make the
review the docstring asks for possible without re-reading the band.

## F11 — P4 — Design doc drift

- §9 shows `self.assert_queue_integrity(candidates)`; the signature is
  `assert_queue_integrity(bands)` and the call passes
  `bands_at_or_above(winning_rank)` (`manager.py:591`). Cosmetic, but it is
  the line a reader would copy.
- §7's lock table lists *reopen* under "Held by". It is held for the
  arithmetic and not for the write (F1). `storage.py:423` repeats the claim.
- §5.3 says the patch is routed "through `reprioritize`". It is not — it goes
  through `_rejoining_the_queue` + `update_task`, which is why it has a
  different lock profile from `reprioritize` (F1) and a different replay order
  (F8). Routing it through `reprioritize` literally would fix both.

---

## The brief, item by item

### 1. Band semantics, numbering, guarantees

- **Order key** is `(PRIORITY_RANK[priority], queue_position)`
  (`queue.py:106-115`); `updated`/`created`/id participate nowhere in
  selection — `test_rewriting_every_timestamp_changes_nothing`
  (`test_queue_verbs.py:130`) is the acceptance test the design asked for and
  it does what it says. `listing_key` (`:119-160`) adds open-before-closed and
  a newest-first tie-break used only for closed/unplaced work.
- **Uniqueness** is per `(project, band)` over open tasks; enforced by lock
  discipline at write time (F1 is the hole), by `assert_queue_integrity` at
  selection (scoped, F2 is the hole) and by `validate`/`check` at rest (F4 is
  the gap between them).
- **What renumbers, when.** Only `_place` on gap exhaustion (automatic
  rebalance, upward, one pass, whole band — F6), `compact` (explicit, two
  passes when needed) and `repair` (rebalances only bands it touched,
  `manager.py:1934-1940`). Nothing else ever rewrites a neighbour; verified by
  `test_a_move_writes_exactly_one_file` (`test_queue_verbs.py:530`).
- **The refusal.** ALLAGENTS: a hand-written number "can collide … corruption
  the queue refuses to answer over." Found and triggered:
  `QueueCorruptionError` (`queue.py:442-458`) raised from
  `assert_queue_integrity` (`manager.py:1866-1881`), reached from
  `get_next_task:591` and `explain_next:620`. Reproduced by hand-editing a
  duplicate (`check_coverage.py`, case 2):
  `get_next_task refused: QueueCorruptionError ... position 100 is claimed by task-001, task-002`,
  listing still renders with `problems` populated, repair resolves it. Scope
  verified too (case 3): a duplicate in `low` does not stop a `medium` answer
  but does stop `get_next_task(priority=low)` — exactly §8.

### 2. `task_next` claimability vs what `--why` reports

**Consistent by construction.** `_claimable` (`manager.py:550-563`) is
`[t for t in tasks if _skip_reason(t) is None]` and `explain_next` and
`queue_listing` print `_skip_reason` for the same tasks with the same
arguments (`:639`, `:696`). Rule order is fixed — lifecycle, open children,
unmet needs, eligibility, band filter (`:520-548`) — so a task failing several
rules always reports the first. Live check: `next --why` and `/next/explain`
return the same winner (task-214, high/62) and the same two skipped entries;
`queue list` marks the same 28 `high` tasks claimable. Two caveats: the
skipped list omits the band (F5), and the two are computed from separate reads
(F9). One exclusion `--why` *cannot* report: open tasks whose files do not
load are not in `tasks` at all, so a broken file ahead of the winner is
neither the winner nor skipped — it is the integrity check's job, and F2 is
where that job is not done.

### 3. `queue check` / `queue repair`

`check` verifies missing, non-positive and duplicate over open places,
including raw reads of files the loader rejected (`_queue_places`). It does
not verify position-on-closed (F4). Exit 0 by design unless `--strict`; that
is documented and sensible. `repair` states what it *assigned* and which
bands it rebalanced (`QueueRepairReport.render`, `manager.py:212-246`); it
does not state which claimant won (F10), cannot be previewed (F7), and leaves
unrepairable files named. Determinism verified: `created` then id
(`baseline_key`, `queue.py:547`), test at `test_queue_verbs.py:484`.

### 4. Lifecycle interactions

| Verb | Position effect | Lock | Logged? | Verdict |
|---|---|---|---|---|
| create | bottom of band, or explicit placement | `.creation` → `.queue` → task (`manager.py:805`) | `queue_move` only when placed | sound |
| claim / release / promote / handoff | untouched | task only | transition | sound; a released task keeps its slot — reasonable, undocumented |
| close (and archive-of-open) | cleared in the same write (`:1422`) | task only, deliberately | transition | sound; renumber skips closed (`apply_position`, `:1541-1572`) |
| archive-of-closed | none | task | note | sound |
| move / reprioritize | new number, logged | `.queue` across the write | `queue_move` | sound; replay checked under lock |
| **`update_task(priority=)`** | bottom of new band | `.queue` for the arithmetic, **not the write** | `queue_move` | **F1** |
| **`update_task(lifecycle=open)`** (the only reopen) | bottom of band | same | `queue_move`, **no transition entry** | **F1**; state moves without a transition record — Auditor 4's item 5 |
| delete | file gone | — | — | frees the number |
| import (`migration/__init__.py:37-56`, `:90`) | bottom, cursor per band | `.queue` | — | sound |
| `migrate-schema` | baseline per band | none (writes to an output dir) | — | sound for its use |
| `load_test_data` (`cli.py:702`) | hardcoded 100/200 in `sample_tasks.py` | none | — | fine for a fresh demo dir; would collide if pointed at a populated band — it is not meant to be |

Nothing silently *loses* order. The one thing that silently *duplicates* it is
F1.

### 5. The live queue

- `agentjobs` project: `queue check` → sound; 96 open tasks across four bands,
  no duplicates, no missing. 42 `queue_move` entries in the corpus (38 by
  `claude`, 4 by `Jeff Posey`), so the §15 step-3 ordering pass (task-209,
  closed completed) left a human-owned order and agents have moved things
  since. Oddities: `high` head has halved to 31 (F6); `high` jumps 750 → 3000
  and 328 → 723 — the residue of earlier rebalances, harmless; `medium`
  112/225/450/900/1800 shows the same halving pattern at its head.
- `mastercalls`: sound, 12 open, untouched baseline numbering.
- `fantasy-football`, `job-hunting`, `product-strategy`: **every open task
  unpositioned** — F3 — and every `next` surface calls them empty — F2.

## Examined, nothing found

- Lock implementation and order (`storage.py:335-439`): exclusive-create,
  delete-pending retry, fixed order `.creation → .queue → task`. No path takes
  them in another order; `repair` and migration take `.queue` then per-task
  via `save_task`, consistent.
- `plan_renumber` direction rule and the two-pass staging; the interrupt-safety
  tests (`test_queue_verbs.py:272-386`) exercise every partial prefix, which
  is the right test.
- `QUEUE_PLACEMENT_SCHEMA` and `QueueMoveRequest`: no position number is
  accepted on any surface; `TaskUpdateRequest` has `extra="forbid"` and no
  `queue_position` (test at `test_queue_surfaces.py:731-781`). The Python
  client has no setter (`:792`). MCP instruction text's "no generic position
  setter" claim holds.
- `queue_move` is in `MANAGER_WRITTEN_LOG_TYPES` and `add_log_entry` refuses
  it (`manager.py:1990`); test at `test_queue_verbs.py:813`.
- `bool` is not a position (`read_queue_record`, `validation.py:183-190`).
- Dashboard survives corruption (`dashboard.py:131-138`) when corruption
  *raises*; REST maps `QueueCorruptionError` → 409 (`routes/tasks.py:175-192`),
  MCP → `queue_broken` with a repair suggestion (`mcp/errors.py:30`,
  `read_tools.py:129-146`).
- React `queueOrder.ts` does not sort and `TaskList.tsx:58` says why — the
  server order is the only order. Within my brief only to that depth;
  Auditor 9 owns the rest.

## What I did not get to

- The frontend reorder path end to end (`TaskList.tsx` drag/keyboard →
  `queue-move` with `expected_revision`) — read only `queueOrder.ts`.
- Whether `explain_next`'s `empty_bands_above` is correct under a `priority`
  filter (it is computed from all open tasks; I did not test the filtered
  case).
- `scripts/review_queue_sandbox.py` and `scripts/bench.py`'s queue usage.
- `docs/agent-workflow.md`'s queue section against ALLAGENTS.md (Auditor 1/2
  territory; I read only ALLAGENTS).
- Running the queue tests; the brief forbids the gate and I did not run
  `pytest` selectively either. Test claims above are from reading the tests.
- Measuring lock contention: `LOCK_TIMEOUT_SECONDS = 10` with a 10 ms poll
  while a 57-file rebalance runs under `.queue` — whether a concurrent create
  can time out during F6's rewrite is a plausible P3 I did not reproduce.

## Questions for other auditors

- **Auditor 4 (storage/manager):** F3's three projects are schema v2 files
  with no `queue_position`. Were they written by a pre-task-204 build of
  `TaskStorage`, or by something that bypassed it? If the former, every other
  registered project that predates `e6188d2` is a candidate. Also: F1's reopen
  path moves `lifecycle` with no `transition` log entry — your item 5.
- **Auditor 7 (API):** `PATCH /tasks/{id}` with `priority` is the
  externally reachable entry to F1. Does the route's error contract cover a
  `QueueCorruptionError` raised *later* by a different caller? (It does not
  raise here — the patch succeeds and the corruption is discovered by the
  next `next`.)
- **Auditor 8 (MCP):** `task_next` requires `actor` and judges eligibility
  for it; `_explain_no_work` is the only surface that mentions broken files
  when nothing is claimable. Worth keeping — it is currently the one honest
  answer on F3's projects.
- **Auditor 9 (frontend):** the dashboard on job-hunting shows
  `nothing_claimable` while `broken_files` has four entries. Does the React
  next-action ladder render the broken-files list near that message, or does
  a phone reader see "nothing to do"?
- **Auditor 10 (dispatch):** `get_next_task` has no caller in
  `src/agentjobs/dispatch/` (grep). Design §14 says task-074/161 consume it.
  What does auto-dispatch actually order by?
- **Auditor 11 (gate):** `tests/test_queue_position.py::TestTheLiveCorpus`
  reads this repository's corpus — so a queue_move on `main` can turn the
  gate red, which `gate_scope.py`'s `tasks/ → pytest` rule must keep covering.
- **Auditor 12 (security):** the F1 window is reachable from the tailnet via
  `PATCH priority`; the consequence is a project-wide denial of `next` until a
  repair, not data loss. Your call whether that counts.
