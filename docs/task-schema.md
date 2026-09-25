# Task Schema Reference

Every task is one record, and the record is the source of truth for the project — not a
chat log, not an issue tracker. This page describes the *document*: the same shape
whether the project stores it as a YAML file or as rows in the SQLite store, because the
document schema and the physical one are deliberately independent
([storage guide §8](storage-sqlite.md)).

They are also **generated state**. This page describes the shape of what AgentJobs
writes, so you can read a task file and know what every field means. It is not an
authoring guide: hand-editing a task skips validation, the per-task lock, and the log
entry that records the change, which is how an invalid record gets written that no
surface will show you. Make changes through the [MCP tools](mcp.md), the REST API, the
CLI, or the web UI — all four reach the same validated write path — and run
`agentjobs validate` if you ever suspect a file was shaped by something else.

Tasks are rows in a SQLite database of the project's own beside the server, outside every
checkout ([the storage guide](storage-sqlite.md)). This repository's own records were
imported there on 2026-09-07, and the frozen copy they were built from was removed on
2026-09-11 (task-380), so no task YAML is tracked here at all. Files are what an import
reads and an export writes, and nothing else.

The schema is **v2**, defined by [`models_v2.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/models_v2.py) and
declared machine-readably in `schema/agentjobs-v2.yaml`. Every file starts with
`schema: 2`. A file without that stamp is v1 and the loader refuses it **by name**
rather than guessing — `agentjobs migrate-schema` converts it. Schema v1 was retired in
task-052; if you are looking at a v1 file, it predates that migration.

The design behind v2, including the alternatives that were rejected, is in
[schema-design.md](schema-design.md). This page is reference; that one is reasoning.

---

## Schema v2

### The change everything follows from

v1's single `status` answered three unrelated questions at once. v2 splits them:

| Question | field | Values |
|---|---|---|
| Where in its life? | `lifecycle` | `draft` · `ready` · `active` · `closed` |
| Who acts next? | `ball` | `agent` · `human` · `external` — **required while open** |
| Why do they hold it? | `ball_reason` | scoped to the holder, see below |
| What must they do? | `ball_prompt` | prose — **required whenever the ball is set** |
| How did it end? | `outcome` | `completed` · `cancelled` · `superseded` · `duplicate` |

`archived` is a separate boolean, orthogonal to how the task ended.

**`ball_reason` is scoped to whoever holds the ball.** `human/work` and `agent/review`
are not representable:

| `ball` | permitted `ball_reason` |
|---|---|
| `agent` | `available` · `work` · `revise` · `answer` · `redirect` · `hold` |
| `human` | `spec` · `review` · `decision` · `approval` · `input` |
| `external` | `dependency` · `service` |

**The agent-side reasons say what a human meant when they sent the task back**, which
is what the axis is for and what one value could not carry (task-231):

| reason | the human's act | what the agent does next |
|---|---|---|
| `work` | approved, or dispatched fresh | get on with it |
| `revise` | the work needs changing | change it, then come back for another review |
| `answer` | supplied what the agent was waiting for — an answer, a decision, a permission, a cleared blocker | resume; prior work stands |
| `redirect` | changed the instructions | re-read `ball_prompt`; prior work stands, the direction does not |
| `hold` | stopped it, with the release condition in `ball_prompt` | **nothing.** Wait for a human to release it |

`hold` is the one agent-side reason that is not workable, so auto-dispatch skips it and
a manual dispatch at a held task is refused (`task_on_hold`). Whether a *question* was
answered or a *blocker* was cleared is read off the state the ball came from — the
preceding handoff entry says `human/decision` or `external/dependency` — rather than
from a second reason value that could disagree with it.

### Fields

| Field | Type | Notes |
|---|---|---|
| `schema` | int | Always `2`. Its absence means v1. |
| `id`, `title`, `created`, `updated` | | Identity and timestamps. |
| `lifecycle`, `ball`, `ball_reason`, `ball_prompt`, `outcome`, `archived` | | The state axes, above. |
| `priority` | enum | `low` · `medium` · `high` · `critical` |
| `queue_position` | int | Order **within** the priority band; `>= 1`, and present if and only if the task is open — the same rule shape as `ball`. Unique among open tasks of one band in one project, which the model cannot check and the store's unique index enforces. Assigned in sparse steps of 100 so an insertion takes a midpoint and rewrites one row rather than a band. It is order and nothing else: `high/900` beats `medium/100` because of the band, not the number. |
| `category`, `tags` | str, list | Project taxonomy. **Nothing validates these at write time** — not the model and not the manager. Nothing reports a category outside the project's configured list against the store today; `agentjobs validate --tasks-dir` does for an export. |
| `effort` | str | Free text. An estimate, not a contract. |
| `assignment` | object | `owner` (live, one actor id) and `eligible` (authoring-time list; empty means anyone). |
| `parent` | str | Task id of an umbrella task. It must exist; a task may not be its own parent, nor be parented into a cycle. A task with an **open** child is never offered by `/next`, but a caller that names it can claim it: what that hands over is the supervisor's seat, and the `ball_prompt` written on the claim says so — a session per child, not the children's work. See [the parent-task protocol](agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them). `GET /api/tasks?parent=<id>` lists one umbrella's children. |
| `posture` | enum | Optional, and the only field here that says anything about what may *execute*: `read_only` · `supervised` · `auto` · `autonomous`. It **asks** for a dispatch envelope and never grants one — see [`posture`](#posture-a-request-not-a-grant) below. Absent on almost every task, which means "whatever this project's machine says". |
| `spec` | object | `summary` and `description` are **required**; `intent`, `constraints`, `out_of_scope`, `context[]` are optional. See the example below. |
| `acceptance[]` | list | `id`, `text`, optional `verify`, optional `check`, `status`: `pending` · `met` · `failed` · `dropped`. `verify` and `check` are [not the same thing](#verify-is-prose-check-is-argv). |
| `deliverables[]` | list | `path`, `note`, `status`: `pending` · `done` · `dropped`. |
| `dependencies[]` | list | `task`, `type`: `needs` · `blocks` · `related`, `note`. |
| `links[]` | list | `url` (validated), `rel`: `pr` · `issue` · `doc` · `design` · `build` · `other`, `title`. |
| `branches[]` | list | `name`, `status`: `active` · `merged` · `abandoned`, `merged_at`. |
| `log[]` | list | The unified log. See below. |

Gone from v1: `phases` · `prompts` · `issues` · `comments` · `status_updates` ·
`human_summary` · `dependencies[].status` · the `Comment` model.

### Consistency rules

Enforced by the model, not merely documented. These are what make limbo
unrepresentable:

1. `ball` is absent-or-null **if and only if** `lifecycle` is `closed`.
2. `ball_reason` must belong to the current holder's vocabulary, and is required
   whenever `ball` is set.
3. `outcome` is set **if and only if** `lifecycle` is `closed`.
4. `ball_prompt` is required whenever `ball` is set — except `agent/available`, where
   the spec is itself the ask.
5. `assignment.owner` must be empty while `draft` or `ready`, and present while
   `active`.
6. `queue_position` is present **if and only if** the task is open, and is `>= 1`. Its
   uniqueness within a band is not checkable by the model — the store's unique index
   enforces it, and `agentjobs validate --tasks-dir` checks it in an export — but its
   presence is.

Six, and `models_v2.py` says "the six rules" where it validates them. The LinkML source
encodes rules 1 to 4; rules 5 and 6 live only in the Pydantic model.

Null and absent mean the same thing for `ball`, `ball_reason` and `outcome`; omission
is what the manager writes, and an explicit `null` is accepted on load.

The rules are re-checked on every write, not only at construction: `SqlTaskStore.mutate_task`
re-validates the mutated model before persisting it, because assigning an attribute
does not re-run a Pydantic validator.

---

### `posture`: a request, not a grant

Added by task-308. A task may name the envelope it wants a dispatched agent to run in:

```yaml
posture: autonomous          # read_only | supervised | auto | autonomous
```

This is the one field in the schema that touches what a process may *do*, and a task
record is something the agent working that very task can write, through the API. So the
obvious reading, "an agent can widen its own permissions by editing a record it can
already write", has to be answered rather than waved at.

**It is answered by a ceiling, not by trusting the writer.** Every machine that
dispatches declares, per project, in `~/.agentjobs/dispatch.yaml`:

```yaml
projects:
  agentjobs:
    posture: auto            # the default a run gets
    max_posture: autonomous  # the widest any run may get, whatever asks
```

That file is machine-local. It is not in the repository, no clone carries it, and no
AgentJobs API, CLI verb or MCP tool writes it. A posture on a task record is clamped to
it: a task asking for `autonomous` on a project capped at `auto` produces a run at
`auto`, and the run's `dispatch` entry records that it was cut down and what asked for
more. Nothing checks *who* wrote the field, deliberately — a provenance check is only
as strong as the identity machinery behind it, and once a ceiling exists it buys nothing.

**Precedence, when more than one source names a posture** — most specific wins:

| Rank | Source | Above the ceiling |
|---|---|---|
| 1 | Chosen for this one dispatch (`--posture`, or the GUI's control) | **Refused.** Somebody is waiting on an answer; silently narrowing it is worse than saying no. |
| 2 | `posture` on the task record | **Clamped.** Refusing would let any agent deny service on its own task by writing an impossible value into it. |
| 3 | `posture` in the project's `dispatch.yaml` | Cannot happen: the config parser refuses a default wider than its own ceiling. |

`max_posture` defaults to the project's `posture` when unset, so a machine that upgrades
never silently widens: before this existed, the project's posture was the only posture a
run could get, and an unset ceiling reproduces exactly that.

**Width order is not the enum's declaration order**, and one place in it surprises
people: `read_only` < `supervised` < `auto` < `autonomous`. `supervised` is *narrower*
than `auto` because the question a ceiling asks is what a run may do **unattended** —
supervised can run nine allow-listed command prefixes and then parks, where `auto` is
classifier-gated over a far larger set. A human approving what supervised parks on is a
second authorisation arriving, not something the run was granted.

Setting a *narrower* posture than the project's is the uncomplicated half of this, needs
no ceiling to be safe, and is the reason to reach for the field on most tasks.

## A complete task

```yaml
schema: 2
id: task-043-cors-vite-dev-origin
title: Allow Vite dev-server origin in CORS config
created: '2026-07-06T19:25:44Z'
updated: '2026-07-29T18:35:00Z'

lifecycle: active          # draft | ready | active | closed
ball: human                # agent | human | external (absent only when closed)
ball_reason: review
ball_prompt: >-            # the ask, addressed to whoever holds the ball
  Review the CORS diff and the new preflight tests; approve merge or request changes.
archived: false

priority: high             # low | medium | high | critical
queue_position: 400        # order within the band; open tasks only
category: developer_experience
tags: [react-frontend, phase-0]
effort: 15 minutes         # free text; an estimate, not a contract

assignment:
  owner: claude            # actor id; set on claim, cleared on release/close
  eligible: [claude, codex]  # who may claim; empty means anyone

parent: null               # task id of an umbrella task

spec:
  summary: >-              # REQUIRED. 1-2 sentences, for every audience
    The React development server runs on Vite at :5173; CORS currently blocks it.
  intent: |                # optional: WHY this task exists
    Browsers enforce same-origin policy; without an allowlist entry every request
    from the frontend dies in preflight.
  description: |           # REQUIRED. WHAT to do -- the working spec
    Append the two :5173 origins to `allow_origins` in src/agentjobs/api/main.py.
  constraints: |           # optional: hard requirements and prohibitions
    - No wildcard origins while allow_credentials is True.
  out_of_scope: |          # optional: explicit non-goals
    The Vite dev proxy configuration itself.
  context:                 # optional: read-this-first pointers, with reasons
    - path: src/agentjobs/api/main.py
      why: The CORS middleware block being changed.

acceptance:
  - id: ac-1
    text: allow_origins includes both :5173 origins
    verify: Load the dev server and watch the network tab for a preflight.  # prose, for a person
    check: [poetry, run, pytest, tests/test_api.py, -q]   # argv; its exit code decides
    status: met            # pending | met | failed | dropped

deliverables:
  - path: src/agentjobs/api/main.py
    note: Updated CORS allow_origins list
    status: done           # pending | done | dropped

dependencies:
  - task: task-042-relocate-demo-tasks
    type: needs            # needs | blocks | related
    note: Sequential Phase 0 ordering

links:
  - url: https://github.com/jeffposey/agentjobs/pull/12
    rel: pr                # pr | issue | doc | design | build | other
    title: CORS PR

branches:
  - name: feat/task-043-cors-vite-dev-origin
    status: active         # active | merged | abandoned
    merged_at: null

log:
  - id: 1
    ts: '2026-07-29T18:30:10Z'
    actor: claude
    type: transition
    data: {lifecycle: active, ball: agent, ball_reason: work}
    body: Claimed by claude.
  - id: 4
    ts: '2026-07-29T18:35:00Z'
    actor: claude
    type: handoff
    data: {ball: human, ball_reason: review}
    body: Branch complete and verified. Need: review the diff, approve or request changes.
```

`acceptance` and `deliverables` keep separate vocabularies on purpose: a criterion is
*verified* (`met`), a deliverable is *produced* (`done`).

### `verify` is prose, `check` is argv

Both say how a criterion is verified and they are **not interchangeable** (task-147).

| | `verify` | `check` |
|---|---|---|
| Shape | A sentence | A list of strings |
| Audience | A person | This machine |
| Executed | **Never** | By `agentjobs check` and `POST .../tasks/{id}/check`, and by nothing else |
| Decides the status | No | Yes: exit 0 is `met`, anything else is `failed` |

They are kept apart rather than merged because most criteria can only have the first.
"The dashboard reads clearly on a phone" is verifiable and not executable, and a schema
offering one field would either lose that criterion or invite a command written where
prose belongs. Nothing migrates an existing `verify` into a `check`: turning prose into
an argv is a judgement per criterion.

Four rules about `check` that the schema enforces rather than describes:

-   **It is a list, never a string.** Nothing splits it and no shell sees it, so
    `["pytest", "-q"]` — not `"pytest -q"`, which is refused by name.
-   **An empty list, a non-string element and an empty first element are each refused**,
    with a message saying which criterion and what was wrong.
-   **Changing a criterion's `check` resets its `status` to `pending`**, on every write
    path including PATCH and MCP `task_update_content`. A status is a claim about a
    check having been run, and the caller cannot have run the new one — so a `status`
    sent in the same patch as a new `check` is overwritten rather than honoured.
    Editing `text` changes no such claim and is left alone.
-   **A check that cannot be started is `failed`, never skipped**, alongside a non-zero
    exit and a timeout. An unrunnable check treated as anything else would let the loop
    this feeds converge by breaking its own tests.

Running them is gated: `assert_dispatch_permitted` — the same four gates a dispatch
walks — opens before any process starts, so a project not enabled for dispatch runs
nothing, and **no read path ever evaluates a check**. Each pass writes exactly one
`check_result` log entry, described with the other entry types below.

**Ten fields are required in practice, not seven.** Seven have no default and must be
written: `id`, `title`, `created`, `updated`, `category`, `spec.summary` and
`spec.description`. `schema` then defaults to `2` and `lifecycle` to `draft` — and a
draft is open, so the rules below immediately require three more: `ball` (rule 1),
`ball_reason` (rule 2) and `queue_position` (rule 6). A file carrying exactly the seven
named above does not load.

*(Corrected 2026-08-22. The sentence used to stop at "seven", which is true of the
schema's `required:` list and false of anything you can put on disk.)*

## `log[]`

One append-only typed log replaces v1's `status_updates`, `comments` and
`prompts.followups`.

Types: `note` · `progress` · `transition` · `handoff` · `decision` · `question` ·
`answer` · `instruction` · `dispatch` · `dispatch_result` · `queue_move` ·
`authorization` · `check_result`.

Integrity rules, enforced: ids are unique and ascending, and `re:` must reference an
**earlier** entry that exists. An open `question` is one with no `answer` threaded to
it, which makes unresolved threads queryable (`Task.open_questions()`).

`transition`, `dispatch`, `dispatch_result`, `queue_move`, `authorization` and
`check_result` are written by the manager, never by a caller — the API rejects an attempt to post one directly, because an entry
that does not accompany a real event is a lie in an append-only record. They are the
model's `MANAGER_WRITTEN_LOG_TYPES`, and every write path consults that set rather than
listing types of its own.

### `question` and `answer`

A question may offer options, and the answer records which were taken (task-017). The
reason is the phone: task-076 and task-077 each arrived at `human`/`decision` with four
substantive questions in the `ball_prompt`, and answering them meant typing four
paragraphs with a thumb. Put as choices with a recommendation marked, the same four took
seconds.

```yaml
- id: 10
  actor: claude
  type: question
  re: 9                        # the handoff that raised it
  body: How long before an idle session counts as stalled?
  data:
    options:
      - label: 15 minutes
        description: More false positives if an agent pauses mid-work.
      - label: 4 hours
        description: Only catches overnight stalls.
        recommended: true      # a mark, never a preselection
    multi_select: false
    placeholder: a number of minutes   # hint for the free-text box
- id: 11
  actor: Jeff Posey
  type: answer
  re: 10                       # the question, which is what makes it an answer
  body: 60 minutes
  data:
    selected: []               # option labels, from the question's own list
    other: 60 minutes
```

**Every field of both payloads defaults**, so the prose-only question — which is every
question this repository held before task-017 — is unchanged and still valid.

Three properties are enforced rather than documented, each because getting it wrong
costs the person trying to answer:

*   **The payloads are typed** (`QuestionData`, `AnswerData` in `LOG_PAYLOADS`), so an
    option list that cannot be rendered is refused at the write, where the agent that
    wrote it is still around to be told. Untyped, it would fail in the browser of the
    person who opened the task to answer it.
*   **Free text is unconditional and is not a field here.** The GUI puts a box on every
    question whatever the payload says, so `options` can never be a closed set. The
    example above is the real one: Jeff took none of the three offered and typed a
    number. `selected` is empty and `other` carries the answer; both may be present.
*   **An answer names a question that is open**, and every label in `selected` is one
    that question offered. Answering a non-question, or answering twice, is refused —
    otherwise `open_questions()` is quietly wrong for the rest of the task's life.

`task_handoff` takes an optional `questions[]` and writes them in the same mutation as
the handoff, so a human woken by it cannot open a form holding two of four. They render
on the task page by default, not behind a control that offers to show them. The GUI's
answer route does the same in reverse: every answer plus the ball move to `agent`/
`answer` is one act, one write. Selecting an option does **not** move the ball on its
own — that would make partial answering impossible to express and would fire an
unrecoverable state change off a radio button.

An unanswered question outliving its handoff is a **backlog, not a leak**: nothing
sweeps it, and it stays answerable. An agent that carried on without it should close the
thread the ordinary way — an `answer` entry saying *"proceeded assuming X; revisit if
wrong"* — which is a better record than silence.

### `queue_move`

Somebody decided where this task stands in its band. Written by `move`, by
`reprioritize`, by a create that named an explicit placement, and by the generic patch
paths that change a task's band or reopen it.

```yaml
- id: 9
  actor: Jeff Posey
  type: queue_move
  body: Moved to the top of the band.
  data:
    band: high
    from: 4200                 # null on a create
    to: 50
    placement: {kind: top}     # top | bottom | before | after (+ target)
    moved_with: [task-121]     # group moves only: who actually moved
    from_band: medium          # reprioritize only: the band it left
    warnings:                  # only when the queue-move check found something
      - kind: promoted_unclaimable
        message: task-121 cannot be claimed where you have just put it: ...
        tasks: [task-121]
    undo: {kind: after, target: task-063}   # only alongside a warning, single moves only
```

**`warnings` and `undo` are the queue-move check** (task-219, design section 5.4). The
move lands first and is never blocked by them: they are computed synchronously, from
the claimability and dependency facts the move handler had already read, and recorded
here so that what the mover was told survives the moment it was told. `kind` is one of
`promoted_unclaimable`, `above_prerequisite`, `demoted_blocker`, `no_op` and
`queue_broken`; **most moves earn none of them and carry neither key.**

`undo` is a placement, not a number, because a rebalance can rewrite every number in a
band while "behind task-063" keeps meaning what it meant. It is omitted for a group
move: the inverse of a group move is a group move, which this shape cannot express, and
half an undo would scatter the children the move had just carried.

**Rebalances and compactions write none of these.** Nobody decided anything, and forty
entries saying "300 became 1400" would bury the ones that record a real choice. Both are
visible in git and in the receipt ledger, which is where mechanical rewrites belong.

### The strong anchor: a `decision` that answers a `queue_move`

A human who read a warning and kept the position anyway records that here, through
`agentjobs.manager.keep_queue_move` — `POST /api/tasks/{id}/queue-keep`, or the *Keep it
here* button on the React notice. `reorder` (task-217) reads it and may not move a
position anchored this way; ignoring the notice instead leaves an ordinary anchor, which
is what every un-warned human move leaves, so there is deliberately no `ordinary` value
to write.

```yaml
- id: 10
  actor: Jeff Posey
  type: decision
  re: 9                        # the queue_move entry above
  body: Kept this place in the 'high' band after reading 1 warning(s) about the move.
  data:
    queue_anchor: strong
    band: high
    queue_position: 50
    kept_over:                 # the warnings verbatim, as they were shown
      - kind: promoted_unclaimable
        message: task-121 cannot be claimed where you have just put it: ...
        tasks: [task-121]
```

`kept_over` is a copy rather than a pointer or a recomputation. The anchor's whole claim
is about what was on the screen at that moment, and the queue it was computed from has
moved on by the time anybody reads it back. Keeping a move that produced no warning is
refused: an anchor written where nothing was ever said would bind `reorder` to a
decision nobody took.

### `dispatch` and `dispatch_result`

That an agent was launched against a task is a durable fact, so it lives in the task
record beside the work it produced. Run directories under
`~/.agentjobs/runs/` are machine-local and disposable; these two entries are the part
that survives. See [agent-dispatch-design.md](agent-dispatch-design.md).

```yaml
- id: 7
  actor: Jeff Posey            # the human who authorized — never the agent
  type: dispatch
  data:
    run_id: run_a1b2c3d4
    agent: claude
    runner: claude
    mode: session              # session | batch
    posture: auto              # read_only | supervised | auto | autonomous
    posture_source: task       # project | task | dispatch | epic | history
    posture_ceiling: auto      # the project's max_posture when this run started
    posture_requested: autonomous  # only when the ceiling cut the source down
    trigger: manual            # manual | auto
    caused_by: 6               # log entry whose actor authorises this dispatch
    argv: ["claude", "--bg", "--remote-control", "-p", "..."]
    cwd: C:/projects/agentjobs
    git_head: 4887b74
    playbook: groom            # playbook runs only; absent on an ordinary dispatch
    playbook_hash: sha256:3f9c… # the brief's content at instantiation
    envelope:                  # task-375
      source: grant            # grant | history -- chosen now, or carried over
      execution_id: exe_5f0c…
    delivery:                  # task-375: what the agent was actually sent
      channel: argv            # argv | stdin | turn | none
      payload_sha256: 9b1e…
      posture_delivered: true  # the posture clause was inside that payload
      acknowledged_by: feed1234
- id: 8
  actor: claude
  type: dispatch_result
  re: 7
  data:
    run_id: run_a1b2c3d4
    outcome: completed         # completed | finished_without_handoff | failed |
                               # timeout | cancelled | crashed | interrupted
    exit_code: 0               # batch only; a session reports none
    duration_seconds: 1049
    log_path: ~/.agentjobs/runs/run_a1b2c3d4/
```

The three `posture_*` fields are task-308's, and they exist because the other two
places an answer could live are both invisible to a reader of this file: the project's
ceiling is machine-local, and the task's own `posture` field may have been edited since.
`posture_source` is absent on every entry written before task-308 — read that as
`project`, which is what it always was, never as unknown. `posture_requested` appears
**only** when the ceiling reduced what the source asked for, so its presence is itself
the signal that something wanted a wider envelope than it got.

`envelope` and `delivery` are task-375's, and they separate three facts one `posture`
field used to stand for. `envelope.source: history` means the run continued an earlier
execution and kept the runner, group and posture it was granted rather than re-reading
today's defaults; `continues_execution_id` names that execution. `delivery` says what
reached the agent: `posture_delivered` is computed from the delivered text, so an entry
can no longer assert a posture its session was never told. `resume_refused` appears when
a resumable session was deliberately not resumed because the posture changed. Both are
absent on entries written before task-375.

`playbook` and `playbook_hash` are present only when the run was given a playbook as
its brief — see [the playbooks design](playbooks-design.md) §4.3. They answer a
different question from `git_head`: the head says which commit the tree was on, and the
hash says which brief actually ran, which diverge whenever the tree was dirty or the
file changed between two runs.

Both payloads are **validated**, not merely documented: an entry of either type whose
`data` does not match its model is rejected on load. An entry that cannot say what ran is
worse than no entry, because it looks like evidence.

`argv` is recorded verbatim, so **a runner must never put a secret in its argv** —
secrets belong in the runner's `env`, which is never logged. The recording is the safety
feature; weakening it to hide a token would be the wrong fix.

**The dispatch count is derived, never stored.** `Task.dispatch_count` counts `dispatch`
entries and `Task.dispatches_since(ts)` counts recent ones, so the number cannot drift
from the evidence for it and no migration was needed to introduce it.

`dispatcher` is a **reserved actor id**, valid in every project without appearing in its
`actors:`. Design section 9 attributes every forced ball move — a run that ended without
handing off, a session parked on a permission prompt — to the dispatcher rather than to
the agent, because the agent did not do it.

### `check_result`

One evaluation pass over the task's executable acceptance checks (task-147). **One entry
per pass, never one per criterion** — a pass is what was asked for and what a loop acts
on, and splitting it would leave a reader reassembling an invocation from timestamps.

```yaml
- id: 12
  actor: Jeff Posey
  type: check_result
  data:
    chain_id: null             # the loop this belongs to; null outside one
    iteration: null            # which turn of that loop; null outside one
    results:
      - id: ac-1
        status: met
        exit_code: 0
        duration_seconds: 4.12
        output_tail: "12 passed in 3.9s"
      - id: ac-2
        status: failed
        exit_code: null
        duration_seconds: 300.0
        cause: timeout         # timeout | not_started | pass_timeout
        output_tail: "[agentjobs] killed after 300s without exiting"
    unchecked: [ac-3]          # criteria with no check, named rather than omitted
```

`status` is only ever `met` or `failed`. There is no third value, because a check that
could not be started is a failure — see the four rules under
[`verify` is prose, `check` is argv](#verify-is-prose-check-is-argv). `cause` appears
when the exit code does not say why: the process timed out, could not be started at all,
or was never reached because the pass ran out of its own budget.

`unchecked` names the prose criteria rather than leaving them out. What a pass *did not*
decide is as much a part of its result as what it did, and a client rendering "2 of 2
passing" over a task with eight criteria would be telling somebody it was done.

`chain_id` and `iteration` are null on every entry this build can write; the loop driver
that fills them is task-150. They are in the payload now so that an entry written today
and one written by the driver are the same shape.

Written by the manager only, and by the same argument as the rest of
`MANAGER_WRITTEN_LOG_TYPES` in its sharpest form: the entry asserts that a command
exited with a code, and a loop decides whether it has converged by reading it. Two
triggers write one — `agentjobs check <task-id>`, which exits non-zero if anything
failed, and `POST /api/projects/{id}/tasks/{task_id}/check`, which needs `dispatch.start`
and is therefore refused to a dispatched run.

### `attachments[]` on an entry

An entry may carry images evidencing it — a screenshot of the thing being objected to.
The field is additive and **absent** unless the entry has images, so no existing file
gains a line for a field it does not use, and no schema version bump is involved.

The bytes are **not** in the record. They are blobs in the database, content addressed
(storage guide §1), and the entry carries only metadata:

| field | meaning |
|---|---|
| `path` | Content-addressed path, `attachments/<task-id>/<sha256><ext>`. |
| `media_type` | `image/png`, `image/jpeg` or `image/webp`, read from the bytes. |
| `sha256` | Content hash. Also the filename, and checked on every read. |
| `size_bytes` | Size of the stored file. |
| `label` | Accessible label; the alt text where it renders. |

That split is the point: a record stays something a person or a tool reads whole without
carrying megabytes of base64 through every listing. Images only, 5 MiB each, and the type
is derived from the magic number rather than taken from the caller's claim.

Two consequences are deliberate. The same image pasted twice is stored once, because the
name *is* the hash. And a file whose bytes no longer hash to its name is refused rather
than rendered. An unreferenced blob is **reported, never deleted** —
`AttachmentStore.orphans()` lists them for a person to decide about, because the same
bytes may be referenced by an entry nobody has looked at yet.

## `display_status`

Computed on read, never stored — one word or short phrase from a fixed vocabulary, and
the same word on every surface: the React app, the CLI and MCP. A stored copy of three
fields is a drift bug waiting for its moment.

It is a Pydantic *computed field*, so it appears in API responses and templates use it
instead of switching on the axes themselves. The store and the exporter both exclude it
when writing, and a file that contains it is rejected by name (`extra="forbid"`).

### `status_category` and the vocabulary

**Every read model carries `status_category` beside `display_status`**, derived by the
same call to `models_v2.task_status()`, so the word and its colour cannot disagree
(task-562). **Every word and every colour is in one data file,
[`src/agentjobs/status_vocabulary.json`](../src/agentjobs/status_vocabulary.json)**: the
server reads it for the labels and categories it sends, and the React app reads the same
file for the colours. Change a label or a colour there and both follow. The rule is **one
colour per category, and every label in exactly one category.** Grey means closed and
nothing else, so any coloured chip is a live task.

| `status_category` | Colour | Labels — the chip's exact words |
|---|---|---|
| `ready` | green | `Ready` |
| `queued` | brown | `Queued`, `Starting` |
| `working` | blue | `Working` |
| `finishing` | purple | `Landing` |
| `needs_you` | red | `Needs spec`, `Needs review`, `Needs decision`, `Needs approval`, `Needs input`, `Error` (a `needs` cycle) |
| `not_now` | pink | `Blocked`, `On hold`, `Quota` |
| `draft` | yellow | `Draft` |
| `closed` | grey, solid | `Completed` |
| `closed_unfinished` | grey, hollow with a dashed border | `Superseded`, `Cancelled`, `Duplicate` |

`closed` and `closed_unfinished` are the one grey, drawn two ways: a task that ended with
the work done is solid, and one that ended without it is hollow.

The same file maps the run-health words that name a task status (Working, Starting,
Waiting on you, Landing, and Feedback, meaning the owner's feedback is queued for the
running session) and the three epic-walk badges (Walking, Waiting, Grounded) to these
categories.

**Each entry may also name an `icon`** (task-578): a [Lucide](https://lucide.dev/icons/)
name, registered in `frontend/src/components/statusIcons.ts`; `statusIcons.test.tsx` fails
when the file and the registry disagree. **No chip draws it.** The owner tried the icon
inside the chip and then beside it, and rejected both: next to the word it repeats what the
word says. Where the icons are used instead is task-584; until then they are data only.

**What the chip does not say.** The owner, the blocker, a quota wait's reset time and the
archived flag are each on the record and drawn beside the chip, never in it — a chip is
read at a glance in a narrow column, and a qualifier there is read as noise. Before
task-562 the same task read `In progress (claude)` in the API, `In flight` in the task
list and `Working` on the live runs board; `Blocked on task-044`, `Waiting on quota
reset (21:30 UTC)`, `Waiting on sub-tasks` and `Completed (archived)` are retired the same
way.

**Why red is "needs you" and pink is "blocked".** Red marks what is waiting and will not
continue until a person acts. Most blocks — a dependency on another task, a quota wait —
clear with nobody acting, and red on them sends somebody to investigate a handled
condition. A `needs` cycle is red because only a person can fix the data.

**The order the facts are weighed in** is in `task_status()`'s docstring, and it is the
one place it is decided. Three rules are worth stating here: a closed task keeps its
outcome even while a finish is still cleaning up; a human ball outranks an unmet `needs`
edge, because a spec can be written whatever the task is blocked on; and an epic nobody
holds reads `Ready`, because since task-164 it can be claimed as the supervisor's seat. A
walked epic is claimed, so it reads `Working`.

**Which facts a surface can see decides how precise its word is.** A read model
(`TaskRead`, the listing row, the dashboard card) folds in the corpus's dependency facts,
the machine's dispatch queue and its live finishes. The CLI's list knows the queue but
not the corpus, so it cannot say `Blocked` for an unmet edge nobody handed off on, or
`Error` for a cycle; a bare `Task.display_status` knows neither. That is a narrower
answer, never a contradictory one.

### `Quota`

One label is derived from more than the axes. An `external`/`service` park reads
`Blocked` — except where the newest handoff is auth recovery's own, its action still
means waiting, and the incident kind is one that lifts with nobody acting. Today that is
`usage_limit` alone, and the label is then `Quota`. The reset time is not in the
label: the React app writes it under the chip in the reader's own zone, from
`self_clearing_wait.resets_at`, and the full timestamp stays in `ball_prompt`.

**The point is that the two states need different things from a reader.** A session
waiting out a usage limit is probed and resumed by AgentJobs itself, so a label saying
only "Blocked" sends somebody to investigate a condition already handled
(task-456). A `spend_limit` and a dead credential store past its probe deadline park on
a *person* instead, so they keep reading `Needs input` and none of this reaches them.

`TaskRead` carries the same fact as `self_clearing_wait` — `{kind, resets_at}`, or null
— because a client needs the structure and not only the prose: the task list's `reset`
filter separates these waits from real blockers, and matching on a label's text is what
[the rendered-value rule](../ENGINEERING.md#verification) exists to prevent. It is
derived from the record on every read, like `display_status`, and is not a stored field.
Both the label and the field come from `models_v2.self_clearing_wait()`, so they cannot
disagree.

### `Landing`

The word was `Finishing` until task-578, and only the word changed: the status key and
category are still `finishing`, as are the `agentjobs finish` verb, `finish.enabled` and
the `live_finish` field. `Landing` names the operation, integrating onto `main`, in the
flight words the system already used for it (the merge runway; takeoff and landing are
different resources). `Finishing` read as "the agent is finishing its work", which is
`Working`.

Two labels are derived from something that is not on the record at all: `Queued`, when a
dispatch of the task is waiting for a slot on this machine (task-459), and `Landing`,
while a scripted finish is rebasing, gating and merging its branch (task-509). Neither is
a state the axes could carry. A queued dispatch deliberately does not claim the task, and
a finish moves nothing on the record it is finishing — approving hands the ball to
`agent`/`work`, and there it stays for the three to four minutes of the attempt.

**The point is the same as above: the two states need different things from a reader.**
`Working` on a task being merged is not wrong, it is merely useless — it is
the label a task an agent is editing gets, and before task-509 nothing anywhere but the
task page's finish panel could tell the two apart.

`Landing` replaces whatever label an open task would otherwise have, and nothing on a
closed one. A finish holds the task's run lock for its whole attempt, so while one is
live nothing else can be happening to the task; but the finish closes the task at its
`close` step and then spends a second or two removing the worktree, and `Completed` is
the more useful truth in that window.

`TaskRead` and the listing row both carry `live_finish` beside the label — `{finish_id,
state, started_at, current_step, step_meaning, branch}`, or null — for the reason
`self_clearing_wait` is carried: the task list's `finishing` filter selects on it, and a
Dispatch button disables itself and names the step from it. Both it and the label come
from `finish_status._status_of()`, the function the task page's own panel is built from,
so a chip and a panel cannot describe the same attempt differently. The list gets it from
one scan of the machine's finishes per request rather than one per row —
`api.live_finish` and `finish_status.live_finishes()` state what that costs.

## How an export is written

`taskfiles.canonical_bytes()` — what `agentjobs storage export` writes — dumps with
`by_alias=True` and `exclude_none=True`, so unset optional fields are **absent** rather
than written as `null`.

`by_alias` is load-bearing: `schema` shadows a Pydantic `BaseModel` attribute, so the
field is `schema_version` in Python with `alias="schema"`. Dumping without the alias
writes the wrong key and produces a file the loader then refuses as v1.

Unknown fields are rejected outright (`extra="forbid"`), so a stale key fails by name
rather than being silently ignored — whether it came from a hand edit, a migrator bug or
a React form posting a retired field name.

## Widening an enum

Adding a member to an enum is backward-compatible for the data: every file already
written stays valid. It used to be a breaking change anyway, because every process that
talks to AgentJobs over HTTP carries its own copy of these models and **re-validated the
service's already-validated JSON against it**. On 2026-08-19, adding `auto` to
`DispatchPosture` made task-107 unreadable to every process started before the change —
the MCP client's `task_handoff` came back `log.12.posture: Input should be 'read_only',
'supervised' or 'autonomous'` with `retryable: false`, against a service that was
serving the same task over `curl` without complaint (task-024).

The service is the authority on task validity, so a reader older than the service can
only produce false negatives. `TaskClient` therefore parses **tolerantly**: an enum value
it does not recognise is kept verbatim as an opaque member and logged as a warning naming
the enum, the value and the task, rather than failing the call. An old client shows
`posture: auto` as text it cannot interpret and everything else about the task still
works.

**Adding a field is the same widening one level over** (task-445). task-375 added
`envelope` and `delivery` to dispatch entries; a supervisor whose MCP server predated it
got `internal_error` — `log.10.delivery: Extra inputs are not permitted` — from a
`task_log_append` that had in fact landed, and its retry recorded the entry twice. So the
tolerant parse also leaves out a key a nested model does not declare, logging the field
and the model. A log entry's `data` is kept as the raw mapping, so a newer writer's
fields still appear in the parsed task, and nothing a client parses is written back.

Tolerance is opt-in, scoped to that parse (`agentjobs.schema_tolerance`), and covers
unknown *members of known enums* and *undeclared keys*. Everything else is unchanged:

- **Writing an unknown enum value or an undeclared field is still refused.** This is
  about what a reader accepts, never about what may be stored.
- **The store and the file loader (`TaskFileCorpus`) stay strict.** A record carrying a value or key this build does not
  know is still a load error, reported by file and field.
- A malformed payload — missing field, wrong type — still fails loudly.

So widening an enum or adding an optional field no longer requires restarting every
session that holds an older build. Those sessions cannot *interpret* what is new, which
is why the warning names it; they can still read and write the record. A session that
started before this tolerance existed still fails, which no change here can reach.

## Editing tasks

Prefer `TaskManager` over hand-editing YAML. The state axes move only through the verbs,
each of which appends its own `transition` or `handoff` log entry:

| verb | effect |
|---|---|
| `claim_task(id, agent=…)` | ready → active, sets owner, ball `agent/work` |
| `handoff(id, actor=…, ball=…, ball_reason=…, ball_prompt=…)` | moves the ball with its ask |
| `release_task(id, actor=…)` | active → ready, clears owner (agent bows out) |
| `close_task(id, actor=…, outcome=…)` | ends the task |
| `add_log_entry(id, actor=…, type=…, body=…)` | note/progress/decision/question/answer/instruction |
| `record_dispatch(id, actor=…, run_id=…, argv=…, …)` | appends the `dispatch` entry for a started run |
| `record_dispatch_result(id, actor=…, run_id=…, outcome=…)` | appends the terminal `dispatch_result` |
| `move(id, actor=…, before=|after=|top=|bottom=, with_children=…)` | changes where it stands in its band |
| `reprioritize(id, priority, actor=…, before=|after=|top=)` | changes band and place together |
| `redact(id, field=…, replacement=…, reason=…, actor=…)` | replaces one prose region and records that it did |

**`redact` is the one verb that changes text already written, and the only one that
reaches a log entry.** The log is append-only, which is right for a record of what
happened and has no answer at all for content that must not persist — a verbatim
quotation of a person in a repository with a public remote, most often. It addresses
`title`, `ball_prompt`, `spec.<name>` and `log[<id>].body`, refuses anything else rather
than guessing, and appends a `note` naming the region, the reason, the actor and how many
characters went. The caller supplies the replacement and it should say what the removed
text meant: a redaction that loses why a task exists is a worse record, not a safer one.
`agentjobs redact` is the same verb, and
[the workflow guide](agent-workflow.md#paraphrase-a-person-never-quote-them) has the rule
it exists to serve.

`update_task()` is for content fields (title, spec, acceptance, tags…). **The axes are
kept out of it by the API's request model, not by the manager.**
`TaskUpdateRequest` names the fields a patch may carry and lifecycle, ball and
outcome are not among them, so `PATCH /api/tasks/{task_id}` cannot move an axis. The
manager's own `update_task()` applies whatever keys it is given, and is protected only
by the consistency rules above rejecting an incoherent result. Calling it in-process
with `lifecycle=` is therefore not refused the way the route is — task-254 is the open
task to give the manager the same allowlist. Prefer the verbs regardless: they are what
append the transition entry saying *why* the axis moved.

**`queue_position` is not a content field either.** There is no `set_queue_position`,
for the same reason there is no `set_lifecycle`: the number is a consequence of a
decision, and the decision is what the record should show. A `priority` change arriving
as an ordinary patch is intercepted and routed through the same placement
`reprioritize` uses, so an existing caller keeps working and cannot break the
uniqueness rule by carrying a number into a band it does not belong to.

Away from Python, the same two verbs are `POST /api/tasks/{id}/queue-move` and
`/reprioritize`, `agentjobs queue move` and `agentjobs queue reprioritize`, and the MCP
tool `task_queue_move`. Every one of them names a **placement** — a neighbour, or an end
of the band — rather than a number, and every one appends the `queue_move` entry above.
That is the whole list: if you have an opinion about what comes first, one of those
records it, and nothing else does. In particular a `needs` dependency does not: it is a
prerequisite, so a false one makes the task unclaimable rather than merely later, and
lies to every reader of the graph.

`get_next_task()` sorts claimable work by `(priority_rank, queue_position)` and by
nothing else — no timestamp participates, including as a fallback. If the bands it
would have to read are not a valid queue it raises `QueueCorruptionError` naming the
tasks and the repair command, rather than answering from some other field.
`explain_next()` returns the same answer with the work it stands in front of and the
claimability rule that excluded each.

A move also comes back with **what the move is worth saying** — see `warnings` above.
`agentjobs queue move` prints them, `POST .../queue-move?envelope=true` returns them as
`queue_warnings` and `queue_undo`, `task_queue_move` carries them in its result and its
summary, and the React list shows them beside the row. One implementation
(`agentjobs.queue_check`), so no two surfaces can describe the same fact differently.
None of it can refuse a move.

A round-trip check:

```python
from agentjobs.projects import ProjectRegistry
from agentjobs.store_factory import task_manager_for

manager = task_manager_for(ProjectRegistry().get("agentjobs"))
task = manager.get_task("task-042-relocate-demo-tasks")
print(task.display_status, len(task.log))
```

`task_manager_for` is the one place that resolves which database a registered project is
served from. Reach storage through it and nothing else: composing a directory yourself
reads whatever happens to be at that path, which on this project is nothing at all since
task-380 and on somebody else's is a pre-import copy that answers from stale data without
erroring.

`load_task()` returns `None` when the file does not exist, and raises `TaskLoadError`
naming the file and field when it exists but cannot be read — including when it is an
unmigrated v1 file. A broken task is reported, never silently absent; `GET
/api/tasks/broken` lists them.

A complete worked example is
[`schema/examples/task-048.v2.yaml`](https://github.com/jeffposey/agentjobs/blob/main/schema/examples/task-048.v2.yaml)
— this repo's own design task, converted, annotated inline.
