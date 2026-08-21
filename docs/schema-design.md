# Task Schema v2 — Design Proposal

**Status: HISTORICAL DESIGN RECORD — accepted on 2026-07-29 and subsequently
implemented as schema v2. Sections describing future work capture the decision-time
plan; use [task-schema.md](task-schema.md) for the current contract.**

Produced under task-048. This document is the deliverable of the schema design pass: the
proposed next iteration of the task schema, the reasoning behind each change, the
alternatives that were rejected and why, and the interfaces the new schema makes
possible. Implementation happens in separate tasks derived from this document after the
open decisions are made.

---

## 1. What "AI-native" means here

Every existing task tracker assumes a human with a browser and a memory. AgentJobs
assumes something different: **the primary reader and writer of a task record is a
stateless agent** that wakes up with no memory of any previous session, reads one YAML
file, and must know — completely — what the work is, why it exists, what has happened,
what was decided, and what to do next. Humans are the second audience: they review,
decide, and approve, usually in small windows of attention.

Five tenets follow, and every design choice below traces back to one of them:

1. **The record is the working memory.** If a fact matters to resuming the task, it
   belongs in the file — not in a chat scrollback, not in someone's head. Corollary: the
   file must make it *hard to omit* the facts that matter (asks, decisions, scope
   changes).

2. **Every open task names who acts next.** The single most common failure mode we have
   observed while dogfooding is a task sitting in limbo — "in progress" while nothing is
   happening, or "done, waiting for someone to notice." v2 makes *who holds the ball*
   a required, queryable fact on every open task. Limbo becomes unrepresentable.

3. **Handoffs carry their ask.** When an agent hands work to a human (or vice versa),
   the schema requires a statement of what is needed. "Waiting for human" without *what
   for* is a notification with no payload.

4. **One way to say each thing.** Where v1 grew two mechanisms for the same concept
   (phases vs. sub-tasks, status_updates vs. comments, description vs. starter prompt),
   v2 keeps exactly one and deletes the other. Redundant mechanisms don't just cost
   maintenance — they make records ambiguous to a zero-context reader, which violates
   tenet 1.

5. **Semantics are enforced; taxonomy is configurable; prose is free.** Closed,
   model-enforced vocabularies for things with workflow meaning (lifecycle, ball,
   outcomes). Config-defined vocabularies for project taxonomy (categories, agents,
   tags). Markdown for everything meant for a reader. Nothing in between — no
   "documented but unenforced" fields, which v1 had several of.

Unchanged and non-negotiable: **one YAML file per task, in git.** Diffable, reviewable,
blame-able, mergeable. The schema is the product; git is its database.

---

## 2. The v2 schema, annotated

A complete example — task-043 as it would have existed under v2, mid-review:

```yaml
schema: 2
id: task-043-cors-vite-dev-origin
title: Allow Vite dev-server origin in CORS config
created: '2026-07-06T19:25:44Z'
updated: '2026-07-29T18:35:00Z'

# ----- state: three orthogonal axes + outcome -----
lifecycle: active          # draft | ready | active | closed
ball: human                # agent | human | external  (null only when closed)
ball_reason: review        # closed vocab, scoped to the ball holder (see §3)
ball_prompt: >-            # REQUIRED whenever ball is set: the ask, for its recipient
  Review the CORS diff and the new preflight tests; approve merge or request changes.
                           # `outcome` is omitted while open; at close it becomes
                           # completed | cancelled | superseded | duplicate
archived: false            # visibility flag, orthogonal to lifecycle

priority: high
category: developer_experience   # validated against config vocabulary at save
tags: [react-frontend, phase-0]  # validated against config vocabulary at save
effort: 15 minutes               # free text; it is an estimate, not a contract

# ----- who -----
assignment:
  owner: claude              # actor id (D4); set on claim, cleared on release/close
  eligible: [claude, codex]  # who may claim; empty list = anyone

parent: null               # task id of umbrella task; a task with open children is
                           # never offered by /next, but naming it in a claim takes
                           # the supervisor's seat (task-164)

# ----- the specification -----
spec:
  summary: >-              # 1-2 sentences; the only summary, for every audience
    The upcoming React frontend runs on Vite at :5173; CORS currently blocks it.
  intent: |                # WHY this task exists (markdown)
    The React frontend (Milestone 1) will call the API from the Vite dev server's
    origin during development. Browsers enforce same-origin policy; without an
    explicit allowlist entry, every request from the frontend dies in preflight.
  description: |           # WHAT to do (markdown) — the working spec
    Append `http://localhost:5173` and `http://127.0.0.1:5173` to `allow_origins`
    in src/agentjobs/api/main.py. Keep origins explicit — never `"*"` while
    `allow_credentials=True` is set; browsers reject the combination.
  constraints: |           # optional: hard requirements and prohibitions
    - allow_credentials stays True with explicit origins.
    - No wildcard origins, ever.
  out_of_scope: |          # optional: explicit non-goals, so agents don't wander
    The Vite dev proxy configuration itself — that belongs to the frontend tasks.
  context:                 # curated "read this first" pointers with reasons
    - path: src/agentjobs/api/main.py
      why: The CORS middleware block being changed (~lines 38-51).
    - path: tests/test_api.py
      why: Where the new preflight regression tests belong.

# ----- verification -----
acceptance:                # replaces success_criteria; one shared status vocab
  - id: ac-1
    text: allow_origins includes both :5173 origins; allow_credentials stays True
    status: met            # pending | met | failed | dropped
  - id: ac-2
    text: OPTIONS preflight from :5173 returns access-control-allow-origin for it
    verify: 'curl -i -X OPTIONS http://localhost:8765/api/tasks -H "Origin: http://localhost:5173" -H "Access-Control-Request-Method: GET"'
    status: met            # `verify` is an optional machine-checkable hint
  - id: ac-3
    text: poetry run pytest passes
    verify: poetry run pytest
    status: met

deliverables:
  - path: src/agentjobs/api/main.py
    note: Updated CORS allow_origins list
    status: done           # pending | done | dropped

# ----- relationships -----
dependencies:
  - task: task-042-relocate-demo-tasks   # validated against the store at save
    type: needs                          # needs | blocks | related
    note: Sequential Phase 0 ordering

links:
  - url: https://github.com/jeffposey/agentjobs/pull/12
    rel: pr                              # pr | issue | doc | design | build | other
    title: CORS PR

branches:
  - name: feat/task-043-cors-vite-dev-origin
    status: active                       # active | merged | abandoned
    merged_at: null

# ----- the unified log (append-only; replaces status_updates + comments) -----
log:
  - id: 1
    ts: '2026-07-29T18:30:10Z'
    actor: claude
    type: transition
    data: {lifecycle: active, ball: agent, ball_reason: work}
    body: Claimed. Branch feat/task-043-cors-vite-dev-origin created from main.
  - id: 2
    ts: '2026-07-29T18:33:00Z'
    actor: claude
    type: progress
    body: |
      CORS origins added; no test coverage existed for CORS at all, so added a
      parametrized preflight test over all four allowed origins plus a negative
      case for an unknown origin.
  - id: 3
    ts: '2026-07-29T18:34:00Z'
    actor: claude
    type: decision
    body: |
      Left black's complaints about untouched regions of the two files alone.
      Rejected: reformatting them here — it would bury the reviewable change
      inside mechanical noise. Repo-wide formatting debt is task-047's scope.
  - id: 4
    ts: '2026-07-29T18:35:00Z'
    actor: claude
    type: handoff
    data: {ball: human, ball_reason: review}
    body: |
      Branch complete and verified: live preflight checked for both origins and
      a rejected origin; pytest 71 passed. Need: review the diff, approve merge
      or request changes.
```

### Field inventory (what changed from v1)

| v2 field | v1 equivalent | Change |
|---|---|---|
| `schema` | — | **New.** Integer schema version (see §8) |
| `lifecycle`, `ball`, `ball_reason`, `ball_prompt`, `outcome` | `status` | **Replaced.** One 8-value enum → three orthogonal axes + close outcome (§3) |
| `archived` | `status: archived` | Now a visibility flag, orthogonal to how the task ended |
| `assignment.owner` | `assigned_to` | **Narrowed.** Live ownership only; an actor id reference (D4); cleared on release |
| `assignment.eligible` | — | **New.** Authoring-time eligibility (absorbs task-045's `supported_agents`) |
| `parent` | — | **New.** Real sub-tasks (absorbs task-045's design) |
| `spec.summary` | `human_summary` | Renamed; the only summary — the human/agent split was audience-by-length, not audience-by-content |
| `spec.intent` / `description` / `constraints` / `out_of_scope` | `description` (one blob) | **Structured.** The blob is split along the questions agents actually ask |
| `spec.context` | — | **New.** Curated read-this-first pointers with reasons |
| `acceptance` | `success_criteria` | Renamed; `verify` hint added; status vocab unified |
| `log` | `status_updates` + `comments` + `prompts.followups` | **Merged.** One append-only typed log (§4) |
| `effort` | `estimated_effort` | Renamed |
| `dependencies[].task` | `dependencies[].task_id` | Renamed; **validated against the store at save** |
| `dependencies[].type` | `dependencies[].type` | Vocabulary renamed: `depends_on` → `needs` (`blocks`, `related` unchanged) |
| `links` | `external_links` | Renamed; `rel` added; URL actually validated |
| `phases` | — | **Deleted** (D1). Sub-tasks via `parent` are the one way to subdivide |
| `prompts` | — | **Deleted** (D1). The spec is the briefing; directives are log entries |
| `issues` | — | **Deleted** (D1). Zero uses in the entire corpus; an issue is a log entry or graduates to a task |
| `dependencies[].status` | — | **Deleted.** No validator, no vocabulary, no discernible purpose |
| `Comment` model | — | **Deleted.** Subsumed by the log (typed entries + `re:` threading) |

---

## 3. The state model

### The insight

v1's `TaskStatus` answers three different questions with one value:

- *Where is this in its life?* — `draft`, `completed`, `archived`
- *Who must act next?* — `in_progress`, `waiting_for_human`, `blocked`
- *Why are they acting?* — `under_review` (the only "why" that leaked into the enum)

`under_review` is the tell: it is "waiting on a human *because* code review" — a special
case that got promoted to the top-level vocabulary because there was nowhere else to put
it. v2 gives each question its own axis.

### The axes

**`lifecycle`** — where the task is in its life. Strictly ordered, small, closed:

```
draft ──► ready ──► active ──► closed
  │         │          │
  └─────────┴──────────┴────► closed (cancelled / superseded / duplicate)
```

- `draft` — being specified; not claimable.
- `ready` — spec complete; claimable by any eligible agent.
- `active` — claimed; work underway (in whoever's court the ball says).
- `closed` — over, with an `outcome`: `completed | cancelled | superseded | duplicate`.
  "How did this end" is data, not a lifecycle fork. `archived` is a separate visibility
  flag — an old completed task and an abandoned draft can both be hidden without
  destroying what they were.

**`ball`** — who acts next: `agent | human | external`. **Required on every open task;
null only when closed.** This is tenet 2 made structural: an open task with nobody
responsible is not representable in v2.

**`ball_reason`** — why, scoped to the holder. Closed, model-enforced:

| ball | reasons | meaning |
|---|---|---|
| `agent` | `available` | ready, unclaimed — "any eligible agent, take it" |
| | `work` | claimed and executing |
| | `revise` | review came back with changes requested |
| | `answer` | a human supplied what the agent was waiting for; prior work stands |
| | `redirect` | a human changed the instructions; prior work stands, the direction does not |
| | `hold` | stopped by a human, release condition in `ball_prompt` — the one agent reason that is not workable |
| `human` | `spec` | the spec needs human completion/refinement (typical in `draft`) |
| | `review` | work product needs review (v1's `under_review`) |
| | `decision` | a choice is blocking progress |
| | `approval` | a gate: merge, spend, publish (distinct from review: yes/no, not critique) |
| | `input` | missing information only a human has |
| `external` | `dependency` | blocked on another task (v1's `blocked`, when task-shaped) |
| | `service` | blocked on a third party / outage / provisioning |

Two boundary cases, resolved here so they are not re-derived later:

- **Blocked before claim.** `external/dependency` describes a *claimed* task that hit a
  wall. A task whose `needs` dependencies are simply not done yet stays `ready` — its
  blockedness is derivable from the store, and restating it as state would be a second
  copy of the dependency list's truth, i.e. a drift bug (same argument as display
  status). Claimability enforces it instead: `ready` tasks with unmet `needs` are
  excluded from `/next` and refuse `claim`.
- **Drafts being written by an agent.** `draft` normally carries `ball: human/spec`. A
  draft being fleshed out *by* an agent has no dedicated reason today; if that workflow
  materializes, an agent-side reason (e.g. `agent/draft`) is an additive change (§9),
  not a redesign.

**`ball_prompt`** — the ask, in prose, addressed to whoever holds the ball. Required
whenever the ball is set (a default is permitted for `agent/available`: the spec is the
ask). This is tenet 3: a handoff without its payload is rejected at the schema level.
This single required field is most of what task-046 needs, made structural.

### The v1 → v2 mapping (proof of coverage)

Every v1 status maps losslessly, and the two ambiguous ones become *more* precise:

| v1 status | v2 |
|---|---|
| `draft` | `lifecycle: draft` · `ball: human/spec` |
| `ready` | `lifecycle: ready` · `ball: agent/available` |
| `in_progress` | `lifecycle: active` · `ball: agent/work` |
| `blocked` | claimed: `lifecycle: active` · `ball: external/dependency`. Unclaimed: `lifecycle: ready` with the unmet dependency carrying the blockedness (see boundary case above) |
| `waiting_for_human` | `lifecycle: active` · `ball: human/decision` (or `input` — migration reads the last status update) |
| `under_review` | `lifecycle: active` · `ball: human/review` |
| `completed` | `lifecycle: closed` · `outcome: completed` |
| `archived` | `lifecycle: closed` · `outcome:` per heuristic · `archived: true` |

### Display status is computed, never stored

CLI, GUI, and API all want one human-readable label. It derives mechanically
(`Needs review`, `Needs decision`, `In progress (claude)`, `Blocked on task-044`,
`Ready`, `Completed`), and the API serves it as a read-only `display_status`. Storing it
was rejected: a denormalized copy of three fields is a drift bug waiting for its moment,
and the derivation is ~15 lines.

### Consistency rules (model-enforced)

1. `ball` is absent-or-null ⟺ `lifecycle: closed`.
2. `ball_reason` must belong to the current ball holder's vocabulary.
3. `outcome` is set ⟺ `lifecycle: closed`, and absent-or-null while open.
4. `assignment.owner` is null when `lifecycle` ∈ {draft, ready}; required when `active`.
5. Every change to any of these fields appends a `transition` log entry — performed by
   the manager, not trusted to callers.

### Null and absent are equivalent (decided 2026-07-29)

`ball: null` and an omitted `ball` mean the same thing; likewise `outcome`. This is a
consequence of D2, not an exception to it: hand-editing YAML is a first-class
interface, so a human being explicit with `outcome: null` must not be punished for it.
Omission is the *canonical* form — what the migrator and manager write — and both are
accepted on load. Pydantic's `Optional[X] = None` gives this for free.

Recorded because the machine-readable schema cannot express it: LinkML has no null
type, so `schema/agentjobs-v2.yaml` compiles optional enums to a bare `$ref` and the
generated JSON Schema **rejects an explicit null**. That artifact is therefore stricter
than the runtime will be, and must not be the sole validator for hand-edited files.
Task-050 owes a regression test for both spellings.

### Other status vocabularies (issue 1 resolved)

- `acceptance[].status`: `pending | met | failed | dropped`
- `deliverables[].status`: `pending | done | dropped`
- `branches[].status`: `active | merged | abandoned` (unchanged — genuinely distinct)
- `dependencies[].type`: `needs | blocks | related`
- Issue and Comment vocabularies: gone with their models.

Two small checklist vocabularies remain instead of one shared enum, deliberately: a
criterion is *verified* (`met`), a deliverable is *produced* (`done`), and collapsing
that distinction re-creates the v1 problem of one word straining across meanings.

---

## 4. The unified log

`status_updates[]` and `comments[]` were two append-only, timestamped, authored lists
with an implied-but-unenforced role split, and `prompts.followups` was quietly a third.
v2 has one log. Entries are immutable and ordered; `id` is a per-task integer assigned
by the manager.

```yaml
- id: 12
  ts: '2026-07-30T09:14:00Z'
  actor: jeff                         # actor id; kind resolved from config (D4)
  type: instruction                   # see table
  re: 11                              # optional: threads to an earlier entry
  body: |
    Approved, but split the storage change into its own commit before merging.
  data: {}                            # optional structured payload, typed per entry type
```

| type | who typically writes it | purpose |
|---|---|---|
| `note` | anyone | free-form remark |
| `progress` | agent | work narration — what was done, what was verified |
| `transition` | manager (system) | automatic record of any state-axis change; `data` carries the delta |
| `handoff` | agent or human | the ball is moving; `body` is the ask (mirrors `ball_prompt`) |
| `decision` | anyone | a choice, its reasoning, **and the rejected alternative** |
| `question` / `answer` | anyone | explicit unresolved-thread mechanism; an open `question` with no `answer` is surfaceable in UIs |
| `instruction` | human | a directive to the working agent (replaces followup prompts) |

Provenance (issue 6) is resolved at this layer: every entry names its actor, and every
state change flows through a logged transition. The actor is a bare id resolved against
config rather than an embedded object (D4), so `kind` has exactly one definition and
cannot drift between entries. **Field-level provenance was rejected** — stamping author+timestamp on every scalar would double the schema's weight
and produce noise no one reads. The log records *events*, which is how humans and agents
actually reconstruct history. If a specific field's origin ever matters, the transition
entries contain it.

---

## 5. Interfaces

### The resumption contract

A fresh agent with zero context resumes a task by reading, in order:

1. `spec` — what and why (`summary` → `intent` → `description` → `constraints` →
   `out_of_scope` → `context` pointers).
2. State axes + `ball_prompt` — what is needed *right now*, from whom.
3. `log`, newest-first — the last `handoff` and every `decision` and open `question`
   since. Decisions are binding: do not relitigate them, and record new ones.
4. `acceptance` — what "done" means and how much of it is already met.

The contract cuts both ways: those four places are also where a *writing* agent is
obligated to put things. A fact recorded anywhere else (chat, commit message only,
nowhere) is a defect in the agent's process. This section becomes the core of
ALLAGENTS.md's task lifecycle once v2 lands, and is the substrate task-046 documents.

### The canonical loop

```
ready ──claim──► active/agent·work ──handoff──► active/human·review
  ▲                    ▲                              │
  │                    └────── instruction ◄──────────┤  (changes requested:
  │                           (ball: agent·revise)    │   ball back to agent)
release                                               │
  │                                            approve & merge
  └── active/agent ◄── unclaimed              closed·completed
```

Every arrow is one manager call, every call appends one log entry, every log entry can
fire one webhook. The loop we have been executing manually in conversation becomes the
API's shape.

### API surface (sketch)

| endpoint | effect |
|---|---|
| `GET /api/tasks?ball=human` | **the human inbox** — everything waiting on a person, with `ball_prompt` as the line item |
| `GET /api/tasks?ball=external` | the blocked list, with reasons |
| `GET /api/tasks/next?agent=claude` | ready + eligible + no open children + no unmet `needs` dependencies |
| `POST /api/tasks/{id}/claim` | ready→active, sets owner, ball agent/work; logs transition |
| `POST /api/tasks/{id}/handoff` | `{ball, ball_reason, ball_prompt, body}` — moves the ball; logs handoff; fires webhook |
| `POST /api/tasks/{id}/log` | append note/progress/decision/question/answer/instruction |
| `POST /api/tasks/{id}/release` | active→ready, clears owner (agent bows out cleanly) |
| `POST /api/tasks/{id}/close` | `{outcome, body}` — ends the task |

`ball=human` is the load-bearing query. It is the human side of the handoff loop: one
list, each row carrying its ask. The React frontend's home view is this query plus its
inverse ("what are the agents doing"). CLI mirrors: `agentjobs inbox`,
`agentjobs next`, `agentjobs claim|handoff|log|close`.

### Notifications (task-046's extension point, sharpened)

v1 fires `task.status_changed` on any change. v2 events are log-entry-typed:
`task.handoff` (with ball holder and prompt in the payload), `task.question`,
`task.closed`. A future notification service subscribes to `task.handoff where
ball=human` and forwards `ball_prompt` to whatever channel exists — the payload is
already composed, by schema requirement, at handoff time. Nothing else about the
webhook infrastructure needs to change.

---

## 6. Issue-by-issue disposition

| # | Issue | Disposition |
|---|---|---|
| 1 | Six parallel status vocabularies | **Adopted.** State axes for Task; two minimal checklist vocabs; Branch kept; Issue/Comment vocabularies deleted with their models |
| 2 | Unvalidated free-text fields | **Adopted.** `Dependency.status` deleted; `Comment.kind` gone with Comment; `dependencies[].task` validated against the store at save; `links[].url` gets real URL validation |
| 3 | `phases[]` vs sub-tasks | **Adopted (D1).** Sub-tasks win; `phases[]` deleted. Migration folds historical phases into a description appendix |
| 4 | `status_updates` vs `comments` | **Adopted (D1).** One typed log |
| 5 | Ownership vs eligibility | **Adopted.** `assignment.owner` (live, structured) + `assignment.eligible` (authoring-time). Absorbs task-045's split |
| 6 | No field-level provenance | **Partially adopted.** Typed actors on every log entry; all state changes logged. Field-level stamping rejected as weight without readers |
| 7 | Unconstrained taxonomy | **Adopted (D2).** Config is the vocabulary authority for `category`, `tags`, actor ids; storage validates at save. Strictness posture is D2 |
| 8 | `status` conflates three axes | **Adopted.** The centerpiece — §3 |

## 7. Rejected alternatives (recorded so they are not relitigated)

- **A database (SQLite / MongoDB) instead of YAML files.** Rejected 2026-07-29, then
  reopened the same day once D2 dropped hand-editing as a requirement — which had been
  carrying much of the original argument — and rejected again on narrower, better
  grounds. Recorded at length because this question will recur.

  *The model is document-shaped, and that is now measured, not asserted.* Of v2's 12
  classes, **11 have no identifier**: `Spec`, `Assignment`, `LogEntry`,
  `AcceptanceCriterion`, `ContextPointer`, `Deliverable`, `Dependency`, `Link` and
  `Branch` are value objects with no independent identity. `gen-dbml` refuses the
  schema outright (`Referenced class 'Assignment' does not have an identifier slot`),
  and `gen-sqlddl` only succeeds by emitting **14 tables**, two of them synthetic join
  tables (`Task_tags`, `Assignment_eligible`) invented solely to hold lists of strings
  that a document store keeps as native arrays. Reading one task from SQL is a
  14-table join; from a document it is one fetch. Relational storage is the awkward
  fit here, not the natural one.

  *Given a document model, SQLite — not Mongo — is the real alternative.* Mongo needs
  a server, so every project that `pip install`s agentjobs would need one running.
  SQLite is a single file, ships in the standard library, is ACID, and holds documents
  fine via JSON columns. At a 31-file / 204 KB corpus, Mongo is dominated by SQLite on
  every axis that matters. Putting Mongo aside is not a concession; it is the wrong
  tool at this size.

  *"Schemaless means cheaper schema changes" is the argument that actually motivated
  reopening this, and it does not hold.* Schemalessness does not remove the schema, it
  relocates it into every reader: old-shaped documents live in the collection forever,
  so each reader carries conditional handling for every past shape, permanently. That
  is more expensive than one versioned migrator, not less. It also contradicts D2
  (`extra="forbid"`) — adopting Mongo *for* flexibility would undo a decision made the
  same morning. And the flexibility being sought already exists: under §9 an additive
  change is free, exactly as in a schemaless store. What made schema change expensive
  was never storage, it was **three hand-maintained parallel definitions** (Pydantic,
  prose docs, GUI templates); that is now one LinkML source generating the rest, and
  that fix is storage-independent. Mongo would *add* schema locations — index
  definitions, DB-level validators, aggregation pipelines.

  *The strongest argument for a database is concurrency, not scale.*
  `TaskStorage.save_task` does read-modify-write with no locking and no
  compare-and-swap, so two agents can both read a `ready` task and both write
  `active` — a real double-claim race in a schema explicitly designed for multiple
  eligible agents. Mongo's atomic `findAndModify` would close it for free. Addressed
  instead by making the API the single writer plus advisory file locking (task-055),
  which is a ~50-line fix rather than a substrate change.

  *What files buy, and why it wins here:* diffable task history, `git blame` on a
  field, and atomic commits pairing a task change with the code change it describes —
  all three actively used in this repo's workflow. Plus zero infrastructure, which is
  load-bearing for the packaged-product goal in §8: `pip install agentjobs` and point
  it at a directory.

  **Revisit if** true multi-writer concurrency appears that cannot be serialized
  through the API, or the corpus reaches the low thousands (at ~6.6 KB/task, ~10k
  tasks means ~66 MB parsed per unindexed query). The standard escape hatch at that
  point is not migrating storage but adding a **derived SQLite index**, rebuilt from
  the files — the pattern dbt, Hugo and Sphinx use. Files stay the source of truth;
  the index is disposable.
- **Field-level provenance.** See §4.
- **Storing the display status.** See §3 — derivable data stored twice is a standing
  drift bug.
- **A single shared status enum for all nested types.** Recreates the v1 problem in
  reverse: one vocabulary straining across genuinely different concepts.
- **Making the state vocabularies config-extensible.** Taxonomy is project-specific;
  *semantics are not.* A task file must mean the same thing in every AgentJobs
  installation, or task files stop being portable and tooling stops being writable.
- **Keeping `prompts.starter` alongside the spec.** The corpus is the evidence: nearly
  every task's starter restates its description. Two places for the briefing means
  they drift; the spec is the briefing. Followup prompts survive as `instruction` log
  entries, which additionally gives them threading and provenance.
- **A task folder (multiple files per task).** Would allow spec/log separation, but
  breaks single-file atomicity, complicates the storage glob, and makes git renames
  noisier. One task, one file.

## 8. Versioning — the revisit, resolved

The recorded decision (task-048 description) was "not yet — revisit when the shape
settles." This document *is* the shape settling, and v2 is deliberately non-additive:
fields are renamed, deleted, and restructured. That is precisely the trigger the
original decision named.

**Proposal (D3):** add `schema: 2` (integer) to every v2 file. The loader treats a
missing `schema` field as v1 and refuses to load it *silently* — v1 files are converted
by a one-shot migrator (`agentjobs migrate-schema`), which the corpus test then
re-verifies in v2 form.

The corpus makes the migrator's real workload concrete (measured 2026-07-29, 25 files):
`prompts.followups` is non-empty in 7 files (each becomes an `instruction` log entry);
`phases[]` is non-empty in 15 (folded into a description appendix per §6); `issues[]`
and `comments[]` are empty everywhere, so their deletion migrates nothing. Nested
checklist statuses map `in_progress` → `pending` (work-in-flight on a checklist item is
not a persistent fact worth keeping); all other values map by name. Future breaking changes bump the integer and ship a converter;
additive changes do not bump anything. This costs one line per file and buys an
unambiguous answer to "what shape is this file?" forever — including for external
projects' task files the moment agentjobs is installed anywhere else.

## 9. Evolution policy (how changes stay cheap after this pass)

1. **Additive** (new optional field, default): just add it. No version bump, no
   migration. Old files load unchanged.
2. **Taxonomy** (new category, agent, tag): edit config. No code change.
3. **New log entry type / ball reason:** add to the model enum — additive in practice,
   since old files contain only old values.
4. **Breaking** (rename, retype, re-semantics): bump `schema`, ship a converter,
   corpus test proves the fleet migrates.
5. The corpus test (`tests/test_task_corpus.py`) runs in every suite: any model change
   that strands an existing file fails CI with the filename and error.

## 10. Reconciliation with open tasks

- **task-045**: its two schema changes are absorbed here (`parent`;
  `assignment.eligible`/`owner`). Task-045 shrinks to implementation: manager/API/GUI
  behavior for sub-tasks (get_subtasks, umbrella non-claimability, `?parent=` filter,
  cycle validation) — atop v2 fields instead of its own. Update after D1–D3.
- **task-046**: the handoff loop becomes largely structural (`ball_prompt` required,
  `handoff` log type, `task.handoff` webhook). 046 narrows to documenting the
  resumption contract in ALLAGENTS.md/agent-workflow.md and exercising the loop.
- **task-047**: unchanged; its enum-typing fix is orthogonal and should still land
  first (v2 implementation wants honestly-typed enums underneath it).

## 11. Decisions (resolved with Jeff, 2026-07-29)

- **D1 — The subtractions: ADOPTED, full set.** Delete `phases[]`, `prompts` (starter
  + followups), `issues[]`, `Comment`, `Dependency.status`, `human_summary`
  (→ `spec.summary`). One mechanism per concept; the migrator handles the corpus
  (§8 workload note).

- **D2 — Strictness posture: STRICT everywhere.** Unknown fields rejected
  (`extra="forbid"`), taxonomy validated against config at save, violations are
  errors — at save *and* at load. Forward compatibility across versions is the
  schema stamp's job (D3), not tolerated mystery fields.

  **Amended 2026-07-29, same day.** The decision is unchanged; its *justification*
  was replaced, and the original is recorded here because a stale rationale is how a
  settled decision gets relitigated.

  D2 was originally argued from hand-editing: *"hand-editing YAML in git remains a
  first-class interface, and strictness is what makes hand edits safe — a tolerated
  typo is an edit that silently does nothing (misspell `pirority:` and the task keeps
  its old priority forever, no error), while a strict load turns the same typo into
  an immediate, named error."* The owner then corrected the premise: **hand-editing
  YAML is not a requirement.** The expectation is that humans edit tasks through
  **UIs**, with an admin tool or a direct file edit as an acceptable *emergency
  hatch*, not a primary interface. This also narrows task-054's GUI scope from
  "a viewer" to "the primary editing surface."

  Strictness survives the change intact, on stronger grounds. The typo argument was
  never really about *humans* — it is about **any** writer. A tolerated unknown field
  is a silent no-op whether it came from a mistyped hand edit, a migrator bug that
  writes `pirority`, an API caller passing a field that a rename retired, or a GUI
  form posting a stale key. Under D2 every one of those fails immediately, by name.
  Strictness is the regression test for the migrator and the API, and those are now
  the writers that matter most.

  Companion requirement, unchanged and still landing independently of and before v2:
  load errors must be loud and precise (filename + field + error). Today
  `TaskStorage.load_task` swallows validation errors and the task silently vanishes
  from listings, which is the worst available behavior (task-049). Note this is
  *more* urgent under UI-primary editing, not less: a task that vanishes from a
  listing is invisible to someone whose only window into the data is that listing,
  whereas a file editor would at least still show the file.

- **D3 — Version stamp: ADOPTED.** `schema: 2` (integer) on every v2 file, one-shot
  `agentjobs migrate-schema` converter, per §8.

- **D4 — Actors are referenced, not embedded: ADOPTED.** A task file names an actor
  by bare id (`actor: claude`, `owner: claude`); `kind` lives in config and is
  resolved on read. The draft had embedded `{id, kind}` objects, which put **five
  identical copies of `{id: claude, kind: agent}` in §2's small example** and would
  put a dozen in a task with real history — every one a denormalized cache of a
  config fact, free to drift. It also contradicted issue 7, which had already made
  config the authority for actor ids. References are now consistent with the four
  other places v2 references rather than embeds: `parent`, `dependencies[].task`,
  `category`, and `tags`.

  Rejected: **keeping the embedded object**, which buys a fully self-contained record
  (tenet 1) at the cost of drift — a reader that has the file but not the config can
  still see *who* acted, just not whether they were an agent, and a zero-context
  agent reads config anyway. Also rejected: **referencing but caching `kind` on
  write**, which is precisely the denormalization v2 already rejected for
  `display_status` (§3), and rejecting it there while adopting it here would be
  incoherent.

  This was the modeling question behind the owner's observation that some boundaries
  looked relational: they mark genuinely *shared* entities. The resolution is
  references **inside the document**, not normalization into tables — see the storage
  entry in §7.

- **D5 — Configured acceptance-criterion types: ACCEPTED in principle, build
  deferred.** Today `acceptance[].text` is free prose plus an optional inline
  `verify` command, so §2's own example hardcodes `verify: poetry run pytest` — a
  string that every task in the repo would copy-paste. The intended shape is
  config-defined criterion types, each carrying its own verify command, referenced
  from an acceptance entry; the same shared-entity pattern as D4.

  Deliberately **out of task-050's scope.** Under §9's evolution policy this is a
  purely additive change — a new optional field with a default — so it needs no
  schema bump and can land whenever it earns priority, without lengthening the v2
  chain. Recorded here so the idea is not lost and not rediscovered as novel.

## 12. Derived implementation tasks

Sized so each is independently reviewable; sequenced by dependency. Task-047 (enum
typing) predates this pass and lands first, per §10.

| task | scope | depends on |
|---|---|---|
| task-049 | Loud load errors: storage stops swallowing validation errors; broken files reported with filename + field + error, never silently omitted. Lands on v1, before v2 (D2 companion) | — |
| task-050 | v2 models: state axes + consistency rules, `spec`, `acceptance`, unified log, `assignment`, `parent`, `schema: 2`, strict mode, computed `display_status`; `docs/task-schema.md` regenerated to v2 | task-047 |
| task-051 | Migrator: `agentjobs migrate-schema` converts the corpus per §3's mapping and §8's workload note; corpus test re-verifies in v2 form | task-050 |
| task-052 | Manager + API: claim / handoff / log / release / close, manager-appended transitions, inbox and next queries, taxonomy + dependency validation at save, `task.handoff` / `task.question` / `task.closed` webhook events | task-051 |
| task-053 | CLI mirrors: `agentjobs inbox / next / claim / handoff / log / close` | task-052 |
| task-054 | **Historical plan for the now-legacy Jinja compatibility UI:** inbox view (`ball=human` with `ball_prompt` per row), agent-activity view, and task detail editing. React later replaced it as the primary human interface. | task-052 |
| task-055 | Single-writer concurrency: `save_task` currently read-modify-writes with no locking, so two agents can both claim one `ready` task. Serialize writes through the manager/API plus advisory file locking; regression test the double-claim race. Independent of v2 — the race exists in v1 today | — |
| task-045 (reshaped) | Sub-task *behavior* atop v2's `parent` field: `get_subtasks`, umbrella non-claimability, `?parent=` filter, cycle/self/exists validation, GUI children list — its two schema changes are absorbed into task-050 | task-052 |
| task-046 (narrowed) | Resumption contract documented in ALLAGENTS.md / docs/agent-workflow.md; first live exercise of the handoff loop | task-052 |
