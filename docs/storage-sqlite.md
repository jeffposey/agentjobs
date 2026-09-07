# Authoritative SQLite task storage

The store built on task-273. This document is the *why*; the schema itself is
`src/agentjobs/sqlstore/migrations/001_initial.sql`, and the boundary every backend
satisfies is `src/agentjobs/storage_protocol.py`.

**Status: built, and switchable per project.** Task-273 built the store; task-311 built
the cutover around it. A project is on one of two backends and
`agentjobs storage status` says which, counted rather than inferred. Nothing migrates on
its own: a machine that has never run `agentjobs storage cutover` has no database at all
and every project reads its files exactly as it always did.

---

## 1. What was decided, and by whom

The owner decided on 2026-09-05 that SQLite hosted alongside the AgentJobs server —
outside every code worktree — becomes authoritative, replacing canonical YAML. That
supersedes the earlier recommendation in
[schema-design.md §7](schema-design.md#7-rejected-alternatives-recorded-so-they-are-not-relitigated)
to keep files canonical and add a disposable index. Five follow-up questions were
answered on 2026-09-06:

| Question | Answer |
|---|---|
| One database or one per project | **One**, with `project_id` on every key |
| Attachments | **Blobs in the database** |
| Pre-cutover history | **Reconstruct, reconcile, and mine git** |
| Coordination with the analytics page | **task-213 runs first**, without a blocking edge |
| Search | **FTS5** |

The reasoning behind each, with its rejected alternative, is on task-273. What follows
is what those answers produced.

## 2. The shape, and the one rule behind it

Two failure modes drove the design. One opaque JSON blob per task reads quickly and
cannot answer a single historical question without parsing every document. Full
normalisation produces a fourteen-table join to read one task, two of those tables
existing only to hold lists of strings.

The rule that settles each field:

> **A value-object list becomes a table when something filters or joins on it, or when a
> verb mutates one element of it. Otherwise it is a validated JSON column on `task`.**

So `tags`, `dependencies`, `acceptance`, `deliverables`, `branches`, the log and
attachments are tables. `spec.context`, `links` and `assignment.eligible` are JSON:
nothing filters them and no verb touches one element. `json_each` is still available for
the occasional question, and at a few hundred rows that is not a cost worth normalising
away.

**One authoritative representation per field**, which bit twice:

- `task_run` is authoritative for what was dispatched, and the `dispatch` log entry's
  payload is rendered from it. Measured across this repository: a run id carries exactly
  one `dispatch` entry, 167 of 167.
- It is **not** authoritative for how a run ended. Two run ids carry *two*
  `dispatch_result` entries each — interrupted at 22:27, completed at 22:31, both true —
  and a row cannot hold two outcomes. The terminal payload stays on the entry that
  recorded it; `task_run.ended_at` is a latest-observed projection kept so that "which
  runs are in the air" is an index probe.

Five columns on `task` are denormalised (`closed_at`, `first_claimed_at`,
`last_activity_at`, `log_count`, and the queue's `priority_rank`). Each is written in the
same transaction as the thing that changes it, so it cannot drift the way a periodically
rebuilt cache would.

## 3. Invariants are constraints, not checks

The nine consistency rules `models_v2` enforces in a validator are `CHECK` constraints
here, so a hand-written `UPDATE`, a migration bug or a future code path cannot produce a
state the model forbids. `tests/test_sqlstore.py` asserts the database *refuses* each,
rather than asserting that some Python noticed.

Three of these retire code:

| Was | Is |
|---|---|
| Audit **F4**: two writers computing the same free queue slot | `ux_task_queue_slot`, a unique partial index. The race is unrepresentable. |
| Audit **F2**: stale `.lock` files with no reaper | Transactions. A lock file can be orphaned by a process that dies; a transaction cannot. |
| `validation.py` scanning the corpus for a parent that does not exist | A foreign key. |

A multi-row reorder is the one thing the unique index makes awkward, because it is
checked per statement. The shape that works — and the only one — is **lift the whole band
out of the way, then place it, inside one transaction**. There is a test for it so a
later simplification that drops the lift fails loudly.

## 4. History, and what the past can honestly say

`task_event` holds one row per state-changing event with `_from` and `_to` for nine axes.
Not a change feed of fields, and not a replay of the log at query time — both were
rejected, the second because it was measured and does not work.

**The generated `open_delta` column carries the whole backlog chart**: `+1` on create,
`-1` into `closed`, `+1` out of it, `0` otherwise. The level is a running sum over one
indexed integer.

**The invariant, asserted in this store's own test suite** because
`docs/analytics-design.md` §6 depends on it:

> `SUM(open_delta)` equals `COUNT(*)` of open tasks — after import, after backfill, and
> after a backup/restore round trip.

### What the log alone can reconstruct

Measured on the live corpus (371 tasks), replaying the log and comparing the result
against each task's own record:

| | tasks needing reconciliation |
|---|---|
| log replay alone | 371 of 371 |
| + manager semantics for entries that record a lifecycle without its ball | 161 |
| + git backfill | 84 |
| + closing clears the queue position, and a claim's actor is its owner | 59 |

**84% now reconstruct exactly.** What remains is residual by design:

- **35 tasks** have no recoverable `ball` history: they predate the schema-v2 migration
  and their logs contain no handoff entries. Git could see the field, but `ball` is not
  in the backfill contract agreed with task-213, and widening it unilaterally is exactly
  the coordination that contract exists to prevent. Recorded as a candidate, not done.
- **22 tasks** have `lifecycle`/`outcome` that git could supply and must not — see Rule B
  below.

Anything still unreconstructable gets **one `import` event carrying the true values**,
marked `source='reconstructed'` so its timestamp reads as an upper bound. No task is left
with a history that contradicts its own row.

### The two rules the git backfill obeys

Both were measured on task-213 and both are load-bearing. Getting either wrong produces a
chart that is wrong without being visibly broken.

**Rule A — clamp a backfilled creation to the earliest evidence.** Git records when a
change was *committed*, which postdates it: three tasks' logs record a close twenty hours
before git first sees the file. Taken literally the backlog series opens at **−3**. A
backfilled creation is timestamped `min(commit time, the record's own created, its first
log entry)`.

**Rule B — backfill only the axes the log does not record.** Backfilling everything git
can see double-counts every close: measured at `SUM(open_delta) = 69` against 125 open
tasks, a 45% error with no hint in any query plan. Git is authoritative for `priority`,
`parent` and `archived`; `queue_position` takes both, de-duplicated against a native move
within five minutes; `lifecycle` and `outcome` never come from git.

Two further traps found while implementing it:

- **A missing `-` line is not evidence of a creation value.** `archived:` and `parent:`
  did not exist until the schema-v2 migration added them to every record, so each arrives
  as an addition with nothing on the left, on a file that is months old. Treating those as
  creation values silently backdates them and loses 39 real parent changes.
- **Git emits a local offset and the manager emits `Z`.** Storing both misorders events
  under a string sort *and* puts changes in the wrong day bucket. Everything is
  normalised to UTC on the way in.

**The sequencing constraint that is fatal to get wrong, and belongs to task-311:** the
backfill reads the git history of the task files, so it must run, and be verified, before
those files are retired. Retire first and the evidence is gone permanently.

### Coverage

`project.history_baseline_at` and `history_baseline_kind` mark the boundary, and
`task_event.source` distinguishes `native` (written by a verb, exact) from
`reconstructed` (replayed, an upper bound) from `backfilled` (from git). That is what lets
the analytics page render "we do not know" instead of zero.

`project.reporting_tz` holds an **IANA zone name**, never a fixed offset: `America/Chicago`
is correct all year and `-06:00` is correct for half of it. SQLite cannot resolve zone
names, so day bucketing happens in Python.

## 5. Concurrency and durability

One process opens the database and one connection writes; readers get their own
connections and WAL means they never block. Every write is `BEGIN IMMEDIATE`, which takes
the write lock up front rather than upgrading mid-transaction — the classic `SQLITE_BUSY`
deadlock, and the same read-then-decide race `mutate_task` exists to prevent.

`synchronous=FULL`, not the usual WAL recommendation of `NORMAL`: `NORMAL` can lose the
last transaction on power loss, and at a few writes a minute an fsync per commit is
invisible while a lost handoff is expensive.

The write context manager is **reentrant**, so a verb calling another verb joins the outer
transaction. Without that, "state, history and the outbox row commit together" would hold
only when a verb was called directly.

## 6. Idempotency and webhooks

The `operation` ledger is **project-scoped rather than task-scoped**, which fixes audit
**P3** twice: finding a prior operation stops being `O(corpus × log)` and becomes an index
probe, and a bare create with no log entry becomes idempotent, which it was not.

A replay returns the current task plus the revision the original produced. Audit **F5**
observed that "replays the original result" was not what happened; this makes the contract
match the behaviour rather than the other way round. Storing the full original response
per operation was rejected — 1,752 operations × a whole task document is roughly doubling
the store to serve a field nobody reads.

Webhooks go through `webhook_outbox`, written **in the same transaction as the state
change**. A replayed operation short-circuits before the transaction body and so enqueues
nothing (audit **F3**), and a crash between commit and send no longer loses the
notification (task-047).

## 7. Backup and restore

`VACUUM INTO`, never a file copy: with WAL on, the database file is half the story and a
copy taken during a write is corrupt in a way that only shows up when it is needed.
Measured at **68 ms over a 12.6 MB store while a writer was committing continuously**.
Because blobs are in the database, the snapshot is the whole backup.

The snapshot and the manifest describing it are taken under **one** hold of the write
lock. Splitting them lets a write land in between, and the manifest then describes a
database one row ahead of the file beside it — which surfaces as a restore refusing a
snapshot that is fine.

`verify()` opens a snapshot read-only, in isolation, and checks `integrity_check`,
`foreign_key_check`, the manifest counts, and the backlog invariant. `restore()` runs all
of it and **refuses** rather than proceeding, moving aside whatever it replaces.

## 8. Upgrading, and how a v4 would be applied

Two version axes, deliberately independent:

- the **document** schema, `schema: 2`, stamped on every record and exported file;
- the **physical** schema, in `PRAGMA user_version`, managed by
  `src/agentjobs/sqlstore/migrations.py`.

Coupling them was rejected: a physical migration must not invalidate every exported
document, and the two change for unrelated reasons.

Migrations are numbered `.sql` files, forward-only, one transaction each — including the
ledger row, because a version bump without the row explaining it is a database nobody can
audit. On open: equal, do nothing; lower, take a `VACUUM INTO` snapshot first and then
apply; **higher, refuse to start and say so**. An old binary meeting a newer database is
the stale-server hazard this repository already documents, and declining is the same
instinct as the source-root check in `agentjobs serve`.

## 8a. The content the import will not accept

The import is the last mechanical place a content rule can be enforced before a record
becomes durable and queryable, and one rule is enforced there: a task record states what
a person meant, not the words they used. The rule and its detector are described in
[the workflow guide](agent-workflow.md#paraphrase-a-person-never-quote-them); what
belongs here is what the boundary does with a hit.

**It refuses, and writes nothing** -- `QuotationPolicyError`, raised inside the write
transaction and before a single task row is inserted, so the rollback takes even the
quarantine rows the same pass may have added. "Nothing was imported" is then literally
true of the store, which makes a re-run after the fix a clean re-run rather than a
repair.

**That is deliberately not §3's answer** to a record it cannot parse. Quarantine keeps
the live tables provably valid while the bad record stays inspectable, which is exactly
right when the problem is the *shape* of a record -- and exactly wrong when the problem
is its text, because `import_quarantine.raw_text` holds the whole file. Quarantining a
record for its content would put the content in the database in the same act that
claimed to keep it out.

The cost is that a false positive stops a cutover. Three things bound it: the same
detector fails the gate over `tasks/`, so a record reaching an import has already passed
the check on its way into `main`; `agentjobs redact` makes a real hit a one-command fix;
and `run(enforce_quotation_policy=False)` lets an operator who has read the hits proceed
anyway, with `ImportReport.render()` saying the policy was off and naming every region it
let through. The refusal and the report name regions and tone groups, never the quoted
text.

## 9. The two worlds a project can be in

`agentjobs storage status` prints one line per project, with both counts, because the
asymmetric states are the informative ones: rows and no files means the records have been
retired from the checkout, files and no rows means no cutover has happened, and both means
a migrated project whose old directory is still on disk.

### `files` — records are YAML in the repository

The original design, and still the default. Two consequences follow from it, and they are
the reason the other world exists:

- **The dashboard reads one working tree**, so a record committed to a feature branch is
  invisible to the person it is addressed to: they open the React app, see the task still
  `ready`, and conclude nothing is waiting for them. That is why
  [ENGINEERING.md](../ENGINEERING.md#where-task-records-live-and-whether-you-commit-them)
  requires records to be committed to `main` and never to a branch. Observed 2026-08-11,
  repeatedly, before the cause was understood.
- **A record and the code it describes are not one atomic commit.** Checking out an old
  revision does not show you the task state as it was then; `main`'s history has it.

### `sqlite` — records are rows beside the server

The database is machine-level, outside every checkout, so:

- nothing you do to a task dirties a working tree, and there is nothing to commit;
- every worktree and every branch sees the same backlog, including a branch with no
  records on disk at all;
- the dispatch gate's clean-tree check stops excusing the directory AgentJobs was
  dirtying itself, which is coverage task-182 had to give up;
- and the records stop travelling with a clone, which is the trade: a fresh clone on
  another machine has the code and not the backlog.

## 10. Cutting a project over

**Quiesce first.** Stop the server and stop any agent that writes. `storage cutover`
refuses while it can see a server listening, which is the part that can be checked rather
than assumed; the rest is yours.

```bash
agentjobs storage status                     # where are we now
agentjobs storage preview --project <id>     # the real import, against a throwaway database
agentjobs storage cutover --project <id>     # back up, import, verify, then switch
```

`preview` runs the import and writes nothing that survives the call, which is where a
malformed record, a quoted remark or a history that will not reconcile is found. The
cutover then does five things in one order that matters:

1.  **Back up** the existing database, if there is one, with its manifest.
2.  **Import** — one transaction, each record inside its own savepoint, so an interruption
    leaves nothing and a record that fails halfway leaves no partial row.
3.  **Verify** field by field against every readable file, plus the backlog invariant and
    the attachment blobs. A row count agreeing with a file count proves almost nothing.
4.  **Record** the switch in `~/.agentjobs/storage.yaml` — last, because it is what every
    client reads to decide where to look.
5.  **Report** everything, including anything quarantined.

A verification that does not pass **switches nothing**: the project stays on its files and
the import stays in the database for inspection. Fix the cause and re-run with `--replace`,
which empties the project inside the same transaction that re-fills it. A re-run without it
is refused, because a second import over a completed one writes the reconstructed history
twice and doubles every event with nothing raising.

**`--backfill-git` is on by default here and nowhere else**, and this is the one-shot part:
the backfill reads the git history of the task files, so it must run before those files are
retired. Retire first and the evidence is gone permanently (§4).

### The backup enrolment checkpoint

**Before you rely on the store, enrol it in whatever backs this machine up.** The database
is now the only copy of the reconstructed and backfilled history — the files it was built
from can be recovered from git, and the history mined out of them cannot be rebuilt once
they are retired.

```bash
agentjobs storage backup --into <path>   # VACUUM INTO + manifest, safe while serving
agentjobs storage verify <path>          # opens it read-only and checks it is restorable
agentjobs storage restore <path>         # refuses rather than proceeding if it does not verify
```

`backup` verifies what it just wrote, so a snapshot that cannot be restored is reported at
the moment it is taken rather than at the moment it is needed. `restore` moves aside
whatever it replaces, so restoring the wrong snapshot is itself recoverable.

## 11. Retiring the files, and going back

**Retirement is a separate, deliberate step, and it is not automatic.** After the cutover
the old directory is still on disk: harmless, because nothing reads it, and useful, because
it is what a rollback would otherwise have to reconstruct. Retire it when you are satisfied
the store is right — the dashboard read, the CLI used, a backup taken and verified:

```bash
agentjobs storage export <somewhere outside the repo>   # keep a copy you can read
git rm -r --cached <the records directory>              # stop tracking them
```

Do it as its own commit, and keep the export: git history holds the files, and an export is
what a person reads without checking out an old revision.

**Rollback works before and after that step**, and in both directions it preserves what was
written since the cutover:

```bash
agentjobs storage rollback --project <id>
```

It exports the store's **current** state into the recorded source directory and then points
the project back at its files. Exporting the pre-cutover snapshot instead would be a
rollback that silently discarded a day's work, which is why the order is export-then-switch.
The database is not deleted: a rollback is a decision that can itself be wrong.

**An export is an interchange artifact, never a mirror.** Nothing calls it on a write and
nothing commits what it produces. A YAML copy maintained automatically beside the database
would be the second authority this migration removed.

## 12. What this does not do

- **It does not remove code worktrees.** Those isolate *code*, and that argument is
  untouched.
- **The dispatch family still opens the store directly.** Every other CLI verb is a
  service client; dispatch is a stated exception with its reasoning in
  `store_factory.dispatch_manager_for` and on task-311.
- **`ball` history is not backfilled**, per §4.
