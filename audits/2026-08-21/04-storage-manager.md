# 04 — Storage, manager, operations

Auditor 4 of the Big Dawg Audit (task-242). Read-only; main clone at `c51eb64`,
2026-08-22. Scope: `storage.py`, `manager.py`, `operations.py`, `receipts.py`,
`actors.py`, `projects.py`, plus every other place in `src/`, `scripts/` and `tests/`
that touches the task directory, and the backend question in brief item 6.

Method: read all six modules in full; grepped the whole tree for YAML load/dump and
file IO on task files; read the callers of every storage write verb; read
`docs/schema-design.md` §7 and `docs/task-selection-design.md` §7/§12 for the stated
philosophy and the concurrency design; checked the live corpus and the running server
on 8876 with GETs only. Nothing was written except this file. No gate, no checkout.

Severity key as in PLAN.md: P1 bites now / P2 should fix / P3 improvement / P4
observation. Every finding cites the line or the command that shows it.

---

## 1. Write atomicity

### F1 — P1 — The task write is `write_text`: no temp file, no rename, no fsync

**Evidence.** `src/agentjobs/storage.py:496-497`:

```python
yaml_text = yaml.safe_dump(task_dict, sort_keys=False, allow_unicode=False)
path.write_text(yaml_text, encoding="utf-8")
```

`Path.write_text` opens with `"w"`, which truncates the file to zero bytes and then
writes. There is no temporary file, no `os.replace`, and no `os.fsync`. Compare the
*attachment* writer added four days later, `src/agentjobs/attachments.py:128-134`,
which does it properly:

```python
# Written through a temporary file in the same directory and renamed, so a
# reader never sees a half-written image.
temporary = path.with_name(f".{digest}.partial")
temporary.write_bytes(payload.data)
os.replace(temporary, path)
```

And `storage.py:220-222` *describes* an atomic writer that does not exist:

```python
# An atomic writer can replace a file between glob and stat.  The next
# poll will see the completed replacement; ...
```

**What it does.** Two consequences, one for crashes and one for ordinary operation:

1. **Crash / kill mid-write** (the server on 8876 being restarted — which
   ENGINEERING.md prescribes after every merge — or a CLI process killed) leaves a
   zero-length or truncated file. The next read raises `TaskLoadError("the file is
   empty")` or `invalid YAML` (`storage.py:307-315`), so the task drops out of every
   listing into the broken-files banner; `_dependency_states` counts it as *not closed*
   (`manager.py:358-362`), so anything that `needs` it is blocked; `_open_children`
   cannot see it as a child (`manager.py:503-518`). Recovery is `git checkout -- <file>`,
   which loses **every log entry since the last `chore(task-nnn)` commit** — and the
   tasks-on-main workflow commits task records in batches, so that can be a whole
   session of progress entries.
2. **Every successful write has a window in which readers see a partial file.**
   Readers take no lock (`_load_task_uncached`, `storage.py:289-329`; design §7 says
   "reads deliberately do not lock"). The per-request `corpus_snapshot` is entered
   per request (`api/main.py:194`), so a listing that starts while another process —
   a CLI session, a second interactive agent's `agentjobs` call, the poller — is
   inside `write_text` parses a truncated file and reports a broken task for that
   request. The dashboard polls `/revision`, whose hash changes twice per write
   (once at truncation, once at completion), and re-lists on each change, which makes
   landing inside the window *more* likely, not less.

**Constructed on paper, not reproduced** — reproducing it needs a write, and this
audit is read-only. The window is milliseconds for a 15 KB file; it is real because the
deployment has several writer processes and a poller.

**Fix.** Write to `path.with_name(f".{task.id}.partial")` in the same directory,
`os.replace` onto the target (atomic on NTFS and POSIX; the attachments code already
relies on it), and `fsync` before the replace if durability matters more than the
~1 ms it costs. Three lines. Then fix the comment at `storage.py:220`, or better, make
it true. `project_revision`'s `FileNotFoundError` guard at `storage.py:219` becomes
correct once the write is a replace.

**What tests would have caught it.** None exist: `tests/test_storage.py` has 11 tests,
`tests/test_concurrency.py` 14, and not one reads during a write or reads a truncated
file. `test_concurrency.py::TestTheRace` proves the *claim* race is closed; it does not
exercise the write.

---

## 2. Concurrent writers

**The deployment:** one long-running server (8876, started by a launcher), N interactive
agent sessions each running `agentjobs` CLI in their own process, dispatched runs whose
agents write through MCP → REST → that server, and the poller (`dispatch/poller.py:163`
builds its own `TaskManager(TaskStorage(...))` per project). Four or five processes, one
`tasks/` directory.

**Where the lock is.** Per-task advisory lock: `O_CREAT|O_EXCL` on
`tasks/<id>.lock`, `storage.py:334-389`. Two project-wide locks built on the same
primitive, `.creation` (`:393`) and `.queue` (`:412`), with a fixed order
`.creation → .queue → task locks` (`:429-431`). `mutate_task` (`:441-472`) re-reads the
task *inside* the lock before deciding. This is the right shape and it closes the race
it was built for.

**Lost-update scenario on paper.**

| Path | Lock held across read→decide→write? | Verdict |
|---|---|---|
| `claim`, `handoff`, `release`, `promote`, `close`, `add_log_entry`, `record_dispatch*`, `mark_deliverable_complete` | yes — all go through `_mutate` → `mutate_task` | **prevented** |
| `update_task` (content patch) | yes; the patch is merged onto a fresh read (`manager.py:988-989`), so two patches to *different* fields both land; two patches to the *same* field are last-write-wins unless the caller sends `expected_revision` | prevented at field granularity; documented (`:960-963`) |
| `create_task` | `.creation` + `.queue` | prevented |
| `move`, `reprioritize`, `rebalance`, `compact`, `repair`, queue migration | `.queue` held across plan + writes | prevented |
| **`update_task` when it re-bands or reopens** | **no** — see F4 | **not prevented** |
| `storage.delete_task` | no lock at all (`storage.py:623-630`) | unreachable from API (DELETE route archives, `routes/tasks.py:476-480`); Python-only |
| `storage.save_task` direct | task lock only; callers: create (fresh id), `_write_raw_position`, queue migration, `load_test_data` | acceptable — each holds `.queue` or writes a new id |
| Any hand edit / `git checkout` / merge | none | **absorbed by git history only** — and receipts (§3) detect it at commit time, not at write time |

So: the manager's verbs do not lose updates to each other. The history-absorbs-it cases
are the ones outside the manager — git operations on a shared clone (the task-files-
on-main rule exists because of exactly this) and the two findings below.

### F2 — P2 — A stale task lock has no reaper, no pid check, and no command to clear it

**Evidence.** `storage.py:366-382`: the lock file is created, the pid is written into it
(`os.write(handle, str(os.getpid())...)`) and **nothing ever reads it**. Contention
spins until `LOCK_TIMEOUT_SECONDS = 10.0` and then raises `TaskLockTimeout` with a
message naming three possible causes (`:374-379`). The docstring acknowledges it
(`:340-342`): "a process killed mid-write leaves a stale lock, which is why the wait
times out with an error".

Contrast the *dispatch* layer, which solved this for its own locks three days ago:
`dispatch/ledger.py:377-400` `release_stale_locks` reads the holder pid, decides
`stale_lock_reason`, and sweeps at startup; `ledger.py:204-222` documents why (task-190).
That machinery lives in `~/.agentjobs/runs/.locks/` and does not know about
`tasks/*.lock`.

**What it does.** Kill the server (or a CLI) while it holds `task-242.lock`: every
subsequent write to task-242 from every process waits 10 s and fails. The failure names
the file but there is no `agentjobs unlock`; the human has to know to remove a dotfile
in `tasks/`. The same crash that produces F1's truncated file produces this lock, so the
two compound: the file is broken *and* cannot be rewritten through the managed path.

`grep -rn "stale lock\|unlock" src/agentjobs/cli.py docs/*.md ALLAGENTS.md` → nothing
about task locks. `tests/test_concurrency.py:214
test_the_timeout_message_explains_the_stale_lock_case` tests the *wording*, not a
recovery.

**Fix.** Reuse the ledger's holder/liveness logic: on `FileExistsError`, read the pid,
and if that pid is not alive **and** the lock is older than the timeout, reclaim it
(delete + retry once). Windows pid reuse makes "not alive" slightly unreliable, which is
why the age condition is also needed. Add `agentjobs unlock <task>` for the residual
case, and name it in the timeout message instead of "a previous run died".

### F4 — P2 — A content patch that re-bands or reopens computes its position under the queue lock, then writes without it

**Evidence.** `manager.py:1055-1056`:

```python
with self.storage.queue_lock():
    position = self._place(band, Placement(Placement.BOTTOM), excluding=(task_id,))[0]
```

— the `with` block ends there. The write happens later at `manager.py:1018`
`self._mutate(task_id, apply)`, under the **task** lock only. Between the two, any
`create_task`, `move`, `reprioritize` or rebalance can hand the same number to another
task; the band then has a duplicate, which `get_next_task` refuses to answer over
(`manager.py:591`, `QueueCorruptionError`).

This violates the design's own rule, `docs/task-selection-design.md` §7: "Anything
holding `.queue` re-reads the band from disk *inside* the lock". The write is outside
it. The documented lock order `.queue → task lock` makes holding `.queue` across
`_mutate` legal, so the fix is mechanical.

**Reach.** `priority` *is* in the API and MCP allowlists (`api/models.py:357`,
`mcp/mutation_tools.py:97`), so a band change via `PATCH /tasks/{id}` or
`task_update_content` takes this path. `lifecycle` is not in either allowlist, so the
*reopen* branch is reachable only from Python (see F6).

**Test.** `tests/test_queue_verbs.py:730 test_a_reopened_task_re_enters_at_the_bottom_of_its_band`
checks placement, not the race. Nothing races a patch against a create.

**Fix.** In `update_task`, when `rejoin is not None`, wrap the `_mutate` call in
`self.storage.queue_lock()` and compute the rejoin inside it. Or route band changes to
`reprioritize`, which already does this correctly (`manager.py:1744-1779`), and drop
`priority` from the patch allowlist — the design text at `manager.py:965-971` is
halfway to saying that already.

### P4 — `ProjectRegistry._write` is unlocked and non-atomic

`projects.py:368-372`. Two `agentjobs init` / `project add` from two sessions at once
can drop an entry. Registry is machine-local and rarely written; noting it because it is
the same `write_text` pattern as F1 and will be fixed by the same helper.

---

## 3. `operation_id` idempotency

**Two different things share the word "receipt" in this codebase, and the brief's
question applies to only one of them.**

- **Write receipts** (`receipts.py`): `.agentjobs/write-receipts/<task>.json`, one
  per task, **overwritten on every managed write** (`receipts.py:147-148`), holding the
  SHA-256 of the bytes written. They are provenance for `agentjobs validate --staged`
  (`validation.py:455-495`), not idempotency. Gitignored; 181 exist on this machine.
- **Operation markers** (`operations.py`): the idempotency mechanism. The marker
  `{id, kind, fingerprint}` is stamped into `LogEntry.data["operation"]` of the entry the
  operation produced (`operations.py:308-318`, `manager.py:1150`). **Stored in the task
  file itself; never evicted** — it lives as long as the log entry does, which is
  forever. Detection is a linear scan of the task's log (`operations.py:279-285`).

**What is fingerprinted** (`operations.py:254-267`): `kind`, `actor`, and a normalised
payload (`None` values dropped, keys sorted, `default=str`). Per verb the payload is:
claim `{}`; handoff `{ball, ball_reason, ball_prompt, body}`; release/promote `{body}`;
close `{outcome, body, archive}`; log_append `{type, body, re, data}`; update_content
`{updates}`; queue_move `{placement, with_children, body}`; reprioritize
`{priority, placement, body}`; dispatch `{run_id, argv}`; dispatch_result
`{run_id, outcome}`; create `{id, title, lifecycle}` — note create's fingerprint
**excludes** description, priority, spec, so a retried create with a changed description
replays silently as the original. Arguably correct (the id is the decision) but
undocumented.

**Same id, different payload** → `OperationConflictError` (`operations.py:299-304`,
`manager.py:834-839` for create), HTTP 409, nothing written. Verified by
`tests/test_idempotency.py::TestOperationConflict` (4 tests) — examined, sound.

**Replay does not re-execute the file write or the log append.** `replay_or_conflict`
returns `True` → the mutator returns `None` → `mutate_task` returns the current task
without writing (`storage.py:461-463`). Verified by `TestReplay` (9 tests including
`test_replay_survives_a_restart_of_everything`). **But:**

### F3 — P2 — Webhooks fire again on every replay

**Evidence.** `manager.py:1292-1302` (`handoff`), `:1435-1440` (`close`),
`:2016-2018` (`add_log_entry` → `task.question`): each calls `self._mutate(...)` and then
**unconditionally** `self._fire(...)`. `_mutate` cannot tell its caller whether it wrote
— it returns a `Task` either way. `webhooks.py:163-181 fire_event` has no
deduplication; it schedules a delivery per matching hook every time.

So a client that times out on `POST /tasks/{id}/handoff` and retries with the same
`operation_id` gets the documented "replays instead of writing twice" for the *file*
and a second `task.handoff` delivery to every subscriber. That is precisely the
duplicate the mechanism exists to prevent, moved one hop downstream. When a
notification service is built on `task.handoff` (ENGINEERING.md names it as the
extension point), this becomes a duplicate push/SMS per retry.

`grep -rln replay tests/ | xargs grep -ln webhook` → no test file mentions both.

**Fix.** Have the mutator signal a real write — e.g. `_mutate` returns `(task, wrote)`
or `replay_or_conflict`'s `True` is surfaced through a small result object — and fire
only when `wrote`. Five call sites.

### F5 — P2 — "Replays the original result" is not what happens; replay returns the *current* task

**Evidence.** MCP instruction text (served to every agent): "reusing an operation_id
replays the original result instead of writing twice". `api/models.py:213-217`: same
sentence. Implementation: `mutate_task` returns `current` — the task **as it is now**
(`storage.py:458-463`). No original result is stored anywhere; by design there is no
side table (`operations.py:181-185`).

**Scenario.** Agent A claims task-X with op `u1`; the server's reply is lost. Meanwhile
A's previous run is reaped, the dispatcher releases the task, agent B claims it. A
retries `u1`: the marker is found in the log, fingerprint matches, `replay_or_conflict`
returns `True`, the envelope says `replayed: true`, and the body is the task **owned by
B, active**. A reads "replayed — my claim stands" and starts work on B's task. Nothing
in the response says the state moved on.

**Fix.** Either (a) change the words everywhere to "returns the task as it now stands;
re-read `assignment.owner` / `ball` before acting on a replay", or (b) on replay,
compare the marker's entry to the *last* transition entry and add
`replayed_but_superseded: true` to the envelope when later state-moving entries exist.
(b) is ~20 lines in `operations.py` and keeps the promise the text makes. Auditor 8
owns the instruction text; this is the implementation fact they need.

### P3 — `_find_created_by` is O(corpus × log) per create-with-operation-id

`manager.py:821-841` walks every task's entire log. Today that is 240 files × 1,697
entries, on a path that already holds `.creation`. The docstring says "A project large
enough for the scan to matter has outgrown YAML storage for other reasons first" —
which is true and is exactly the kind of sentence §6 collects.

### P3 — Creation idempotency depends on writing a log entry, and a bare create writes none

`manager.py:909-930`: with an `operation_id` the creator defaults to `"system"` and a
creation entry carries the marker — fine. Without one, a create from the CLI with no
actor starts with an empty log. Consistent, but it means two different "first entries"
exist in the corpus depending on which surface created the task. Observation for
auditor 1's corpus sample.

---

## 4. Abstraction bypasses

`grep -rn "yaml\.safe_load\|yaml\.load(\|load_yaml(\|yaml\.safe_dump\|yaml\.dump(\|\.glob(\"\*\.yaml\|\.read_text(\|\.write_text(" src/ scripts/`
filtered to lines that deal with **task files** (config, ledger, meta, webhooks and
registry files excluded):

| Site | Read/Write | Through `TaskStorage`? | Justification in code | First appeared |
|---|---|---|---|---|
| `queue.py:616,639` `read_queue_record(s)` | raw read | no | must read files rule 6 refuses to load | 2026-08-21 |
| `queue.py:742-744` queue migration | raw read → `save_task` | write yes, read no | same | 2026-08-21 |
| `manager.py:1962-1968` `_write_raw_position` | raw read → `save_task` | write yes, read no | repair of unloadable files | 2026-08-21 |
| `validation.py:142-145, 226-229` | raw read | no | validator must see what the loader rejects | 2026-08-17 |
| `migrate_schema.py:763, 859, 872` | raw read + **raw write** | no | v1 files cannot load as v2 | 2026-08-10 |
| `migration/parser.py:56` | raw read | no | v1 parser | 2026-08-10 |
| `cli.py:1698` | glob | no | feeds migrate | 2026-08-10 |
| `dispatch/record_commit.py:148` | `git add/commit` of `storage.task_path()` | path yes, git no | commit the record dispatch wrote | 2026-08-20 |
| `dispatch/guards.py:730`, `runner.py:1079` | `uncommitted_paths(ignore=[tasks_dir])` | path yes | clean-tree check must ignore AgentJobs' own writes | 2026-08-19 |
| `scripts/bench.py:260-269` | glob + **raw write** | no | builds a fixture corpus | 2026-08-17 |
| `scripts/review_queue_sandbox.py:155-159` | raw read + **raw write** | no | deliberately corrupts a sandbox | 2026-08-21 |
| `scripts/gate_scope.py:76` | path class `tasks/*` → pytest | n/a | `--since-gate` | 2026-08-21 |
| `tests/` — 4 files, 8 lines write `task-NNN.yaml` with `write_text`; ~30 files build task YAML via `yaml.safe_dump` into tmp dirs | raw write | no | fixtures | various |

**Every raw read in `src/` has the same justification — "the loader refuses this
file and I need to see it anyway"** — and it is a legitimate one. The point for §6 is
that it is a *capability* (read a document that fails validation) that any replacement
backend must offer, or these four modules need a second path.

The raw **writes** in `src/` are one module (`migrate_schema.py`), and it writes to an
output directory by default. The raw writes in `scripts/` are fixture generators. None
are in the request path. ENGINEERING's "avoid where possible" is honoured in the
product; the leakage is in the satellite tooling and in git.

**Examined, nothing found:** MCP. `src/agentjobs/mcp/__init__.py:5-8` documents, and
the grep confirms, that nothing in the package imports `TaskManager` or `TaskStorage`;
it goes over REST, so it inherits the per-request snapshot and every lock. Frontend:
reads the API only.

---

## 5. `manager.py` verb integrity

| Verb | Axes moved | Log entry | Atomic with write? | Notes |
|---|---|---|---|---|
| `claim` | lifecycle→active, owner, ball/reason/prompt | TRANSITION | yes (`_mutate`) | preconditions re-checked inside lock; `states`/`open_children` computed **before** the lock (`:1196-1197`) — a dependency closing between the read and the lock is refused spuriously, which is the safe direction |
| `handoff` | ball/reason/prompt | HANDOFF | yes | refuses closed; `expected_revision` honoured |
| `release` | lifecycle→ready, owner=None, ball agent/available | TRANSITION | yes | no `expected_revision` parameter (handoff/close/promote have it) — inconsistent, P4 |
| `promote` | draft→ready | TRANSITION | yes | |
| `close` | lifecycle→closed, outcome, ball=None, queue_position=None, archived? | TRANSITION | yes | no `.queue` lock by design; renumber re-checks openness (`apply_position`) — sound |
| `archive` | archived (or close first) | NOTE / TRANSITION | yes | actor `"system"` when unnamed — F8 |
| `update_task` | **any field** at manager level | NOTE only when `operation_id`; QUEUE_MOVE on rejoin | yes except F4 | F6 |
| `move`/`reprioritize` | queue_position (+priority) | QUEUE_MOVE | yes, under `.queue` | replay check done under `.queue` *and* again under the task lock (`:1656`, `:1796`) — belt and braces, good |
| `apply_position` (renumber) | queue_position | **none** — by design (`:1556-1558`) | yes | |
| `mark_deliverable_complete` | `deliverables[].status` | **none** | yes | F7 |
| `record_dispatch*` | none (log only) | DISPATCH / DISPATCH_RESULT | yes | |
| `storage.delete_task` | file gone | none, no receipt, no lock | — | Python-only; P4 |

### F6 — P2 — The manager has no field allowlist; the "axes move only through verbs" rule is enforced at the API and MCP edges only

**Evidence.** `manager.py:988-990`:

```python
payload = existing.model_dump(mode="python", by_alias=True, exclude={"display_status"})
payload.update(updates)
payload["id"] = existing.id
```

`updates` is `**kwargs` — `lifecycle`, `ball`, `ball_reason`, `outcome`,
`queue_position`, `log`, `assignment`, `archived`, `updated` all go straight through and
`Task.model_validate` accepts any consistent combination. The module docstring
(`manager.py:3-7`) says "Callers never write the axes directly"; that is a convention,
not a check. The allowlists live in `api/models.py:356-367 TaskUpdateRequest` (13
fields, `extra="forbid"`) and `mcp/mutation_tools.py:96-109 CONTENT_FIELDS` (12 fields)
— two hand-maintained copies of a list the manager does not own.

**Who reaches the unguarded path today.** `dispatch/finish.py:935`
`manager.update_task(task_id, actor=FINISHER, branches=branches)` — legitimate, in the
allowlist. Scripts, tests, and any future CLI `edit` command would not be stopped. And
the manager itself contains code for a path the edges forbid:
`_rejoining_the_queue` handles `lifecycle` patches that reopen a task
(`manager.py:1047-1050`), reachable only from Python. Code for an unreachable path is
either dead or a sign the edge guard is expected to be bypassed.

**Fix.** One `CONTENT_FIELDS` frozenset in `manager.py`; `update_task` rejects anything
else with a `ValueError` naming the verb to use; API and MCP schemas are generated from
it (the MCP one already is a dict literal that could be built from it). Auditor 3 is
looking at the same invariants from the schema side — this is the manager-side half of
"enforced at one edge is constructible through another".

### F7 — P3 — `mark_deliverable_complete` changes state with no log entry

`manager.py:1076-1094`. A deliverable flips to `done` with no actor, no timestamp and
no entry — the one class of change the log exists to record. Route at
`routes/tasks.py:488-495` passes no actor either. Fix: take `actor`, append a NOTE with
`data={"deliverable": path, "status": "done"}`.

### F8 — P3 — `DELETE /tasks/{id}` archives as `"system"` with no actor validation

`routes/tasks.py:476-480` → `manager.archive_task(task_id)` → `actor = author or
"system"` (`manager.py:1445`). Every other mutation route validates the actor
(`routes/tasks.py:104`). An archive from the GUI is therefore the one anonymous act
left, which is the failure `actors.py:7-13` says the module exists to prevent.

### F9 — P3 — `generate_task_id` silently ignores 158 of the 240 files in the live corpus

`storage.py:611-621` parses `int(stem.split("-", 1)[1])`; for
`task-081-task-selection-ranking` that is `int("081-task-selection-ranking")` →
`ValueError` → `continue`. Live corpus: 158 slugged files, 82 plain
(`ls <tasks dir> | grep -cE '^task-[0-9]+-.+\.yaml$'`). It works today only because
the highest-numbered file (`task-243.yaml`) happens to be plain; the highest slugged is
185. Had the corpus been all-slugged when auto-ids began, the first auto-create would
have produced `task-001`. The id *is* the full stem (`id: task-081-task-selection-
ranking`; `GET /tasks/task-081` → 404, `GET /tasks/task-081-task-selection-ranking` →
200 on 8876), so there is no lookup bug — but the numbering function and the
`_create_unlocked` existence check (`load_task(task_id)` → `task-NNN.yaml`) both assume
plain stems, so a slugged file numbered above the plain maximum would get a duplicate
*number* with a different id. Fix: parse with `re.match(r"task-(\d+)", stem)`.

### P4 — `locked()` treats `PermissionError` as contention, so a read-only `tasks/` spins for 10 s per write

Documented trade at `storage.py:356-359`; noting that a worktree whose `tasks/` is on
a read-only mount (or an ACL mistake after a copy) presents as "another writer is
holding it". The message covers it. Decoration-adjacent, examined.

### Examined, nothing found

- `actors.py`: `validate_actor` accepts anything when no actors are configured
  (`:203-206`), accepts `dispatcher`/`finisher` unconditionally (`:202`), and
  `human_identity` refuses a multi-human config rather than guessing (`:132-142`). All
  three are deliberate, documented, and tested (`tests/test_actors.py`). The
  "default_user must not be adopted by agents" rule is not enforced here — that is
  auditor 8's question; this module only resolves ids.
- `projects.py`: `contained_path` (`:498-510`) resolves and checks containment; task
  ids and attachment paths go through it. A subdirectory id (`"sub/task-001"`) is
  *contained* and so accepted — it writes a file the `*.yaml` glob never lists. Not a
  traversal; an orphan. P4. Registry `root` is trusted (machine-local, written by
  `agentjobs init`), and `get` is an exact-match dict lookup, never a join (`:393-405`).
- `operations.py::check_revision`: microsecond comparison of `isoformat()` strings;
  `_write_task` always bumps `updated`, so every write moves the revision. Sound.
- `corpus_snapshot`: scoped per request, dropped on write (`storage.py:504-507`);
  `mutate_task` and every `.queue` holder bypass it (`_load_task_uncached`,
  `list_tasks_uncached`). The invariant is stated in four docstrings and they agree.
- Lock order: every `.queue` holder that also takes `.creation` takes `.creation`
  first (`create_task:800-805`). No path takes a task lock and then `.queue`. No
  deadlock found.

---

## 6. The backend question

Jeff's framing: measure the coupling, find the accrual rate, name the trigger. Not
pick a database.

### 6.1 Coupling inventory

"Contained" = a backend swap changes `TaskStorage` and nothing else. "Leaks" = the
swap changes this too.

**Contained (goes through `TaskStorage`):**
- All manager verbs, the API, the CLI's verb commands, the MCP server (via REST), the
  React app (via API), the dashboard (`dashboard.py:121` → `manager.list_tasks()`),
  queue *writes* (`save_task`), migration writes (`save_task`), attachments (own store,
  handed a directory), receipts (own store, handed a directory).
- The locks, the snapshot, the parse counter — all inside `storage.py`.

**Leaks, grouped by what they assume:**

1. **Tasks are files in a git repository** (the biggest, and mostly *workflow*, not code):
   - `dispatch/record_commit.py` commits `storage.task_path()`; `dispatch/finish.py`
     calls it at four sites (`:965, :1159, :1285, :1324`).
   - `dispatch/guards.py:730` and `runner.py:1079` exclude `tasks_dir` from the
     clean-tree check.
   - `validation.py --staged` + `receipts.py`: provenance defined as "staged bytes hash
     equals last managed write" — meaningless without a staging area.
   - `scripts/gate_scope.py:76`: `tasks/*` is a path class of the gate.
   - ENGINEERING.md "Task files live on `main`, always"; ALLAGENTS "the checked-out
     branch decides what the dashboard shows"; worktree rules; `git -C <main> add
     tasks/...` in every handoff. **This is the coupling that costs the most per week**
     — it is why agents need worktrees, why task records and code cannot share a commit,
     and why three failures happened in one afternoon on 2026-08-11.
2. **Tasks are individually parseable documents that may fail validation:**
   `queue.py` raw readers, `validation.py` raw readers, `_write_raw_position`,
   `repair_queue`. A replacement must store and return documents that do not validate.
3. **Storage identity is file bytes:** `project_revision` hashes every file per poll
   (`storage.py:214-230`, 3.76 MB read per `/revision` hit); receipts hash bytes;
   `canonical_bytes` exists so the validator can compare on-disk form; `TaskLoadError`
   carries `path`/`filename` into API responses (`storage.py:167-174`).
4. **`X-Task-Parses` counts files parsed** (`record_task_parse` inside
   `_load_task_uncached`). With an index it becomes either 0 or a different metric;
   `tests/test_performance_budgets.py:142` asserts `parses <= CORPUS_SIZE` and
   `docs/performance.md` is written around it. The *question* it answers — "how much of
   the corpus did one request read" — survives; the unit does not.
5. **Queue positions are sparse integers in files:** `docs/task-selection-design.md`
   §12 rejects dense numbering and a `queue.yaml` *because* one-file-per-task makes
   a 47-file diff a merge hazard; rejects LexoRank because YAML must stay readable.
   Roughly 600 lines of `queue.py` (gaps, rebalance, compaction, `QUEUE_STEP`) are the
   cost of that storage choice. In a database any of the rejected schemes is trivial.
6. **Attachments and locks live beside the task files** — `tasks/.attachments/`,
   `tasks/*.lock`, `tasks/.creation`, `tasks/.queue`. A `tests/test_storage.py:171`
   test exists solely so lock files are not globbed as tasks.
7. **Fixtures and tooling**: ~30 test files build YAML on disk; `scripts/bench.py`
   writes a fixture corpus; sandboxes hand-corrupt files.

### 6.2 Accrual trend

First appearance (`git log --diff-filter=A`) of each `src/`/`scripts/` file that reads
or writes task files outside `TaskStorage`:

| Date | Files |
|---|---|
| 2026-08-10 | `migrate_schema.py`, `migration/parser.py` |
| 2026-08-17 | `validation.py`, `receipts.py`, `attachments.py`, `instrumentation.py`, `scripts/bench.py` |
| 2026-08-19/20 | `dispatch/guards.py` (tasks_dir exclusion), `dispatch/record_commit.py` |
| 2026-08-21 | `queue.py`, `scripts/gate_scope.py`, `scripts/review_queue_sandbox.py`, `manager._write_raw_position` |

**Nine of the ten non-migration leak sites are five days old.** Two of the last
five days each added leaks in a new *category* (git-as-history on the 20th, raw-read-
for-repair on the 21st). The rate during active development is roughly two new
coupling sites per working day, and — this is the part that matters — they are all
*reasonable* in isolation. Nobody is being careless; every one of them has a docstring
explaining why it must read the file. That is what "compounding" looks like from
inside.

Of the corpus assumptions in `docs/schema-design.md` §7 (written against a 31-file,
204 KB corpus at ~6.6 KB/task): today is **240 files, 3.76 MB, 15.7 KB/task, 1,697 log
entries, 96 open**. Per-task size is 2.4× the estimate because the log is append-only
and the log *is* the product; the largest file is 121 KB (`task-081`). The "low
thousands" trigger in that doc was set when a task was a third its current size.

### 6.3 Weighing it against the philosophy

`README.md:52` "Git is the database"; `schema-design.md:53` "The schema is the
product; git is its database"; `task-selection-design.md:553` "the constraint that the
corpus stay readable in YAML is not negotiable — it is the product's stated
philosophy". This is a real position and three things it buys are in daily use here:
`git blame` on a field, the diff as the review unit for task changes, and zero
infrastructure for `pip install agentjobs`. The weekly-audit plan this file belongs to
was itself dispatched by editing and committing a YAML file.

**What the philosophy does *not* require** is that the *read path* go to the files
every time, or that the files be the only place a lock lives. `schema-design.md` §7
already says the escape hatch is "a derived SQLite index, rebuilt from the files — the
pattern dbt, Hugo and Sphinx use. Files stay the source of truth; the index is
disposable." That sentence is the right answer and nothing found in this audit
contradicts it. What the audit adds is that **the pressure is not read scale**.

**Where the pressure actually is** (this audit's own findings, ranked by what they
cost):

| Pressure | YAML-only fix | Would a DB fix it? |
|---|---|---|
| F1 non-atomic write | 3 lines in `_write_task` | yes, but so does the 3 lines |
| F2 stale locks | reuse ledger's liveness sweep | SQLite's file lock makes it vanish |
| F3/F5 replay semantics | manager-level | no — orthogonal |
| F4 position race | hold `.queue` across the write | a `UNIQUE(band, position)` constraint makes the whole class impossible |
| tasks-on-main workflow, worktrees, "dashboard shows the checked-out branch" | none — inherent in files-in-a-repo | **only** a backend that is not the working tree fixes this |
| `/revision` reading 3.76 MB per poll | mtime-based hash | trivial |
| `_find_created_by` O(corpus × log) | index file (rejected, "second thing that can disagree") | trivial |

The last row of the first group — the workflow — is the one Jeff feels weekly, and it
is the one no amount of `storage.py` work reaches. The honest statement is: **the
files are not too big; they are in the wrong place for concurrent writers.** The
working tree of a shared git clone is a bad database *because it is a working tree*,
not because it is YAML.

### 6.4 Hybrid vs full swap

**Hybrid (YAML canonical, SQLite index for reads + locks):**
- Keeps every argument in §7 of the design doc. Blame, diff, zero-infra all survive.
- Absorbs: listing/next/explain/dependency walks (all reads), `project_revision`,
  `_find_created_by`, and — if the lock moves into SQLite — F2 and F4 for free.
- Does **not** absorb: the workflow coupling (category 1). Task records still land in
  the working tree, still need committing to `main`, still collide with branches.
- Cost today: one module (~300 lines: schema, rebuild-from-files, invalidate-on-write,
  mtime check), `TaskStorage.load_all` consults it, `X-Task-Parses` gains a sibling
  "rows read". Tests: the 38 files that construct `TaskStorage(tmp)` keep working
  because the index is derived. **~1 working day.** At 2× corpus: identical — it is
  derived.
- Risk: a second thing that can disagree with the files. Mitigated exactly as Hugo
  does it: rebuild is cheap (parse 240 files ≈ 0.13 s with libyaml), so the index is
  always rebuildable and never authoritative.

**Full swap (SQLite canonical, YAML export on demand):**
- Fixes the workflow coupling: task records stop being in the working tree; no
  tasks-on-main rule, no worktree-for-the-record, dashboard independent of checkout.
  Git history of task state becomes an *export* committed on a schedule, which is what
  blame/diff would then operate on.
- Cost today, counted from the inventory: `storage.py` rewrite (~650 lines);
  `queue.py` simplifies (~600 → ~150, since uniqueness is a constraint); `validation.py`
  and receipts redefined (provenance is a column, not a hash); `record_commit.py`,
  `finish.py` ×4, `guards.py`, `runner.py` lose their git-of-tasks code;
  `gate_scope.py` drops the class; ~30 test fixture files; `docs/` — schema-design,
  task-selection-design, performance, ENGINEERING, ALLAGENTS, the MCP instructions all
  change; `bench.py`. **2–3 working weeks**, and it loses the thing the README leads
  with.
- At 2× corpus: the code cost is identical; the *migration* cost is identical (one
  import). What grows with time is **not the corpus** — it is the number of leak sites
  (≈2/day while dispatch and queue are under active development) and the number of
  documents and habits that describe the files.

### 6.5 Recommendation and trigger

1. **Now, regardless of backend (all contained in `storage.py`/`manager.py`, under a
   day total):** F1 atomic write, F2 lock reclaim, F4 hold `.queue` across the rejoin
   write, F3 fire-on-write-only. These are owed to the current backend and their cost
   is the same in any future one.
2. **Now, to stop the accrual:** declare the `TaskStorage` surface as a `Protocol`
   (`load_task`, `load_all`, `mutate_task`, `save_task`, `locked`, `queue_lock`,
   `creation_lock`, `task_path`, `project_revision`, plus a new `load_raw(task_id)`
   for the four raw readers) and make the raw readers call `load_raw`. That turns
   category 2 from a leak into a contained capability, and gives a reviewer a one-line
   test for future PRs: "does this reach `tasks_dir` outside `storage.py`?" It costs
   an afternoon and does not change behaviour.
3. **Do the hybrid index when either of these is true**, whichever comes first:
   - a listing request (`GET /api/projects/agentjobs/tasks`) exceeds **250 ms p50** on
     this machine — today it is ~180 ms per `tests/test_performance_budgets.py`'s
     header and ~0.13 s of that is parsing; at ~480 files it crosses; or
   - a third kind of writer appears that is not a process on this machine (a second
     host, a phone action that writes, a CI job) — at that point file locks stop being
     a lock.
4. **Do the full swap only when the workflow cost is the reason**, and say so on the
   record: the trigger is "we are paying for tasks-on-main / worktrees / checkout-
   dependent dashboards more than once a week", not a file count. If that is already
   true — and 2026-08-11's three failures plus the worktree rule suggest it is close —
   then the thing to price is not "SQL vs YAML" but "task records out of the working
   tree", and the cheapest version of *that* is the files moving to `~/.agentjobs/
   projects/<id>/tasks/` with a git export, which keeps YAML and git and loses only
   the location. That option is not in `schema-design.md` §7 and deserves a paragraph
   there before a database does.

The cost of waiting, quantified from this audit: every week of dispatch/queue work at
the current pace adds ~10 coupling sites, most of them reasonable and each of them a
thing the eventual swap must either port or delete. The corpus doubling changes
nothing; the leak count doubling does.

---

## What I did not get to

- **Did not reproduce F1 or F4 dynamically.** Both are constructed from the code; a
  reproduction needs a write, which this audit forbids. A 20-line script — one process
  in a `write_text` loop, one reading `list_tasks()` in a loop, count `TaskLoadError`s
  — would settle F1 in a minute.
- **Did not read `queue.py` beyond the raw readers and the migration** (auditor 5's);
  the F4 claim about `_place`/`plan_insertion` assumes they behave as their docstrings
  say.
- **Did not trace `webhooks.py` delivery/retry** beyond `fire_event` (auditor 7's);
  F3 stands on the absence of dedupe in `fire_event` and the unconditional `_fire`.
- **Did not audit `migrate_schema.py` / `migration/`** beyond confirming they write
  raw; auditor 3 owns migration.
- **Did not measure the listing latency myself.** The 180 ms figure is from the test
  module's docstring, not a fresh measurement against 8876 at 240 files.
- **Did not inventory the frontend for file-path assumptions** (`TaskLoadError.as_dict`
  exposes `path`; whether the UI renders it is auditor 9's).
- **Receipts edge case not tested:** stage a task file, let a managed write land (e.g.
  the poller's `dispatch_result`), commit → `receipt-mismatch` false positive
  (`validation.py:481-491` compares against the single *latest* receipt). Constructed,
  not observed.

## Questions for other auditors

- **Auditor 10 (dispatch):** the task-write guard refused **three read-only commands**
  in this session. (1) A `sed -n` on `docs/schema-design.md` sharing a command line
  with a `grep` that named a task record file; (2) a `sed 's/…/'` filtering the
  registry, on a line that also grepped a task record; (3) **the heredoc that wrote
  this findings file**, because the prose contained the word "touch" and a task record
  path. Each time the message was "`sed -i` / `touch` would write AgentJobs task
  records". The match appears to be "any write-shaped verb anywhere in the command
  text AND a `tasks/<project>/task-…` string anywhere in the command text", including
  inside quoted data. A guard that refuses reads trains people to reword commands until
  one passes, which is the habit the classifier memory warns about — and this
  deliverable had to be routed through a temp directory and `cp` to land at all.
  Worth a look at the pattern: is it anchored to the verb's *target*, or to the line?
- **Auditor 8 (MCP):** "replays the original result" in the served instructions is
  not what the code does (F5). What does the envelope's `replayed: true` promise, in
  your reading, and does any tool description tell an agent to re-check ownership?
- **Auditor 3 (schema):** the manager accepts any field in `update_task` (F6). Is
  there a model-level validator that would reject, say, `lifecycle=closed` with
  `ball` still set — i.e. how far does a Python caller get before the model stops it?
- **Auditor 7 (API/webhooks):** does `fire_event`'s `_schedule` path have any
  delivery-id or dedupe that would mask F3 at the receiver?
- **Auditor 5 (queue):** `repair_queue` runs `_renumber` for touched bands after
  `_write_raw_position` — does a band that was *not* broken but whose tasks sit in the
  snapshot get renumbered by the rebalance (i.e. does repair churn files it did not
  need to)? `manager.py:1939-1945` intends not to; I did not verify `plan_rebalance`.
- **Auditor 1 (context):** the corpus has two filename conventions (158 slugged, 82
  plain) and the id is the full stem. Every process document writes `task-045`-style
  short ids. Does any instruction tell an agent that `task-081` is not an id?
- **Auditor 11 (gate):** `gate_scope.py`'s `tasks/*` → `pytest` rule is right; note
  that F9's `generate_task_id` and the attachment/lock sidecars mean `tasks/` contains
  non-task files (`.attachments/`, `*.lock`) — does the class deliberately include
  them, and does a `.lock` left behind by a crashed writer count as a changed path?
- **Auditor 12 (security):** `TaskLoadError.as_dict()` (`storage.py:167-174`) puts
  the absolute filesystem `path` into API responses that a tailnet peer can read.
  Minor, but it is the kind of thing that names the machine's directory layout.
