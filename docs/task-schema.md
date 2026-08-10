# Task Schema Reference

Every task is a single YAML file. These files are the source of truth for the project —
not a chat log, not an issue tracker. The schema below is defined by the Pydantic models
in [`src/agentjobs/models.py`](../src/agentjobs/models.py), which is authoritative if
this document ever drifts from it.

Tasks live in the directory named by `tasks_directory` in `.agentjobs/config.yaml`
(`tasks/agentjobs/` for this repo's own backlog). `TaskStorage` globs `*.yaml`
non-recursively, so files in subdirectories are invisible to the store — that is how
`tasks/test-data/` stays out of the real backlog.

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
