# 02 — SQLite storage: the new source of truth

Auditor 02, Big Dawg Audit II, night of 2026-09-11. Scope: `src/agentjobs/sqlstore/`,
`store_factory.py`, `storage_protocol.py`, `storage_split.py`, `storage_config.py`,
`cutover.py`, `taskfiles.py`, `docs/storage-sqlite.md`, `docs/migration-guide.md`, and the
machine-level backup arrangement the store now depends on.

**Method.** Read every module in scope. Then ran probes against throwaway stores built with
`store_factory.open_store` under a temporary `AGENTJOBS_HOME`, using the CLI where the
question was about the CLI. Read the live databases with `mode=ro` connections only. Took one
`storage backup` of the live `agentjobs` database into a scratch directory, which is what the
nightly job does, and restored it to a scratch path. Nothing under `~/.agentjobs` was written.
Every command below was run on `main` at `096f33ae`.

**Headline.** The store's core — WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`, one transaction
per verb — is sound and behaved as documented under six concurrent writer processes and a
hard kill mid-transaction. The migration is complete to the file. Backups exist, verify, and
restore. The defects are at the edges: one verb silently does nothing under the new store
(P1), the CLI invents a private database when it cannot resolve a project (P2), the guard
that keeps operator commands off a live database checks the wrong port on this machine (P2),
and the nightly backup that everything now depends on scrapes CLI prose and has already
broken once (P2).

---

## Already on the record (baseline)

- **task-411** — corpus checks skip under pytest. Not re-tested here; noted because the
  quotation check it would re-enable relies on `agentjobs redact` as the fix, and finding 1
  below shows that fix does not work on the log.
- **task-409** — a store change moves no file. Not in my scope; confirmed indirectly by
  finding 5: the `roadmap` gate stage opens the live database with a write-capable connection.
- **task-398 / task-380** — closed today. Their acceptance criteria are consistent with what I
  measured (finding 11). The brief's `rows=404 files=376` is stale: `storage status` now
  prints `rows=409 files=0` for `agentjobs`, and the gap is explained below.
- **task-261** (draft, 2026-08-22, from the first audit) — "read-only commands run outside a
  project silently create `tasks/` and answer *No tasks found*", and "defaults read
  `gui.port`". Both survived the migration in new clothes; findings 2 and 3 confirm it.
- **task-395** (draft, 2026-09-08) — "Enrol the AgentJobs database in this machine's backups";
  says it is *not established* whether anything carries a snapshot off-machine on a schedule.
  Finding 4 establishes it, and finds the schedule already broke once.
- **task-247** (first audit; file writes not atomic) — refuted for the new store by finding 10:
  a process killed inside a transaction leaves nothing behind.

Backlog search for prior records: read-only FTS over the live `task_fts` table for
`redact`, `8765`, `local AND database`, `restore`, `outbox`, `backup`, and
`migration AND (status OR roadmap OR gate)`; open matches were task-261, task-395, task-379,
task-408. `agentjobs search` does not exist as a CLI verb and `/api/projects/agentjobs/tasks/search`
returns 404, which is how the search was done this way.

---

## Findings

### 1. `agentjobs redact` on a log body records a redaction it does not perform — P1

**New.** **Verified by running.**

`TaskManager.redact` (src/agentjobs/manager.py:1684-1780) mutates `entry.body` on the
in-memory task and appends a `note` saying what was redacted, then persists through
`_mutate`. `SqlTaskStore._replace_log` (src/agentjobs/sqlstore/store.py:727-743) inserts only
entries whose `entry_id` is not already in `log_entry` and never updates an existing row. Its
docstring says the one exception is "an entry whose body changed, which the manager never
does" — written in `e6518dd4` (2026-09-06), the same day `bc6761b2` added the verb that does
exactly that.

Probe (`probe_redact.py`, throwaway store, today's code):

```
redact() returned body   : ['[paraphrased]']
stored body after reload : ['SECRET WORDS HERE']
redaction note present   : True
raw log_entry.body       : SECRET WORDS HERE
title after redact       : Redacted title          <- spec/title redaction works
quotations still findable: True
```

So the record now carries an entry saying "Redacted log[2].body: audit probe" while entry 2
still holds the words. The CLI (`agentjobs redact`) reaches the same method over the API, so
every surface is affected. Spec fields and `title` redact correctly because they are columns
on the `task` row, which is upserted.

**Why this is P1 rather than P2.** The quotation policy (task-376) names `agentjobs redact`
as the one-command fix for a hit, the importer refuses a corpus on a hit, and task-411 is
about to re-enable the gate check that finds hits. When that check goes red on a log body the
prescribed fix will report success and change nothing, and the repository has a public remote.
Task-376's own twelve redactions were done on 2026-09-06/07 against the file backend and were
imported as already-clean YAML, so they are not affected; anything redacted since the
2026-09-07 cutover is.

**What would have caught it.** `tests/test_quotation.py::TestRedactionVerb` asserts on the
task object `redact()` returns (line 283-298), never on a reload. A one-line
`manager.get_task(id).log[n].body` assertion fails on today's code.

**Fix.** In `_replace_log`, for an entry whose id exists, `UPDATE log_entry SET body=?`
when the body differs — and emit the change into the redaction note's `data` so the store's
own history says a stored row was rewritten. Alternatively give the store an explicit
`redact_entry(task_id, entry_id, body)` primitive and have the manager call it inside the same
transaction; that keeps `_replace_log` append-only and makes the one legitimate rewrite
visible in the code. Either way, add the reload assertion.

### 2. The CLI silently creates and answers from a private "shadow" database whenever it cannot resolve a project — including in every worktree — P2

**Confirms task-261** (the file-era form of the same hazard). **Verified by running, and
observed on the live machine.**

`cli._build_manager` (src/agentjobs/cli.py:211-224) catches `ProjectError` — which includes
`AmbiguousProjectError`, the registry's "this directory is inside no registered project and
several are registered" refusal — and falls through to a `_local` project whose database is
`~/.agentjobs/databases/local-<dirname>-<digest>.db` (store_factory.py:183-201). The
registry's own error text, which tells the user to name a project, is never shown.

Live evidence: `~/.agentjobs/databases/local-agentjobs-24cff10cef.db` exists (created
2026-09-11T16:57Z, touched again 02:15Z today). Its `project` row has
`root = C:\projects\worktrees\agentjobs-392`, and
`blake2s("C:\projects\worktrees\agentjobs-392\tasks\agentjobs", 5) = 24cff10cef`. Some
command was run without `--project` inside the task-392 worktree; the worktree is not under
the registered root `C:\projects\agentjobs`, so the CLI built a shadow store. It holds zero
tasks, so nothing was lost this time.

Probe (`probe_cli.sh`, temp home with two registered projects, cwd in neither):

```
$ agentjobs list        -> "No tasks found."  rc=0
$ agentjobs create --title "where did I go" --description ... --ready
$ agentjobs list        -> "- task-001 | where did I go [Needs spec, ...]"
databases/: local-tasks-76e42e2862.db  proba.db  probb.db
$ agentjobs storage status   -> lists proba and probb only; the shadow file is not mentioned
```

It happened again while this audit ran. At 21:38 local the live `~/.agentjobs/databases/`
held one shadow file; by 21:52 it held five. The four new ones were read back read-only:

```
local-tasks-5ac3195b10.db  root C:\Users\jpose\.claude\jobs\2424402b\tmp\emptydir   0 tasks  02:48Z
local-tasks-00506e3e74.db  root C:\Users\jpose\AppData\Local\Temp\aj-audit01-9h3vn3yz  3 tasks  02:52Z
local-tasks-e34ea71a81.db  root C:\Users\jpose\AppData\Local\Temp\aj-audit01-v21thae4  6 tasks  02:52Z
local-tasks-d71dd64fff.db  root C:\Users\jpose\AppData\Local\Temp\aj-audit01-63snqph7  6 tasks  02:52Z
```

Other auditors' sandboxes, run against temporary directories without `AGENTJOBS_HOME` set,
wrote fifteen throwaway tasks into the authoritative databases directory
(`local_database` resolves through `default_home()`, projects.py:88-93, which is the real
home unless the environment says otherwise). `storage status` does not list them, `storage
backup` does not snapshot them, and nothing removes them. None of these is mine: my probes ran
under a temporary home and their shadow file (`local-tasks-76e42e2862.db`) is in the scratch
directory.

Every dispatched run is told to work from `../worktrees/agentjobs-<nnn>`. The dispatch prompt
passes `--project` to `run register`, but any other verb an agent types without `--project`
in that directory reads an empty backlog and writes records nobody will ever serve. The
docstring at cli.py:205-209 argues the fallback is safe because "no server is serving a
project nobody registered"; the case that actually occurs is a worktree *of* a registered
project.

**Fix.** Fall back to `_local` only when the registry is empty (the genuine single-directory
case). On `AmbiguousProjectError`, print the registry's message and exit 1. Have
`storage status` list every `local-*.db` under `databases/` so a shadow file is visible.
Consider resolving a worktree to its parent repository (`git rev-parse --git-common-dir`)
before giving up.

### 3. The "server is running" guard checks port 8765; this machine serves on 8876, so `storage import`, `split` and `restore` will run against the live database by default — P2

**Confirms task-261** (port identity). **Verified by running the guard; consequence partly
verified.**

`_refuse_while_serving(port)` (cli.py:2164-2184) is the only thing keeping the operator
commands off a database the server holds, and every caller defaults `port` to 8765
(cli.py:2273, 2361, 2482). `~/.agentjobs/dispatch.yaml` and the launcher both say 8876.

```
>>> _find_process_by_port(8765)   -> None
>>> _find_process_by_port(8876)   -> 298608      (the live dashboard)
```

What then happens (probe B in `probe_restore2.sh`, another process holding the file, guard
told a port nothing listens on):

```
snap-proba.db: ... VERIFIED -- safe to restore
PermissionError: [WinError 32] The process cannot access the file because it is being used
by another process: '...\proba.db' -> '...\proba.db.replaced.20260912T025029Z'   rc=1
```

On Windows the restore fails after verification and before anything moves, so no data is
lost — but the failure is a raw traceback, not a refusal. On POSIX `Path.replace` succeeds
under an open file, the snapshot is copied in, and the running server keeps writing to the
renamed inode: two authoritative files and no error. `import --replace` and `split` under a
live server are the same shape with a delete-first step. Also worth knowing: `storage backup`
has no guard and does not need one (VACUUM INTO is safe while serving — confirmed against the
live store, finding 4).

**Fix.** Read the port from the same place dispatch does (`api_base` in `dispatch.yaml`, then
`gui.port`), or better, detect the holder of the *file* rather than a port — on Windows an
exclusive open attempt, on POSIX `flock`. `restore()` should also refuse when
`destination` has a `-wal`/`-shm` sibling newer than a few seconds, which is a cheap proxy
for "someone has this open".

### 4. The nightly backup that now holds the only off-machine copy scrapes CLI prose, broke on 2026-09-11, and aborts the whole machine's backup when `storage status` fails — P2

**Answers task-395's open question (refutes "not established"); the fragility is New.**
**Verified by reading the scheduled task, the script, and the log; the status crash verified by
running.**

What exists: Windows task `\Backups\Restic-Vault-All`, daily 02:00, runs
`C:\projects\vault\tools\backup.ps1 -All`, which calls `agentjobs-backup.ps1`. That script
parses `agentjobs storage status` text to discover projects, runs
`agentjobs storage backup --project <id> --into <staging>/<id>.db` and `storage verify` for
each, and restic backs the staging directory up to `F:\restic-vault`. Snapshots for 09-07,
09-08 and 09-09 are in the restic log; the run on 2026-09-11 02:00 has **Last Result 1**
(`schtasks /query /v`), and the script's own comment says why: task-402 removed the backend
column from `storage status` output, the parser found no projects, and the throw "stopped
the entire nightly backup — vault, keys and media included". The parser was rewritten to match
on indentation structure the same day. There is no snapshot for 09-10 in the listing either.

The coupling is deliberate in `backup.ps1` (line 543: "failure must stop this backup"), and
it means any AgentJobs failure stops the vault's backup too. `storage status` fails on more
than a format change — with one database file corrupt it exits with a raw traceback
(`sqlite3.DatabaseError: file is not a database`, cutover.py:542 via connection.py:68) rather
than reporting that project as unreadable and continuing:

```
$ agentjobs storage status        (proba.db first 4 KiB zeroed)
default database: ...
DatabaseError: file is not a database        rc=1
```

**Fix.** Give `storage status` a `--json` output and make the backup script consume it;
make `status` catch per-database open errors and print `rows=? (unreadable: <reason>)`
with a non-zero exit only after every project is listed. In `backup.ps1`, decide explicitly
whether an AgentJobs snapshot failure should abort the vault's backup or be reported and
skipped — the current all-or-nothing coupling trades a small loss for a large one. Then
close task-395 with what is actually true.

### 5. "Read-only" operator commands open the live database with a write connection and will apply a pending migration from a short-lived process — P3

**New.** **Verified by running.**

`open_database` (store_factory.py:129-150) always constructs a writer and runs
`upgrade()`. `storage status`, `storage backup`, `storage export` and `roadmap.store_tasks`
(roadmap.py:264, which `scripts/export_roadmap.py` runs on every gate) all use it. Only
`dispatch_manager_for` has `_assert_schema_current` (store_factory.py:320-345), whose own
docstring says applying a migration from a CLI process while a server holds the file "is the
one thing multi-process SQLite does not make safe".

Probe (`probe_status_migrates.py`): built `probc.db` at `user_version 2`, registered it, ran
the status command:

```
built probc.db at user_version 2
$ agentjobs storage status --project probc      rc=0, prints "probc: rows=0 files=0"
user_version after `storage status`: 3
schema_migration rows: [(1,'0.0.0'), (2,'0.0.0'), (3,'0.1.0')]
files after status: ['probc.db', 'probc.db-shm', 'probc.db-wal', 'probc.pre-v3.20260912T025054Z.db']
```

A status command migrated the database and wrote a snapshot beside it, silently. The day a
migration 004 lands, the first `storage status` or gate run from a fresh checkout will
migrate the live database under whatever server version is running it. The nightly backup
script (finding 4) runs `storage status` too.

**Fix.** Route every non-server caller through `_assert_schema_current` (or an
`open_database(path, migrate=False)` that refuses a behind-schema file with the same message),
and open read-only paths with `mode=ro`. Reserve `upgrade()` for the server and for
`storage import`, which already require the server stopped.

### 6. Every open is a write, so a reader cannot open the store while another process holds a transaction longer than the 5 s busy timeout — P3

**New.** **Verified by running.**

`open_store` calls `ensure_project`, which is `INSERT OR IGNORE` inside `BEGIN IMMEDIATE`
(store.py:106-111). While another process holds a write transaction, that begin waits
`BUSY_TIMEOUT_MS` then raises `TaskLockTimeout`:

```
holder: holding write lock for 8s
reader : TaskLockTimeout ... (database is locked); nothing was written   (returned after 6.05s)
writer : TaskLockTimeout ... (database is locked); nothing was written
```

Both failed in `open_store -> ensure_project -> write()`. In the server the transaction is
already committed by then, and ordinary verbs hold the lock for milliseconds, so the exposure
is the long transactions — `storage import` (one transaction for the corpus) and `split` — plus
any future long verb. Those are supposed to run with the server stopped, which is finding 3's
guard. The `roadmap` gate script does *not* call `ensure_project` and reads fine under a held
lock (WAL readers never block), which is the behaviour docs §5 promises for readers generally.

**Fix.** Make `ensure_project` a read first (`SELECT 1 FROM project WHERE …`) and write only
when missing; readers then never take the write lock on open. Consider raising the timeout for
the dispatch family, which is the one legitimate second writer.

### 7. `docs/storage-sqlite.md` §6 and `connection.py` describe a webhook outbox that nothing writes — P3

**Refutes docs §6.** **Inferred from reading; row count verified.**

The schema creates `webhook_outbox` (001_initial.sql:375-387) and docs §6 says webhooks are
"written in the same transaction as the state change" so that "a crash between commit and
send no longer loses the notification". `grep -rn outbox src/` finds only the table, the
importer's delete list, and two docstrings in connection.py. `TaskManager._fire`
(manager.py:2813-2816) calls `webhook_manager.fire_event` directly after the transaction has
committed, exactly the fire-and-forget the outbox was meant to replace. Live count:
`SELECT count(*) FROM webhook_outbox` → 0 rows after 2,124 operations.

**Fix.** Either implement the outbox (enqueue inside `_persist`, drain from the poller) or
delete the table and the four sentences that describe it. A design document that describes a
durability property the code lacks is worse than one that says the property is future work —
`docs/webhooks.md` already says the durable receiver is future work, so the two documents
disagree today.

### 8. The snapshot manifest can be one row ahead of the snapshot when a second process commits between `VACUUM INTO` and the count — P4

**New.** **Inferred from reading.**

`snapshot()` (backup.py:96-103) takes `database.exclusive()`, which is the *in-process*
`RLock`, then runs `VACUUM INTO` and `_manifest` on the same connection with no transaction
open. A commit from another process — the server, since backups are taken while it serves —
between those two statements puts the new row in the manifest counts and not in the
snapshot, and `restore` then refuses a good snapshot with "manifest says N rows, snapshot
holds N-1". The docstring claims the two are "taken under one hold of the write lock", which is
true of the Python lock and not of SQLite's. Not reproduced: it needs a commit inside a
~70 ms window.

**Fix.** Compute the counts from the *snapshot* file after `VACUUM INTO` returns (open it
read-only and count), or wrap both in `BEGIN` on a reader connection — `VACUUM INTO` cannot run
inside a transaction, which is why the counts should come from the artefact, not the source.

### 9. `restore()` copies the snapshot into the live path non-atomically — P4

**New.** **Inferred from reading.**

backup.py:205-218 moves the old file aside, unlinks `-wal`/`-shm`, then
`destination.write_bytes(snapshot.read_bytes())`. A crash mid-copy leaves a truncated live
file with the good copy at `<name>.replaced.<stamp>` and nothing printed to say so. Write to a
temp name in the same directory and `os.replace` it in; print the moved-aside path on success
so the operator knows where the previous state went (the CLI currently prints only
`restored <snap> to <target>`).

### 10. Durability and atomicity: confirmed as designed; task-247 does not apply to the store — P4 (observation)

**Refutes task-247 for the new backend.** **Verified by running.**

Pragmas on the writer as actually opened: `journal_mode=wal synchronous=2 (FULL)
busy_timeout=5000 foreign_keys=1`. Probes (`probe_mp.py`):

```
crash inside an open transaction : task-999 present after crash: 0   integrity_check: ok
hard exit right after a commit   : task-998 present after commit+kill: 1
```

Cross-process contention: six processes × 25 `mutate_task` appends to one task, all in
parallel — `150/150` entries landed, `150` distinct ids, `0` timeouts, `1.22 s` wall,
`task.log_count=150`, `revision=151`, backlog invariant `1=1`. `tests/test_sqlstore.py::
TestConcurrency` covers the same shapes with threads; nothing in the suite drives a second
*process*, which is the case dispatch actually exercises. Worth one test.

### 11. Migration completeness: the corpus arrived whole, and the `404 vs 376` gap is post-cutover creates — P4 (observation)

**Confirms task-380 / task-398 closure.** **Verified by running.**

- `git ls-tree 18a896ac^ -- tasks/agentjobs` (the tree before task-380 removed the frozen
  copy): **376** `.yaml` files.
- Live `agentjobs.db`: **376** rows with `created_at <= 2026-09-07T19:03:37.838233Z` (the
  recorded cutover instant); **409** rows in total at the start of the night (410 by the
  time of the backup below); `import_quarantine`: 0 rows; `integrity_check: ok`;
  `foreign_key_check`: clean; `sum(open_delta)=142 = open tasks 142`; `seq IS NULL`: 0 rows
  (migration 003 did its job); `task_event.source`: 446 backfilled / 1,604 reconstructed /
  147 native.
- Every one of the five per-project files holds exactly one `project_id`
  (`agentjobs`: 409; the others 20/25/27/10 per `storage status`).
- Importer idempotency: `pytest tests/test_sqlstore.py -k "import or Import" -q` — 10 passed,
  36 deselected, 0.53 s, run tonight; that set includes the second-import refusal and the
  `--replace` re-import. The
  importer refuses a re-run outright unless `--replace`, and a half-imported project is
  impossible by construction: the whole import is one transaction and a killed import leaves
  nothing (finding 10 is the same mechanism).

Two loose ends, neither a defect: `storage.yaml` still carries `backend: sqlite` on every
entry, which the loader ignores and `save()` drops; and the retired fallback
`~/.agentjobs/agentjobs.db` is 16.8 MB, holds **zero** task and project rows, and its mtime
is 2026-09-12 02:15Z — something still opens it. Docs §11 says it may be removed once every
project is served from its own file and a backup is kept; both are true.

### 12. Backup and recovery: the artefact restores, and the app reads it — P4 (observation)

**Verified by running, on the real corpus, into scratch.**

```
$ agentjobs storage backup --project agentjobs --into <scratch>/agentjobs-audit.db
agentjobs-audit.db: schema v3, integrity ok, 0 FK violations, 410 tasks, 2199 history events,
12 attachment blobs / backlog invariant 143=143 / VERIFIED -- safe to restore
```

Taken while the live server was serving, as the nightly job does. `restore()` into a scratch
path, then opened through `SqlTaskStore`: 410 tasks listed in 0.11 s, `task-411` loads,
invariant holds, and every counted table matches the live file row for row (task 410,
log_entry 4,130, task_event 2,199, blob 12, attachment 12, operation 2,124, task_run 195).
Recovery story, as it stands: manual snapshots in `~/.agentjobs/` and `databases/` from
2026-09-07 through today, plus restic nightly (finding 4). The gap is not the mechanism but
the monitoring: nothing tells anyone when the 02:00 run exits 1.

### 13. The abstraction boundary: who reaches storage without `task_manager_for` — P4 (observation)

**Verified by grep; consequences are findings 2 and 5.**

Direct constructions of a store or database outside `store_factory` and the server's own
`api/dependencies.py:286`:

| Site | Why | Verdict |
|---|---|---|
| `cli.py:224` | the unregistered-directory fallback | finding 2 |
| `roadmap.py:264` | gate export, read-only | finding 5; should open `mode=ro` |
| `cutover.py:264,351,478,544`, `storage_split.py:348-370` | operator tools, server stopped | intended; guarded only by finding 3 |
| `store_factory.dispatch_manager_for` | dispatch family | documented exception; task-379 (ready) is the open decision |
| `scripts/sandbox_store.py`, `tests/support.py:84`, `tests/corpus_source.py:98` | sandboxes and tests | fine |

All of them resolve the file through `database_for`/`local_database`, so the rule's intent
(one place decides which file) holds. The literal rule in ENGINEERING.md ("through
`store_factory.task_manager_for`") is not what the code enforces and should say
`open_store`/`open_database` instead, or the roadmap exporter should be brought in line.

### 14. Per-project databases: no leak found — P4 (observation)

**Verified by running (queries) and reading.**

Every query in `SqlTaskStore` is scoped by `project_id`; `blob` is deliberately shared and
content-addressed; `split` copies only referenced blobs and deletes only unreferenced ones. An
unregistered project id gets `databases/<id>.db` on first open (`default_project_database`).
One observation: `AGENTJOBS_DATABASE` redirects *every* project to one file
(storage_config.py:163-165) and is read on every resolution, so a test harness or a dispatched
run that inherits it merges five projects' writes into one file with no warning. The docstring
argues the case; a startup log line naming the override would cost nothing.

---

## What I did not get to

- Attachment blobs under concurrent writers, and `orphans()` after `delete_task`.
- `storage export` of the live corpus and re-import of the export (tests cover it; I did not
  run it on real data).
- `split` against a real shared file — only its tests. The five live splits ran on 2026-09-09.
- Reader-connection growth in the server thread pool (`_all_readers` is never pruned while the
  process lives) and WAL checkpoint starvation under continuous readers; the live WAL was
  4.3 MB, which is unremarkable.
- Whether `agentjobs restart` (`taskkill /F`) skips `close_databases()` and what that costs on
  the next open beyond WAL recovery.
- `backfill.py` correctness (rules A and B), `reporting_tz`, and the analytics queries.
- The `TaskLockTimeout → 409` mapping end to end over HTTP.
- `_replace_children`'s delete-then-insert on every save: cost at 400 tasks is invisible; I did
  not measure it at 4,000.
- The `webhook_outbox` question (finding 7) at the level of `docs/webhooks.md` — I read only
  the storage doc's claim.

## Questions for other auditors

- **CLI auditor (task-261):** finding 2 and 3 are that record's port and fallback complaints,
  post-migration. Does the CLI auditor agree the fallback should refuse on
  `AmbiguousProjectError`, and does any other verb besides `_build_manager` swallow it?
- **Gate / `--since-gate` auditor (task-409):** `scripts/export_roadmap.py` opens the live
  database with a *write-capable* connection on every gate run and would apply a pending
  migration (finding 5). Is that acceptable for a gate stage, given three dispatched gates
  can overlap?
- **Dispatch auditor:** `dispatch_manager_for` is a second writer process by design. With
  finding 6, a dispatch verb that starts while the server holds a long transaction fails
  at *open*, not at the write. Has anyone seen `TaskLockTimeout` from `dispatch walk`?
- **Backups / vault auditor:** `backup.ps1` couples the vault's nightly to AgentJobs
  succeeding (finding 4). Is that the owner's intent? The 2026-09-11 02:00 run exited 1 and,
  as far as the log shows, nothing alerted.
- **Docs auditor:** `docs/storage-sqlite.md` §6 (outbox) is false today (finding 7); §5 and §7
  are accurate as far as I tested; §11's advice about the empty shared file is accurate and
  should probably be acted on.
- **Supervisor:** port 8902, the one assigned to me, was already held by `python.exe` PID
  44052 when I checked. I started no server and used no port.

## Command log (what I ran, for a reader who wants to repeat it)

```
agentjobs storage status
agentjobs show task-411 / task-409 / task-398 / task-380 / task-376 / task-395 / task-261
git ls-tree -r --name-only 18a896ac^ -- tasks/agentjobs | grep -c .yaml          -> 376
ro_query.py ~/.agentjobs/databases/agentjobs.db  (PRAGMA user_version/journal_mode,
    integrity_check, foreign_key_check, counts by project, created_at <= cutover,
    quarantine, webhook_outbox, task_event by source, open_delta invariant, seq IS NULL)
ro_query.py ~/.agentjobs/agentjobs.db            -> 0 tasks, 0 projects
ro_query.py ~/.agentjobs/databases/local-agentjobs-24cff10cef.db -> project root worktrees/agentjobs-392
schtasks /query /tn "\Backups\Restic-Vault-All" /xml ; /fo LIST /v     -> Last Result 1
probe_redact.py            (finding 1)
probe_mp.py + mp_worker.py (findings 6, 10)
probe_cli.sh, probe_restore2.sh (findings 2, 3, 4, 12)
probe_status_migrates.py   (finding 5)
agentjobs storage backup --project agentjobs --into <scratch>/agentjobs-audit.db ; storage verify
probe_restore_live_copy.py (finding 12)
pytest tests/test_sqlstore.py -k "import or Import" -q
```

All probe files live in the audit session's scratch directory and are not part of the
repository. Every throwaway store was under a temporary `AGENTJOBS_HOME`; the live home was
only ever opened read-only, except for the one `storage backup` snapshot, which reads.
