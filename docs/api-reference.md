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
| `GET` | `/api/tasks` | List tasks as listing rows; filter with `lifecycle`, `ball`, `priority`, or `parent` |
| `GET` | `/api/tasks/full` | The same listing as complete records. Expensive; see below |
| `GET` | `/api/tasks/next` | Return the next claimable task; accepts `agent` and `priority` |
| `GET` | `/api/tasks/next/explain` | Why that task is next, and every open task ahead of it |
| `GET` | `/api/tasks/claimable` | Every task that may be worked now, in the queue's order; `/next` is its head. Accepts `agent`, `priority`, `parent` |
| `GET` | `/api/tasks/{task_id}` | Return one task record |
| `GET` | `/api/tasks/{task_id}/detail` | Return the full review/resumption view with relationships |
| `GET` | `/api/tasks/broken` | Report task records that exist but cannot be loaded |
| `GET` | `/api/search?q=...` | Search task id, title, spec, ball prompt and tags. Answers with listing rows |
| `GET` | `/api/search/full?q=...` | The same hits as complete records. Expensive; see below |
| `GET` | `/api/dashboard` | Return dashboard counts and activity |
| `GET` | `/api/attention` | The tasks stopped waiting on a person -- the header's red badge, plus the attention episode driving the Windows taskbar and notification. **Reconciles the episode**, so the answer is idempotent rather than read-only |
| `POST` | `/api/attention/ack` | Record that a person deliberately acted on the episode they were shown. Stops the next interruption being suppressed; does **not** clear the indicator. Takes `episode_id`; a stale one is a no-op. No run may call it |
| `GET` | `/api/push` | The application-server key a browser subscribes against, and every device registered for this project. **Reading it mints the machine's VAPID keypair on first use**, and it answers with device labels, so it needs `push.manage` like the three writes below |
| `POST` | `/api/push/subscribe` | Register a device for mobile push. Takes the browser's own `PushSubscription.toJSON()` plus a `label` and a `detail` mode. Idempotent by endpoint; a device registered mid-episode waits for the next one |
| `POST` | `/api/push/unsubscribe` | Forget a device, by `subscription_id` from the page or by `endpoint` from a service worker. Idempotent |
| `POST` | `/api/push/test` | Push one message on purpose, to one device or to all. Does not consume the episode's interruption |
| `GET` | `/api/model` | Whether this machine can draft a task spec with a model right now, and one sentence saying why not when it cannot. Answers a boolean and a configured model id -- never the credential, a prefix of it, or its length |
| `POST` | `/api/model/draft` | Expand a title and a rough description into spec fields for a form to fill. **Writes nothing**: the person edits the draft and presses create. Needs `model.draft`, which no run holds. A refusal is a 200 with `drafted: false` and a reason from a closed set |
| `GET` | `/api/analytics` | Backlog, throughput, aging and where work is stuck, plus the process series (lifecycle segments, cost per task, finishes, gates, runs, the execution journal, review and questions), each with its own coverage, over one range. `range` is `30d`, `90d`, `12m` or `all`. Includes the landing estimate's own accuracy (`estimates`) and the correction in force (`estimator`) |
| `POST` | `/api/analytics/finish-estimator/reset` | Forget the landing estimate's learned correction (task-586). Deletes nothing: only landings that start afterwards teach it again. Owner only (`dispatch_admin`) |
| `GET` | `/api/revision` | Return the project revision used for client refresh |

The human inbox is `GET /api/tasks?ball=human`; external blockers are
`GET /api/tasks?ball=external`. These are derived from schema-v2 axes, not legacy status
strings.

**`GET /api/tasks` answers with rows, not records.** A row carries the state axes, the
queue position, the label and the dependency facts -- everything a listing draws -- and
leaves out `spec`, `log`, `acceptance`, `deliverables`, `links` and `branches`. Opening a
task fetches those from `GET /api/tasks/{id}/detail`, which every surface that shows a
task already calls.

The reason is that the listing used to carry them: on a 479-task backlog that was
10.4 MB, and it grew with every log entry appended to any task (task-484). The rows are
427 KB. `GET /api/tasks/full` returns the old shape for the two callers that need whole
records -- `TaskClient.list_tasks`, which must return `Task`, and `agentjobs branches`,
which reads `branches[]` off every task. It is the expensive one deliberately, so a new
caller has to ask for it by name.

**`GET /api/search` and `GET /api/dashboard` answer with rows for the same reason**
(task-495). Both returned whole records until then: measured against a generated corpus
of 480, the search was 5.17 MB and the dashboard 5.23 MB -- about 10.8 KB per record each,
against the listing's 785 bytes -- because a search result and a dashboard card are lists,
and a whole record is what they were drawing one from.

*   `GET /api/search` answers with the same rows `GET /api/tasks` does.
    `GET /api/search/full` is the old shape, for `TaskClient.search_tasks`, which parses
    each item into a `Task` and so cannot take a row -- a row parses without complaint
    and with an empty log.
*   `GET /api/dashboard` answers with a *card*: the listing row, plus the one-sentence
    `summary` every card on that page prints under the title, plus `can_brief`. That last
    is one bit rather than the field behind it: the slot board's Dispatch button has to
    know whether pressing it would stop to ask a person for text, the answer is keyed on
    `spec.description`, and the description is the largest field on a record and one no
    card draws. It is computed by the same function the dispatch gate calls. There is no
    `/api/dashboard/full`, because nothing ever wanted one.

## Finish and gate history

| Method | Path | Purpose |
| --- | --- | --- |
| `PUT` | `/api/history/finishes/{finish_id}` | Index a scripted finish and its steps: the `finish` row and every `finish_step` so far. An upsert; the body is the whole record each time |
| `PUT` | `/api/history/gates/{gate_id}` | Index a gate run and its stages: the `gate_run` row and every `gate_stage` so far. An upsert |

Both answer `{written, reason}`. `written: false` is an answer, not an error: `exists`
means an `imported` record met a row already there, and `unknown_task` means a finish
named a task this project does not have. The finisher and `scripts/check.py` are the
writers, over the service rather than the database (task-273); `agentjobs storage
import-finishes` is the one-time import of the directories on disk. Nothing reads these
rows yet -- the series over them are task-473's. See [storage-sqlite.md](storage-sqlite.md#13-finish-and-gate-history-task-472).

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
| `POST` | `/api/tasks/{task_id}/authorization` | Record that a person authorised a dispatch, as the agent they told. `actor` is the agent, `authorized_by` the human. Starts no run; refused for a `run` credential |
| `POST` | `/api/tasks/{task_id}/finish-retry` | Retry a stopped scripted finish on the approval already given, after the agent repaired it (task-575). The server judges the repair against the approved head and starts the finish itself, or hands the task to human/review; `outcome` is `retrying`, `handed_back`, `declined` or `replayed` |
| `POST` | `/api/tasks/{task_id}/redact` | Replace one prose region with a stated redaction, recording that it happened |

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

### Where the records are, and moving them

```
agentjobs storage status                  # per project: database, rows, files
agentjobs storage preview [--project <id>] [--backfill-git]
agentjobs storage import [--project <id>] [--replace] [--no-backfill-git]
agentjobs storage split --project <id>    # out of a shared database into its own
agentjobs storage export <dir> [--project <id>]
agentjobs storage backup [--into <path>]  # snapshot + manifest, verified as it is taken
agentjobs storage verify <snapshot>
agentjobs storage restore <snapshot> [--force]
```

`import` takes a directory of task YAML written by an older AgentJobs, once and in one
direction; `cutover` is its old name and still works. Every one of these opens the
database as the single writer, so `import`, `split` and `restore` refuse while a server
is listening. The sequence, what each step guarantees and the backup-enrolment checkpoint
are in [the storage guide](storage-sqlite.md).

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
| `POST` | `/api/dispatch/arm` | Arm the pull mode with a bound: free slots then fill themselves from this project's queue (task-462) |
| `POST` | `/api/dispatch/disarm` | Stop it starting anything more. Kills nothing |
| `POST` | `/api/tasks/{task_id}/dispatch` | Start an agent on this task |
| `POST` | `/api/tasks/{task_id}/check` | Run this task's executable acceptance checks and answer with what each one did |
| `GET` | `/api/tasks/{task_id}/chains` | Every bounded agent loop this task has had, each with its iteration history |
| `POST` | `/api/tasks/{task_id}/chain` | Authorise a bounded chain of dispatches against this task's checks. Needs `dispatch.start` |
| `POST` | `/api/tasks/{task_id}/chain/revoke` | Stop a chain before its next iteration. An empty body stops whatever is live |
| `GET` | `/api/dispatch/runs` | Runs, live and historical, from the ledger |
| `POST` | `/api/dispatch/runs/{run_id}/cancel` | Cancel one live run |
| `GET` | `/api/dispatch/runs/{run_id}/output` | The run's captured transcript |
| `GET` | `/api/dispatch/runs/{run_id}/tail` | The tail of it, for a live view |
| `GET` | `/api/dispatch/runs/{run_id}/transcript` | The same run as structured entries, for a panel that renders rather than dumps |
| `GET` | `/api/dispatch/finishes/{task_id}` | What a scripted finish is doing to this task's branch, or last did |
| `GET` | `/api/dispatch/finishes/{task_id}/output` | That finish's output in full, as text |

`transcript.log` is a raw TTY capture, so a line appears in it once per terminal
repaint. Link to it and read it; never compute a count from it.

`POST .../check` is in this section because it belongs here: it executes commands out of
the task record on this machine. It walks the same four gates a dispatch walks, so a
project not enabled for dispatch is refused under the gate's own code, and it needs
`dispatch.start` -- which no run holds, so a dispatched agent cannot call it. That is not
a formality. Every run holds `task.edit` against every task, so a run that could call
this could write a `check` onto a task and then have this machine run it.

**A chain is that same act, bought in advance** (task-150). `POST .../chain` records a
human's authorisation of up to N dispatches against one task, bounded by an iteration cap
(default 5, ceiling 20), a wall-clock bound (default 4h, ceiling 12h) and a digest over
the criteria's `(id, check)` pairs as they stand at that moment. The driver re-reads it
from the stored task before every iteration and recomputes the digest, so a check edited
mid-chain stops the chain rather than moving the finish line.

**Nothing about a chain may be sent in the request.** There is no field for the digest,
the chain id or the covered criteria: they are computed here from the record, which is
dispatch design section 2's forgeability rule applied to a loop. What the body carries is
the two bounds and the `user` authorising them, validated as a configured human exactly
as a dispatch's is.

Four refusals, each under its own code: `no_checks` (nothing is machine-decidable, so the
loop has no termination condition it does not control), `already_passing` (there is
nothing to converge on), `chain_already_live`, and `bound_exceeds_ceiling`. Authorising
runs the checks once and records the result as iteration 0, which is the baseline the
regression guard compares against.

`POST .../chain/revoke` takes an empty body and stops whatever is live -- section 9 asks
for a kill switch as blunt as `agentjobs dispatch stop`, and one needing a chain id
copied off a log entry would not be one. It stops no run that is already executing;
cancelling one of those is `POST .../dispatch/runs/{id}/cancel`.
`~/.agentjobs/DISPATCH_DISABLED` stops every chain on the machine at once, because the
sentinel is one of the four gates the driver re-walks before every iteration.
`GET .../chains` is the read behind the task page's panel and executes nothing.
`agentjobs chain authorize | run | revoke | show` are the same four from a terminal.

It answers with a `results` vector -- one entry per criterion that has a `check`, each
with `status` (`met` or `failed`), `exit_code`, `duration_seconds`, an optional `cause`
and a 40-line `output_tail` -- plus `unchecked`, `ok`, and the `entry_id` of the one
`check_result` log entry the pass wrote. A task whose criteria are all prose is refused
`no_checks` rather than answered with an empty success: zero of zero passing is not a
definition of done being met. `agentjobs check <task-id>` is the same pass from a
terminal, and exits non-zero when anything failed. The field itself is described in
[the schema reference](task-schema.md#verify-is-prose-check-is-argv).

Arm and disarm need `dispatch.admin`, which no run holds: an agent cannot arm the
machine to keep starting agents. A bound is required and has no default -- `starts`
with a count, `until` with a moment, or `open` for *until disarmed* -- and both routes
answer with the whole dispatch state, whose `pull` field carries the arming and what
`task_next` says it would start next.

### The routes that are not project-scoped

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runs/live` | Every run happening on this **machine**, in every project, with its remaining capacity |
| `GET` | `/api/recent/closures` | The last few tasks to close on this **machine**, in every project this caller may see (task-460) |
| `GET` | `/api/sessions/idle` | Every Claude Code process on this machine, the idle sweep's verdict on each and why, and its record of stops and switch-overs (task-447) |
| `PUT` | `/api/sessions/idle/settings` | Turn idle-session enforcement on or off, or change `idle_minutes`. Needs `dispatch.admin` |

Every other route on this page is mounted twice -- once at `/api/...` for the default
project and once at `/api/projects/{project_id}/...` -- and answers about that one
project. These are mounted once and have no project-scoped spelling. For runs, because the
resource it describes is not a project's: `limits.max_concurrent_runs` is machine-level,
the run ledger under `~/.agentjobs/runs/` is machine-level, and the run occupying the
last slot is usually on some other project's task. Serving the same body under every
value of `{project_id}` would be a URL asserting a scope the answer does not have
(task-328). For closures, for the same reason one layer up: what landed overnight is
routinely in a different project from the one whose Dashboard is open, which is the
whole value of the region that reads it.

Two of `/api/runs/live`'s fields are worth reading carefully:

- **`health`, not `live`.** `live` means only that nothing has declared the run over. A
  session parked on a permission prompt, a session that has emitted nothing for the
  stall window, and a batch run whose supervising process is gone are all live and none
  of them is working. `health` is `working`, `starting`, `parked`, `silent`, `orphaned`
  or `unknown`, and it is the field a surface should render.
- **`holders` is not `runs`.** A scripted finish and a repository's merge runway hold
  locks rather than run slots, so they are real machine activity with no run record.
  They are listed separately and are deliberately **not** in `occupied`, which counts
  exactly what the concurrency guard counts. The React app nonetheless *shows* a finish
  as running -- in the header readout's green count and as a card on the slot board
  (task-352) -- because a gate running for a task is running, whatever it holds; a
  client of this endpoint should read `runs` plus the `finish` holders as "what is
  running", and `occupied` as "how many slots are taken", which are different questions.

`/api/recent/closures` takes `limit` (default 5, ceiling 20) and `days` (default 7,
ceiling 90), and returns them alongside the rows so a client's empty state can name the
window it was actually given rather than hard-coding one. Three things about it:

- **Finished means the task closed.** A run that ended without closing its task is not
  finished work; that is a run that stopped, and `/api/runs/live` is where it is said.
- **The timestamp is the close, not the last edit.** Rows are ordered by the store's
  `closed_at`, which is stamped once at the close and survives every later write. A
  closed record is edited often -- a correction, a redaction, a late decision entry --
  and each of those moves `updated`, so ordering by it would re-date a month-old
  closure to this morning.
- **Exposure applies per project.** A project this caller may not see contributes no
  rows at all, not a redacted row and not a count: each row carries a task id, a title
  and a project name. See [exposure.md](exposure.md).

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
| `GET` | `/api/version` | Package version, schema version, YAML loader, source root and commit, start time, the API contract digest, the served bundle id, and whether the frontend bundle is present |
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

Two more fields answer the question those cannot: **is the client talking to me the one
I was built for?**

-   **`api_digest`** is a SHA-256 of the OpenAPI document this process serves. The
    TypeScript client is generated from that same document and carries the digest it was
    generated against, so one comparison settles it. Neither `version` nor
    `schema_version` can: on 2026-08-17 both matched exactly on both sides while a
    response field had been added under a long-running process, and the page went blank
    reading an array that was not there. It moves when the contract moves and not when
    anything else does, which is why a commit hash was rejected for the job.
-   **`bundle_id`** identifies the build of the React app being served from disk, or is
    null when there is none. It is derived from the built asset bytes, so unlike the
    digest it also moves for the many rebuilds that touch no API route — which is what
    lets a tab left open across a rebuild notice it is running the previous build.

The app polls this and shows a dismissible banner naming the mismatch and the remedy. It
never blocks: an unreachable, slow or older server leaves the page exactly as it was,
because a detector that breaks the app when it cannot determine skew is worse than none.

## Webhooks

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/webhooks` | List configured webhooks |
| `POST` | `/api/webhooks` | Register one |
| `GET` | `/api/webhooks/{webhook_id}` | Read one |
| `DELETE` | `/api/webhooks/{webhook_id}` | Remove one |
| `POST` | `/api/webhooks/{webhook_id}/test` | Send a test delivery |

See the [webhook guide](webhooks.md) for events, signatures, and payloads.
