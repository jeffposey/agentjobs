# Task Schema Reference

Every task is a single YAML file. These files are the source of truth for the project —
not a chat log, not an issue tracker.

They are also **generated state**. This page describes the shape of what AgentJobs
writes, so you can read a task file and know what every field means. It is not an
authoring guide: hand-editing a task skips validation, the per-task lock, and the log
entry that records the change, which is how an invalid record gets written that no
surface will show you. Make changes through the [MCP tools](mcp.md), the REST API, the
CLI, or the web UI — all four reach the same validated write path — and run
`agentjobs validate` if you ever suspect a file was shaped by something else.

Tasks live in the directory named by `tasks_directory` in `.agentjobs/config.yaml`
(`tasks/agentjobs/` for this repo's own backlog). `TaskStorage` globs `*.yaml`
non-recursively, so files in subdirectories are invisible to the store — that is how
`tasks/test-data/` stays out of the real backlog.

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
| `agent` | `available` · `work` · `revise` |
| `human` | `spec` · `review` · `decision` · `approval` · `input` |
| `external` | `dependency` · `service` |

### Fields

| Field | Type | Notes |
|---|---|---|
| `schema` | int | Always `2`. Its absence means v1. |
| `id`, `title`, `created`, `updated` | | Identity and timestamps. |
| `lifecycle`, `ball`, `ball_reason`, `ball_prompt`, `outcome`, `archived` | | The state axes, above. |
| `priority` | enum | `low` · `medium` · `high` · `critical` |
| `category`, `tags` | str, list | Project taxonomy. Validated against config by the manager, not the model. |
| `effort` | str | Free text. An estimate, not a contract. |
| `assignment` | object | `owner` (live, one actor id) and `eligible` (authoring-time list; empty means anyone). |
| `parent` | str | Task id of an umbrella task. It must exist; a task may not be its own parent, nor be parented into a cycle. A task with an **open** child is not claimable and is never offered by `/next` — an umbrella is finished by its children. `GET /api/tasks?parent=<id>` lists one umbrella's children. |
| `spec` | object | `summary` and `description` are **required**; `intent`, `constraints`, `out_of_scope`, `context[]` are optional. See the example below. |
| `acceptance[]` | list | `id`, `text`, optional `verify`, `status`: `pending` · `met` · `failed` · `dropped`. |
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

Null and absent mean the same thing for `ball`, `ball_reason` and `outcome`; omission
is what the manager writes, and an explicit `null` is accepted on load.

The rules are re-checked on every write, not only at construction: `TaskStorage.mutate_task`
re-validates the mutated model before serialising it, because assigning an attribute
does not re-run a Pydantic validator.

---

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
    verify: poetry run pytest tests/test_api.py    # optional machine-checkable hint
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

Only seven fields are required: `id`, `title`, `created`, `updated`, `category`,
`spec.summary` and `spec.description`. `schema` defaults to `2`; `lifecycle` defaults to
`draft`, which then requires a `ball` by rule 1.

## `log[]`

One append-only typed log replaces v1's `status_updates`, `comments` and
`prompts.followups`.

Types: `note` · `progress` · `transition` · `handoff` · `decision` · `question` ·
`answer` · `instruction`.

Integrity rules, enforced: ids are unique and ascending, and `re:` must reference an
**earlier** entry that exists. An open `question` is one with no `answer` threaded to
it, which makes unresolved threads queryable (`Task.open_questions()`).

`transition` entries are written by the manager, never by a caller — the API rejects an
attempt to post one directly, because a transition that does not accompany a real state
change is a lie in the record.

### `attachments[]` on an entry

An entry may carry images evidencing it — a screenshot of the thing being objected to.
The field is additive and **absent** unless the entry has images, so no existing file
gains a line for a field it does not use, and no schema version bump is involved.

The bytes are **not** in the YAML. They live in sidecar files under the tasks directory
at `attachments/<task-id>/<sha256><ext>`, and the entry carries only metadata:

| field | meaning |
|---|---|
| `path` | Sidecar path, relative to the tasks directory. |
| `media_type` | `image/png`, `image/jpeg` or `image/webp`, read from the bytes. |
| `sha256` | Content hash. Also the filename, and checked on every read. |
| `size_bytes` | Size of the stored file. |
| `label` | Accessible label; the alt text where it renders. |

That split is the point: a task file stays something a person reads in a text editor and
git diffs line by line, which a base64 blob would end. Images only, 5 MiB each, and the
type is derived from the magic number rather than taken from the caller's claim.

Two consequences are deliberate. The same image pasted twice is stored once, because the
name *is* the hash. And a file whose bytes no longer hash to its name is refused rather
than rendered. Git keeps every blob forever, so unreferenced files are **reported, never
deleted** — `AttachmentStore.orphans()` lists them for a person to decide about.

## `display_status`

Computed on read, never stored — `Needs review`, `In progress (claude)`,
`Blocked on task-044`, `Ready`, `Completed (archived)`. A stored copy of three fields is
a drift bug waiting for its moment.

It is a Pydantic *computed field*, so it appears in API responses and templates use it
instead of switching on the axes themselves. `TaskStorage` excludes it when writing, and
a file that contains it is rejected by name (`extra="forbid"`).

## How files are written

`TaskStorage._write_task()` dumps with `by_alias=True` and `exclude_none=True`, so unset
optional fields are **absent** rather than written as `null`.

`by_alias` is load-bearing: `schema` shadows a Pydantic `BaseModel` attribute, so the
field is `schema_version` in Python with `alias="schema"`. Dumping without the alias
writes the wrong key and produces a file the loader then refuses as v1.

Unknown fields are rejected outright (`extra="forbid"`), so a stale key fails by name
rather than being silently ignored — whether it came from a hand edit, a migrator bug or
a React form posting a retired field name.

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

`update_task()` edits content fields (title, spec, acceptance, tags…) and deliberately
cannot touch the axes.

A round-trip check:

```python
from pathlib import Path
from agentjobs.storage import TaskStorage

task = TaskStorage(Path("tasks/agentjobs")).load_task("task-042-relocate-demo-tasks")
print(task.display_status, len(task.log))
```

`load_task()` returns `None` when the file does not exist, and raises `TaskLoadError`
naming the file and field when it exists but cannot be read — including when it is an
unmigrated v1 file. A broken task is reported, never silently absent; `GET
/api/tasks/broken` lists them.

A complete worked example is
[`schema/examples/task-048.v2.yaml`](https://github.com/jeffposey/agentjobs/blob/main/schema/examples/task-048.v2.yaml)
— this repo's own design task, converted, annotated inline.
