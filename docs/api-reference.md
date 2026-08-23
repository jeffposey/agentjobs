# AgentJobs REST API reference

> **This API has no authentication of any kind.** Every route below is open to anything
> that can reach the port — there is no token, no session, and no per-project
> permission. That includes the routes that start agent processes on the machine
> (`POST /api/tasks/{task_id}/dispatch`, and the dispatch enable toggles). Bind it to
> loopback — `agentjobs serve` refuses a wildcard bind for this reason — and reach it
> from elsewhere only through a private network with its own access control. See
> [mobile access](mobile-access.md) for the tailnet setup this project uses.

AgentJobs exposes the schema-v2 task workflow as JSON. The generated OpenAPI document
is the endpoint and payload source of truth:

- Interactive reference: [`http://localhost:8765/docs`](http://localhost:8765/docs)
- Repository contract: [`frontend/openapi.json`](https://github.com/jeffposey/agentjobs/blob/main/frontend/openapi.json)
- Generated TypeScript client: `frontend/src/api/generated/`

Run `agentjobs open` or `agentjobs serve` before using the local URLs.

Every operation in the OpenAPI document appears somewhere on this page, and
`tests/test_api_reference_coverage.py` fails the build if one stops doing so. A route
added without a line here is a test failure, not a documentation debt someone notices
later.

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
| `DELETE` | `/api/tasks/{task_id}` | Archive the task. **An open task is closed as `cancelled` first** — this is not a soft hide |
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

### Human review actions

The React UI drives these; they are ordinary routes and a script may call them.
Each one records a handoff, so each writes its own `ball_reason`:

| Method | Path | Ball it writes | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/tasks/{task_id}/approve` | `agent` / `work` | Approve work that was handed back for review |
| `POST` | `/api/tasks/{task_id}/request-changes` | `agent` / `revise` | Send it back with specific revisions |
| `POST` | `/api/tasks/{task_id}/reject` | — closes the task | Reject the work outright |
| `POST` | `/api/tasks/{task_id}/answer` | `agent` / `answer` | Answer a question the agent raised |
| `POST` | `/api/tasks/{task_id}/redirect` | `agent` / `redirect` | Change direction without rejecting |
| `POST` | `/api/tasks/{task_id}/hold` | `agent` / `hold` | Park the task without releasing it |
| `POST` | `/api/tasks/{task_id}/resume` | `agent` / `work` | Take it off hold |
| `POST` | `/api/tasks/{task_id}/promote` | `agent` / `available` | Draft becomes ready and claimable |

**Whether approval merges anything depends on the machine.** By default it records the
handoff back to `agent/work` and runs no git at all. On a machine with `finish.enabled`
for the project, approving instead triggers the scripted finish (task-241): rebase, the
full gate, `--no-ff` merge, rebuild, restart, verify, close. A person still approves per
task either way — what varies is whether an agent or a script performs the merge
afterwards. See [the dispatch design, §5a](agent-dispatch-design.md).

### Who has to say who they are

`actor` is **required** on `handoff`, `release`, `promote`, `close` and `log`; `claim`
requires `agent` instead, which is the same thing under an older name. There is no
anonymous state change: the log entry each verb appends names somebody.

`operation_id` is optional on those verbs and **required** on the four queue mutations
below. Sending the same `operation_id` twice replays the first result rather than
writing twice, so a retry after a timeout is safe.

*(Corrected 2026-08-22. This section previously said `actor` and `operation_id` were
both optional on the state verbs. Only the second half was ever true.)*

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

Each of those four also exists in the default-project form — `GET /api/queue`,
`POST /api/queue/repair`, `POST /api/queue/compact` — as described under
[Project scoping](#project-scoping). `queue/compact` **requires a band**; there is no
bare form that compacts everything.

All four mutations **require** `actor` and `operation_id`. The `operation_id` half is
stricter than the state verbs above, where it is optional so callers written before it
existed keep working: nothing was ever written against these routes, and a reorder that
a timeout silently applies twice puts a task somewhere nobody asked for.

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

## Dispatch

**These routes start processes on the machine.** Read the auth warning at the top of
this page before exposing them. Dispatch is off unless a machine-local
`~/.agentjobs/dispatch.yaml` enables it *and* the project is enabled; the API can flip
the second switch and can never define what runs. See
[the dispatch design](agent-dispatch-design.md).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/dispatch` | Whether dispatch is configured and enabled, and the runners this machine defines |
| `POST` | `/api/dispatch/enable` | Enable dispatch for the project |
| `POST` | `/api/dispatch/disable` | Disable it. Always available, deliberately without ceremony |
| `POST` | `/api/tasks/{task_id}/dispatch` | Start an agent on this task |
| `GET` | `/api/dispatch/runs` | Runs, live and historical, from the ledger |
| `POST` | `/api/dispatch/runs/{run_id}/cancel` | Cancel one live run |
| `GET` | `/api/dispatch/runs/{run_id}/output` | The run's captured transcript |
| `GET` | `/api/dispatch/runs/{run_id}/tail` | The tail of it, for a live view |

`transcript.log` is a raw TTY capture, so a line appears in it once per terminal
repaint. Link to it and read it; never compute a count from it.

## Playbooks

A playbook is a reusable brief for recurring work, stored per project in git as
markdown with a YAML frontmatter contract. See
[the playbooks design](playbooks-design.md).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/projects/{id}/playbooks` | Every playbook the project holds, with the files that would not load |
| `GET` | `/api/projects/{id}/playbooks/{name}` | One playbook, including its brief |

Both also exist in the default-project form — `GET /api/playbooks` and
`GET /api/playbooks/{name}` — as described under
[Project scoping](#project-scoping).

**This surface is read-only, and its incompleteness is the design.** Running a playbook
is a dispatch, and only a human starts a dispatch, so there is no `POST` here and the
MCP server exposes `playbooks_list` with no counterpart that runs one. The collection
omits each playbook's body — it is a discovery listing, not a way to pull every brief
at once — and the single-playbook route is what carries it.

`exists: false` on the collection means the project has no playbooks directory. That is
the state every project starts in and is not an error; `agentjobs playbook init` copies
the shipped references in, and never overwrites a file already there. A file that is
present and does not validate is reported in `problems` beside the valid ones rather
than omitted, and asking for it by name is `422`, not `404`: it is there, and it is
repairable.

### On the command line

```bash
agentjobs playbook list              # names, descriptions, and each one's contract
agentjobs playbook show groom        # the frontmatter, then the brief
agentjobs playbook show groom --contract   # the frontmatter alone
agentjobs playbook init              # copy the shipped references in
```

`init` writes one file per shipped playbook and **skips any name already present**,
saying which it kept. That is per file rather than all-or-nothing on purpose: a project
that has tuned its own `groom.md` should be able to take a newly shipped `reorder.md`
without the tuned one being touched, and the tuned one is exactly what "never
overwrite" protects. From the moment a copy exists it is authoritative — a brief behind
a name must never depend on which version of AgentJobs is installed.

## Projects and system

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/projects` | Registered projects, with actor vocabularies and default user |
| `POST` | `/api/projects` | Register an existing AgentJobs project |
| `POST` | `/api/projects/init` | Initialize a directory as a project and register it |
| `POST` | `/api/projects/inspect` | Report what a directory looks like before committing to it |
| `GET` | `/api/all/tasks` | Every task across every registered project |
| `GET` | `/api/tasks/{task_id}/attachments/{filename}` | Fetch a file attached to a task |
| `GET` | `/api/health` | Liveness. Returns `{"status": "ok"}` |
| `GET` | `/api/version` | Package version, schema version, YAML loader, source root and commit, start time, and whether the frontend bundle is present |

`GET /api/version` is the route to ask when something looks stale. `source_root` and
`source_commit` are fixed at process start, so they describe the code **in memory**
rather than the files on disk — which is what makes them evidence that a merge is live
rather than merely committed. `frontend_bundle` says whether `/app/` can be served at
all.

## Webhooks

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/webhooks` | List configured webhooks |
| `POST` | `/api/webhooks` | Register one |
| `GET` | `/api/webhooks/{webhook_id}` | Read one |
| `DELETE` | `/api/webhooks/{webhook_id}` | Remove one |
| `POST` | `/api/webhooks/{webhook_id}/test` | Send a test delivery |

See the [webhook guide](webhooks.md) for events, signatures, and payloads.
