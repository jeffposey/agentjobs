# Understanding the schema

The design doc explains *decisions*. The reference pages are *lookup*. Neither
teaches you the model. This page does, using the one task you already know —
**task-048, the design pass itself** — shown in both schemas.

Budget about 15 minutes. You do not need to read `schema-design.md` first.

---

## Historical v1 in one screen

Schema v1 is retired. This section exists only to explain the migration that produced
the current v2 model; new tasks and integrations must use v2.

A task is **one YAML file**, loaded by one Pydantic model. `Task` is the root, and it
owns ten lists of small nested objects:

```
Task ─┬─ phases[]            sub-units inside the task
      ├─ success_criteria[]  the definition of done
      ├─ prompts             {starter, followups[]}   instructions for agents
      ├─ status_updates[]    agent-authored progress log
      ├─ comments[]          human/agent discussion log
      ├─ deliverables[]      files this task produces
      ├─ dependencies[]      links to other tasks
      ├─ external_links[]    URLs
      ├─ issues[]            problems hit while working
      └─ branches[]          git branches
```

Plus flat fields: `id`, `title`, `created`, `updated`, `status`, `priority`,
`category`, `assigned_to`, `estimated_effort`, `human_summary`, `description`, `tags`.

**That's the whole of v1.** The one thing worth holding onto: `status` is a single
8-value enum — `draft`, `ready`, `in_progress`, `blocked`, `waiting_for_human`,
`under_review`, `completed`, `archived`. Almost everything v2 changes traces back to
that one field.

---

## The change everything else follows from

`status` answers three unrelated questions with one value:

- *Where is this in its life?* — `draft`, `completed`, `archived`
- *Who has to act next?* — `in_progress`, `waiting_for_human`, `blocked`
- *Why do they have to act?* — `under_review`, and nothing else

`under_review` is the tell. It means "waiting on a human **because** code review" — a
why that got promoted into the vocabulary because there was nowhere else to put it.
There is no `waiting_for_human_because_decision`, so that case is indistinguishable
from every other kind of waiting.

**Here is task-048 right now, in both schemas:**

=== "v1"

    ```yaml
    status: waiting_for_human
    ```

    Waiting on a human. For what? Unknown — you have to read `comments[]` to find out,
    and the historical UI had no field to render, which is why its review banner said the same
    generic sentence for every task.

=== "v2"

    ```yaml
    lifecycle: active        # where in its life
    ball: human              # who acts next
    ball_reason: review      # why they hold it
    ball_prompt: >-          # what they actually have to do
      Review docs/schema-design.md and approve or reject the merge. Five decisions
      need your sign-off, not three: D1-D3 as originally recorded, plus D4 (actors
      referenced by id) and D5 (criterion types accepted but deferred)...
    ```

Two of those fields are **required while a task is open**. That is the whole point:
a task nobody is responsible for, or a handoff with no stated ask, cannot be written
down. Limbo stops being representable.

`ball_reason` is scoped to whoever holds the ball — an agent can be `available`,
`work`, or `revise`; a human can be `spec`, `review`, `decision`, `approval`, or
`input`; `external` can be `dependency` or `service`. That's the "sub-parameter for
why" from your own first design note, made into a closed vocabulary.

---

## One blob becomes a spec

v1 gave task-048 a **5,240-character `description`** plus a **1,900-character
`prompts.starter`** that largely restated it. v2 splits that along the questions an
agent actually asks:

| field | answers |
|---|---|
| `spec.summary` | what is this, in two sentences |
| `spec.intent` | **why** does this task exist |
| `spec.description` | **what** to do |
| `spec.constraints` | hard requirements and prohibitions |
| `spec.out_of_scope` | explicit non-goals |
| `spec.context[]` | read these files first, and why |

**`out_of_scope` is the one to notice.** It is where the scope-drift story lands. In
the converted file it reads:

```yaml
out_of_scope: |
  - Implementing any schema change.
  - Writing those derived task files is itself deliverable work, not clerical.
    It is in scope for the task overall but NOT for any session that has not been
    told to do it.
```

v1 had nowhere to put that sentence, which is why nothing stopped a session from
acting on it.

---

## Three logs become one

v1 recorded task-048's history in **three** parallel append-only lists —
`status_updates[]`, `comments[]`, and `prompts.followups` — with an implied but
unenforced split between them. v2 has one `log[]` where every entry carries a type:

`note` · `progress` · `transition` · `handoff` · `decision` · `question` · `answer` ·
`instruction`

Two things become visible only once entries are typed:

**Decisions become individually addressable.** In v1, D1/D2/D3 are three sentences
inside one paragraph of one status update. In v2 they are three `decision` entries —
so "show me every decision on this task" is a query, and each one carries its own
rejected alternative.

**Authorship stops lying.** v1's status update says *"JEFF'S IDEA #1 — decompose
status"* inside an entry whose `author` is `claude`. The idea is Jeff's; the entry is
Claude's. In v2 it is entry 2, `actor: jeff`, `type: instruction`. The record now
matches what happened.

---

## Shared things get referenced, not copied

v1 wrote authors as free text (`author: claude`). The v2 draft over-corrected and
embedded `{id: claude, kind: agent}` — which put five identical copies in one small
example. **D4** settled it: the task file names the actor by bare id, and `kind`
lives in config.

```yaml
log:
  - id: 1
    actor: claude      # not {id: claude, kind: agent}
```

Same pattern already used by `parent`, `dependencies[].task`, `category`, and `tags`:
shared things get referenced from inside the document. This is what your relational
instinct was tracking — and the fix is references, not tables.

---

## What v1 had that v2 doesn't

| gone | where it went |
|---|---|
| `phases[]` | sub-tasks via `parent` — one way to subdivide, not two |
| `prompts` | the spec *is* the briefing; followups became `instruction` log entries |
| `issues[]` | empty in all 25 corpus files; an issue is a log entry or its own task |
| `comments[]` | merged into `log[]` |
| `status_updates[]` | merged into `log[]` |
| `human_summary` | `spec.summary` — the split was by length, not audience |
| `dependencies[].status` | deleted; no validator, no vocabulary, no purpose |

---

## Read next, in this order

1. **[The converted file itself](https://github.com/jeffposey/agentjobs/blob/main/schema/examples/task-048.v2.yaml)**
   — `schema/examples/task-048.v2.yaml`. Every mechanism above, in one real record,
   annotated inline. It validates against the v2 schema.
2. **[The v2 entity diagram](v2-erd.md)** — now that the fields mean something.
3. **[Design rationale](../schema-design.md)** §3 and §7 — the state model in full,
   and the alternatives that were rejected.
4. **[Reference pages](v2/index.md)** — only when you need a specific field.

!!! tip "The fastest single check"
    Open `schema/examples/task-048.v2.yaml` next to
    `tasks/agentjobs/task-048-schema-design.yaml`. Same task, both schemas. If the v2
    version reads more clearly to you than the v1 one, the design works; if some part
    of it reads worse, that part is worth arguing about before task-050 starts.
