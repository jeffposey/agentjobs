# Task Schema Reference

Every task is a single YAML file. These files are the source of truth for the project —
not a chat log, not an issue tracker.

Tasks live in the directory named by `tasks_directory` in `.agentjobs/config.yaml`
(`tasks/agentjobs/` for this repo's own backlog). `TaskStorage` globs `*.yaml`
non-recursively, so files in subdirectories are invisible to the store — that is how
`tasks/test-data/` stays out of the real backlog.

## Two schemas, briefly

| | Defined by | State today |
|---|---|---|
| **[v2](#schema-v2)** | [`models_v2.py`](../src/agentjobs/models_v2.py) | Implemented and tested, **not yet in use**. No file on disk is v2 yet. |
| **[v1](#schema-v1-current-on-disk-format)** | [`models.py`](../src/agentjobs/models.py) | What every task file currently contains, and what storage, the API and the GUI run on. |

Both are documented because both are real right now. Task-051 converts the corpus and
repoints the application at v2; when it lands, the v1 half of this page goes with it.

A v2 file is identifiable at a glance: it starts with `schema: 2`. A file without that
stamp is v1, and v2's loader refuses it by name rather than guessing
(`agentjobs migrate-schema` converts it).

The design behind v2, including the alternatives that were rejected, is in
[schema-design.md](schema-design.md). This page is reference; that one is reasoning.

---

## Schema v2

### The change everything follows from

v1's single `status` answered three unrelated questions at once. v2 splits them:

| Question | v2 field | Values |
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

### Fields

| Field | Type | Notes |
|---|---|---|
| `schema` | int | Always `2`. Its absence means v1. |
| `id`, `title`, `created`, `updated` | | As v1. |
| `lifecycle`, `ball`, `ball_reason`, `ball_prompt`, `outcome`, `archived` | | The state axes, above. |
| `priority` | enum | `low` · `medium` · `high` · `critical` |
| `category`, `tags` | str, list | Project taxonomy. Validated against config by the manager, not the model. |
| `effort` | str | Free text. An estimate, not a contract. |
| `assignment` | object | `owner` (live, one actor id) and `eligible` (authoring-time list; empty means anyone). |
| `parent` | str | Task id of an umbrella task. A task may not be its own parent. |
| `spec` | object | See below. |
| `acceptance[]` | list | `id`, `text`, optional `verify`, `status`: `pending` · `met` · `failed` · `dropped`. |
| `deliverables[]` | list | `path`, `note`, `status`: `pending` · `done` · `dropped`. |
| `dependencies[]` | list | `task`, `type`: `needs` · `blocks` · `related`, `note`. |
| `links[]` | list | `url` (validated), `rel`: `pr` · `issue` · `doc` · `design` · `build` · `other`, `title`. |
| `branches[]` | list | `name`, `status`: `active` · `merged` · `abandoned`, `merged_at`. |
| `log[]` | list | The unified log. See below. |

`acceptance` and `deliverables` keep separate vocabularies on purpose: a criterion is
*verified* (`met`), a deliverable is *produced* (`done`).

### `spec`

One blob became six fields, split along the questions an agent actually asks:

| Field | Answers |
|---|---|
| `summary` | what is this, in two sentences (**required**) |
| `intent` | **why** does this task exist |
| `description` | **what** to do |
| `constraints` | hard requirements and prohibitions |
| `out_of_scope` | explicit non-goals |
| `context[]` | `{path, why}` — read these first, and why |

### `log[]`

One append-only typed log replaces v1's `status_updates`, `comments` and
`prompts.followups`.

```yaml
log:
  - id: 4                     # per-task integer, unique and ascending
    ts: '2026-07-29T18:35:00Z'
    actor: claude             # bare id; kind is resolved from config
    type: handoff
    re: 2                     # optional: threads to an earlier entry
    data: {ball: human, ball_reason: review}
    body: |
      Branch complete and verified. Need: review the diff, approve or request changes.
```

Types: `note` · `progress` · `transition` · `handoff` · `decision` · `question` ·
`answer` · `instruction`.

Integrity rules, enforced: ids are unique and ascending, and `re:` must reference an
**earlier** entry that exists. An open `question` is one with no `answer` threaded to
it, which makes unresolved threads queryable.

### `display_status`

Computed on read, never stored — `Needs review`, `In progress (claude)`,
`Blocked on task-044`, `Ready`, `Completed`. A stored copy of three fields is a drift
bug waiting for its moment.

### Gone from v2

`phases` · `prompts` · `issues` · `comments` · `status_updates` · `human_summary` ·
`dependencies[].status` · the `Comment` model. Unknown fields are rejected outright
(`extra="forbid"`), so a stale key fails by name rather than being silently ignored.

A complete worked example is
[`schema/examples/task-048.v2.yaml`](https://github.com/jeffposey/agentjobs/blob/main/schema/examples/task-048.v2.yaml)
— this repo's own design task, converted, annotated inline.

---

## Schema v1 (current on-disk format)

!!! note "This is what your files contain today"
    Everything below describes v1, which every task file currently uses and which
    storage, the manager, the API and the GUI still run on. It is defined by
    [`src/agentjobs/models.py`](../src/agentjobs/models.py), authoritative if this
    document drifts. Task-051 migrates the corpus to v2 and retires this section.

## Minimal task

Only six fields are required. Everything else has a default.

```yaml
id: task-050-add-caching
title: Add a caching layer
created: '2026-07-29T18:00:00Z'
updated: '2026-07-29T18:00:00Z'
category: performance
description: |
  ## Objective
  Reduce repeated reads against the task store.
```

## Full example

```yaml
id: task-042-relocate-demo-tasks
title: Relocate demo tasks to tasks/test-data/
created: '2026-07-06T19:25:44.199247Z'
updated: '2026-07-29T18:28:36.607165Z'
status: completed
priority: high
category: developer_experience
assigned_to: claude
estimated_effort: 30 minutes
human_summary: >-
  Moves the seven demo tasks out of the real backlog so tasks/agentjobs/
  contains only genuine project work.
description: |
  ## Context
  Markdown. This is the specification an agent works from.
phases:
  - id: phase-1
    title: Move the files
    status: completed
    notes: Used git mv to preserve history.
    completed_at: '2026-07-29T18:20:00Z'
success_criteria:
  - id: sc-1
    description: tasks/test-data/ contains task-001.yaml through task-007.yaml
    status: completed
prompts:
  starter: |
    ## Objective
    Working instructions for the agent picking this up.
  followups:
    - timestamp: '2026-07-29T18:10:00Z'
      author: jeff
      content: Also check the README layout section.
      context: Raised during review
status_updates:
  - timestamp: '2026-07-29T18:20:10Z'
    author: claude
    status: in_progress
    summary: 'Started: relocating demo tasks'
    details: Longer prose for a reader with no other context.
comments:
  - id: comment-1
    task_id: task-042-relocate-demo-tasks
    author: jeff
    content: Approved.
    created: '2026-07-29T18:27:00Z'
    kind: feedback
deliverables:
  - path: tasks/test-data/
    status: completed
    description: Relocated demo task YAML files
dependencies:
  - task_id: task-041-phase-6-human-workflow-ux
    type: depends_on
    note: Sequential Phase 0 ordering
external_links:
  - url: https://github.com/jeffposey/agentjobs/pull/12
    title: PR 12
issues:
  - id: issue-1
    title: load-test-data wrote to the wrong directory
    status: resolved
    resolution: Default changed to ./tasks/test-data.
tags:
  - phase-0
  - react-frontend
branches:
  - name: chore/task-042-relocate-demo-tasks
    status: merged
    merged_at: '2026-07-29T18:28:36Z'
```

## Task fields

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `id` | string | **yes** | — | Unique. Convention: `task-###-slug`. |
| `title` | string | **yes** | — | One-line summary of the work. |
| `created` | datetime | **yes** | — | ISO 8601. |
| `updated` | datetime | **yes** | — | Overwritten on every `save_task()`. |
| `category` | string | **yes** | — | Free-form; used for filtering. |
| `description` | string | **yes** | — | Markdown. The specification. |
| `status` | `TaskStatus` | no | `draft` | See below. |
| `priority` | `Priority` | no | `medium` | See below. |
| `assigned_to` | string | no | `null` | Agent or person. |
| `estimated_effort` | string | no | `null` | Free text, e.g. `2-3 hours`. |
| `human_summary` | string | no | `null` | 1–2 sentences for a human reviewer. |
| `phases` | `Phase[]` | no | `[]` | Sub-units *inside* one task; not separately claimable. |
| `success_criteria` | `SuccessCriterion[]` | no | `[]` | Checklist defining "done". |
| `prompts` | `Prompts` | no | empty starter | Agent-facing instructions. |
| `status_updates` | `StatusUpdate[]` | no | `[]` | Append-only work log. |
| `comments` | `Comment[]` | no | `[]` | Human/agent discussion. |
| `deliverables` | `Deliverable[]` | no | `[]` | Files the task should produce. |
| `dependencies` | `Dependency[]` | no | `[]` | Links to other tasks. |
| `external_links` | `ExternalLink[]` | no | `[]` | PRs, docs, dashboards. |
| `issues` | `Issue[]` | no | `[]` | Problems hit while working. |
| `tags` | string[] | no | `[]` | Filtering and search. |
| `branches` | `Branch[]` | no | `[]` | Git branches for this task. |

### `human_summary` vs `description`

`human_summary` is for someone deciding whether to care; `description` is for whoever
does the work. Keep both — the GUI shows the summary in list views.

## Enums

### `TaskStatus`

`draft` · `ready` · `in_progress` · `blocked` · `waiting_for_human` · `under_review` ·
`completed` · `archived`

`get_next_task()` returns **only** `ready` tasks. A task sitting in `draft` will never
be picked up automatically, which is the intended way to park an idea.

`Task.is_active()` treats `ready`, `in_progress`, `blocked`, `waiting_for_human`, and
`under_review` as active.

### `Priority`

`critical` · `high` · `medium` · `low` — ranked in that order by `priority_rank()`,
which breaks ties in `get_next_task()` (most recently updated first).

## Nested types

Each of these validates its `status`/`type` field and rejects anything else.

### `Phase`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `id` | **yes** | — | e.g. `phase-1` |
| `title` | **yes** | — | |
| `status` | no | `draft` | any `TaskStatus` |
| `notes` | no | `null` | |
| `completed_at` | no | `null` | |

### `SuccessCriterion`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `id` | **yes** | — | e.g. `sc-1` |
| `description` | **yes** | — | |
| `status` | no | `pending` | `pending` `in_progress` `completed` `failed` |

### `Prompts` / `Prompt`

`Prompts` has `starter` (**required** string) and `followups` (`Prompt[]`, default `[]`).

A `Prompt` requires `timestamp` and `author`, and optionally carries `content` (inline),
`prompt_file` (path reference), and `context`.

### `StatusUpdate`
| Field | Required | Notes |
|---|---|---|
| `timestamp` | **yes** | |
| `author` | **yes** | Agent or person |
| `status` | **yes** | The `TaskStatus` transitioned to |
| `summary` | **yes** | Short |
| `details` | no | Expanded prose |

Append via `manager.update_status()` or `manager.add_progress_update()` — the former
also changes the task's status and fires webhooks, the latter only logs.

### `Deliverable`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `path` | **yes** | — | Repo-relative |
| `status` | no | `pending` | `pending` `in_progress` `completed` |
| `description` | no | `null` | |

### `Dependency`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `task_id` | **yes** | — | |
| `type` | no | `depends_on` | `depends_on` `blocks` `related` |
| `status` | no | `null` | |
| `note` | no | `null` | |

### `ExternalLink`

`url` and `title`, both **required**.

### `Issue`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `id` | **yes** | — | |
| `title` | **yes** | — | |
| `status` | no | `open` | `open` `in_progress` `resolved` `wont_fix` |
| `resolution` | no | `null` | |

### `Branch`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `name` | **yes** | — | Git branch name |
| `status` | no | `active` | `active` `merged` `abandoned` |
| `merged_at` | no | `null` | |

Recorded when the branch is created and flipped to `merged` after the merge gate — see
[ENGINEERING.md](../ENGINEERING.md#branch-lifecycle).

### `Comment`
| Field | Required | Default | Allowed |
|---|---|---|---|
| `id` | **yes** | — | |
| `task_id` | **yes** | — | |
| `author` | **yes** | — | |
| `content` | **yes** | — | |
| `created` | **yes** | — | |
| `updated` | no | `null` | Set when edited |
| `reply_to` | no | `null` | Parent comment id |
| `kind` | no | `comment` | `comment` `feedback` `question` (not validated) |

## How files are written

`TaskStorage.save_task()` dumps with `exclude_none=True`, so unset optional fields are
**absent** from the YAML rather than written as `null`. Enums serialize as their string
values (`use_enum_values=True`), which is why you see `status: completed` and never
`status: TaskStatus.COMPLETED`.

Field order in the file follows model declaration order, not alphabetical.

### Enum fields are always enum members

`task.status` is a `TaskStatus` and `task.priority` is a `Priority`, whether the value
was loaded from YAML, passed as a string, or fell through to the default. `.value` is
therefore always safe.

This was not always true. Until task-047, `Phase`, `StatusUpdate` and `Task` set
`use_enum_values=True`, which converts values that pass through validation — but
**defaults bypass validation**, so the same field was a `str` when loaded and an enum
when defaulted, while the annotation claimed the enum in both cases. Removing the
setting changed no output: `storage.py` dumps with `mode="json"`, which converts enums
to their values regardless, and the serialization of the entire task corpus is
byte-for-byte identical before and after.

The lasting lesson is in the workaround it produced. `manager.update_status()` carried
`previous_status if isinstance(previous_status, str) else previous_status.value`, which
looks careful and is dead code: `TaskStatus` subclasses `str`, so the `isinstance` check
is always true and the `.value` branch never ran. Defensive code written against an
inconsistency was itself wrong, and it type-checked. Both that guard and its twin in
`examples/collaborative_agent.py` are gone.

Comparing against the enum (`task.status == TaskStatus.READY`) is still the clearest
style, and string comparison keeps working because both enums subclass `str`.

## Editing tasks

Prefer `TaskManager` / `TaskStorage` over hand-editing YAML — they validate on save and
keep `updated` accurate. Hand-editing is workable for small text tweaks but silently
accepts invalid values until something tries to load the file.

A round-trip check:

```python
from pathlib import Path
from agentjobs.storage import TaskStorage

task = TaskStorage(Path("tasks/agentjobs")).load_task("task-042-relocate-demo-tasks")
print(task.status, len(task.status_updates))
```

`load_task()` returns `None` and logs an error on a validation failure rather than
raising, so a malformed task disappears from listings instead of breaking the server —
worth knowing when a task you expect is missing.
