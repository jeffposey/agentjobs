# Auditor 3 — Schema v2 & validation

**Scope:** `src/agentjobs/models_v2.py`, `validation.py`, `schema_tolerance.py`,
`migration/`, `migrate_schema.py`, `docs/task-schema.md`, `docs/schema/`, plus the
enforcement sites those depend on (`manager.py` verbs, `storage.py` write path, the API /
MCP / CLI handoff surfaces) — read only as far as needed to answer "where is this
invariant enforced".

**Method:** read the files above in full; diffed the LinkML schema against the Pydantic
model by script; probed the model and the manager verbs in a throwaway task directory
with `AGENTJOBS_HOME` pointed at a scratch dir (no real state touched); ran `agentjobs
validate` (read-only) against the live corpus. Every claim below cites a `file:line` or a
command whose output is quoted. Stance per the shared preamble: for each check, *what
would this have caught?*

**Headline:** the model's six consistency rules are genuinely enforced at every write
(`storage.mutate_task` re-validates), and the LinkML/Pydantic pair is in step today. The
real findings are one tier up: the rules constrain *pairs* of axes, not the
*combinations* the verbs are supposed to be the only source of, so several incoherent
states are reachable through ordinary verbs; the tolerance mechanism built on
2026-08-19 does not cover the enum that was widened on 2026-08-21; the manager's generic
patch has no allowlist of its own; and the validator contradicts the write path on the
product's own reserved actors, which is why it currently reports 221 problems on the
repository's own backlog.

| Sev | # | Title |
|---|---|---|
| P1 | 1 | Tolerant reader rejects an unknown `ball_reason` — the exact enum widened 2026-08-21 |
| P2 | 2 | A held task (`agent/hold`) is offered by `get_next_task` and claimable; claim silently overwrites the hold |
| P2 | 3 | Ball/reason is not tied to lifecycle: `active+agent/available`, `draft+agent/work`, `ready+agent/work` all constructible through `handoff` |
| P2 | 4 | `manager.update_task` has no allowlist: accepts `lifecycle`, `ball_prompt`, `archived`, `outcome`, `log: []`; a reopen writes no `transition` entry |
| P2 | 5 | `agentjobs validate` flags the product's own reserved/internal actors (`dispatcher`, `finisher`, `system`) — 96 of its 221 live findings |
| P2 | 6 | Re-running `migrate-schema` on a half-migrated corpus produces duplicate `queue_position`s |
| P2 | 7 | Parent claimability: code and `docs/task-schema.md` say "claimable by name"; `GLOBAL-AGENTS.md` says "not claimable while a child is open" |
| P3 | 8 | `migrate_schema` writes outside `TaskStorage`: no lock, no receipt, non-canonical bytes; `storage.py` docstring claims otherwise |
| P3 | 9 | `TestTheRealCorpus` in `test_migrate_schema.py` skips every file — 0 of 247 are v1 — and asserts nothing (decoration) |
| P3 | 10 | Nothing checks that `docs/schema/v2`, `schema/generated` are regenerated output; the LinkML cross-check cannot see the dispatch payload models |
| P3 | 11 | `docs/task-schema.md` drift: "seven required fields", "manager validates category", "update_task cannot touch the axes", five rules listed of six |
| P3 | 12 | `test_schema_skew` covers one enum in one untyped position; parametrising it over the top-level axes would have caught #1 |
| P3 | 13 | Rule 5 does not cover `closed`: a closed task may keep `assignment.owner`; `LogEntry.actor` accepts `""` |
| P4 | 14 | Small inconsistencies: "five" vs "six" rules in `models_v2.py`; stale `task-050` comment in the regen script; tolerated unknown `Priority` makes `priority_rank()` raise |

---

## 1. The four axes — where each invariant is enforced

Legend: **model** = `Task._check_consistency` / `_check_log` / `_check_parent`
(`models_v2.py:728-857`), re-run on every managed write by
`storage.mutate_task` (`storage.py:469-471`) and on load (`storage.py:329`);
**verb** = a `TaskManager` method precondition; **validate** = `validation.py`
(corpus-wide, opt-in, not in the gate); **edge** = API request model or MCP input
schema only.

| Invariant (as the docs state it) | Enforced | Constructible anyway? |
|---|---|---|
| `ball` absent iff `closed` (rule 1) | model `:741-748` | no |
| `ball_reason` required with `ball`, scoped to holder (rule 2) | model `:750-768` | no — but see #1 for the tolerant reader |
| `outcome` set iff `closed` (rule 3) | model `:770-780` | no |
| `ball_prompt` required unless `agent/available` (rule 4) | model `:787-794` | no |
| `owner` empty in draft/ready, present in active (rule 5) | model `:796-803` | **closed+owner** is accepted (#13) |
| `queue_position` present iff open, ≥1 (rule 6) | model `:805-822` | no; uniqueness only via `validate` `:118-213` |
| log ids unique/ascending, `re` earlier & existing | model `:826-850` | no |
| not own parent; no parent cycle; parent exists | model `:852-857` (self); verb `_validate_parent` `manager.py:717-752` (cycle, existence) | a hand-written file with a dangling/cyclic parent loads; `validate` `:279-312` reports it |
| `transition`/`dispatch`/`dispatch_result`/`queue_move` only manager-written | verb `add_log_entry` `manager.py:1990` via `MANAGER_WRITTEN_LOG_TYPES` | a file carrying a fake `transition` loads fine (by design; the model cannot know provenance) |
| lifecycle moves only through claim/handoff/release/promote/close | verb preconditions `manager.py:1203,1276,1318,1364,1408` | **yes — `update_task`** (#4) |
| "parent not claimable while a child is open" | **not enforced, by decision** (task-164, `manager.py:1180-1195`); `get_next_task` skips it (`:537-539`) | claimable by name |
| `hold` is not workable — "auto-dispatch skips it and a manual dispatch is refused" | `dispatch/auto.py:199`, `dispatch/guards.py:184` | **`get_next_task` and `claim_task` ignore it** (#2) |
| "the state axes move only through the verbs" / "limbo is unrepresentable" | pairs of axes, yes; *combinations*, no | **yes** (#3) |

The pattern the brief asked about — *an invariant enforced only at one edge is
constructible through another* — shows up three times: the content-field allowlist
(#4), the `hold` check (#2), and `agent/available` (#3, MCP refuses it, API and manager
accept it).

### Finding 1 — P1: the tolerant reader rejects an unknown `ball_reason`

**Evidence.** `schema_tolerance.py` and `docs/task-schema.md:354-383` promise that a
client older than the service keeps working when an enum is widened. Rule 2 defeats
that for `ball_reason`:

```
models_v2.py:762-768
    allowed = BALL_REASONS.get(self.ball)
    if allowed is not None and self.ball_reason not in allowed:
        raise ValueError(f"ball_reason '{self.ball_reason.value}' does not belong to ...
```

The `.get` degrade on the line above it (`:757-761`) handles an unknown **ball**. An
unknown **reason** under a known ball is a pseudo-member that is not in the frozenset,
so the rule raises. Reproduced in memory:

```
with tolerant_enum_values():
    Task.model_validate({... "ball": "agent", "ball_reason": "warp", ...})
-> Value error, ball_reason 'warp' does not belong to ball 'agent'; expected one of:
   answer, available, hold, redirect, revise, work
```

(unknown `ball` = `"robot"` in the same harness is tolerated, `display_status` degrades
to `Active`.)

**Why P1.** `BallReason` is the enum that was widened two days after tolerance shipped:
`a79bfc5 2026-08-21 feat(schema): give the agent-side ball_reason three more values`
(tolerance: `ddc4d32 2026-08-19`). Any MCP server or `TaskClient` process started before
that commit gets the 2026-08-19 failure back — `retryable: false` from the client's own
validator against a service that is serving the task fine — for every task whose
top-level `ball_reason` is `hold`, `revise`, `answer` or `redirect`. Today that is one
live task (`grep -l '^ball_reason: hold'` over the task files in `tasks/agentjobs/` →
`task-233`), and it will be every task a human puts on hold or sends back. The field is
checked on every parse, so the stale process cannot read, list-by-mutation-envelope, or
hand off that task. (`log[].data.ball_reason` is **not** affected: `data` is
`Dict[str, Any]`, `models_v2.py:629`.)

**Fix.** Inside a tolerant context, skip rule 2's membership check when `ball_reason`
is a pseudo-member (e.g. `self.ball_reason not in BallReason.__members__.values()`), or
have `_missing_` mark pseudo-members and have rule 2 test that flag. Add the
parametrised skew test from #12 so the next widened enum is caught before a session is.

### Finding 2 — P2: a held task is offered by `get_next_task` and claimable

**Evidence.** `_skip_reason` (`manager.py:520-548`) tests lifecycle, open children,
unmet needs, eligibility and band — never `ball_reason`. `claim_task` (`:1203-1224`)
tests lifecycle `READY`, eligibility and needs, then unconditionally writes
`ball_reason = WORK`. The API's `hold` route (`api/routes/tasks.py:847-878`) goes
through `_send_back` → `manager.handoff`, which checks only `is_open`
(`manager.py:1276`), so a `ready` task can be held. Reproduced:

```
after hold on a ready task: lifecycle=ready ball=agent/hold display='On hold'
get_next_task offers the held task? True
claim on held task: ACCEPTED, ball_reason now work
```

`docs/task-schema.md:66-67` says "`hold` is the one agent-side reason that is not
workable, so auto-dispatch skips it and a manual dispatch at a held task is refused".
Both true (`dispatch/auto.py:199-205`, `dispatch/guards.py:184`) — and both are the
*dispatch* door. `agentjobs next`, `task_next`, and an interactive agent claiming by id
walk through the other one, and the claim erases the hold with no trace except the
handoff entry a reader would have to notice. A dispatched agent is told to call
`task_next` first (MCP instructions), so the mechanism the doc relies on is exactly the
one that ignores the hold.

**Fix.** `_skip_reason`: `if task.ball_reason is BallReason.HOLD: return "on hold"`
before the eligibility checks; `claim_task`: refuse with the same text. One test each,
plus one that `explain_next` reports the reason.

### Finding 3 — P2: ball/reason is not tied to lifecycle

Rule 5 ties **owner** to lifecycle. Nothing ties **ball** to lifecycle, so
`handoff` — which checks only `is_open` (`manager.py:1276-1279`) — can produce states
the verbs were designed to make impossible. All reproduced through `TaskManager`:

| Handoff | Result | `display_status` | Consequence |
|---|---|---|---|
| active task → `agent/available` | `lifecycle=active owner=claude ball=agent/available prompt=None` | **`Ready`** | Not offered by `/next` (lifecycle ≠ ready), owner still set, no ask. Looks ready, is not. |
| draft task → `agent/work` | `lifecycle=draft owner=None ball=agent/work` | **`In progress`** | `auto.py:193` tests `ball is AGENT and is_open and not HOLD` → **eligible for auto-dispatch** at an unpromoted draft the agent then cannot claim (`claim` requires `READY`). |
| ready task → `agent/work` (what `POST /resume` and `/approve` write, `tasks.py:673,903`) | `lifecycle=ready owner=None` | `In progress` | same as above; and `approve` on a never-claimed task is a legal call. |

The surfaces disagree about the first row: the MCP schema's agent branch is
`["work","revise","answer","redirect","hold"]` with the comment "agent/available is
task_release, not a handoff" (`mcp/mutation_tools.py:112-126`), while
`HandoffRequest.ball_reason: BallReason` (`api/models.py:647`) and the manager accept
it. One edge enforces what the others do not.

**Fix** (pick one; the first is the schema-first answer): add a rule 7 to the model —
`agent/available ⇔ lifecycle ready`; `agent/work|revise|answer|redirect|hold ⇒ lifecycle
active` (or `ready` for `hold`, if a held unclaimed task is wanted — decide and write it
down) — so every surface inherits it. Or refuse in `manager.handoff` and have the
human-action routes precondition on `active`. Either way, remove `available` from
`HandoffRequest`'s accepted values or document why the API differs from MCP.

### Finding 4 — P2: `manager.update_task` has no allowlist of its own

**Evidence.** `update_task(**updates)` does `payload.update(updates)` then
`Task.model_validate(payload)` (`manager.py:1000-1004`). The allowlist the docstring
refers to (`:970`, "the allowlist never gains `queue_position`") lives only at the
edges: `TaskUpdateRequest` (`api/models.py:349-367`) and MCP `CONTENT_FIELDS`
(`mcp/mutation_tools.py:96-109`). Through the manager — the CLI, `dispatch/finish.py:935`,
tests, any script — it accepts anything the model validates:

```
update_task(lifecycle="ready", ball="agent", ball_reason="available", outcome=None) on a closed task
-> ACCEPTED: lifecycle=ready pos=300 log types=['transition', 'transition', 'queue_move']
update_task(ball_prompt="smuggled") -> ACCEPTED
update_task(archived=True)          -> ACCEPTED
update_task(log=[])                 -> ACCEPTED   (the append-only log, emptied)
```

The reopen is not an accident: `_rejoining_the_queue` (`manager.py:1020-1070`) is
written for "a closed task whose `lifecycle` is patched back to something open" and
says "there is deliberately no `reopen` verb yet, so this generic patch is the only
path". But a reopen through it writes a `queue_move` entry and **no `transition`
entry** — the two transitions above are the create and the close — so the one axis
move the record is supposed to always explain is the one it does not.
`docs/task-schema.md:402-403` ("`update_task()` edits content fields … and deliberately
cannot touch the axes") is false at this layer.

**Fix.** Move the allowlist into the manager as one module-level set (the same pattern
as `MANAGER_WRITTEN_LOG_TYPES`, consulted by the API and MCP rather than copied), and
make reopen a verb that writes its `transition`. Until then, at minimum refuse `log`,
`lifecycle`, `ball*`, `outcome`, `archived`, `queue_position`, `assignment`, `schema`,
`id`, `created` by name.

### Finding 13 — P3: two holes in the cheap rules

- `closed` + `assignment.owner: claude` validates (rule 5 enumerates draft/ready/active
  only, `models_v2.py:797-803`). `close_task` clears the owner (`:1417`), so this is
  reachable by hand edit or `update_task(assignment=…)` — the latter happens to fail
  today only because the patch replaces the whole `Assignment`. Add `closed ⇒ owner
  empty`.
- `LogEntry.actor: ""` validates (`:620`), and `add_log_entry(actor="")` writes it.
  `validate` reports it only when the project configures actors. `min_length=1`.

---

## 2. Tolerance policy

**What it accepts.** Inside `tolerant_enum_values()`, `ValueEnum._missing_`
(`models_v2.py:73-103`) turns any unknown string into a pseudo-member for *every*
`ValueEnum` subclass — all seventeen of them, not only the dispatch enums the incident
was about. Outside the context it returns `None` and Pydantic rejects as before. The
only caller is `TaskClient._parse_task` (`client.py:678`); grep finds no other entry
point in `src/`, `scripts/`, `tests/`, or the frontend. Storage never enters it
(`storage.py:25,329` call `load_task` bare), so the "must stay off on the write path and
in storage" rule in the module docstring holds. **Examined, nothing found** on the
strict/tolerant boundary itself.

**Where tolerated data flows next.** The pseudo-member compares equal to its string,
`.value` is the string, and Pydantic serialises it verbatim, so a tolerated task
round-trips **unchanged, not normalised** — an older MCP server echoes `posture:
warp_drive` back in its mutation envelope. MCP *read* tools pass the service's JSON
through without parsing (`tests/test_schema_skew.py` docstring), so only the
mutation-result path and the client's typed methods see it. The client never writes a
whole `Task` back (`grep model_dump client.py` → none), so tolerated values cannot reach
storage from a client.

**Where it degrades badly.**

- `ball_reason` — #1 above. Rejected, not degraded.
- `Priority` unknown → accepted, but `Task.priority_rank()` does
  `PRIORITY_RANK[self.priority]` → `KeyError: <Priority.urgent: 'urgent'>` (reproduced).
  Only matters if a client sorts; I did not find a client-side caller. P4.
- `Lifecycle` unknown → `closed = self.lifecycle is Lifecycle.CLOSED` is False, so the
  task is treated as open and must carry a ball — acceptable degrade.
- `LogEntryType` unknown → `DISPATCH_PAYLOADS.get` returns None, entry accepted. Good.

### Finding 12 — P3: the skew test proves tolerance for one enum in the easiest position

`tests/test_schema_skew.py` rewrites `log[].data.posture` — a value that no cross-field
rule ever reads. Every top-level axis enum (`Ball`, `BallReason`, `Lifecycle`,
`Outcome`, `Priority`) participates in `_check_consistency`, and none is tested. The
transport already recurses into anything (`NewerServiceTransport._rewrite`); parametrise
the rewrite over `("ball_reason", "warp")`, `("ball", "robot")`, `("lifecycle",
"zombie")`, `("priority", "urgent")`, `("outcome", "vanished")`. The `ball_reason` case
fails today; that is the point.

---

## 3. Migration

**v1 → v2 completeness.** `migrate_schema.py` accounts for every v1 field
(`MAPPED_FIELDS` / `INTENTIONALLY_DROPPED`, `:33-62`), refuses unknown ones
(`:422-428`), and `verify_no_loss` (`:612-685`) checks every string and every
collection count. The whole corpus converts in memory and nothing is written unless
every file passes (`:869-875`). This is good machinery. Examined, nothing found on
completeness.

**Idempotence.** A v2 file raises `AlreadyV2Error` and is *skipped*, not failed
(`:771-773`), so re-running on a fully converted corpus converts 0, skips N, writes
nothing (reproduced: "pass 3 (all v2): converted=0 skipped=3 failures=0"). Idempotent.

**Does anything still emit v1 shapes?** Grep for `status_updates|human_summary|
assigned_to|waiting_for_human|under_review|in_progress` outside the migrator hits only
dashboard *stat names* (`api/models.py:93-95`, `dashboard.py:141-145`,
`Dashboard.tsx:238-239`) — counts derived from v2 axes, not v1 records. The markdown
importer (`migration/converter.py`) builds v2 `Task` objects and claims queue positions
under the queue lock through storage (`migration/__init__.py:86-91`). **Examined,
nothing found.**

### Finding 6 — P2: a half-migrated corpus gets duplicate positions on the second run

**Evidence.** `_assign_queue_positions` (`migrate_schema.py:784-818`) plans positions
over **only the records this run converted**; already-v2 files in the same directory
are skipped before it runs and never seen by `plan_queue_migration`. Reproduced with
three v1 files, converting one, then the directory:

```
pass 1: Converted 1
pass 2: Converted 2, Skipped 1 (task-901-x)
queue_position after pass 2: {'task-901-x': 100, 'task-902-x': 100, 'task-903-x': 200}
validate_corpus: [... 'queue-duplicate', 'queue-duplicate']
```

The CLI's own help says "a corpus half-converted is worse than one not converted" and
the all-or-nothing write is there to prevent it — but a partial corpus is still the
ordinary shape of a *retry* (fix the one file that failed, run again), and the retry
corrupts the queue. `plan_queue_migration` already supports "below whatever is already
there" (`queue.py:701-704`) if handed the positioned siblings.

**Fix.** Feed `_assign_queue_positions` the existing v2 records from the target
directory (read through `TaskStorage.list_tasks_uncached`) alongside the converted ones,
as `migration/__init__.py:_claim_next_position` already does for the markdown importer.

### Finding 8 — P3: the migrator writes outside `TaskStorage`

`migrate_corpus` writes with `destination.write_text(yaml.safe_dump(data, ...))`
(`migrate_schema.py:871-874`): no per-task lock, no receipt, and not the canonical dump
(`exclude_none`, `mode="json"`, alias handling in `storage.py:489-496`). Reproduced:

```
receipt for task-901 after migrate: None
non-canonical task-901-x: ['+archived: false', '+assignment:', '+  eligible: []', ...]
```

So every migrated file fails `validate` with `non-canonical-serialization` and would
fail `validate --staged` with `no-write-receipt` — the gate that exists to detect
unmanaged writes fires on the product's own migrator. `storage.py:497-499` says the
receipt is recorded "at the single point every managed write passes through, so the
manager, the API, the CLI, MCP, the GUI and the schema migrator all produce receipts" —
the last item is false. **Fix:** `TaskStorage(destination.parent).save_task(load_task(data))`.

### Finding 9 — P3: `TestTheRealCorpus` (migration) is decoration

`tests/test_migrate_schema.py:417-437` iterates every real file and `continue`s on
`v1.get("schema")`. `grep -L "^schema: 2"` over every task file in `tasks/agentjobs/`
and `tasks/test-data/` → **0 of 247** lack the stamp. The loop body never executes; the
assertion is `assert not []`. It would catch nothing and has caught nothing since
task-052. Either pin a v1 fixture corpus under `tests/` (a `git show <pre-052>:tasks/...`
snapshot) or delete it.

---

## 4. Doc / model drift

**LinkML vs Pydantic** (scripted diff of `schema/agentjobs-v2.yaml` against
`models_v2.py`): every shared enum and class has identical members/attributes —
`Lifecycle`, `Ball`, `BallReason` (13 values), `Outcome`, `Priority`, `LogEntryType`
(11), `Task`, `LogEntry`, `Attachment`, all value objects. Differences are all
one-sided by design: LinkML-only `Actor`/`ActorKind`/`AnyValue` (D4); Pydantic-only
`DispatchData`, `DispatchResultData`, `DispatchSelectionData`, `DispatchCandidateData`
and their four enums, because LinkML types `data` as `AnyValue`. **In step today.**

**Regeneration freshness.** `schema/agentjobs-v2.yaml`, `schema/generated/*`,
`docs/schema/v2/*`, `docs/task-schema.md` and `models_v2.py` were all last touched in
the same commit (`a79bfc5`), and `docs/schema/v2/enums/BallReason.md` /
`agentjobs-v2.schema.json` / `models_v2_preview.py` all carry the three new reasons. So
the committed docs are regenerated output **as of today**.

### Finding 10 — P3: nothing checks that, and the LinkML cross-check has a blind spot

- No gate stage, `gate_scope.py` rule, or test references `regen-schema-docs.sh`,
  `schema/generated`, or `docs/schema/` (grep → empty). The regen script validates the
  corpus with `linkml-validate` and exits 1 on failure — but only when somebody runs
  it. The guard that exists is `tests/test_models_v2.py::TestAgreesWithTheLinkMLSchema`
  (model ↔ `schema/agentjobs-v2.yaml` source) and
  `TestDocumentationMatchesTheModel` (every enum value and `Task` field name appears
  in `task-schema.md` as a backticked token). That catches a *missing* value, not a
  wrong sentence (#11), and nothing covers the generated tree.
- `models_v2.py:10-13` says "if they drift, that test fails". True for the half LinkML
  models; the dispatch payloads — the part the docs say is "validated, not merely
  documented" (`task-schema.md:288-290`) — are invisible to it. Either model the
  dispatch payloads in LinkML (as classes the `data` slot can range over) or state the
  limit in the docstring.
- **What it would have caught:** a regen forgotten after the next enum widening. That
  has not happened yet; this is the rule-of-three check. Cheapest fix: a test that
  `gen-json-schema` output equals the committed `agentjobs-v2.schema.json` (LinkML is
  already a dev dependency, the test suite already imports `linkml.validator`).

### Finding 11 — P3: `docs/task-schema.md` statements that are not true of the code

| Line | Says | Reality |
|---|---|---|
| 207-209 | "Only seven fields are required: id, title, created, updated, category, spec.summary, spec.description" | A file with exactly those fails rule 1 (`ball is required`) and rule 6 (`queue_position is required`). Reproduced. Ten fields, or say "seven plus the open-task axes". |
| 96-108 | "Consistency rules … 1–5" | The model enforces six (`models_v2.py:730`); rule 6 is described in the field table (line 80) but missing from the list. |
| 81 | "`category`, `tags` … Validated against config by the manager, not the model" | The manager does not validate `category` anywhere (`grep categor manager.py` → defaults only). Only `validate` does (`validation.py:244-260`). Live corpus: 84 `unknown-category` findings. |
| 113-115 | "re-checked on every write … `mutate_task` re-validates" | True (`storage.py:469`). |
| 402-403 | "`update_task()` … deliberately cannot touch the axes" | False at the manager (#4). True of the API and MCP edges. |
| 300-303 | "`dispatcher` is a reserved actor id, valid in every project" | True for `validate_actor` (`actors.py:202`); false for `agentjobs validate` (#5). |
| 84 | parent "with an open child is never offered by `/next`, but a caller that names it can claim it" | Matches code (task-164). Contradicts `GLOBAL-AGENTS.md:125` (#7). |
| 223-227 | manager-written log types "rejected by the API" | Enforced in the manager (`manager.py:1990`), so every surface. True. |
| 66-67 | `hold`: "auto-dispatch skips it and a manual dispatch … is refused" | True, and incomplete (#2). |

### Finding 7 — P2: the parent-claimability rule is stated both ways

`manager.py:1180-1195` (task-164) and `docs/task-schema.md:84` and
`docs/schema-design.md:89`: a parent with open children *is* claimable by name; only
`/next` skips it. `C:/ai/shared/GLOBAL-AGENTS.md:125`: "`parent` makes a real umbrella
task: a parent is not claimable while a child is open." Every agent session loads the
second sentence unconditionally and the first only on demand. An agent told "work
task-160" will believe the claim must fail, or — worse — treat a successful claim as
evidence the children are closed. Auditor 1 owns the static stack; the fix is one
sentence there.

### Finding 5 — P2: `agentjobs validate` contradicts the write path on reserved actors

**Evidence.** `AGENTJOBS_HOME=<scratch> poetry run agentjobs validate` on the live
corpus: **221 problem(s) across 240 task file(s)** — `124 unknown-actor`, `84
unknown-category`, `13 non-canonical-serialization`. The unknown-actor breakdown:

```
 70 'system'      26 'dispatcher'      10 'jeff'      10 'Codex'      6 'Claude'      2 'human'
```

`system` is what the manager itself writes (`manager.py:906,957`; migrator `:553`);
`dispatcher` and `finisher` are `RESERVED` and accepted by every write path
(`actors.py:175-204`). `validation._check_taxonomy` uses `load_actors(config)`
(`validation.py:261`), which **excludes** `RESERVED` on purpose (`actors.py:178-185`, so
a fresh project's vocabulary stays empty) and has never heard of `system`. So 96 of the
221 findings are the validator objecting to the product's own writes. The remaining 28
(`jeff`, `Codex`, `Claude`, `human`) are real historical drift.

`tests/test_validate.py::TestRealCorpus` (`:659-690`) filters to structural rules so
the gate stays green, and its docstring says why: "failing this test on them would only
teach people to skip it". That is the right call for the test and the wrong state for
the CLI — the command a doc tells you to run "if you ever suspect a file was shaped by
something else" (`task-schema.md:11-12`) prints 221 lines on a healthy corpus, which
teaches the same lesson.

**Fix.** `_check_taxonomy`: treat `RESERVED` and `system` as known (or reserve
`system` properly in `actors.py` — it is an actor the product writes as, which is the
definition). Then decide the category policy: either `.agentjobs/config.yaml` declares
the eleven categories actually in use, or the manager enforces the list at create/update
so the doc at line 81 becomes true. Either way the 13 non-canonical files deserve a look
(hand edits, or an older writer? I did not check — see below).

---

## 5. `ball_reason` vocabulary across surfaces

| Surface | Vocabulary | Scoping to holder |
|---|---|---|
| Model | 13 values, `BALL_REASONS` map (`models_v2.py:123-165`) | rule 2, every load and every write |
| LinkML | 13, same names, scoping described in prose (`agentjobs-v2.yaml:85-123`) | not expressible; relies on the model |
| REST `POST /handoff` | `HandoffRequest.ball_reason: BallReason` — all 13 (`api/models.py:647`) | model only (a `human/work` returns 400 from the `ValueError`) — **accepts `agent/available`** |
| REST human actions | fixed per route: approve→`work`, request-changes→`revise`, answer→`answer`, redirect→`redirect`, hold→`hold`, resume→`work` (`api/routes/tasks.py:673-903`) | n/a |
| MCP `task_handoff` | discriminated union: agent `[work, revise, answer, redirect, hold]`, human `[spec, review, decision, approval, input]`, external `[dependency, service]` (`mcp/mutation_tools.py:112-152`) | at the schema **and** the model — the only surface that also refuses `available` |
| Frontend generated type | flat union of 13 (`frontend/src/api/generated/types.gen.ts:153`) | none client-side |
| CLI | **no handoff, release or reason surface at all** — commands: init, serve, stop, status, restart, open, create, load-test-data, work, promote, migrate-schema, finish, validate, mcp, list, attachments, show, next, queue·, project·, dispatch· (`cli.py`); `work` claims and closes directly (`:809,819`) | n/a |
| Migrators | markdown importer uses `work/dependency/review/spec` only (`converter.py:182-271`); v1 converter `available/work/dependency/review/decision/spec` (`migrate_schema.py:210-267`) | model |

Scoping is enforced in one place and every write passes through it: **examined,
nothing found** on the scoping claim. The inconsistencies are in what each surface
*offers* (the `available` row of #3, the absent CLI surface — auditor 6).

---

## 6. Minor (P4)

- `models_v2.py:667` "The five consistency rules" vs `:730` "Enforce the six rules".
- `scripts/regen-schema-docs.sh:26` "Pydantic preview (v2 -- the input to task-050, not
  yet wired in)" — task-050 shipped; `models_v2_preview.py` is an orphan artefact nobody
  reads. Delete the step or say what it is for now.
- Tolerated unknown `Priority` → `priority_rank()` raises `KeyError` (section 2).
- `docs/schema/understanding.md` lists the agent-side reasons correctly (six) — in
  step after `a79bfc5`; noting so the synthesis does not re-check it.

---

## What I did not get to

- **`client.py` write methods and `attachments.py`** beyond confirming no whole-`Task`
  write-back; whether any client call re-posts a parsed (possibly tolerated) value.
- **`_rejoining_the_queue` concurrency** (`manager.py:1064-1066`): it takes the queue
  lock, computes a bottom position, releases the lock, then `_mutate` writes — a window
  in which a create can take the same number. I did not construct the race. Auditor 5.
- **The 13 `non-canonical-serialization` live files** — hand edits or an older writer's
  output? Each is one `git log -p` away; I did not look.
- **`docs/schema-design.md`** in full; only the lines cited. Its "rejects an explicit
  null" claim about the generated JSON Schema (`models_v2.py:736-737`) is unverified.
- **`validation._check_paths`** semantics on Windows (case, separators, junctions) —
  `root not in resolved.parents` on a junctioned Obsidian path may misreport. Not tested.
- **`display_status` consumers** in the frontend for the `Ready`/`In progress` labels
  #3 produces — whether any filter acts on them. Auditor 9.
- **PATCH `spec` semantics** (`TaskUpdateRequest.spec: Optional[Spec]` replaces the
  whole block; a partial `spec` body drops `intent`/`constraints`). Plausible, unverified.
- I did not read `migration/parser.py` or `reporter.py`.

## Questions for other auditors

- **Auditor 10 (dispatch):** `auto.py:193` admits any open task with `ball: agent` that
  is not `hold` — including `draft` and unclaimed `ready` (#3). What does a dispatched
  agent do when `claim` then refuses? And `guards.py:184` refuses a held task while
  `get_next_task` offers it (#2) — is the guard checked after `task_next` in a run?
- **Auditor 5 (queue):** the post-lock write window in `_rejoining_the_queue` above;
  and whether `queue repair` would notice the duplicates #6 produces.
- **Auditor 1 (context):** #7 — `GLOBAL-AGENTS.md:125` vs task-164. Also: `ALLAGENTS.md`
  says "`ball_prompt` is required whenever the ball is set" without the
  `agent/available` exception the model and `task-schema.md:105` carry.
- **Auditor 6 (CLI):** no `handoff`/`release`/`close` command; `work` performs claim and
  close directly through the manager, so the CLI is the surface where #4's missing
  allowlist is closest to a user.
- **Auditor 4 (storage/manager):** #8 — the migrator is the one writer that bypasses
  `TaskStorage`; does your abstraction-bypass grep list it? Also `system` as an
  un-reserved actor the manager writes (#5).
- **Auditor 7 (API):** `HandoffRequest` accepts `agent/available` while MCP refuses it
  (#3); `TaskUpdateRequest` is the only thing standing between a PATCH and #4.
- **Auditor 12 (security):** `update_task(log=[])` erases an append-only record with no
  trace in the file; reachable only from Python today, but by any script on the machine.
  And a tolerant reader carries arbitrary strings from a newer service into
  `display_status`-adjacent rendering — low, but yours to weigh.
- **Auditor 9 (frontend):** does anything filter on `display_status == "Ready"`? #3 makes
  that label lie for an active task.
