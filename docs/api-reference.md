# AgentJobs REST API reference

AgentJobs exposes the schema-v2 task workflow as JSON. The generated OpenAPI document
is the endpoint and payload source of truth:

- Interactive reference: [`http://localhost:8765/docs`](http://localhost:8765/docs)
- Repository contract: [`frontend/openapi.json`](https://github.com/jeffposey/agentjobs/blob/main/frontend/openapi.json)
- Generated TypeScript client: `frontend/src/api/generated/`

Run `agentjobs open` or `agentjobs serve` before using the local URLs. AgentJobs has no
authentication; keep it on loopback unless a private HTTPS proxy and access policy are
in place.

## Project scoping

Every project-owned endpoint is available in two forms:

- `/api/...` uses the default project resolved for the server process.
- `/api/projects/{project_id}/...` addresses one registered project explicitly.

Use `GET /api/projects` to discover project identifiers. The React application uses
the scoped form so switching projects never depends on the server's current directory.

## Task reads

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/tasks` | List tasks; filter with `lifecycle`, `ball`, `priority`, or `parent` |
| `GET` | `/api/tasks/next` | Return the next claimable task; accepts `agent` and `priority` |
| `GET` | `/api/tasks/next/explain` | Why that task is next, and every open task ahead of it |
| `GET` | `/api/tasks/{task_id}` | Return one task record |
| `GET` | `/api/tasks/{task_id}/detail` | Return the full review/resumption view with relationships |
| `GET` | `/api/tasks/broken` | Report task files that exist but fail validation |
| `GET` | `/api/search?q=...` | Search task id, title, spec, ball prompt and tags |
| `GET` | `/api/dashboard` | Return dashboard counts and activity |
| `GET` | `/api/revision` | Return the project revision used for client refresh |

The human inbox is `GET /api/tasks?ball=human`; external blockers are
`GET /api/tasks?ball=external`. These are derived from schema-v2 axes, not legacy status
strings.

## Task creation and editing

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/tasks` | Create a draft or ready task |
| `PATCH` | `/api/tasks/{task_id}` | Update editable metadata and specification fields |
| `DELETE` | `/api/tasks/{task_id}` | Archive the task through the storage policy |
| `PATCH` | `/api/tasks/{task_id}/deliverables/{path}` | Mark a deliverable done |

State axes do not move through the generic patch route. Use the verbs below so
preconditions are enforced and transition history is appended.

## Canonical state verbs

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/tasks/{task_id}/claim` | Atomically claim ready work for an eligible agent |
| `POST` | `/api/tasks/{task_id}/handoff` | Move the ball with a holder, reason, and concrete ask |
| `POST` | `/api/tasks/{task_id}/release` | Return claimed work to the ready pool |
| `POST` | `/api/tasks/{task_id}/close` | Close with `completed`, `cancelled`, `superseded`, or `duplicate` |
| `POST` | `/api/tasks/{task_id}/log` | Append a typed note, progress, decision, question, answer, or instruction |
| `POST` | `/api/tasks/{task_id}/progress` | Append a structured progress entry |

The React review actions use `/approve`, `/request-changes`, and `/reject`. Approval
records the human handoff back to `agent/work`; it does not run git or merge a branch.

## The queue

Order is an explicit, stored field, not a sort over timestamps. `queue_position` is
unique within a priority band, and selection answers `(band, position)` with no
tie-break. Every route here reads or changes that one managed order; none of them
re-sorts, and none accepts a position.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/projects/{id}/queue` | The whole ordered backlog, band by band, with claimability on every entry |
| `POST` | `/api/tasks/{task_id}/queue-move` | Move a task within its band: `before`, `after`, `top` or `bottom` |
| `POST` | `/api/tasks/{task_id}/reprioritize` | Change a task's band, and optionally where it lands in it |
| `POST` | `/api/projects/{id}/queue/repair` | Give every open task a place again, naming everything it guessed |
| `POST` | `/api/projects/{id}/queue/compact` | Renumber one band to 100, 200, 300..., changing nobody's place |

All four mutations **require** `actor` and `operation_id`. That is stricter than the
state verbs above, where both are optional so callers written before they existed keep
working: nothing was ever written against these routes, and a reorder a timeout can
silently apply twice puts a task somewhere nobody asked for.

There is no way to set `queue_position` — not through `PATCH /api/tasks/{task_id}`,
not through the Python client, not through MCP. A caller that could write a number
would be choosing a place without knowing what else is in the band, which is exactly
how two tasks come to share one. The caller names a neighbour or an end; the server
does the arithmetic under the queue lock.

**A broken queue is refused by whatever answers and rendered by whatever repairs.**
`GET /api/tasks/next` and `/next/explain` return `409 Conflict` naming the offending
ids and the repair command, rather than answering from a field that happens to be
intact. `GET .../queue`, `queue/repair` and `agentjobs queue check` keep working
against the same corpus, because you have to be able to see a broken queue in order to
fix it.

### On the command line

```
agentjobs next [--why]                    # what to work on, and why not the other one
agentjobs queue list [--band high] [--claimable] [--agent codex]
agentjobs queue move <id> --before <id> | --after <id> | --top | --bottom [--with-children]
agentjobs queue reprioritize <id> --to high [--top | --before <id> | --after <id>]
agentjobs queue check [--strict]          # reports; --strict exits non-zero
agentjobs queue repair
agentjobs queue compact <band>
```

`agentjobs queue list` is written to be read: band headings, position, id and title,
with `!` and the excluding rule on anything not claimable. `agentjobs next` exits
non-zero on a broken queue; `queue list` and `queue check` do not.

## Minimal client example

```python
from agentjobs import Ball, BallReason, TaskClient

with TaskClient(base_url="http://localhost:8765") as client:
    task = client.get_next_task(agent="codex")
    if task:
        client.claim_task(task.id, agent="codex")
        client.handoff_task(
            task.id,
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Review the verified branch and approve or request changes.",
        )
```

## Webhooks

Webhook management is available under `/api/webhooks` and the equivalent scoped path.
See the [webhook guide](webhooks.md) for events, signatures, and payloads.
