# Auditor 07 — Schema, validation, and record integrity

**Scope:** `models_v2.py`, `validation.py`, `schema_tolerance.py`, `record_check.py`,
`quotation.py`, `migration/`, `migrate_schema.py`, `docs/task-schema.md`, `docs/schema/`,
`docs/schema-design.md`, plus the SQL store's DDL and write path where the invariants now
live (`sqlstore/migrations/001_initial.sql`, `sqlstore/store.py`).

**Method.** Main clone at `096f33ae`, read-only. Three throwaway stores in the job scratch
directory (`SqlTaskStore(open_database(<scratch>/sandbox.db), "sandbox")`, never the
registry). The live database was opened only with `?mode=ro`. `agentjobs storage export`
was run once, into scratch, to get a file corpus `validate` can read. Focused pytest over
the six test modules in scope. Nothing on 8876 was touched; no server was started.

**Two records shaped this file.** Auditor 02 independently found finding 1 below and I
confirm it with my own probe rather than restate theirs; auditor 08 covers the
`update_task` allowlist from the manager's side and I cite them where we overlap.

**Headline.** The state axes are enforced at the SQL layer as CHECK constraints and a
unique queue index, and every pairwise rule the last audit listed holds when written
through the store — probed, not read. What has moved is *where the holes are*: the
redaction verb, the one write allowed to change a log entry, does not reach the row; the
enum vocabularies now exist in three copies with nothing pinning the SQL one; and the
integrity tool that used to be red on this repository's corpus is now unable to read it
at all, and is redder than before on an export of it.

| Sev | # | Title | Record |
|---|---|---|---|
| P1 | 1 | `redact` on `log[N].body` records a redaction the store never performs; the test that covers it asserts on the returned object | Confirms auditor 02 F1; **New** test-shape angle |
| P1 | 2 | `agentjobs validate` on this repository's own corpus: 453 problems on an export of 416 records, up from 221; `validate` itself can no longer read the live corpus | Confirms task-262 (worse) |
| P2 | 3 | The CLI now claims "a record that would fail validation cannot be a row"; five of `validate`'s rules have no store-side equivalent, shown by writing violations through the manager | **New** |
| P2 | 4 | Enum vocabularies live in three places (Pydantic, LinkML, SQL CHECK) and nothing pins the SQL copy; the next widening needs a table-rebuild migration and no test fails when it is forgotten | **New**; task-248 itself is fixed (verified) |
| P2 | 5 | `update_task` still has no allowlist: `queue_position`, `archived`, `ball_prompt`, `log=[]`, `lifecycle` all land; `log=[]` now desynchronises the denormalised `log_count` column | Confirms task-254; see auditor 08 |
| P2 | 6 | Ball/reason is still untied from lifecycle: draft→`agent/work`, held→claimed, active→`agent/available` all accepted; `manager.py` contains no reference to `HOLD` | Confirms task-259 |
| P2 | 7 | `migrate-schema` re-run still duplicates positions and writes non-canonical bytes; importing that corpus now *quarantines one record and reports success* | Confirms task-272; **New** import consequence |
| P2 | 8 | `TestRealCorpus` in `tests/test_validate.py` is now a permanent skip (directory retired), distinct from task-411's fixture problem; six corpus checks in `test_task_corpus.py` skip as task-411 says | **New** (first half); Confirms task-411 |
| P3 | 9 | Quotation guard: false positives on machine text after a reporting cue and on vulgar identifiers; false negatives on single quotes, un-cued block quotes, and a cue more than 200 characters back; the author-facing tier is wired into MCP only, not the REST routes the GUI uses | **New** |
| P3 | 10 | Dangling context pointers: 143 raw, 55 on open tasks; task-411's figure of eighteen is the test's filtered view | Confirms task-411, extends it |
| P3 | 11 | `closed` + `assignment.owner` still constructible; DDL rule 5a covers only draft/ready | Confirms 2026-08-21 auditor 3 #13 (never filed) |
| P3 | 12 | Doc and help-text drift left by the storage move: `validate` "if a file was shaped by something else", `TaskStorage._write_task()`, `quotations --storage-dir` "defaults to tasks_directory", "fails the gate over `tasks/`" | **New** |
| P4 | 13 | Small things: the audit brief's `agentjobs search` does not exist; `_replace_log`'s docstring is contradicted by `redact`; `actor: ""` accepted | **New** |

Every probe script is in the job scratch directory (`probe_axes.py`, `probe_live.py`,
`probe_migrate.py`); the outputs quoted below are from tonight's runs.

---

## 1. The four axes under the SQL store

The last audit's question was whether states the verbs forbid are constructible through
storage. The answer for rows is different from the answer for files, and better.

**Enforced by the database, verified by running.** `001_initial.sql:88-105` encodes rules
1, 2a, 2b, 3, 4, 5a, 5b and 6 as CHECK constraints, and `ux_task_queue_slot` makes
band-uniqueness a unique index. `save_task()` does not re-validate a `Task` whose
attributes were assigned after construction (`store.py:501-510`; only `mutate_task`
re-runs `Task.model_validate`, `:530`), so I assigned three violations and saved:

```
t.ball = None (open)            -> IntegrityError: CHECK constraint failed: (lifecycle = 'closed') = (ball IS NULL)
t.ball_reason = REVIEW (agent)  -> IntegrityError: CHECK constraint failed: ball IS NULL OR (ball = 'agent' AND ball_reason IN ...)  -- rule 2b
t.assignment.owner = "bot" (ready) -> IntegrityError: CHECK constraint failed: lifecycle NOT IN ('draft', 'ready') OR owner IS NULL
```

So the pairwise rules cannot be bypassed by the one write path that skips the model.
The live store is clean on every relational property I could query read-only
(`probe_live.py`): 416 rows, 0 quarantine, `integrity_check` ok, `foreign_key_check`
empty, 0 dangling dependencies, 0 dangling parents, 0 closed-with-owner, 0 empty actors,
0 duplicate slots, 0 needs cycles, 0 `log_count` drift, `sum(open_delta)=149 =
open tasks 149`. That last equality is the history invariant and it holds.

**Not enforced anywhere, verified by running.** What follows is what the store lets
through when the write goes through the manager. Findings 3, 5, 6 and 11 are the
detail; the table is the summary.

| Invariant | File era (2026-08-21) | Row era (tonight) |
|---|---|---|
| rules 1–4, 6 | model, re-run on write | model **and** CHECK; unbypassable |
| queue uniqueness in a band | `validate` only | unique index; unbypassable |
| `closed ⇒ owner empty` | not enforced | not enforced (5a is draft/ready only) — #11 |
| ball/reason ↔ lifecycle | not enforced | not enforced — #6 |
| `hold` not claimable | not enforced | not enforced — #6 |
| dependency target exists | `validate` only | **nothing** — #3 |
| needs cycle | `validate` only | `_needs_cycles` at selection; nothing at write — #3 |
| context/deliverable path inside repo | `validate` only | **nothing** — #3 |
| category / actor in project vocabulary | `validate` only | **nothing** — #3 |
| log append-only | model (ids) | `_replace_log` never deletes; **never updates either** — #1 |
| log body redactable | `redact` verb (files) | verb runs, row unchanged — #1 |

### Finding 1 — P1: `redact` on a log body does not reach the row

**Verified by running** (`probe_axes.py` section 1, throwaway store, today's code):

```
returned object body: 'The reviewer rejected it.'
reloaded body        : 'The reviewer said "this is garbage, honestly".'
raw row body         : 'The reviewer said "this is garbage, honestly".'
redaction note present: True
```

`manager.redact` (`manager.py:1684-1780`) assigns `entry.body = replacement` on the
in-memory task and appends a `note` with `data.redaction`, then persists through
`_mutate`. `SqlTaskStore._replace_log` (`store.py:727-743`) inserts only entry ids the
database does not have and says so: "entries are never updated or deleted here ... The
exception is an entry whose body changed, **which the manager never does**." The manager
does, in exactly one verb, and it is the verb whose whole purpose is that change. Spec
fields and `title` redact correctly (section 2: `reloaded description: 'He rejected the
idea.'`) because they are columns overwritten by the upsert.

The result is worse than the verb not existing: the record now carries a note saying the
quotation was removed, the redaction is recorded in `operation`, `agentjobs quotations`
would still find the text on the next scan, and anybody reading the note believes the
words are gone. The REST route `POST /{task_id}/redact` (`api/routes/status.py:599-611`)
and the CLI both reach the same method, so every surface is affected.

**Why the suite is green.** `tests/test_quotation.py:314-329`
(`test_it_reaches_a_log_entry_which_nothing_else_can`) asserts on the task object
`manager.redact()` *returns* and never reloads. That is the `Verification` rule in
ENGINEERING.md ("do not set up the state your test is meant to be checking") in a
different costume: the assertion reads the value the code under test just wrote into the
object it was handed. The fix to the test is one line — `manager.get_task(task.id)`
before the assertion — and it fails today. The same test file's
`test_the_record_is_left_in_canonical_form` (`:331-350`) does export the task and would
have caught this if it had redacted a log body rather than `spec.description`.

**Fix.** Auditor 02 proposes an `UPDATE log_entry SET body=?` branch in `_replace_log`
or a dedicated `redact_entry` store primitive; either is right, and the second keeps
`_replace_log` honestly append-only. Add the reload to the test. Then search for other
in-place log edits nothing persists: I found none, but `add_log_entry` with an existing
id would be silently ignored the same way.

### Finding 5 — P2: `update_task` has no allowlist (confirms task-254)

**Verified by running** (`probe_axes.py` sections 3–5). Through `TaskManager.update_task`,
which is what the CLI, `dispatch/finish.py` and any script call:

```
update_task(log=[])            -> ACCEPTED; returned log len 0
                                  stored entries 2, log_count column 0, reloaded log len 2
update_task(lifecycle="ready", ball="agent", ball_reason="available", outcome=None) on closed
                               -> ACCEPTED lifecycle ready pos 400
                                  log types: ['transition', 'queue_move']   (no transition for the reopen)
                                  events   : [create, close, ('reopen','closed','ready')]
update_task(ball_prompt="smuggled") -> ACCEPTED
update_task(archived=True)          -> ACCEPTED
update_task(queue_position=5)       -> ACCEPTED   (docstring manager.py:1173: "The allowlist never gains queue_position")
update_task(id="task-999")          -> ignored (id is pinned back, :1191)
```

Two things are new since August. First, `log=[]` no longer erases the log — the store
refuses to delete — but it writes `log_count = 0` beside two surviving rows.
`001_initial.sql` introduces those denormalised columns with "written in the same
transaction as the thing that changes them, so they cannot drift the way a rebuilt cache
would"; this is the drift. `log_count` is what the list endpoint and the analytics read
instead of counting. Second, the reopen is now visible in `task_event` as `reopen` even
though the task's own log still has no `transition` entry for it, so the history table
and the record disagree about whether the axis move was explained.

Auditor 08 (finding on `update_task`, their §"Manager-level patch") reaches the same
place from the queue side, including the `IntegrityError` that now escapes when a patch
collides with the unique index. `docs/task-schema.md:616-624` states the gap accurately
and names task-254; the record is honest, the code is unchanged.

**Fix.** As task-254 says: the allowlist as one module-level set in the manager, consulted
by the API and MCP rather than copied; a `reopen` verb that writes its transition. Until
then `log`, `lifecycle`, `ball*`, `outcome`, `archived`, `queue_position`, `assignment`
refused by name. Add a test that `log_count` equals `COUNT(*)` after every manager verb.

### Finding 6 — P2: ball/reason still untied from lifecycle (confirms task-259)

**Verified by running** (`probe_axes.py` section 8):

```
draft -> agent/work        ACCEPTED: draft agent work  display_status 'In progress'
ready -> agent/hold        ACCEPTED
  claim on held task       ACCEPTED; ball_reason now work
active -> agent/available  ACCEPTED: active, owner bot, display_status 'Ready'
```

`grep -n HOLD src/agentjobs/manager.py` returns nothing: neither `_skip_reason` nor
`claim_task` has heard of the state `docs/task-schema.md:66-67` says is not workable.
The dispatch door still refuses it (`dispatch/auto.py`, `dispatch/guards.py`, not
re-read tonight); the `next`/`claim` door does not.

The live store has no held task tonight, and no draft at `agent/work`, but it does hold
two `ready` tasks at `agent/answer` (task-167-agentjobs-menu, task-063-schema-v2). Those
are legal under the rules as written and are exactly the shape task-259 is about: the
ball says an agent should act, the lifecycle says nobody has claimed it, and `next`
will hand either out as ordinary work with its `answer` ask overwritten by `claim`'s
default prompt.

**Fix.** task-259's rule 7 in the model — which now also means one more CHECK in the
DDL, and therefore a migration (see #4). Decide whether `hold` on a `ready` task is a
wanted state before writing the constraint.

### Finding 11 — P3: closed + owner

**Verified by running** (section 6): `update_task(assignment={"owner": "bot", ...})` on a
closed task → `ACCEPTED owner on closed: bot`. `001_initial.sql:107-108` encodes 5a as
`lifecycle NOT IN ('draft','ready') OR owner IS NULL` and 5b as `lifecycle <> 'active' OR
owner IS NOT NULL`; `closed` satisfies both with any owner. Live count is 0 because
`close_task` clears the owner. Auditor 3 reported this on 2026-08-21 (#13) and it was
never filed. One extra disjunct in 5a and one line in `_check_consistency`.

---

## 2. `agentjobs validate` on its own corpus

### Finding 2 — P1: red, redder than in August, and unable to read the live corpus

**Verified by running.** `agentjobs validate` with no arguments now prints
(`cli.py:180-188`):

> A project's records are rows in its database, so `validate` has no files to read. ...
> The database enforces every consistency rule as a constraint, so a record that would
> fail validation cannot be a row.

So the brief's instruction cannot be followed literally. The nearest thing is the
round trip the tool itself recommends for an import candidate: export, then validate the
export with the project's own `.agentjobs/config.yaml`.

```
agentjobs storage export <scratch>/export --project agentjobs
  wrote 416 task file(s) and 12 attachment(s)
agentjobs validate --tasks-dir <scratch>/export
  ❌ 453 problem(s) across 416 task file(s).
```

| rule | count | 2026-08-21 |
|---|---|---|
| `unknown-actor` | 300 | 124 |
| `unknown-category` | 152 | 84 |
| `absolute-path` | 1 | 0 |
| `non-canonical-serialization` | 0 | 13 |

Unknown actors: `dispatcher` 113, `finisher` 83, `system` 76, `jeff` 10, `Codex` 10,
`Claude` 6, `human` 2. So **272 of 300** are the validator objecting to ids the product
itself writes as (`RESERVED` in `actors.py`, and `system` from the manager), exactly the
mechanism task-262 describes, and the count has more than doubled because every
scripted finish since August wrote `finisher` and `dispatcher` entries. Unknown
categories: 24 distinct values in use against six configured (`architecture` 36,
`reliability` 22, `dispatch` 17, `bug` 16, ...). The one `absolute-path` is task-387
pointing at another project's config file.

The 13 non-canonical files are gone — the export writes canonical bytes — which is the
one number that improved, and it improved because the files no longer exist rather than
because anyone fixed them.

**What this means for the tool.** `validate --tasks-dir` is now documented as the check
to run on "one about to be imported" (`cli.py` help). An export of this project's own
live, constraint-satisfying store fails it 453 times, so the check is either wrong about
what an import candidate must satisfy or the project's config is wrong about its own
vocabulary. Task-262 asked for the decision three weeks ago; it is still an unspecced
draft. The brief said a red integrity tool is a P1, and I agree: a check nobody can run
green teaches people to ignore it, and the paraphrase gate (#9) is downstream of the same
habit.

**Fix.** Task-262's three options still apply, and the numbers say which: treat
`RESERVED` and `system` as known in `_check_taxonomy` (removes 272), then either declare
the categories in use or make the manager enforce the six. Separately, decide whether
`validate` should grow a `--project` mode that runs the relational and taxonomy rules over
rows — see #3.

### Finding 3 — P2: "a record that would fail validation cannot be a row" is false

**Verified by running** (`probe_axes.py` sections 9–10), through the manager, into the
store, with no error:

```
update_task(dependencies=[{"task": "task-998", "type": "needs"}])  -> ACCEPTED: ['task-998']   (no such task)
update_task(spec.context=[{"path": "../../etc/passwd"}])            -> ACCEPTED
add_log_entry(actor="")                                             -> ACCEPTED
create_task(category="not-a-category")                              -> ACCEPTED
update_task(parent="task-999")                                      -> REFUSED (Parent task 'task-999' does not exist.)
```

Of `validation.py`'s nine rules, the CHECK constraints cover `unreadable`, `queue-*`
and `self-parent`; `missing-parent` is refused by `_validate_parent`; **`missing-dependency`,
`dependency-cycle` (at write), `path-escapes-project`, `absolute-path`, `unknown-actor`,
`unknown-category`** have no equivalent for rows. The live corpus happens to be clean on
the relational ones tonight (section 1), which is luck plus care, not enforcement: a
dangling `needs` makes a task permanently unclaimable with nothing in any listing saying
so (`validation.py:322`, the message the file check would have printed).

The message at `cli.py:184` should say which rules the database enforces and which
nothing does. Whether a store-mode `validate` is worth building is a decision; that the
sentence is currently wrong is not.

### Finding 8 — P2: the two corpus test classes, and why they are different problems

**Verified by running:** `pytest tests/test_schema_skew.py tests/test_quotation.py
tests/test_record_check.py tests/test_validate.py tests/test_migrate_schema.py
tests/test_task_corpus.py -q -rs` → **173 passed, 8 skipped**.

- `tests/test_task_corpus.py:72,83` — six skips, "this repository's own backlog could not
  be read from either backend". This is task-411, confirmed on `096f33ae`.
- `tests/test_validate.py:707` — two skips, "this repository's records have been retired
  from the checkout". This is **not** task-411. `TestRealCorpus.corpus_or_skip()` looks
  for `tasks/agentjobs/*.yaml`, which task-380 deleted, so the class can never run again
  on this repository regardless of any fixture. It is the file-era check whose gate-green
  filtering task-262 documents, and it is now decoration. Delete it, or port it to read
  `tests/corpus_source.py` like its sibling — in which case it inherits task-411.

`scripts/gate_scope.py`'s claim that pytest guards the corpus is therefore wrong twice
over on today's tree: one class skips for a fixture reason, the other for a structural
one. Task-409 is the third leg of the same problem and auditor 05 owns the gate side.

---

## 3. Tolerance policy

**task-248 is fixed, verified by running.** `models_v2.py:996-1005` now skips rule 2's
membership test when `ball_reason` is a pseudo-member (`self.ball_reason in
BallReason.__members__.values()`), and `tests/test_schema_skew.py:63-69` parametrises
the skewed read over `ball_reason`, `ball`, `lifecycle`, `priority`, `outcome`; the five
cases pass. `priority_rank()` degrades to `len(PRIORITY_RANK)` rather than `KeyError`
(`:1197-1203`). The policy is coherent for the *Python* reader: tolerance is client-only,
`storage` never enters it, and every top-level axis is covered.

### Finding 4 — P2: the third copy of every enum, and nothing pins it

**Inferred from reading, with one script to check the premise.** The SQL DDL enumerates
`lifecycle`, `ball`, `ball_reason` (per holder, `001_initial.sql:99-104`), `outcome`,
`priority`, `posture`, `log_entry.type` (all eleven, `:207-209`), `task_event.kind`,
`dependency.type`, `acceptance.status`, `deliverable.status`, `branch.status`,
`task_run.mode`, `task_run.trigger`. I compared each list against the Pydantic enum
(`probe_live.py` §DDL): **every list agrees tonight**, so this is the rule-of-three
check, not a defect that has fired.

What would catch it firing: nothing. `grep -rn "001_initial\|ball_reason IN\|CHECK ("
tests/` finds only `test_analytics_contract.py`'s docstring. The LinkML/Pydantic pair
has `TestAgreesWithTheLinkMLSchema`; the SQL copy has no counterpart. The failure mode is
the exact shape of task-248 with the roles reversed: add `BallReason.PAUSED` to the
model, ship, and the first handoff to it fails with `IntegrityError: CHECK constraint
failed ... rule 2b` — on the service this time, not the client, and for every process.
SQLite cannot `ALTER` a CHECK, so the migration is a table rebuild, and `migrations.py`
is forward-only with no example of one yet.

**Fix.** One test that parses each `IN (...)` list out of the applied schema
(`sqlite_master`, not the `.sql` file, so a later migration counts) and asserts equality
with the enum; and a sentence in `docs/task-schema.md`'s "Widening an enum" section
saying a widening is now a physical migration. Consider whether `ball_reason`'s
per-holder table (`BALL_REASONS`) should be the *only* copy and the CHECK generated from
it at migration time.

---

## 4. The quotation guard

**Works as specified, verified by running.** `agentjobs quotations` over the live store:
`✓ 416 task record(s) scanned; no quoted remarks.` (exit 0). The importer refuses before
the transaction opens (`importer.py:236-238`), and `record_check` warns per verb with the
region scope narrowed to what that verb wrote (`record_check.py:246-262`).

### Finding 9 — P3: false positives, false negatives, and a tier that is missing where humans write

Probes (`probe_live.py` §quotation), each a single `scan_text` call:

| input | fires? | note |
|---|---|---|
| `The server said "connection refused: damn socket already in use".` | **yes** (vulgarity) | machine text; the docstring predicts this class |
| `The traceback described "ridiculous recursion depth" in the parser.` | **yes** (dismissal) | `described` is a cue; the quote is an error string |
| `The fixture is "the crap_filter module".` | **yes** (vulgarity, no cue needed) | an identifier |
| `The owner said 'this is garbage, honestly'.` | no | single quotes are not a shape |
| `> this ordering is nonsense` (block quote, no cue) | no | dismissal needs a cue; block quotes rarely have one |
| cue, then 210 characters, then `"this is garbage"` | no | `_ATTRIBUTION_WINDOW = 200` |
| `"NOT ACCEPTABLE here"` after a cue | no | two-word shout; documented limit |
| `"DO NOT MERGE THIS"` after a cue | **yes** (emphasis) | a three-word status shout |

None of these is a bug against the module's own docstring, which is unusually honest
about its limits. Two are worth a task anyway: single-quoted speech is how a lot of
agent-written prose quotes people, and the block-quote shape needs a cue that a block
quote almost never carries (the `>` *is* the attribution). Both are false negatives in
the direction the rule exists to catch.

**The tier that matters is not where humans write.** `check_record()` is called from one
place: `mcp/mutation_tools.py:492`. `grep -rn check_record src/agentjobs/api/` is empty.
The REST routes — which the React app and `TaskClient` use, and which is where a person
writes a handoff or a note — return no `warnings`. The module docstring describes three
tiers "escalating with how durable the damage would be"; the first tier exists for agents
on MCP and not for the person the rule is about. Inferred from reading.

**Stale claims.** `quotation.py:44-46` and `docs/storage-sqlite.md:328-330` both say the
detector "fails the gate over `tasks/`" so "a record reaching an import has already
passed the check". `tasks/` is gone (task-380) and the replacement check skips
(task-411), so the import refusal is currently the only enforced tier, and the argument
that bounds its false-positive cost ("already passed on its way into main") is not true.

### What `redact` leaves behind

For `title`, `ball_prompt` and `spec.*`: the replacement text, one `note` entry with
`data.redaction = {field, reason, removed_chars}`, and an `operation` row — verified
(section 2 of `probe_axes.py`). The character count is the only trace of the removed
text; the FTS row is rebuilt from the new text (`_reindex`, `store.py:857-875`) so search
does not find it either. `task_event` records the write as `update_content`.

For `log[N].body`: everything above **plus the original text, unchanged** (#1).

What it does not leave: nothing records the *old* revision anywhere, so a redaction is
unrecoverable by design, which the docstring says and which is right. But nothing
prevents redacting the redaction note itself (`log[<note id>].body` is addressable), and
a second redaction of the same region records `removed_chars` of the *replacement*.
Neither is a defect; both are worth a sentence in the doc.

---

## 5. Migration re-runs

### Finding 7 — P2: task-272 still true, and the import now hides it (confirms task-272)

**Verified by running** (`probe_migrate.py`: three v1 files; convert one; convert the
directory):

```
pass 2: Converted 2, Skipped 1 (task-901-x.yaml ... already declares schema 2)
positions after pass 2: {'task-901-x': 100, 'task-902-x': 100, 'task-903-x': 200}
validate_corpus rules: [non-canonical-serialization x3, queue-duplicate x2]
```

Unchanged from August: `_assign_queue_positions` plans over the records this run
converted and never sees the sibling it skipped. The migrator still writes with
`destination.write_text(yaml.safe_dump(...))` rather than the canonical dump, so every
migrated file is `non-canonical-serialization` before anyone touches it.

**The ground shift.** In August the corrupt queue was the end of the story, because the
files were the store. Tonight the files are an import candidate, so I imported them:

```
imported 2 tasks, 4 history events ...
backlog invariant: sum(open_delta)=2 open_tasks=2 OK
quarantined 1 unreadable records:
  task-902-x.yaml: UNIQUE constraint failed: task.project_id, task.priority_rank, task.queue_position
```

The unique index does its job; the importer does its job (a savepoint per record,
`importer.py:396-408`); the invariant reconciles. And one of three tasks is now in
`import_quarantine` labelled "unreadable" for a reason that has nothing to do with the
file, after a run whose first line says it imported successfully. The CLI's `storage
import` path "verifies field by field" per its help; whether it exits non-zero on a
quarantine I did not check (auditor 02's territory). A duplicate position is a
`queue-duplicate` finding the migrator could have refused to write, and instead it is a
missing task an operator has to notice in a report.

**Fix.** task-272's, unchanged: feed the existing v2 siblings to `plan_queue_migration`,
and write through the store's canonical dump. And have `storage import` refuse — or at
least exit 1 — when a record is quarantined for a *constraint* rather than for being
unparseable, because that is a corpus defect and not a file defect.

The last audit's #9 (`TestTheRealCorpus` skipping every file) is resolved by deletion:
`tests/test_migrate_schema.py:406-412` records why. Refutes the third acceptance
criterion of task-272 in the good direction.

---

## 6. Corpus and doc integrity

### Finding 10 — P3: dangling context pointers (confirms and extends task-411)

**Verified by running**, read-only over the live store, resolving each
`spec.context[].path` against the repository root with no filtering: **143 pointers at
paths that do not exist, 55 of them on open tasks.** Most-cited targets:
`src/agentjobs/storage.py` (15, deleted by task-402), then five to seventeen records each
pointing at `tasks/agentjobs/task-NNN.yaml` files task-380 removed.

Task-411 says eighteen. The difference is scope, not disagreement: the test it describes
(`tests/test_task_corpus.py:168-190`) skips URLs, glob-like paths, pending deliverables,
absolute paths, and anything git-ignores, and I applied none of those filters. The
`tasks/agentjobs/…` class is new since task-411 was written — those pointers were valid
until task-380 merged the same evening — and it is the larger class. The decision
task-411 asks for (is a closed task's pointer a promise or history?) decides whether the
number is 55 or 143.

### Finding 12 — P3: doc and help text the storage move left behind

Inferred from reading; each line quoted was checked against the code tonight.

| where | says | reality |
|---|---|---|
| `docs/task-schema.md:15` | "run `agentjobs validate` if you ever suspect a file was shaped by something else" | there are no files; `validate` refuses without `--tasks-dir` |
| `docs/task-schema.md:84` | uniqueness "the model cannot check and `agentjobs validate` does" | the unique index does; `validate` cannot see rows |
| `docs/task-schema.md:542-548` | "## How files are written — `TaskStorage._write_task()` dumps with…" | `TaskStorage` no longer exists (`taskfiles.TaskFileCorpus` and `SqlTaskStore` do) |
| `cli.py:3445` (`quotations --storage-dir` help) | "Defaults to the project's configured tasks_directory" | the default reads the store (`:3460-3464`); the help describes the file era |
| `validation.py:3` | "The backstop for everything the Codex hook cannot see" | the hook is `validate --install-hook`, and there is nothing under `tasks/` for it to check |
| `store.py:731-733` | "an entry whose body changed, which the manager never does" | `redact` does (#1) |
| `quotation.py:44-46`, `storage-sqlite.md:328-330` | "fails the gate over `tasks/`" | retired (task-380), replacement skips (task-411) |

The rest of `docs/task-schema.md` is in better shape than in August: every row of the
last audit's drift table (#11) has been corrected, and the `update_task` paragraph now
states the gap and names task-254. `docs/schema-design.md` I read only where cited.

### Finding 13 — P4: small things

- The audit brief and runbook say to search the backlog with `agentjobs search`; no such
  command exists (`agentjobs --help`). I used a read-only SQLite query. Auditor 14 may
  want this for the "was the brief followable" question.
- `add_log_entry(actor="")` is accepted (section 10). `LogEntry.actor` has no
  `min_length`; the DDL has `NOT NULL` but not `<> ''`. Live count 0.
- `create_task(category="not-a-category")` is accepted; `docs/task-schema.md:85` now says
  so explicitly, which is the doc being fixed to match the code rather than the reverse.
  Fine, but it is the reason 152 of #2's findings exist.

---

## What I did not get to

- **The dispatch door for `hold`** (`dispatch/auto.py`, `dispatch/guards.py`). I cite
  August's reading; I did not re-run it. Auditor 04.
- **`storage import`'s exit code when a record is quarantined for a constraint** (#7).
  I ran the importer class, not the CLI. Auditor 02.
- **`_check_paths` on Windows junctions** — the Obsidian vault junctions into
  `C:/projects`; whether `resolved.parents` sees through them was not tested, same as
  last time.
- **The API's `TaskUpdateRequest` allowlist** as the sole guard on #5 over HTTP —
  auditor 08 reports it holds; I did not probe HTTP at all.
- **Whether any client re-posts a tolerated pseudo-member**, and `attachments.py`.
  Unchanged from August's list.
- **`docs/schema/` generated tree freshness** (August #10). Not re-checked; `git log`
  shows no schema-tree commit since `a79bfc5`, and `models_v2.py` gained `posture`,
  question/answer payloads and the dispatch-walk payloads since — so the generated docs
  are probably stale, but I did not diff them. Inferred, not verified.
- **`record_check`'s summary and default-prompt conditions** over the live corpus
  (`scripts/corpus_stats.py` — task-396 says it reads the retired directory).
- **`migration/` (the Markdown importer)** beyond confirming it writes through
  `storage.save_task` (`migration/__init__.py:122`).

## Questions for other auditors

- **Auditor 02 (SQL store):** we agree on #1. Does `agentjobs storage import` exit
  non-zero when a record lands in quarantine (#7)? And is `log_count` read anywhere a
  user sees — the list page, the analytics — such that #5's drift would show?
- **Auditor 08 (manager/queue):** `update_task(queue_position=5)` is accepted by the
  manager against its own docstring (#5). Does anything in `queue.py` assume the number
  can only come from `_place`?
- **Auditor 04 (dispatch/finish):** `finisher` and `dispatcher` are 196 of the 300
  unknown-actor findings (#2). Is there a reason those are not `RESERVED`-visible to
  `load_actors`, or is it simply task-262 waiting?
- **Auditor 03 (authorization):** `POST /{task_id}/redact` exists over HTTP. Which
  capability gates it, and can a run redact a task that is not its own? The verb rewrites
  history, so it is the one I would want narrowest.
- **Auditor 05 (gate):** with `TestRealCorpus` structurally skipped (#8) and
  `test_task_corpus` fixture-skipped (task-411), is there any stage of today's gate that
  reads a task record at all? `roadmap` reads the store; does anything else?
- **Auditor 09/frontend, if any:** does the React app call `check_record` warnings from
  anywhere, or does a human handing off in the browser get no paraphrase warning (#9)?
- **Auditor 14 (meta):** `agentjobs search` in the brief (#13), and whether the
  "run validate on its own corpus" instruction should be rewritten for a row store.
