# AgentJobs REST API reference

> **This API has no authentication of any kind.** Every route below is open to anything
> that can reach the port — there is no token, no session, and no per-project
> permission. That includes the routes that start agent processes on the machine
> (`POST /api/tasks/{task_id}/dispatch`, and the dispatch enable toggles). Bind it to
> loopback — `agentjobs serve` refuses a wildcard bind for this reason — and reach it
> from elsewhere only through a private network with its own access control. See
> [mobile access](mobile-access.md) for the tailnet setup this project uses.
>
> **There is authorization, which is a different thing.** Every mutating route is checked
> against a capability table keyed by the kind of caller, so a **dispatched agent** cannot
> approve a review, start a run, change dispatch configuration, register a project,
> repair the queue, or act on a task other than its own — and no caller may submit an
> `actor`/`user` naming somebody they are not. That constrains agents on this machine; it
> authenticates nobody. See [authorization](authorization.md), whose closing section says
> plainly what it does not cover.

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
| `POST` | `/api/tasks/{task_id}/queue-keep` | Keep a place that was moved over a stated warning, recording the strong anchor |
| `POST` | `/api/tasks/{task_id}/reprioritize` | Change a task's band, and optionally where it lands in it |
| `POST` | `/api/projects/{id}/queue/repair` | Give every open task a place again, naming everything it guessed |
| `POST` | `/api/projects/{id}/queue/compact` | Renumber one band to 100, 200, 300..., changing nobody's place |

Each of those also exists in the default-project form — `GET /api/queue`,
`POST /api/queue/repair`, `POST /api/queue/compact` — as described under
[Project scoping](#project-scoping). `queue/compact` **requires a band**; there is no
bare form that compacts everything.

Every one of these mutations **requires** `actor` and `operation_id`. The `operation_id` half is
stricter than the state verbs above, where it is optional so callers written before it
existed keep working: nothing was ever written against these routes, and a reorder that
a timeout silently applies twice puts a task somewhere nobody asked for.

There is no way to set `queue_position` — not through `PATCH /api/tasks/{task_id}`,
not through the Python client, not through MCP. A caller that could write a number
would be choosing a place without knowing what else is in the band, which is exactly
how two tasks come to share one. The caller names a neighbour or an end; the server
does the arithmetic under the queue lock.

### What a move answers back

A move always lands. Nothing here can refuse one — but the reply says whether the order
just written can execute, computed synchronously from the claimability and dependency
facts the move handler had already read. No model, no delay, and **silence on an
ordinary move**:

| `kind` | What it means |
| --- | --- |
| `promoted_unclaimable` | The task you promoted will be skipped, and why |
| `above_prerequisite` | It now stands ahead of something it `needs`, or behind something that needs it |
| `demoted_blocker` | The move pushed a task down that other open work is waiting on |
| `no_op` | The band came out in the order it went in |
| `queue_broken` | The band is not in a state to be reasoned about |

Ask for `?envelope=true` and the response carries `queue_warnings` and `queue_undo` —
the placement that puts the task back, offered only alongside a warning and only for a
single-task move. Without the envelope the route answers with the bare task exactly as
it always did. `agentjobs queue move` prints the findings on stderr after the success
line, and `task_queue_move` carries them in its result and its summary. One
implementation behind all of them.

`queue-keep` is the other half. It records that a person read those findings and kept
the position anyway — a **strong anchor**, which an automatic reorder may not overturn.
It is refused when the last move produced no warnings, because an anchor claims
"informed, and kept anyway". Undo needs no route of its own: it is a move back.
[task-schema.md](task-schema.md#queue_move) has the exact record both write.

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
| `GET` | `/api/dispatch/runs/{run_id}/transcript` | The same run as structured entries, for a panel that renders rather than dumps |
| `GET` | `/api/dispatch/finishes/{task_id}` | What a scripted finish is doing to this task's branch, or last did |
| `GET` | `/api/dispatch/finishes/{task_id}/output` | That finish's output in full, as text |

`transcript.log` is a raw TTY capture, so a line appears in it once per terminal
repaint. Link to it and read it; never compute a count from it.

### The one route that is not project-scoped

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runs/live` | Every run happening on this **machine**, in every project, with its remaining capacity |

Every other route on this page is mounted twice -- once at `/api/...` for the default
project and once at `/api/projects/{project_id}/...` -- and answers about that one
project. This one is mounted once and has no project-scoped spelling, because the
resource it describes is not a project's: `limits.max_concurrent_runs` is machine-level,
the run ledger under `~/.agentjobs/runs/` is machine-level, and the run occupying the
last slot is usually on some other project's task. Serving the same body under every
value of `{project_id}` would be a URL asserting a scope the answer does not have
(task-328).

Two fields are worth reading carefully:

- **`health`, not `live`.** `live` means only that nothing has declared the run over. A
  session parked on a permission prompt, a session that has emitted nothing for the
  stall window, and a batch run whose supervising process is gone are all live and none
  of them is working. `health` is `working`, `starting`, `parked`, `silent`, `orphaned`
  or `unknown`, and it is the field a surface should render.
- **`holders` is not `runs`.** A scripted finish and a repository's merge runway hold
  locks rather than run slots, so they are real machine activity with no run record.
  They are listed separately and are deliberately **not** in `occupied`, which counts
  exactly what the concurrency guard counts.

### The two readings of one run

`/output` and `/tail` serve `transcript.log`. `/transcript` serves a different file: the
JSONL the runner writes about the session, one event per line. They are not two formats
of the same thing, and neither replaces the other.

The distinction matters because the TTY capture cannot be made readable by cleaning it
up. It is a **repaint**: a TUI draws a space by emitting `ESC[1C` and a line break by
emitting an absolute cursor position, so anything that removes the escape sequences and
keeps the rest deletes every space in it. That is how an approval dialog came to render
as `NewMCPserverfoundinthisproject:agentjobs` (task-023). `/transcript` reads what the
session recorded about itself instead -- its prose, each tool call with its input,
whether the call failed, and the patch an edit applied -- and returns those as entries,
with runs of consecutive calls summarized into one.

`source: "none"` is an ordinary answer rather than an error, and `note` says which case
it is: a batch run has no session, a session that has just started has not reported its
id, and a driver that keeps no such file never will. A caller that gets it falls back to
`/tail` -- which is also the right view when a session dies in a way no renderer models,
the unparsed bytes being the only evidence there is.

### Watching a finish

The finish routes are keyed on the **task**, not on a finish id, because the question a
page asks is "what is happening to this one" -- and the reader who pressed Approve has
no finish id to ask with: the finish that answers them does not exist yet at the moment
they press.

`GET /api/dispatch/finishes/{task_id}` answers `null`, with a `200`, for a task no
finish has ever run for. That is the ordinary case for almost every task and is not a
`404`: an absent finish is not a missing resource, and a page polling every two seconds
would otherwise teach its reader to ignore them.

Otherwise it answers a `TaskFinishView`, whose `state` is one of:

| State | Means |
| --- | --- |
| `starting` | Spawned; it has not written anything yet. Covers the second or two of process start-up |
| `running` | Working. `steps[]` holds what has landed, plus the one in flight; `gate` holds how far into the gate it is |
| `finished` | Merged, delivered and verified. `merge_commit` names the merge |
| `escalated` | It stopped and handed back. `stopped_at` names the step and `reason` says why |
| `declined` | It was never a finish candidate, so the approval behaved as it always did |
| `interrupted` | It wrote no ending and its process is gone -- a reboot, or something killed it |

`live` is derived from the task's run lock rather than from a clock: a finish holds it
for its whole attempt, so a holder that can be shown to be gone is what makes the
difference between `running` and `interrupted`. `elapsed_seconds` is computed on the
server, because `started_at` is that machine's clock and the phone reading the page is
not on it.

`output_tail` is empty for the whole of a running finish, and that is correct rather
than missing: a spawned finish writes its step table when the process *ends*. What is
live is `steps[]`.

## Playbooks

A playbook is a reusable brief for recurring work, stored per project in git as
markdown with a YAML frontmatter contract. See
[the playbooks design](playbooks-design.md).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/projects/{id}/playbooks` | Every playbook the project holds, with the files that would not load |
| `GET` | `/api/projects/{id}/playbooks/{name}` | One playbook, including its brief |
| `POST` | `/api/projects/{id}/playbooks/{name}/run` | Start an agent on it: `202`, with the run and the task it is against |

All three also exist in the default-project form — `GET /api/playbooks`,
`GET /api/playbooks/{name}` and `POST /api/playbooks/{name}/run` — as described under
[Project scoping](#project-scoping).

The collection omits each playbook's body — it is a discovery listing, not a way to
pull every brief at once — and the single-playbook route is what carries it.

### Running one

`POST .../run` takes the same three fields the dispatch endpoint takes, because it
**is** the dispatch endpoint with a brief attached:

```json
{ "user": "Jeff Posey", "task": "task-123", "group": "deep" }
```

- `user` — the human asking. Must be an actor the project configures with
  `kind: human`. Required for a **project-target** playbook, which creates a run task
  attributed to them: that creation entry is the authorisation the human-clocked rule
  then reads. For a **task-target** playbook it is task-188's authorising entry, written
  onto the task named, exactly as `POST /tasks/{id}/dispatch` writes it. The project's
  `default_user` is never substituted for a `user` nobody sent.
- `task` — required for a task-target playbook and refused for a project-target one,
  which creates its own run task. A mismatch either way is `409 target_mismatch`.
- `group` — a runner group this machine already defines. It cannot open a closed gate.

**Every dispatch gate binds unchanged.** The master switch, the sentinel file,
per-project enablement, the concurrency cap, the clean-tree rule and the human-clocked
rule all refuse a playbook run exactly as they refuse a dispatch, under the same
`code`s, and naming a playbook opens none of them — a playbook is repository content,
and repository content never decides what may execute on a machine. Dispatch being off
is checked *before* a run task is created, so a refusal that could never have started a
run leaves nothing behind. A refusal that arrives later — a busy machine, a dirty tree
— names the run task it did create, which stays on the record and can be dispatched
from its own task page once the cause is cleared.

The `202` body is the dispatch response plus `playbook`, `playbook_path`,
`playbook_hash` and `created_run_task`. The hash is the file's sha256 at instantiation
and it is pinned on the task's `dispatch` log entry: `git_head` says which commit the
tree was on, and this says which brief actually ran.

**MCP still has no run tool, and that is the design** — an MCP mutation is callable by
an agent, and an agent starting a playbook run is an agent causing a dispatch.

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

agentjobs playbook run groom --actor "Jeff Posey"
agentjobs playbook run flesh-out --task task-123          # a task-target playbook
agentjobs playbook run groom --actor "Jeff Posey" --group deep
```

`run` starts an agent and spends money. `--actor` names the human creating the run task
for a project-target playbook and **has no `default_user` fallback**, unlike every other
`--actor` in this CLI: the entry it writes is read a moment later as the authorisation
for the run, and one attributed to whoever the config happens to name is not a person's
signature. A task-target playbook creates nothing, so `--actor` does not apply to it and
the target task's own newest entry must be a human's — the same rule
`agentjobs dispatch run` applies. There is no flag here that authorises a dispatch of an
existing task on somebody's behalf.

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
| `GET` | `/api/whoami` | Who this request resolved as: `owner`, `tailnet`, or a dispatched `run` naming its run and task |

`GET /api/whoami` reports identity and enforces nothing -- every other route answers
exactly as it did before, for every caller. It is how a dispatched agent can see that the
service resolved it as its **run** rather than as the person at the machine: a run
presents a credential minted at dispatch, and loopback without one is the owner. The
answer never echoes the credential, and a run's `actor_id` is always null, because a run
is not a human and must never be attributed as one. See the
[principals design](principals-design.md).

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
