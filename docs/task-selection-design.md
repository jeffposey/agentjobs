# Queue position — design proposal

**Task:** task-081. **Status:** accepted 2026-08-20 (Jeff), including the review-pass
fixes: the renumber skip-if-closed rule (§6), same-band group moves (§5.2), the
selection-time integrity-check scope (§8), and reopen in the lock table (§7).
**Supersedes the framing in the task's original title** ("ranking signals and
tie-breaking"). The decision recorded on 2026-08-15 is that there is no ranking
function and no tie-breaker: there is an explicit, human-owned queue, and the
scheduler reads it.

This document specifies `queue_position` — a first-class task field that fixes the order
of open work inside a priority band — and everything that has to change so that one
answer to "what is next" is produced, stored, displayed and obeyed everywhere.

---

## 1. The gap

`get_next_task()` filters for claimability and then sorts:

```python
candidates.sort(key=lambda task: (task.priority_rank(), -task.updated.timestamp()))
```

Two things are wrong with the second term, and only the first is obvious.

**It is a mutable timestamp.** `updated` is rewritten by `_write_task` on every managed
write — a progress log entry, a typo fix in a spec, a grooming pass that deliberately
changed nothing. So **any edit to a task promotes it toward the front of the queue**. The
signal being ranked is recency of *attention*, which is close to the inverse of what a
backlog needs: the task nobody has touched in a month is the one most likely to be
forgotten, and it sorts last.

This was observed rather than theorised. On 2026-08-11 a grooming pass closed two tasks
and promoted four. Before it, `get_next_task()` returned task-080. After it, the same
call returned task-058. No priority changed, no dependency closed, and nobody decided
058 mattered more than 080. Grooming rewrote `updated`, and `updated` was the queue.

**The first term separates almost nothing.** Measured 2026-08-20 on the live agentjobs
corpus: 75 ready tasks, of which **47 are `high`**, 25 `medium`, 3 `low`. Priority is
deliberately coarse — that is what makes it useful as a band — so for three quarters of
the backlog the sort key degenerates to the timestamp alone. `GET /api/tasks/next` on
that corpus returns **task-203**, which is simply the task created most recently.

The frontend is worse, and independently so. `buildTaskRows` sorts by `updated`
descending *first* and consults priority only to break a tie between two tasks written in
the same millisecond:

```ts
const timeDifference = new Date(right.updated).getTime() - new Date(left.updated).getTime();
return timeDifference || (PRIORITY_RANK[left.priority] ?? 2) - (PRIORITY_RANK[right.priority] ?? 2);
```

So the list a human reads and the answer the API gives are ordered by two different rules,
neither of which anybody chose.

### What is not the gap

Two adjacent mechanisms are working correctly and must not be extended to cover this:

- **Dependencies** answer *eligibility*: may this start yet. They are semantic
  prerequisites. Adding a `needs` edge to express "do A before B" when B does not
  actually require A poisons the dependency graph, makes the cycle checker and the
  blocked-task reporting lie, and is unrecoverable later because nothing in the record
  distinguishes a real prerequisite from a scheduling hint.
- **Priority** answers *urgency band*. Four values is the right coarseness for a human to
  keep honest. Splitting it finer (`high+`, `high-`) is the same mistake as ranking by
  timestamp: it encodes order in a field whose job is classification.

What is missing is the third thing: an explicit order *within* a band. That is what this
design adds.

---

## 2. The decision

**`queue_position` is a first-class integer field on the task, and it is scheduling
data.**

`priority` is the urgency band. `queue_position` is the authoritative order inside that
band. Together, after claimability filtering, they are the complete order of open work:

```
sort key = (priority_rank, queue_position)
```

Nothing else participates. Not `updated`, not `created`, not the task id, not filesystem
order. There is no fallback: if the queue cannot produce this key for every task the
answer depends on, selection **fails loudly** rather than guessing (§8).

Deliberately *not* called a tie-breaker. A tie-breaker is what you reach for when the
real key is ambiguous; this is the real key. The user-facing term is **Queue position**,
or **work order** in prose.

---

## 3. Where it lives, and when it is valid

### 3.1 The field

```yaml
priority: high
queue_position: 300
```

On the Pydantic model:

```python
queue_position: Optional[int] = Field(
    default=None,
    ge=1,
    description="Order within the priority band. Present if and only if the task is open.",
)
```

And in `schema/agentjobs-v2.yaml`, so the LinkML-generated JSON Schema, ER diagram and
reference pages carry it too:

```yaml
      queue_position:
        range: integer
        minimum_value: 1
        description: >-
          Explicit order within the priority band. Unique among open tasks of the
          same priority in one project. Present if and only if the task is open.
```

### 3.2 The invariant, and why it mirrors `ball`

> **A task has a queue position if and only if it is open, and no two open tasks in the
> same project and the same priority band share one.**

The first half is the same shape as the rule that already makes limbo unrepresentable —
*`ball` is absent if and only if the task is closed* — and it is deliberately the same
shape. An open task is work someone will pick up, so it has a place in line; a closed
task is not in line at all. `Task._check_consistency` enforces it as rule 6, so a
hand-written file that violates it will not load.

Drafts hold positions too. A draft is open, it is not claimable, and someone has already
formed an opinion about where it belongs; selection filters it out on lifecycle, not on
position. Archived-but-open tasks likewise: `archived` is a visibility flag, not an end
state.

The second half — uniqueness — **cannot** be checked by the model, because a task file
knows nothing about its siblings. It is a corpus invariant, enforced in three places:

| Where | How |
|---|---|
| `validation.py` | `_check_queue` reports missing, duplicate, non-positive, or present-on-closed positions as findings. Runs in `agentjobs validate` and the pre-commit hook. |
| `TaskManager` | Every position-assigning path holds the project queue lock (§7) and computes from a fresh read, so a duplicate cannot be created by a race. |
| `get_next_task` | Calls `assert_queue_integrity()` over the bands the answer reads (§8) and raises `QueueCorruptionError` rather than sorting something it does not trust. |

### 3.3 Uniqueness is per band, and that has a consequence

Positions are unique within `(project, priority)` over open tasks only. Two facts follow,
both intended:

- **Changing priority is a queue operation.** A task moving from `medium` to `high` is
  leaving one line and joining another; it must be assigned a position in the target
  band. This is why `priority` cannot stay an ordinary content patch (§5.3).
- **Closing frees a position.** Gaps are normal and are never closed up eagerly; the
  numbers mean order, not rank-from-the-top.

---

## 4. Numbering: sparse integers

Positions are assigned in steps of `QUEUE_STEP = 100`: a band reads 100, 200, 300, ….

An insertion between neighbours takes the midpoint. `100` and `200` become `100, 150,
200`; the next insert at the same spot yields `125`; and so on. About six insertions can
land between one original pair before the gap is exhausted, at which point the band is
rebalanced (§6) and insertion retries once.

**Why sparse integers, and why this is the load-bearing choice:**

- **A move writes exactly one file.** The moved task gets a new number; its neighbours are
  untouched. In a git-backed, one-file-per-task corpus worked by several agents at once,
  that is the difference between a reorder being a one-line diff and being a 47-file diff
  that conflicts with everything in flight.
- **It is readable and typeable.** A human looking at `queue_position: 300` can see it is
  third, and a human who wants something first can ask for it by name rather than by
  computing a key.

Rejected alternatives are in §12; the two near misses are dense `1..N` numbering (every
insert rewrites the band) and fractional/lexicographic keys such as LexoRank (never needs
a rebalance, but `queue_position: "0|hzzzzz:"` fails the constraint that the corpus stay
readable and hand-auditable in YAML).

---

## 5. The verbs

Reordering is a managed domain operation, not a content patch. There is no
`set_queue_position`, exactly as there is no `set_lifecycle` — for the same reason: the
number is a consequence of a decision, and the decision is what the record should show.

Every verb below is idempotent through the existing `operation_id` mechanism, takes an
`actor`, and runs under the lock discipline in §7.

### 5.1 Create → bottom of the band

A new task is assigned `max(band) + QUEUE_STEP`, or `QUEUE_STEP` if the band is empty. A
task nobody has placed does not get to preempt an order somebody thought about.
`create` may take an explicit placement (`--before`, `--after`, `--top`) for the case
where the person creating it already knows; `--before`/`--after` must name a task in the
band the new task is joining, the same rule as `move`.

This is the guarantee behind "creation defaults must always produce a valid position":
there is no path that creates an open task without one, including import, migration, and
`load_test_data`.

### 5.2 `move` → the only way the order changes

```
move(task_id, *, before=None, after=None, top=False, bottom=False, with_children=False)
```

Exactly one of the four placements. `before`/`after` name a task in the **same band**;
naming one in a different band is an error rather than an implicit reprioritise, because
those are two different decisions and conflating them makes the log unreadable.

`with_children` moves the task's open descendants with it, contiguously, in their existing
relative order, into the slot after the moved task. Bands are flat — an epic's children are
ordinary members of their own bands (§12, "hierarchical positions") — so without this verb
an epic and its work would drift apart every time either end moved. It is one locked
operation over N+1 files.

**A group move carries only the descendants in the moved task's own band.** Contiguity is
a within-band property — positions in different bands are never compared, so "next to its
parent" has no meaning for a `medium` child of a `high` epic. Descendants in other bands
keep their positions, and the log entry's `moved_with` names exactly the tasks that moved,
so the record also shows which children stayed where they were. A caller who wants a
child's band to change says so with `reprioritize`; the group move never changes anyone's
priority.

A move appends a `queue_move` log entry to the moved task (and, for a group move, to the
subtree root, naming the children):

```yaml
- type: queue_move
  actor: jeff
  body: Moved to the top of the high band, ahead of task-063.
  data:
    band: high
    from: 4200
    to: 50
    placement: {kind: before, target: task-063}
    moved_with: [task-121]
```

`queue_move` joins `MANAGER_WRITTEN_LOG_TYPES`: it asserts that something happened, so no
caller may forge one.

### 5.3 `reprioritize` → changes band and place together

```
reprioritize(task_id, priority, *, before=None, after=None, top=False)
```

Default placement in the target band is the **bottom**. A band change already says
everything about urgency; where inside the new band it lands is a separate question the
caller may answer explicitly, and "bottom" is the answer that assumes least.

`update_task(priority=...)` — the existing generic patch, which the REST `TaskUpdateRequest`
still exposes — does not become an error. The manager intercepts a `priority` change and
routes it through `reprioritize` with default placement, under the queue lock, so every
existing caller keeps working and the invariant cannot be broken by a path that predates
it. The API allowlist simply never gains `queue_position`, so the field is unreachable by
patch.

### 5.4 `close` / `archive` → clears the position

`close` clears `queue_position` as part of the same write that clears `ball`. No queue lock
is needed: removing a value cannot create a duplicate. This keeps the common, hot path
cheap. (The renumbering operations, which *can* collide with an unlocked close, carry the
burden of that collision themselves — §6.)

If a closed task is ever reopened, it re-enters at the bottom of its band through the same
assignment path as creation — which means reopening, unlike closing, holds the queue lock
(§7). It does not remember where it used to be, and should not: the queue moved on
without it.

### 5.5 What a caller may *not* do

- Hand-edit `queue_position` in a YAML file. The canonical-form check and the receipt
  ledger already treat hand edits as corruption; this field is no different.
- Add a `needs` dependency to express order. §1 says why; §11 makes it a rule agents are
  told.
- Use `queue_position` for anything but order. It is not a score, not an estimate, and
  carries no meaning across bands: `high/900` is ahead of `medium/100` because of the
  band, not the number.

---

## 6. Renumbering, and the rule that makes it safe

Two operations rewrite a whole band:

- **Rebalance** — automatic, triggered when an insertion has no gap to land in. Restores
  usable spacing. Not a decision; no ordering changes.
- **Compaction** — explicit (`agentjobs queue compact`), for readability after a band has
  drifted into large numbers. Never automatic, because a background process quietly
  rewriting 47 task files is exactly the kind of thing that should require typing.

Both must satisfy one rule, because a multi-file write in this corpus is not atomic and an
agent or a `git status` can read the directory halfway through:

> **At every instant during a renumber, the band read from disk is a valid queue in the
> same order it had before.**

That is achievable without a transaction, using direction:

- **Renumbering upward** — every target greater than every current value — is applied
  **tail first**. At any point, the suffix that has moved sits in the high range in its
  original relative order, and the prefix that has not still sits below all of it. Order
  preserved, no duplicates.
- **Renumbering downward** — every target less than every current value — is applied
  **head first**, by the mirror argument.
- **Anything else** is done as two passes: up into a free high range, then down to the
  canonical `100, 200, 300, …`.

Rebalance always takes the upward form (targets start above the current band maximum), so
it is always a single tail-first pass. Positions therefore creep upward over the life of a
band; compaction is the answer when that becomes ugly, and it is cosmetic.

A renumber must also survive the one writer that does not take the queue lock: `close`
(§5.4). The band snapshot is taken under the lock, but a task in it can close before its
write arrives, and blindly applying the snapshot would put a position back onto a closed
task — violating rule 6 in a file the renumber itself just wrote. So each per-task write
goes through `mutate_task`, which re-reads under that task's own lock, and **a task that
is no longer open is skipped, not written**. A skipped task leaves a wider gap, which is
the normal state of a sparse band; the direction argument is unaffected because skipping
a write never reorders anything.

A crash mid-renumber leaves a correctly ordered band with odd numbers in it — not a
scrambled queue. `agentjobs queue check` reports it; `agentjobs queue repair` finishes the
job. The repair is deterministic from the band's current order, so re-running it is safe.

Neither operation writes a `queue_move` entry to the tasks it renumbers. Nobody decided
anything, and 47 log entries saying "300 became 1400" would bury the entries that record
real decisions. Both are visible in git and in the receipt ledger, which is where mechanical
rewrites belong.

---

## 7. Concurrency

A new project-scoped advisory lock, alongside the existing `.creation`:

```python
QUEUE_LOCK = ".queue"
```

It uses the same `O_CREAT | O_EXCL` mechanism, times out with a message naming the file,
and cannot collide with a task id (ids never start with a dot).

**Held by:** create, reopen, move, reprioritize, rebalance, compaction, repair, migration —
every path that assigns or changes a position.
**Not held by:** close, archive, and every read.

Reads deliberately do not lock. The worst a concurrent reader sees is a queue that is one
move out of date, which is indistinguishable from having asked a moment earlier. Close
deliberately does not lock either — clearing a value cannot create a duplicate — and the
renumbering operations absorb the resulting race by re-checking openness per task (§6).

**Lock order is fixed and global**, so two writers cannot deadlock:

```
.creation  →  .queue  →  individual task locks, ascending by id
```

Anything holding `.queue` re-reads the band from disk *inside* the lock — never from the
snapshot cache — for the same reason `mutate_task` re-reads a task inside its lock: a
decision made on a stale copy is the bug the lock exists to prevent.

Creation needs both locks because id assignment and position assignment are two separate
"compute a maximum, then write" races, and a create racing a move can duplicate a position
even when neither races another create.

---

## 8. Corruption is loud

`QueueCorruptionError` is raised — never swallowed, never fallen back from — when
selection is asked for an answer it cannot honestly give:

- an open task in a checked band with no `queue_position`
- two open tasks in one checked band with the same position
- a position below 1

**The check covers exactly what the answer reads.** Selection walks bands top-down to the
first band containing a claimable candidate — the winning band — and
`assert_queue_integrity` runs over **every open task in that band and the bands above
it**, not just the claimable candidates. It has to be wider than the candidates, because
`explain_next()` (§9) asserts an order over the *skipped* open tasks too, and an
explanation built on a task with a duplicated position is a lie with a straight face. It
deliberately stops there rather than covering the whole corpus: a duplicate in the `low`
band does not falsify the claim that a particular `high` task is next, and making every
selection hostage to corruption in a band it never reads would punish the wrong caller.
`validate` and `agentjobs queue check` cover every band, always.

The message names the offending task ids, the band, and the repair command. `get_next_task`
raises rather than returning; the REST endpoint answers `409 Conflict` with the same detail;
the CLI prints it and exits non-zero; the React dashboard shows the queue as broken with the
repair command in the panel rather than rendering a list in an order it cannot justify.

**Refusing to answer is the point.** A queue that quietly answers while corrupt trains
everybody to ignore corruption, and the failure it produces — silently working the wrong
task — is invisible. This is the same argument as the source-root check that refuses to
start a server importing another checkout's code.

Two deliberate exceptions keep the system repairable while it is broken:

- `validate_corpus`, `agentjobs queue check` and `agentjobs queue list` **report** rather
  than raise. You must be able to see a broken queue in order to fix it.
- `queue repair` operates on a corrupt corpus by definition. It assigns positions to open
  tasks that lack one (at the bottom of their band, ordered by `created` then id) and
  breaks duplicates deterministically (by `created`, then id), then rebalances. It never
  invents an opinion it does not have: everything it does is stated in its output, and
  anything it guessed is exactly what a human should review afterwards.

---

## 9. Selection, and explaining itself

```python
def get_next_task(self, priority=None, *, agent=None) -> Optional[Task]:
    candidates = [...unchanged claimability filter...]
    self.assert_queue_integrity(candidates)   # scope: winning band and above — §8
    candidates.sort(key=lambda t: (t.priority_rank(), t.queue_position))
    return candidates[0]
```

The filter is unchanged: `ready`, eligible for the requesting agent, no unmet `needs`, no
open children. Claimability decides *whether*; the queue decides *which*. A blocked task
does not block the queue — it is filtered out, and the queue moves past it.

### Why this one

The scheduler now has a defensible answer, so it should give it. `explain_next()` returns,
and every surface renders, a structure like:

```json
{
  "task": "task-120-issue-reporter-workflow",
  "band": "high",
  "queue_position": 100,
  "empty_bands_above": ["critical"],
  "skipped": [
    {"task": "task-081-task-selection-ranking", "position": 50,
     "reason": "not ready (active, held by agent)"},
    {"task": "task-137-embedded-helper-ui", "position": 80,
     "reason": "has 7 open children"}
  ]
}
```

`skipped` lists only tasks ahead of the winner in the same band or above it, with the
claimability rule that excluded each. (This is why the §8 integrity check covers those
tasks: the explanation asserts their order, so their positions must be trustworthy.) It is
the answer to the question a human actually asks when a tool hands them a task — "why not
the one I was expecting?" — and it is the difference between a scheduler and an oracle. It
also makes the queue self-teaching: someone who sees their favourite task skipped for "has
7 open children" has just learned the rule.

---

## 10. Surfaces

Everything below reads or mutates the same managed order. None of them re-sorts.

**REST**
- `GET /api/tasks/next` — unchanged shape; `409` on queue corruption.
- `GET /api/tasks/next/explain` — the §9 structure.
- `POST /api/tasks/{id}/queue-move` — `{before|after|top|bottom, with_children, actor, operation_id, expected_revision}`.
- `POST /api/tasks/{id}/reprioritize` — `{priority, placement…}`.
- `GET /api/projects/{id}/queue` — the whole ordered queue, band by band, with the
  claimability reason for anything not claimable. This is the list a human reviews.
- `POST /api/projects/{id}/queue/repair` and `/compact`.
- `queue_position` appears in every task read model.

**Python client** — one method per verb above, plus `queue()` returning the ordered
listing. No generic position setter.

**CLI** — a `queue` sub-app, in the shape of the existing `dispatch` sub-app:

```
agentjobs queue list [--band high] [--claimable]   # the reviewable order
agentjobs queue move <id> --before <id> | --after <id> | --top | --bottom [--with-children]
agentjobs queue reprioritize <id> --to high [--top]
agentjobs queue check                              # reports, never raises
agentjobs queue repair | compact
agentjobs next [--why]                             # --why prints §9
```

**React** — the task list renders in queue order (the `updated`-first sort in
`buildTaskRows` is deleted, not amended). Position is a visible, sortable column. Reorder
by drag-and-drop **and** by keyboard: focus a row, <kbd>Alt</kbd>+<kbd>↑</kbd>/<kbd>↓</kbd>
to step, <kbd>Alt</kbd>+<kbd>Home</kbd>/<kbd>End</kbd> for top/bottom, each firing exactly
one `queue-move`. Drag is an accelerator for the keyboard path, never the only way — a
reorder that can only be performed with a mouse cannot be performed on the phone this
backlog is read on, and cannot be tested without a pointer harness. The dashboard's next
action shows the §9 explanation on demand.

**MCP** — `task_next` and `tasks_list` results carry band and position; `task_next` carries
the explanation. One new mutation tool, `task_queue_move`, with `actor` + `operation_id`
like every other mutation. No generic setter, per the server's standing contract: a task
moves through domain verbs or not at all.

**Validation and generated docs** — the `_check_queue` findings from §3.2; the LinkML slot,
which regenerates the JSON Schema, the ER diagram and the v2 reference pages via
`scripts/regen-schema-docs.sh`; and a paragraph in `docs/task-schema.md`.

---

## 11. What agents are told

Added to `ALLAGENTS.md` and `docs/agent-workflow.md`:

> **Work what the queue says is next.** `agentjobs next` (or `task_next`) is the answer,
> and `--why` explains it. If you think something else should be first, **move it** —
> `agentjobs queue move` — so the next session inherits the decision.
>
> Do not add a `needs` dependency to make one task come before another. Dependencies are
> prerequisites; a false one deadlocks the graph and lies to every reader of it. Do not
> hand-edit `queue_position`. Do not rely on an instruction given in chat to reorder work:
> chat does not survive the session, and the queue does.

This is the whole point of the feature, stated as a rule. The order is durable only if the
people and agents with opinions put them in the record instead of in a message.

---

## 12. Rejected alternatives

**A project-level `queue.yaml` listing task ids in order.** Attractive: uniqueness is
structural, a reorder touches one file, and the file reads like a queue. Rejected on three
counts. It breaks the resumption contract — a task record has to be sufficient working
memory on its own, and under this scheme a task cannot tell you where it sits. It is a
merge hotspot: every agent reordering anything conflicts with every other, in a repository
whose entire storage design is one-file-per-task to avoid exactly that. And it introduces a
second source of truth that can disagree with the corpus (an id in the queue that no longer
exists, a task in no queue), which is a class of bug the current design does not have.

**Dense contiguous `1..N`.** Reads beautifully and is what a person would write by hand.
Rejected because every insertion or move rewrites every file below it — a 47-file commit
to move one task up, guaranteed to conflict with concurrent work.

**Fractional or lexicographic keys (LexoRank, fractional indexing).** Never needs a
rebalance, which is genuinely elegant. Rejected because `queue_position: "0|hzzzzz:"` is
not something a human can read, audit in a diff, or reason about, and the constraint that
the corpus stay readable in YAML is not negotiable — it is the product's stated philosophy.
The rebalance this avoids is rare, cheap and, per §6, provably order-preserving.

**A finer-grained `priority`.** Rejected: it makes classification do ordering's job, and
gives no way to say "these two are equally urgent but this one first".

**Hierarchical positions (a task's position relative to its siblings, sorted by path).**
Keeps epics contiguous for free and is more expressive. Rejected as more machinery than the
problem needs: uniqueness would be scoped per parent *and* per band, children can sit in a
different band from their parent, and every comparison becomes a path walk. The
`--with-children` group move (§5.2) buys the contiguity that matters at a fraction of the
cost. **Reopen if** the ordered backlog turns out to interleave epics in a way group moves
cannot keep tidy.

**Scoring — unblocking value, dependency centrality, age, estimated effort.** Rejected as
the original framing of this task, and worth stating explicitly because it is the seductive
option. A score is a model of what a human wants, and it is wrong in a way nobody can see:
when the answer is surprising, there is no place to look and nothing to correct. Note the
evidence in the task record cuts *for* the human: ranking the dispatch hierarchy by hand
found the obviously correct next action in under a minute, using judgement the function did
not have. The fix is to let the human write that judgement down, not to approximate it.
Nothing here forbids a *suggestion* later — "task-077 unblocks five others, move it up?" —
so long as it proposes a move a human accepts, and never reorders anything itself.

**A global queue across projects.** Out of scope by construction: projects are configured
separately and their bands mean different things.

---

## 13. Decisions

- **D1 (2026-08-15, Jeff).** Priority alone is insufficient and dependencies must not be
  abused as order. `queue_position` is authoritative, first-class order within a band.
  Claimability filters, then `(priority, queue_position)` selects. Timestamps and ids are
  not fallbacks.
- **D2.** Positions live on the task, not in a project-level list (§12).
- **D3.** Present if and only if the task is open — the same rule shape as `ball`.
- **D4.** Sparse integers, `QUEUE_STEP = 100`, midpoint insertion, rebalance on exhaustion.
- **D5.** Renumbering is direction-ordered (tail-first upward, head-first downward) so
  every partial state on disk is a correctly ordered queue, and it skips any task that
  closed since the band snapshot rather than writing a position onto a closed task.
- **D6.** Reorder is a managed verb with an `operation_id`, logging `queue_move`;
  rebalance and compaction log nothing on tasks.
- **D7.** Corruption raises, checked over the bands the answer reads (the winning band and
  above). No timestamp or id fallback anywhere, ever. `check`, `list` and `repair` are the
  exceptions that keep a broken queue fixable, and they cover every band.
- **D8.** Bands are flat; epic contiguity is served by `--with-children`, not by
  hierarchical keys. A group move carries only same-band descendants — cross-band
  contiguity is not a representable concept, and the move never changes a priority.
- **D9.** Reprioritising defaults to the bottom of the target band.
- **D10.** The migration baseline is `created` ascending, then id — immutable inputs only.

---

## 14. Relationship to other work

- **task-074 auto-dispatch** and **task-161 project-level dispatch** consume
  `get_next_task()` with no human reading the answer. They are the reason the queue must be
  right rather than merely defensible: today they would dispatch an agent onto whichever
  task was edited last.
- **task-078 agent loops** re-asks for the next task every iteration. A queue ordered by
  `updated` means a loop that logs progress reorders its own future work.
- **task-045 subtask support** gave us `parent`, which is what `--with-children` moves and
  what the "has open children" skip reason reports.
- **task-063 / task-053 schema v2 CLI** own the `next`, `claim` and `handoff` CLI verbs the
  `queue` sub-app sits beside; the queue commands should follow whatever shape lands there.
- **task-120 issue reporter** is placed ahead of this program by §15 — this design does not
  claim the front of the queue for itself.
- **task-109 MCP managed interface** was named first in the original program order and has
  since shipped and closed, so it drops out rather than being placed.

---

## 15. Migration, and the first real order

Two steps, deliberately separate, because they answer different questions.

**Step 1 — the deterministic baseline.** Every open task is assigned a position: within
each band, order by `created` ascending, then id ascending, and assign `100, 200, 300, …`.
`created` is immutable and expresses arrival, which is the least-wrong thing to assume
about tasks nobody has ordered yet. Deterministic, so re-running the migration on the same
corpus produces the same corpus, and a reviewer can regenerate it to see what changed.

**Step 2 — the requested program order.** The baseline satisfies the schema. It does not
satisfy anybody, because a mechanical seed is by definition not a considered work order.
Two moves are then recorded explicitly, as `queue_move` entries, with no dependency edges
invented:

1. `task-120-issue-reporter-workflow` (with its same-band children — §5.2) to the top of
   the `high` band.
2. The implementation children of this design immediately after it.

**Step 3 — the ordering pass, which is what actually closes this program.** The whole open
backlog is presented band by band as a readable list, reviewed by Jeff, and the approved
order written through `queue move`. This is acceptance criterion sc-7 and it is a separate
child task (§16, child 6) because it needs the verbs to exist first and it needs a human
in the loop. Until it happens, the backlog's order is an artifact of the migration and this
program is not done.

---

## 16. Derived implementation tasks

Six, in dependency order, created as drafts under task-081 and held there until this
design is approved. Each leaves the system in a working state, and none of them is a
one-line sort change.

1. **task-204 — schema, validation and the migration baseline.** The `queue_position` field on
   `Task` with the open-if-and-only-if rule, the LinkML slot and regenerated schema docs,
   `_check_queue` in `validation.py`, and the deterministic §15 step-1 migration that
   assigns every open task in the corpus a position. Nothing reads the field yet, so this
   is safe to land alone: it makes the corpus ordered without changing any behaviour.

2. **task-205 — manager: assignment, the verbs, and selection.** The `.queue` lock and its ordering
   discipline, position assignment on create and reopen, `move` (with `--with-children`,
   same-band only), `reprioritize`, the `priority`-patch interception, clearing on close,
   rebalance and compaction with the direction rule **and the skip-if-closed rule** (§6),
   `assert_queue_integrity` / `QueueCorruptionError` / `repair`, the `queue_move` log
   type, and `get_next_task` switched to `(priority_rank, queue_position)` with
   `explain_next()`. Two acceptance tests that matter: **rewrite `updated` on every open
   task in any order and prove the queue does not move**, and **close a task in a band
   mid-rebalance and prove the rebalance neither writes to it nor disturbs the order of
   the rest**.

3. **task-206 — REST, Python client and CLI.** The endpoints in §10, the client methods, and the
   `agentjobs queue` sub-app plus `next --why`. Includes the `409` corruption path end to
   end.

4. **task-207 — React: the ordered list and reordering.** Delete the `updated`-first sort, render
   position, drag-and-drop plus the keyboard equivalents, the queue-corrupt state, and the
   "why this one" panel on the dashboard. Reviewed on a sandbox server with both a healthy
   and a corrupt queue seeded.

5. **task-208 — MCP, agent rules and reference docs.** Position and explanation in `task_next` /
   `tasks_list`, the `task_queue_move` mutation tool, the §11 rules in `ALLAGENTS.md` and
   `docs/agent-workflow.md`, and `docs/task-schema.md`.

6. **task-209 — order the live backlog (sc-7).** Present every open agentjobs task, band by band, as a
   readable list; get Jeff's approval of that list; write it with `queue move`. Depends on
   task-206. The approval is the evidence, and this is the child that closes the program.

---

## Appendix: acceptance criteria coverage (task-081)

| Criterion | Where |
|---|---|
| sc-1 — first-class field, total sort after claimability | §2, §3.1, §9 |
| sc-2 — unique position per open task; corruption is loud, no fallback | §3.2, §8 |
| sc-3 — create, move, reprioritize, close, migration, rebalance, concurrency | §5, §6, §7, §15 |
| sc-4 — every surface reads the same order and explains what is next | §9, §10 |
| sc-5 — deterministic baseline, task-120 ahead, no invented dependencies | §15 |
| sc-6 — derived implementation children with a no-timestamp test | §16 (child 2) |
| sc-7 — the live corpus actually ordered, reviewed by a human | §15 step 3, §16 (child 6) |
