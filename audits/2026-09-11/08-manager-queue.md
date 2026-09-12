# 08 — Manager, operations, and the queue

Big Dawg Audit II, night of 2026-09-11. Audited at commit `096f33ae` on `main`.

Scope: `manager.py`, `operations.py`, `receipts.py`, `actors.py`, `projects.py`,
`queue.py`, `queue_check.py`, `docs/task-selection-design.md`, `docs/agent-workflow.md`.

**Method.** Every "verified by running" item below was exercised against throwaway
SQLite stores under a scratch `AGENTJOBS_HOME` (so no registry, identity file or
database of the owner's was read or written). Scripts: `exp_inproc.py` (single process,
nine experiments), `race_driver.py` + `race_worker.py` (real concurrent processes on one
database file), `exp_259.py`, `exp_forge.py`. All under the session scratch directory;
none committed. The live store was read only through `agentjobs show`, `agentjobs list`
and `agentjobs queue check`.

**One-line verdict on what is sound.** The claim path is correct under real
multi-process contention (four processes, one winner, three refusals, no torn state),
and the queue's numbering survives three processes doing 45 interleaved create + move
operations with zero duplicates and zero errors. The SQL constraints have made every
form of queue corruption the design document worries about unrepresentable. What is
*not* sound is everything the idempotency contract promises beyond "no second write",
and a set of side doors through which the record can be made to say things the verbs
never did.

---

## F1 — The `operation_id` contract is still false in the three ways task-255 says

**Severity:** P2. **Confirms task-255** (still draft, still `human/spec`, 20 days old).
**Verified by running.**

Evidence, from `exp_inproc.py` E1–E3:

```
E1 handoff replay
  log entries after first handoff: 3
  log entries after replay      : 4   (one progress entry added in between)
  replay returned original result? False
  replay returned current task?    True
  webhook events after first: 1  after replay: 2
  events: ['task.handoff', 'task.handoff']
E2 close replay
  task.closed fired: 2 times for one close + one replay
E3 claim replay after release and re-claim by another agent
  replayed claim by agent-a returns owner = agent-b, lifecycle = active
  (no error, no superseded flag)
```

Where it comes from:

- `manager.py:1444-1544` (`handoff`) and `:1629-1682` (`close_task`): `_mutate` runs,
  `apply` returns `None` on replay, `store.py:526-527` returns `current` without
  writing, and then `_fire("task.handoff", ...)` at `manager.py:1535` / `:1678` runs
  unconditionally. Nothing distinguishes a write from a replay at the call site.
- No original result is stored: `operations.py:112-127` (`replay_or_conflict`) is a
  boolean, and the marker (`operations.py:71-73`) holds id, kind and fingerprint only.
- The webhook contract in the *schema* says the opposite of what the code does.
  `sqlstore/migrations/001_initial.sql:369-372` reads "Webhook outbox. Enqueued in the
  same transaction as the state change, so a replayed operation enqueues nothing (audit
  F3) ... (task-047, task-255)". The `webhook_outbox` table exists. **Nothing in
  `src/` writes to it** — `grep -rn webhook_outbox src/agentjobs` finds only the
  importer's table-clearing list. E4 confirms: after four fired events, `SELECT
  count(*) FROM webhook_outbox` is 0. The schema comment describes a fix that was
  designed, migrated and never wired.
- Coverage: `tests/test_idempotency.py` has 13 replay tests; none mentions a webhook,
  and none asserts anything about what a replay *returns* beyond "no second entry".
  `test_a_repeated_close_does_not_reject_itself_as_already_closed` would pass with any
  number of webhook deliveries.

What would this have caught? A handoff to `human/review` retried after a timeout
delivers two signed `task.handoff` webhooks and, when the notification receiver
ENGINEERING.md names as the extension point exists, two pushes. The E3 case is worse:
agent A's retried claim reports success on a task agent B now owns.

**Fix.** The one task-255 already wrote: make `_mutate` (or `replay_or_conflict`) return
whether it wrote, and gate the five `_fire` calls on it; either wire the outbox the
schema already declares or delete the table and its comment; on replay compare the
marker's entry id with the newest axis-moving entry and flag `superseded`. Add the test
that asserts one delivery per one write. The task needs promoting from draft — it has
had four witnesses since August and gained a fifth tonight.

## F2 — The reopen/reband position race is still there, and its failure mode has changed from silent corruption to an unhandled 500

**Severity:** P2. **Confirms task-253 in mechanism, refutes it in consequence.**
**Verified by running** (real concurrent processes).

`race_driver.py` RACE 2: process A patches a `medium` task to `priority=high`, with a
2-second sleep injected between `_rejoining_the_queue` returning and `_mutate` writing
(monkeypatched in the worker only; no source edited). Process B creates a `high` task
0.7 s in.

```
[reband] computed rejoin position 300 then sleeping 2.0
[create] CREATED task-005 position 300
[reband] EXC IntegrityError : UNIQUE constraint failed:
         task.project_id, task.priority_rank, task.queue_position
    duplicate slots: []
    check_queue: []
```

The code is exactly as task-253 describes: `manager.py:1258` takes `queue_lock()` —
which on SQL is one `BEGIN IMMEDIATE` transaction (`store.py:1046-1049`) — computes the
number, **commits**, and the write happens later in a *second* transaction at
`manager.py:1221`. A create or move landing between the two commits takes the number.

What changed since August: `ux_task_queue_slot` (`001_initial.sql:114-116`) makes the
duplicate unrepresentable, so the loser's transaction rolls back cleanly. The task-253
spec's consequence — "next/dashboard then refuse with QueueCorruptionError until
someone runs repair" — is **no longer true**; `check_queue` is empty afterwards.

The new consequence: `sqlite3.IntegrityError` propagates out of `update_task`. The
PATCH route at `api/routes/tasks.py:497-521` catches `TaskNotFoundError`,
`OperationConflictError`, `RevisionConflictError` and `ValueError` — `IntegrityError`
is none of those. `api/main.py:277-327` registers handlers for `ProjectError`,
`TaskLoadError`, `MutationError`, `Forbidden` and `RequestValidationError` only. So the
caller gets FastAPI's generic 500 with no `retryable` hint and no `current_task`.
*(The 500 is inferred from reading the handlers; the IntegrityError itself was
observed.)* This is reachable from the tailnet by any human PATCHing `priority` while
anyone else files a task in that band.

Contrast: `reprioritize` (`manager.py:2357-2393`) holds the queue lock across its
`_write_place`, and `move` (`:2127`) holds it across the whole verb. Only the generic
patch's rejoin path releases early.

**Fix.** Hold the transaction across compute-and-write: move the `_mutate` call inside
the `queue_lock()` block in `_rejoining_the_queue`'s caller, which on SQL is free
(`write()` is reentrant, `connection.py:164-200`). Separately, map `IntegrityError` to a
409 `retryable` refusal at the API boundary so the next race of this shape is a
retry rather than a stack trace. Update task-253's spec to say the corruption half is
gone and the 500 half is what remains, then promote it.

## F3 — Verb coverage holds at the HTTP boundary, not in the manager; and `add_log_entry` can write entries that say a verb ran when it did not

**Severity:** P2 for the forgeable entries, P3 for the manager-level patch.
**New** (the patch half is adjacent to task-259; the forgery half is not on record).
**Verified by running.**

*Manager-level patch.* `exp_inproc.py` E5, calling `TaskManager.update_task` directly:

```
PATCH ['ball','ball_reason','ball_prompt']  -> accepted; ball human | log entries added: 0
PATCH ['lifecycle','assignment','ball',...] -> accepted; lifecycle active owner agent-z | added: 0
PATCH ['archived']                          -> accepted; archived True on an ACTIVE task | added: 0
PATCH ['lifecycle','outcome',...,'queue_position'] -> accepted; lifecycle closed | added: 0
reopen via patch                            -> accepted; position 300, one queue_move entry
```

Every axis — lifecycle, ball, owner, outcome, archived, even `queue_position` — moves
through `update_task` with **zero** log entries unless an `operation_id` was supplied.
The docstring at `manager.py:1173-1178` says "the allowlist never gains
`queue_position`", but the allowlist is `api/models.py` `TaskUpdateRequest`
(`:381-400`), not the manager. Over HTTP and MCP (`task_update_content` goes through the
client to PATCH) the allowlist holds and I could not move an axis. There is no CLI
`update` command. So today the only callers who can reach this are in-process:
`dispatch/finish.py:1539` (branches only, benign) and any future one. The design
document's claim that "a task moves through the domain verbs or not at all" is a
property of one request model, not of the manager it describes.

*Forgeable entries.* `exp_forge.py`, through the public `add_log_entry`:

```
add_log_entry type handoff  -> ACCEPTED; task ball still agent/available
add_log_entry type decision, re=<queue_move id>, data={queue_anchor: strong}
  -> ACCEPTED; _move_provenance reports anchor = strong
keep_queue_move on the same warning-free move -> refused (as designed)
```

- `MANAGER_WRITTEN_LOG_TYPES` (`models_v2.py:271-278`) reserves transition, dispatch,
  dispatch_result and queue_move. **`handoff` is not reserved**, so a `handoff`-typed
  entry with `data={"ball": "human", "ball_reason": "review"}` lands while the axes
  stay `agent/available`. The record now contains a handoff that moved nothing.
  ALLAGENTS.md tells every agent to "read the `log[]` newest-first: the last `handoff`";
  `dispatch/handback.py:162-166` decides whether a settling run has human feedback
  waiting by scanning for the newest `HANDOFF` entry by a human actor. Over HTTP the
  route at `api/routes/status.py:583-589` passes `payload.type` and `payload.data`
  straight through; MCP alone refuses the type (`mcp/mutation_tools.py:145`).
- `stamp()` (`operations.py:130-139`) reserves exactly one data key, `operation`.
  `queue_anchor` (`queue_check.py:91-99`) is not reserved, so a caller can write the
  strong anchor that `keep_queue_move` (`manager.py:2245-2316`) exists to gate — the
  anchor "a person defended against a stated objection" — on a move that produced no
  objection. The reorder playbook (`playbooks/references/reorder.md` §6, acceptance
  "No strong anchor was moved") then treats the position as untouchable. Reachable over
  HTTP and MCP, both of which accept free-form `data`.

**Fix.** Add `HANDOFF` (and arguably `ANSWER`, `QUESTION`) to
`MANAGER_WRITTEN_LOG_TYPES` — E5 shows `answer`/`question` are already refused by
accident because their schema forbids the extra keys. Make `stamp()` reserve every
manager-owned data key (`operation`, `queue_anchor`, `kept_over`, and the axis names
`ball`/`ball_reason`/`lifecycle`) rather than one. For the manager, either give
`update_task` its own allowlist mirroring the API's or have it refuse any key in
`{lifecycle, ball, ball_reason, ball_prompt, outcome, archived, assignment,
queue_position}` — the reopen path is the only legitimate exception and it should be a
verb (`reopen`), which the docstring at `manager.py:1240-1241` already admits.

## F4 — A ready task handed to a human is still offered by `next` and claimable, and the claim erases the pending ask

**Severity:** P2. **Confirms task-259** (draft). **Verified by running.**

`exp_259.py`: create `ready`, `handoff` to `human/decision` with prompt "decide something
first":

```
ready task after handoff: lifecycle ready, ball human, decision
explain_next picks: task-001   skipped: []
claim succeeded; ball now agent/work,
  prompt: "Execute the spec; log progress and hand off when done."
```

`_skip_reason` (`manager.py:672-699`) tests lifecycle, open children, unmet needs,
eligibility and band. It never looks at `ball`. `handoff` (`:1491-1500`) refuses only a
closed task. So the pending decision is invisible to `task_next --why` (the `skipped`
list is empty because nothing was skipped), and `claim_task` (`:1420-1424`) overwrites
`ball_prompt` with `WORK_PROMPT`. The question survives only as a log entry.

**Fix.** As task-259 says: a `ready` task is claimable only when `ball == agent`, and
`_skip_reason` should return "held by human (decision)" so `--why` teaches the rule.

## F5 — Reserved actor ids are accepted from any caller; DELETE and deliverables still write unattributed

**Severity:** P3. **Confirms task-263 in part; refutes it in part.** Verified by
running for the manager half, inferred from reading for the HTTP half.

What task-332 fixed since August (refutes those lines of task-263): `PATCH` now
validates its actor (`api/routes/tasks.py:492-493`); `actor_disagreement`
(`capabilities.py:253-327`) refuses a run claiming a human id and a human claiming
another human; `human_identity` (`actors.py:257-362`) no longer falls through to
`default_user` for a proven-but-unmapped login.

What remains true:

- `validate_actor` (`actors.py:435-436`) returns `dispatcher`/`finisher` before any other
  check, and `actor_disagreement` sees them as `kind: agent` (`actors.py:423`), which a
  human principal may claim (`capabilities.py:313-327` only checks `require_human` or a
  human-kind claim). A person on bare loopback, or a run whose ledger record has no
  `agent_id` (`capabilities.py:348-354`), can write "AgentJobs finisher merged" entries.
  The docstring at `actors.py:397-399` — "nobody may act as it" — is not enforced
  anywhere I could find (`grep -rn RESERVED src/agentjobs/api src/agentjobs/mcp
  src/agentjobs/capabilities.py` is empty).
- `DELETE /tasks/{id}` (`api/routes/tasks.py:527-537`) takes no actor;
  `archive_task` (`manager.py:1779-1803`) closes an open task as `cancelled` by
  `"system"` and fires `task.closed` with `triggered_by: system`. E6 output:
  `('system', 'transition', 'Task archived.')`, webhook `{'triggered_by': 'system',
  'outcome': 'cancelled'}`.
- `mark_deliverable_complete` (`manager.py:1279-1306`) writes no log entry at all. E7:
  status `done`, log length unchanged.
- The CLI still falls back to `default_user` for `promote`, `queue move` and
  `reprioritize` (`cli.py:2963-2980`), which is the half of task-263 about agents being
  clocked as the human when they use the CLI.

**Fix.** `validate_actor` should take a `principal`-aware flag, or `actor_disagreement`
should refuse `RESERVED` ids unless the caller is in-process (the dispatcher and finisher
call the manager directly and never cross the wire). DELETE and deliverables take an
actor body. Update task-263's evidence list to strike what task-332 fixed, so its
remaining scope is honest, then promote it.

## F6 — `queue check`, `queue repair` and the whole "corruption is loud" chapter describe states SQL cannot hold

**Severity:** P3 (documentation and dead code). **Refutes task-258's premise; New as a
doc finding.** Verified by running.

E8 tried, with raw SQL inside the store's own transaction, to produce each corruption
the design names:

```
duplicate slot   -> IntegrityError: UNIQUE constraint failed: task.project_id, task.priority_rank, task.queue_position
NULL on open task-> IntegrityError: CHECK constraint failed: (lifecycle = 'closed') = (queue_position IS NULL)
position 0       -> IntegrityError: CHECK constraint failed: queue_position IS NULL OR queue_position >= 1
```

All three of `find_queue_problems`' kinds (`queue.py:480-518`: missing, not-positive,
duplicate) are unrepresentable as rows. Consequently:

- `assert_queue_integrity` and `QueueCorruptionError` are unreachable on every project
  now on SQL. `repair_queue` (`manager.py:2504-2563`) cannot find anything to repair;
  its own helper's docstring (`:2565-2575`) says so and keeps it "because the bands it
  renumbers can still be made untidy by importing a corpus that was untidy on disk" —
  but the importer would hit the same constraints. `agentjobs queue check` on the live
  project: "The queue is sound", exit 0, which is now the only answer it can give.
- `docs/task-selection-design.md:415-450` (§8 "Corruption is loud"), `:506` ("409 on
  queue corruption"), `docs/agent-workflow.md:695-725`, `docs/mcp.md:255`
  (`queue_broken`), ALLAGENTS.md:31-33 and README.md:90 all describe a failure the
  system can no longer produce, and tell agents to run a repair that cannot do anything.
  Nothing here is *wrong* in the sense of misleading a reader into a bad action; it is a
  chapter of the design that has become a tombstone.
- task-258 ("refuse instead of 'nothing claimable' over an all-broken corpus; baseline
  the three projects ...; repair gets a dry run"): its premise — open tasks with no
  position that rule 6 makes unloadable — cannot occur for a project on SQL. Whether it
  still applies to the projects the summary names depends on whether they have been cut
  over; `agentjobs storage status` answers that (I did not run it, see below).

**Fix.** Rewrite §8 as "corruption is unrepresentable", keep `queue check` as a
one-line sanity tool, and either delete `repair_queue` or reduce it to `rebalance`.
Close or re-scope task-258 after checking storage status for the projects it names.

## F7 — `docs/mcp.md` credits the `operation` ledger table with replay detection; the code reads the log

**Severity:** P4. **New.** Verified by running (E4) and reading.

`docs/mcp.md:234-237`: "The marker is stored durably with the record, in the
project-scoped `operation` ledger, so replay detection survives ...". The ledger *is*
written (`store.py:762-778`, `INSERT OR IGNORE`; E4 shows 4 rows after 4 operations)
but nothing reads it: `find_operation` (`operations.py:103-109`) scans `task.log`, and
`_find_created_by` (`manager.py:1024-1044`) still does the linear scan of every task
whose docstring apologises for YAML. The claim in the doc happens to be true (both
survive restarts) for the wrong reason, and `_find_created_by` is O(corpus) on every
create that carries an `operation_id` when a primary-key lookup exists one table away.

**Fix.** Read the ledger in `_find_created_by` (one indexed lookup) or drop the table.

## F8 — The claim verb decides dependency and child state outside its transaction

**Severity:** P4. **New.** Inferred from reading; not reproduced.

`claim_task` (`manager.py:1400-1401`) computes `_dependency_states()` and
`_open_children()` before `_mutate` opens the transaction, then uses them inside
`apply` (`:1416-1420`). The lifecycle check is fresh (inside the transaction) and that is
what makes the four-process race in `race_driver.py` RACE 1 come out right. But a child
created, or a `needs` dependency reopened, between the snapshot and the `BEGIN
IMMEDIATE` is invisible to the claim: the parent is claimed with `WORK_PROMPT` rather
than the supervisor prompt, or the task is claimed with an unmet need. The window is
milliseconds and the harm is a wrong `ball_prompt`, which is why this is P4 — but the
comment at `:1302-1305` says preconditions are checked "*inside* the lock against a
fresh read", and two of the four are not.

**Fix.** Compute both inside `apply` from `list_tasks_uncached()`; the move verb already
does this (`_move_facts`, `:1940-1960`) for the same reason.

## F9 — Things that are sound, stated once

Verified by running:

- **Claim race, cross-process:** four processes, `BEGIN IMMEDIATE` serialises them, one
  `CLAIMED`, three `not available to claim ... owned by agent-2`. No torn row.
- **Create-vs-move race for a band position:** three processes, each 15 × (create in
  `high`, move to top); 45 creates, 45 moves, 0 errors, 0 duplicate slots, and
  `_place`'s single rebalance retry (`manager.py:1842-1875`) worked as designed —
  top-of-band positions went 5900, 2950, ..., 5 and never collided.
- **Operation conflict detection:** same id with a different actor or verb raises
  `OperationConflictError` and writes nothing (E3, E4).
- **`keep_queue_move`** refuses a warning-free move, as documented (F3 shows the side
  door, not a flaw in the verb).
- **`_local` project id** cannot be registered: `_ID_PATTERN` (`projects.py:32`)
  requires a leading letter or digit, so the CLI/server fallback id can never collide
  with a registered project. `resolve_default` (`projects.py:204-231`) refuses to guess
  between several projects rather than picking one.

---

## What I did not get to

- **HTTP-level reproduction of F2's 500.** The reband window cannot be widened inside a
  separate server process without editing source, so the 500 is inferred from the
  handler list rather than observed over the wire.
- **`agentjobs storage status`** for the projects task-258 names (job-hunting,
  product-strategy, fantasyfootball). Whether F6's refutation extends to them depends on
  their backend.
- **`receipts.py` entirely.** It documents receipts for "every task file AgentJobs
  writes"; there are no task files. It is still imported by `cli.py`, `queue.py`,
  `taskfiles.py` and `validation.py`. I did not establish whether any of those paths are
  live on SQL or whether the module is dead code that `validate` still consults.
- **`queue_listing`, `dependency_facts` and the `--why` output for eligibility
  restrictions** beyond one smoke test (E9 confirmed `restricted to agent-q` on claim; I
  did not check `explain_next(agent=...)` reports the same string).
- **`redact`** (`manager.py:1684-1777`), the one verb that reaches into the log — not
  exercised.
- **`docs/agent-workflow.md`** beyond the corruption section: 1,149 lines, read only by
  grep.
- **Webhook signing and delivery** (`webhooks.py`): out of scope, but F1 depends on
  `fire_event` having no dedupe, which I took from task-255's evidence rather than
  re-reading.
- The runbook's instruction to search the backlog with `agentjobs search` — there is no
  such command (`No such command 'search'`). I used `agentjobs list --lifecycle` three
  times and grepped titles; a description-level search of 149 open tasks was not done,
  so a duplicate of F3's forgery finding could exist under a title I did not match.

## Questions for other auditors

- **Webhooks auditor:** is `webhook_outbox` (declared in `001_initial.sql:375`) on your
  map as unused? F1 says nothing writes it; if your reading of `webhooks.py` disagrees,
  one of us is wrong.
- **API / authorization auditor:** does anything between the front door and
  `api/routes/status.py:583` restrict `LogAppendRequest.type` or `.data`? I found no
  guard; if one exists in middleware, F3's HTTP reach is narrower than stated.
- **Dispatch auditor:** `dispatch/handback.py:162-166` treats the newest `HANDOFF`
  entry by a human as feedback waiting. Given F3 (a human-attributed `handoff` entry can
  be appended without moving the ball), can a settling run be tricked into re-dispatching
  itself, or does the `after_entry` bound cover it?
- **Storage auditor:** `_find_created_by` scans the corpus on every `operation_id`
  create (F7). On the live project (~450 tasks) what does that cost per create, and is it
  in your benchmark?
- **Documentation auditor / auditor 01:** task-253, task-255, task-258, task-259 and
  task-263 are all still draft. Tonight's evidence changes the *scope* of three of them
  (253's consequence, 258's premise, 263's evidence list). Whoever writes the synthesis
  should say which of these are promoted, re-scoped, or closed, rather than leaving a
  sixth witness on each.
- **Playbooks auditor:** the reorder playbook honours strong anchors by reading
  `data.queue_anchor`. F3 shows the anchor is forgeable through `task_log_append`. Does
  the playbook's prompt tell the model to verify the anchor came from `queue keep`, or
  does it take the data key at face value?
