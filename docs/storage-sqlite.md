# Authoritative SQLite task storage

The store built on task-273. This document is the *why*; the schema itself is
`src/agentjobs/sqlstore/migrations/001_initial.sql`, and the boundary the store
satisfies is `src/agentjobs/storage_protocol.py`.

**Status: built, and the only storage there is.** Task-273 built the store, task-311 built
the import around it, and task-402 deleted the file backend it was once an alternative to.
Every project's records are rows in a SQLite database of its own beside the server;
`agentjobs storage status` prints the file each project is served from, counted rather than
inferred.

## What a record still is

The original pitch for one YAML file per task was that a record was a thing you could read,
move and review with no tool at all. Three of those properties survive the move and one
does not, so here is each of them plainly.

**You can still read a record without AgentJobs.** `agentjobs show <id>` prints the whole
record as JSON and the REST API serves the same, but neither is the floor: the store is one
SQLite file, a format with a published specification and a reader in every language, so
`sqlite3 ~/.agentjobs/databases/<project>.db` answers a question no command was written for.
What is gone is reading a record in an editor that had never heard of the tool, and it is
worth being honest that this is a real loss and not only a rearrangement.

**You can still get your records out**, in the same format they went in:

```bash
agentjobs storage export <destination> --project <id>
```

That writes the store's current state as task YAML with its attachment bytes — an
interchange artifact, not a mirror. Nothing calls it on a write and nothing commits what it
produces, because a YAML copy maintained automatically beside the database would be the
second authority this migration exists to remove.

**Reviewing a task record in a pull request is gone, deliberately.** Task state is not code:
a diff of `ball_reason` changing from `work` to `review` is a fact about the work rather
than a proposal about it, and there was nothing for a reviewer to approve or reject. What
it cost was concrete and paid daily — a handoff committed to a feature branch was invisible
to the person it was addressed to, because the dashboard reads one working tree, and that
was observed repeatedly through 2026-08-11 before the cause was understood. The log is
still an append-only history of who changed what and why; it is queried rather than
diffed.

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
| One database or one per project | **One**, with `project_id` on every key — *reversed by task-400, see §11* |
| Attachments | **Blobs in the database** |
| Pre-import history | **Reconstruct, reconcile, and mine git** |
| Coordination with the analytics page | **task-213 runs first**, without a blocking edge |
| Search | **FTS5** |

The reasoning behind each, with its rejected alternative, is on task-273. What follows
is what those answers produced.

**The first answer was reversed on 2026-09-09 (task-400) and the row is kept so the
reversal is legible.** One file for every project made any decision about one project's
records a decision about all of them, and that cost outweighs the convenience of a single
file. `project_id` on every key stays, and is now what makes a project's rows separable:
§11. Two projects can still share a file where an operator asks for it.

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

That is enforced twice rather than documented once (task-371). `sqlstore/reporting_tz.py`
checks the value at `ensure_project`, which is the one door it comes through today, and
says in the refusal *which half of the year* an offset is wrong for — the person who typed
one typed it because task-273's first draft offered `date(ts, :tz)`, and needs the argument
rather than a validation error. Migration `002` carries the shape half of the same rule as
a trigger, for the writer that does not come through that door. A wrong value here does not
fail: it silently misfiles late-evening work by a day for six months, which is why it is a
constraint and not a convention.

**Set it at import** — `agentjobs storage import --reporting-tz America/Chicago` — because
that is currently the only setter. A project cut over without the flag holds `UTC`, and
changing it afterwards means SQL.

`task_event.mechanical` marks a bulk renumber: a commit that rewrote `queue_position` on
more than one task with no per-task log entry, which is a side effect of moving one task
rather than a decision about the others. The analytics page keeps those out of its activity
series. Only positions are ever marked — a commit changing `priority` across several tasks
is a grooming pass, and those are decisions.

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

The cost is that a false positive stops an import. Three things bound it: the same
detector fails the gate over `tasks/`, so a record reaching an import has already passed
the check on its way into `main`; `agentjobs redact` makes a real hit a one-command fix;
and `run(enforce_quotation_policy=False)` lets an operator who has read the hits proceed
anyway, with `ImportReport.render()` saying the policy was off and naming every region it
let through. The refusal and the report name regions and tone groups, never the quoted
text.

## 9. Importing an existing corpus

**A new project needs none of this.** `agentjobs init` registers the project, creates its
database, and makes no tasks directory (task-399), so a fresh install begins where a
migrated one ends up. This section is for somebody arriving with a corpus of task YAML
written by an older version, and it runs **once, in one direction**.

What the numbers mean first. `agentjobs storage status` prints one line per project with a
row count and a file count, because the asymmetric states are the informative ones: rows
and no files is the settled state, and rows plus files is a project whose old directory is
still on disk awaiting section 10. A file count on its own no longer describes anything the
application will read.

**Quiesce first.** Stop the server and stop any agent that writes. `storage import` refuses
while it can see a server listening, which is the part that can be checked rather than
assumed; the rest is yours.

```bash
agentjobs storage status                     # where are we now
agentjobs storage preview --project <id>     # the real import, against a throwaway database
agentjobs storage import --project <id>      # back up, import, verify, then record
```

`cutover` is the old name for that third command and still works, from when it named a
change of backend rather than an import.

`preview` runs the import and writes nothing that survives the call, which is where a
malformed record, a quoted remark or a history that will not reconcile is found. The
import then does five things in one order that matters:

1.  **Back up** the existing database, if there is one, with its manifest.
2.  **Import** — one transaction, each record inside its own savepoint, so an interruption
    leaves nothing and a record that fails halfway leaves no partial row.
3.  **Verify** field by field against every readable file, plus the backlog invariant and
    the attachment blobs. A row count agreeing with a file count proves almost nothing.
4.  **Record** the project's database in `~/.agentjobs/storage.yaml` — last, because it is
    what every client reads to decide where to look.
5.  **Report** everything, including anything quarantined.

A verification that does not pass **records nothing**: the entry is not written and the
import stays in the database for inspection. Fix the cause and re-run with `--replace`,
which empties the project inside the same transaction that re-fills it. A re-run without it
is refused, because a second import over a completed one writes the reconstructed history
twice and doubles every event with nothing raising.

**Read the quarantine list before you read the verdict.** `VERIFIED` is a claim about the
files that could be **read**: a record the import could not parse is quarantined and then
compared against nothing, so a corpus can verify while part of the backlog stays behind.
Since task-378 the verdict says so itself — `VERIFIED -- but 7 record(s) never reached the
store` — because the preceding lines naming them are not what an operator remembers.

The likely cause, and the one this repository met, is an older record that is open with no
`queue_position`: the rule that open work must have a place in line is younger than some of
these files, and such a record does not load at all. Three of the four projects imported on
2026-09-08 were in that state, 17 open tasks between them.

```bash
agentjobs queue check            # in that project's directory; names each one
agentjobs queue repair           # gives every open task a place, and prints its guesses
```

Repair, commit the records, and preview again before importing. The positions it
assigns are guesses and are printed for exactly that reason.

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

## 10. Retiring an imported corpus

**Retirement is a separate, deliberate step, and it is not automatic.** After the import the
old directory is still on disk: harmless, because nothing reads it, and useful, because it is
what a mistake would otherwise have to be reconstructed from. Retire it when you are
satisfied the store is right — the dashboard read, the CLI used, a backup taken and
verified:

```bash
agentjobs storage export <somewhere outside the repo>   # keep a copy you can read
git rm -r --cached <the records directory>              # stop tracking them
```

Do it as its own commit, and keep the export: git history holds the files, and an export is
what a person reads without checking out an old revision.

**There is no way back to a directory of files, and there is no longer anything to go back
to.** `storage rollback` existed while two backends did; task-402 deleted the file backend
and the rollback with it. What is left of it is `storage export`, which still writes the
store's current state as readable YAML — so a corpus can be recovered from AgentJobs, but
not served from a checkout by it. That is what makes this a one-way import rather than a
switch, and it is the reason section 9 tells you to preview and to take a backup before you
rely on the store.

## 11. One database per project

**A project's records live in a file of its own** (task-400). Every project on a machine
used to share `~/.agentjobs/agentjobs.db`, and the cost was that any decision about one
project's records was silently a decision about all of them: publishing a backlog, backing
one up on its own schedule, handing one to somebody else, deleting one. Storage was the
only place where projects shared a container, and AgentJobs already scopes the registry,
the actor vocabulary, dispatch configuration and authorization to a project.

`storage.yaml` gained a per-project `database`, and the top-level key is now a fallback:

```yaml
database: C:/Users/me/.agentjobs/agentjobs.db     # for a project that names none
projects:
  agentjobs:
    backend: sqlite
    cutover_at: '2026-09-07T05:12:00Z'
    source: C:/projects/agentjobs/tasks
    database: C:/Users/me/.agentjobs/databases/agentjobs.db
```

**The whole precedence is `StorageSettings.database_for(project_id)`**: the environment,
then the project's own entry, then the machine's fallback. Nothing else resolves a
database, and `open_database` takes a path rather than defaulting to one — a caller with a
project in hand has to say which project.

- **A new project gets its own file**, `~/.agentjobs/databases/<project id>.db`, written
  into its entry by the import. `--database` names another; `--shared-database` puts it
  in the machine's file. Sharing is a thing an operator asks for rather than what happens
  by accident.
- **A file with only the top-level `database:` key keeps working untouched.** Every
  project in it resolves exactly as before. This change adds a field; it does not require
  one.
- **`AGENTJOBS_DATABASE` still overrides every project**, deliberately. It is the
  one-invocation escape hatch — a test harness, a machine pointed at a copy on another
  volume — and a per-project form would be a variable per project whose failure mode is
  that the one you forgot silently keeps writing elsewhere, which is the divergence
  per-project files exist to remove. To move *one* project, edit `storage.yaml`.

### Moving a project out of a shared database

```bash
agentjobs storage status                    # says which projects share a file
agentjobs storage split --project <id>      # back up, copy, verify, record, then remove
```

Same sequence as the import, and the ordering is the whole of it: back up the source,
copy into a database the command created, verify field by field against the source,
record the new location in `storage.yaml`, and **only then** delete the source rows. The
record is written before the delete on purpose — a crash after it points the project at a
file that holds its records, whereas a delete-first crash points it at one that no longer
does. A verification that does not pass stops before either, leaving both files intact and
the project still served from the shared one.

**What moves is decided by the schema, not by a list.** Every table carrying a
`project_id` column is copied, discovered from `sqlite_master` at run time, so a table
added by a later migration comes along without anybody remembering to update a constant.
A hand-written list would fail by leaving a future table's rows behind in the shared file,
silently. Discovery also excludes the fts5 shadow tables for free — none of them has that
column — while `task_fts` itself does and copies like anything else.

`blob` is the exception, because it is content addressed: the same image attached in two
projects is one row. Only the blobs this project's attachments reference are copied, and
the source drops a blob only when nothing references it any more. Deleting by project
instead would leave the other project's attachment pointing at bytes that no longer exist.

`storage backup` snapshots **every** database on the machine, and `--into` needs
`--project` before it can mean one file. `storage restore` refuses to guess which file a
snapshot replaces when there is more than one: a snapshot is a whole file, and guessing
would put one project's records over another's.

### What is left of the shared file

**`split` never deletes the database it moved a project out of**, and once the last
project has left, that file is still there holding nothing. This is deliberate on both
counts: a command that removes rows should not also remove the container they were in,
and the machine's top-level `database:` key still names it as the fallback for a project
whose entry names no file of its own.

So an operator who splits every project ends up with an empty database beside the
registry, at whatever size it had grown to — SQLite does not give the pages back. It is
safe to remove once `agentjobs storage status` shows every project served from its own
file and a backup of it has been kept, and removing it is a separate, deliberate act
rather than something a split does on your behalf. Until then, the honest description of
it is a fallback nothing currently resolves to.

**Check what your backups now cover.** `storage backup` snapshots the database of every
project the configuration names, so after a split it writes one snapshot per project into
`databases/` and stops covering the old shared file entirely. A backup routine that names
`~/.agentjobs/agentjobs.db` by path keeps succeeding against a file that no longer holds
any records, which is the failure mode worth looking for the day a split is run.

## 12. What this does not do

- **It does not remove code worktrees.** Those isolate *code*, and that argument is
  untouched.
- **The dispatch family still opens the store directly.** Every other CLI verb is a
  service client; dispatch is a stated exception with its reasoning in
  `store_factory.dispatch_manager_for` and on task-311.
- **`ball` history is not backfilled**, per §4.
