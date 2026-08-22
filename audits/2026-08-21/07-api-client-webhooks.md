# 07 — REST API, client, webhooks

Auditor 7 of the Big Dawg Audit (task-242). Read-only pass over `src/agentjobs/api/`,
`client.py`, `webhooks.py`, `scripts/export_openapi.py`, `instrumentation.py`, the
frontend's `check:api` stage, `docs/webhooks.md`, `docs/api-reference.md`, and the
tests that cover them. Live probes were `curl` GETs against the server on 8876
(`source_commit 17fdfd0`, the task-241 merge; every commit since is task YAML, so the
running code is current — examined, nothing found).

Line numbers are against `main` at `2b6c89f`.

---

## Findings

### P1 — `POST /api/webhooks/{id}/test` always returns 500

**Evidence.** The route is `async def` (`src/agentjobs/api/routes/webhooks.py:78-91`)
and calls `webhook_manager.test_webhook(webhook_id)` synchronously on the event-loop
thread. That method ends with `asyncio.run(self._dispatch(...))`
(`src/agentjobs/webhooks.py:198`). `asyncio.run` refuses to start inside a running
loop. Reproduced outside the server with the manager alone:

```
$ poetry run python probe_webhook_test.py
sync call (no loop):
  ok, returned (delivery failure is only logged)
call from inside a running event loop (how the async route calls it):
  RAISED RuntimeError : asyncio.run() cannot be called from a running event loop
```

The route catches only `ValueError` (`routes/webhooks.py:87`), so the `RuntimeError`
escapes as an unhandled 500 with no JSON body. The coroutine is created and never
awaited (Python warns `coroutine 'WebhookManager._dispatch' was never awaited`), so
no HTTP request is ever made either.

**Why nothing caught it.** No test touches any webhook route: grepping `tests/*.py`
for `api/webhooks` or `/webhooks` returns nothing. `tests/test_webhooks.py` tests the
storage and manager classes only, and its one manager-level test ends in
`assert True` (`tests/test_webhooks.py:146`). `docs/webhooks.md:78` and
`docs/api-reference.md:137` both advertise the endpoint.

**Fix.** Make `test_webhook` return the coroutine (or take an `await`able path) and
`await` it in the route; or route it through `_schedule` like `fire_event` does and
return 202. Add one `TestClient` test per webhook route — they are five routes and
currently have zero.

---

### P2 — A replayed `operation_id` re-fires the webhook

**Evidence.** `TaskManager.handoff` calls `self._mutate(task_id, apply)` and then,
unconditionally, `self._fire("task.handoff", task, {...})`
(`src/agentjobs/manager.py:1292-1302`). `close_task` does the same at `:1435-1440`, and
`add_log_entry` fires `task.question` at `:2017-2018`. On replay, `replay_or_conflict`
returns `True` (`src/agentjobs/operations.py:114-131`), `apply` returns `None`, and
`TaskStorage.mutate_task` returns the current task **without writing**
(`src/agentjobs/storage.py:462-463`: "The mutator may return None to mean leave it
alone"). Control then returns to the verb, which fires anyway.

So the documented contract — "reusing an operation_id replays the original result
instead of writing twice" (MCP instructions; `routes/status.py:8-12`) — holds for the
task file and not for the side effect. A client that retries a handoff after a timeout
produces one log entry and **two** signed `task.handoff` deliveries, byte-identical
except for `timestamp`. No test covers it: `tests/test_idempotency.py` contains neither
`webhook` nor `fire`.

**Fix.** Have the verbs fire only when the mutator actually wrote. The cheapest signal
already exists — `_run` in `routes/status.py:237-252` detects replay by comparing log
length before and after. Move that comparison into the manager (`_mutate` can return
`(task, written)`), and gate `_fire` on it. Then add the test this finding is missing.

---

### P2 — Webhook secrets are returned by the API and declared in the schema

**Evidence.** `Webhook.secret` is a plain field (`src/agentjobs/webhooks.py:33`), the
model is the `response_model` for list, get and create
(`routes/webhooks.py:26,34,49`), and `frontend/openapi.json` carries it:

```
Webhook schema props: ['active', 'created', 'events', 'id', 'last_triggered', 'secret', 'url']
```

`GET /api/projects/agentjobs/webhooks` returned `[]` on the live server (2.5 ms, zero
parses), so nothing is exposed today — but the moment a subscription exists, every
reader of the API (auditor 12 will say who that is; `docs/api-reference.md:10-12`
says the API has no authentication) gets the HMAC key, which is all a receiver trusts.
The secret is also stored in cleartext in `webhooks.yaml` (`webhooks.py:65-68`), which
is a separate question for auditor 12.

**Fix.** A `WebhookRead` response model without `secret` (or with a fingerprint of it),
used by all three routes. Keep `Webhook` as the storage model.

---

### P2 — `openapi.json` documents an error contract the app does not have

**Evidence** (from parsing `frontend/openapi.json`):

```
paths: 88   operations: 99
response codes: {'200': 91, '422': 90, '201': 6, '202': 2}
ops documenting any non-2xx other than 422: 0
'ErrorBody' in components: False
```

Three things are wrong at once:

1. **The 422 on 90 operations never happens for validation.** `main.py:258-276`
   registers a `RequestValidationError` handler that returns **400** with
   `{"detail": "<first error only>"}`. `tests/test_api.py:107-115` asserts the 400. The
   only real 422 is the `TaskLoadError` handler (`main.py:235-245`), whose body is
   `{"detail", "broken"}` — not `HTTPValidationError` either.
2. **The 400/404/409 that do happen are documented nowhere**, and the structured
   `ErrorBody` (`api/models.py:614-633`) — the one thing an agent is meant to branch
   on — is absent from `components`. The generated TypeScript client therefore types
   every failure as `HTTPValidationError`.
3. The frontend already knows and works around it by hand:
   `frontend/src/api/mutation-error.ts:1-14` — *"the OpenAPI schema declares only 422
   for these operations, so every 409 arrives typed as a validation error it is not.
   Regenerating the client would not fix that, because the omission is in the schema."*
   That comment is a finding someone filed in a code comment instead of a task.

Smaller honesty defects in the same document:

- `DELETE /api/webhooks/{id}` is documented as `200` and returns `204`
  (`routes/webhooks.py:63-75`; `DELETE webhook responses: ['200', '422']`).
- `GET /api/all/tasks` is `List[Dict[str, Any]]` (`routes/projects.py:274`) — the
  real shape is `{project_id, project_name, task: Task}` and is known.
- `POST .../webhooks/{id}/test` is `Dict[str, Any]`; the real shape is
  `{status, message}` (`routes/webhooks.py:86`).
- `GET /api/tasks/{id}/attachments/{filename}` has an empty `{}` schema; it returns
  bytes with a media type (`routes/tasks.py:377-382`).

**Fix.** Declare `responses={400: ErrorBody, 404: ErrorBody, 409: ErrorBody}` on the
mutation routers (FastAPI `APIRouter(responses=...)` does it once per router), drop
the default 422 (`app = FastAPI(..., responses=...)` or an `openapi()` override), put
`status_code=204` on the webhook delete, and give the two `Dict[str, Any]` routes
models. Regenerate; `mutation-error.ts` can then narrow against a real type.

---

### P2 — Every error from the seven human-action routes is a 404

**Evidence.** `/approve`, `/request-changes`, `/answer`, `/redirect`, `/hold`,
`/resume` and `/reject` all wrap the manager call in `except ValueError` →
`HTTP_404_NOT_FOUND` (`routes/tasks.py:699-703, 737-741, 782-783, 910-914, 935-939`).
The manager raises `ValueError` for far more than "not found":

- `handoff` on a closed task: `"Task 'x' is closed; the ball cannot move."`
  (`manager.py:1277`) → **404**, the task exists.
- `close_task` on a closed task via `/reject`: `"already closed"` (`manager.py:1411`)
  → **404**.
- `stamp()` refusing a reserved data key (`operations.py:137-141`) → 404.

The same failures through `/handoff` and `/close` go through `_classify`
(`routes/status.py:117-160`) and come back as `409 invalid_transition` with a
`suggested_action`. A GUI or MCP client that treats 404 as "task gone" — the
`task_not_found` code's own `suggested_action` says "list the project's tasks" —
is sent to look for a task that is sitting right there, closed.

**Fix.** Route the human actions through `_run`/`_classify` like the verbs they
compose (`approve` is `manager.handoff`; `reject` is `manager.close_task`). That also
gives them `ErrorBody`, `operation_id` and `expected_revision`, which they lack today.

---

### P2 — Three error envelopes plus a bare 500, with no rule for which route gets which

**Evidence.** The shapes actually emitted:

| Shape | Emitted by |
| --- | --- |
| `{"detail": str}` | every `HTTPException`: task reads 404 (`tasks.py:388-393`), `POST /tasks` 400/409 (`tasks.py:424-432` — an `OperationConflictError` here has no `code`), `PATCH` 400/404/409 (`tasks.py:462-473`), `DELETE` 404, deliverables 404, `queue_broken` 409 (`tasks.py:175-192`), all webhook 404s, dispatch cancel 409 (`dispatch.py:471-472`), project resolution 404/409 (`dependencies.py:136,171-178`), `ProjectError` 400 and validation 400 (`main.py:221-276`) |
| `ErrorBody` (`code`, `message`, `detail`, `retryable`, `task_id`, `current_task`, `field_errors`, `suggested_action`) | the nine verbs in `status.py`, `/dispatch`, dispatch enable/disable (`dispatch.py:365-380` — its docstring says "so the body has the same shape every other refusal in this API has", which is not true of the first row) |
| `{"detail": str, "broken": {...}}` 422 | `TaskLoadError` (`main.py:235-245`) |
| no JSON, 500 | anything unhandled — P1 above; also `QueueCorruptionError` on any route other than `/next` and `/next/explain` (only `tasks.py:207,225` catch it) |

The same condition lands in different rows: "not found" is `{"detail"}` from `GET`
and `ErrorBody task_not_found` from a verb; "operation_id reused with a different
payload" is `ErrorBody operation_conflict` from a verb and `{"detail"}` from
`POST /tasks`/`PATCH`. `readRefusal()` in the frontend returns `null` for the first
row, so the UI loses the message for every route in it.

**Fix.** One exception handler that renders `HTTPException` into `ErrorBody` (with
`code` derived from the status and `detail` preserved) makes row 1 a subset of row 2
without touching a route. The Python client already reads either
(`client.py:69-117, 707-714`).

---

### P3 — `GET /api/projects` parses every task file in every project to print a count

**Evidence.** `routes/projects.py:186-200`: `count = len(storage_for(project).list_tasks())`
per project. Live:

```
== /api/projects            x-response-time-ms: 569.6   x-task-parses: 309
== /api/projects/agentjobs/revision   x-response-time-ms: 49.0   x-task-parses: 0
```

`TaskStorage.project_revision()` (`storage.py:205`) already returns `(revision,
task_count)` with zero parses — the `/revision` route and its budget test
(`tests/test_performance_budgets.py:158-165`) prove it. The frontend calls `/api/projects`
from three components (`App.tsx:55,146,483`); `staleTime` keeps it to roughly one call
per load, but that one call is the slowest request on the app's critical path, 10×
the task list's cost for less information.

**Fix.** `count = storage_for(project).project_revision()[1]`. Add `/api/projects` to
the parse-budget parametrize with a budget of 0.

---

### P3 — The Python client's default author is an actor the server refuses

**Evidence.** `TaskClient.add_progress_update(..., agent="")` sends
`"author": agent or "system"` (`client.py:570-577`). The route validates it through
`acting_actor` (`routes/status.py:491`) → `validate_actor`
(`actors.py:194-215`), which accepts only configured ids plus `RESERVED`, and
`RESERVED` is `{dispatcher, finisher}` (`actors.py:175-178`) — no `system`. On any
project with an `actors:` block, the client's default is a guaranteed
`400 unknown_actor`. `tests/test_client.py:142` passes because it mocks the transport.

**Fix.** Make `agent` required (it is `actor` everywhere else in the client — the
parameter name is also the odd one out), or drop the method: `add_log_entry` covers it.

---

### P3 — Three write routes attribute to `"system"` with no actor validation at all

**Evidence.**
- `PATCH /tasks/{id}` takes `actor` as a bare query parameter and passes it straight
  to the manager (`routes/tasks.py:439-442,459`) — never through `acting_actor`.
  The manager writes `actor or "system"` (`manager.py:976,998`).
- `DELETE /tasks/{id}` takes no actor; `archive_task` writes `author or "system"`
  (`manager.py:1445`), closes an open task as `cancelled` and fires `task.closed`
  with `triggered_by: "system"`.
- `PATCH /tasks/{id}/deliverables/{path}` takes no actor (`routes/tasks.py:488-501`).

`routes/status.py:182-195` says why the verbs validate: "a typo ... wrote an
unresolvable attribution into an append-only log." These three write the same log
and skip the check. D2 (`routes/tasks.py:96-101`) is enforced at one edge and
constructible through another — the pattern auditor 3 was told to look for.

**Fix.** Move `actor` into the `PATCH` body and validate it; give `DELETE` and
deliverables a request body with `actor`; validate all three with `acting_actor`.

---

### P3 — Webhook delivery: no retry, no ordering, dropped task references, unlocked bookkeeping

**Evidence.**
- `_dispatch` makes one attempt with a 10 s timeout and logs a warning on any
  exception (`webhooks.py:228-237`). `docs/webhooks.md:81-82` says failures "are
  logged and do not block the state-changing request" — true, and the whole of the
  delivery guarantee.
- `_schedule` calls `loop.create_task(coro)` and discards the handle
  (`webhooks.py:214`). The asyncio docs are explicit that a task with no strong
  reference can be garbage-collected mid-flight. Several deliveries for one event
  run concurrently, so two handoffs seconds apart can arrive out of order.
- On success, `record_trigger()` + `save_webhook()` does a read-modify-write of
  `webhooks.yaml` (`webhooks.py:239-240, 92-98`) with no lock, from a background
  task, while a request thread may be creating or deleting a subscription.
  `_write_webhooks` is a plain `write_text` (`webhooks.py:65-68`) — no temp-file
  rename, unlike task files. Two concurrent deliveries can lose one `last_triggered`;
  a crash mid-write truncates the subscription list.

**Fix.** Hold task references in a set on the manager; serialise bookkeeping writes
through the same atomic-write helper task storage uses; either document "at most
once, no order" in `docs/webhooks.md` or add a bounded retry. A delivery id header
(see next finding) is the prerequisite for any receiver-side dedup.

---

### P3 — Signature scheme has no delivery id and no receiver guidance on replay

**Evidence.** HMAC-SHA256 over the compact, key-sorted JSON body, hex digest in
`X-Hub-Signature-256: sha256=...` (`webhooks.py:176-179, 223-226, 257-264`). The body
carries a `timestamp` (`webhooks.py:249-253`), so a replay window *is* signed — but
`docs/webhooks.md:50-57` tells receivers only to `compare_digest` the body and never to
check the timestamp, and there is no delivery/event id at all. Combined with the
re-fire on replay (P2 above), a receiver today cannot tell a legitimate second handoff
from a duplicate delivery of the first. The server side has nothing to compare
timing-safely (it only signs), so "timing-safe compare" is a receiver question and the
doc gets that part right.

**Fix.** Add `delivery_id` (uuid) to the payload and as a header; tell receivers to
reject timestamps older than N minutes and to dedup on `delivery_id`.

---

### P3 — `docs/webhooks.md` overstates when `task.handoff` fires, and `events` is unvalidated

**Evidence.** The events table says `task.handoff` fires when "the ball moves to an
agent, human, or external dependency" (`docs/webhooks.md:11`). Only the `handoff`
verb fires it (`manager.py:1293`). `claim_task` (ball → agent/work, `manager.py:1224-1226`),
`release_task` (ball → agent/available, `:1327-1329`) and `promote_task` (`:1371-1373`)
all move the ball and fire nothing. A subscriber waiting for "work became claimable"
never hears about a release or a promote.

`WebhookCreateRequest.events` is `List[str]` with no check against the four known
names (`routes/webhooks.py:30`; `fire_event` matches by string, `webhooks.py:171`).
Subscribing to the retired `task.status_changed` — the name `docs/webhooks.md:17-19`
tells you was retired — succeeds with 201 and never fires.

**Fix.** Reword the table (or fire on every ball move — a product decision; the
doc and code must agree either way). Validate `events` against an enum and 400 on
unknowns.

---

### P3 — Python client covers 20 of 99 operations; the CLI does not use it at all

**Evidence.** `grep -c TaskClient src/agentjobs/cli.py` → `0`; the CLI drives the
manager directly. The client's only consumer is the MCP server
(`src/agentjobs/mcp/*.py`). Routes with no client method: all five webhook routes,
all eight dispatch routes plus `POST /tasks/{id}/dispatch`, all seven human actions,
`/dashboard`, `/revision`, `/all/tasks`, `POST /projects`, `/projects/init`,
`/projects/inspect`, attachments, `DELETE /tasks/{id}`. The one drift guard is
`tests/test_mcp_routing.py:126` (ProjectSummary ⇔ ProjectResponse field sets); nothing
checks any other client method against `openapi.json`.

Errors are surfaced, not swallowed: `_request` raises `TaskClientError` with status,
`detail` and the parsed body for any non-2xx or transport failure
(`client.py:692-705`), and exposes `code`/`retryable`/`suggested_action`
(`:90-117`). Examined, nothing found there.

**Fix.** Either state in the client's docstring that it is the MCP transport and not a
general SDK (and stop documenting it as one in `docs/api-reference.md:117-133`), or
add a contract test that walks `openapi.json` and asserts each `operationId` has a
client method or is on an explicit exclusion list.

---

### P3 — `docs/api-reference.md` drift (overlap with auditor 2; recorded here because it is the API's own doc)

**Evidence.**
- Verbs table (`:56-63`) omits `/promote`; `/dispatch` and attachments appear nowhere
  in the file.
- `:65` names `/approve`, `/request-changes`, `/reject`; `/answer`, `/redirect`,
  `/hold`, `/resume` exist (`routes/tasks.py:789-914`).
- `:48` "Archive the task through the storage policy" — it closes an open task as
  `cancelled` first (`routes/tasks.py:478`), which is a state change the sentence hides.
- `:10-12` "AgentJobs has no authentication" — accurate, and the most important line
  in the file; it should be the first.

---

### P3 — Test decoration

Each of these passes while proving nothing the brief asked about:

- `tests/test_webhooks.py:113-146` — builds a subscription, hands off, `assert True`.
  Would catch: an exception in the fire path. Would not catch: wrong event name,
  wrong payload, wrong signature, duplicate delivery, the P1.
- `tests/test_openapi_contract.py` — one test, that scoped routes declare
  `project_id`. Would not catch any of the P2 schema findings.
- `tests/test_client.py:142` (`add_progress_update`) — asserts the request body
  contains `"author": "system"`, i.e. it pins the defect in the P3 above.

---

### P4 — Instrumentation: examined, accurate, enforced in six places

- `X-Task-Parses` counts at the disk read, in a `finally` (`storage.py:309-312`), so a
  snapshot hit inside `corpus_snapshot()` (`main.py:194`) is not counted. Live check:
  `GET /tasks` → 240 parses, `ls tasks/agentjobs/*.yaml | wc -l` → 240. Accurate.
- `X-Response-Time-Ms` wraps `call_next` inside the `@app.middleware` (`main.py:189-197`);
  CORS is added afterwards (`:202`) and so sits outside it. The figure excludes
  uvicorn parse/send and CORS — fine, and the docstring says "inside the application".
- Enforcement: `tests/test_performance_budgets.py:128-141` budgets six routes at
  "never twice", `/revision` at zero. Not budgeted: `/api/projects` (309 above),
  `/queue`, `/all/tasks`. `scripts/bench.py:390` reads the header for reports only.

### P4 — Small things

- `export_openapi.py --check` (`:126-132`) compares `read_text()` (universal newlines)
  to an LF string, so a CRLF `openapi.json` on disk passes the check while differing
  byte-for-byte from what a write would produce. Harmless until `core.autocrlf`
  flips on a Windows clone.
- `check-generated-client.mjs:104-112` regenerates with whatever `@hey-api/openapi-ts`
  is in `node_modules`. A `package-lock.json` bump without `npm ci` produces a client
  the gate blesses and CI regenerates differently. `bootstrap.py` runs `npm ci`, which
  is why this has not bitten.
- CORS `allow_origins` lists 8765 and 5173 only (`main.py:205-211`); the real
  deployment is 8876 behind a tsnet proxy. Moot while the SPA is same-origin; it
  becomes a silent header-hiding bug the day anything cross-origin reads the
  measurement headers.
- `handle_validation_error` reports only the first of N field errors
  (`main.py:262-270`). `ErrorBody.field_errors` exists for exactly this and is unused
  here.
- `RequestValidationError` strips `body`/`query` from the loc but not `path`, so a
  bad path parameter reads `path.task_id: ...`.

---

## Generated-client freshness: what the `api` stage proves

`check:api` = `check:api-schema` + `check:api-client` (`frontend/package.json:14-16`).

- `check:api-schema` imports the app, renders `app.openapi()` with `sort_keys=True`,
  and string-compares it to `frontend/openapi.json` (`export_openapi.py:110-132`).
  Proves: **app ⇔ committed schema**, against the working tree.
- `check:api-client` snapshots `src/api/generated`, runs `openapi-ts` against
  `openapi.json`, and byte-compares (`check-generated-client.mjs:125-138`). Proves:
  **committed schema ⇔ generated client**, against the working tree. An uncommitted
  but matching client is a note, not a failure (`:135-136`), by design (task-189).

Together that is a complete chain app → schema → TS client. What it cannot see:

1. **Truthfulness of the schema.** Both checks verify the document is *what FastAPI
   emits*, not that FastAPI emits the truth — every P2 above under "openapi.json
   honesty" passes this stage green.
2. **The Python client.** Hand-written, never regenerated, one field-set contract
   test. Drift there is invisible to the gate.
3. **Callers of the generated client.** Covered by `tsc` in the `build` stage, not
   here — correct, but worth saying so nobody assumes `api` green means the UI compiles.

---

## Parity tables

### Manager verb → route

| Manager | Route | Actor validated | `operation_id` | `ErrorBody` |
| --- | --- | --- | --- | --- |
| `create_task` | `POST /tasks` | yes (if supplied) | yes | **no** |
| `update_task` | `PATCH /tasks/{id}` | **no** | yes | **no** |
| `mark_deliverable_complete` | `PATCH /tasks/{id}/deliverables/{path}` | **none** | no | no |
| `archive_task` | `DELETE /tasks/{id}` | **none** | no | no |
| `promote_task` | `POST .../promote` | yes | yes | yes |
| `claim_task` | `POST .../claim` | yes | yes | yes |
| `handoff` | `POST .../handoff` + 6 human actions | yes / `acting_user` | yes / **no** | yes / **no** |
| `release_task` | `POST .../release` | yes | yes | yes |
| `close_task` | `POST .../close` + `/reject` | yes / `acting_user` | yes / **no** | yes / **no** |
| `move` | `POST .../queue-move` | yes | required | yes |
| `reprioritize` | `POST .../reprioritize` | yes | required | yes |
| `compact_band` | `POST /queue/compact` | yes | required | via `lock_timeout` only |
| `repair_queue` | `POST /queue/repair` | yes | required | via `lock_timeout` only |
| `add_log_entry` | `POST .../log` | yes | yes | yes |
| `add_progress_update` | `POST .../progress` | yes | yes | yes |
| `delete_task`, `apply_position`, `rebalance_band` | none | — | — | deliberate (no generic setter) |
| `check_queue` | folded into `GET /queue.problems` | — | — | — |
| `record_dispatch*` | internal to dispatch | — | — | — |

Routes with no verb behind them are registry (`/projects*`), ledger (`/dispatch*`),
and webhooks — all correctly outside the manager. Every task-facing router is mounted
twice (`main.py:303-309`); the handlers are shared, so the two mounts cannot drift.

### Event inventory (what fires, what it carries)

| Event | Fired by | Metadata merged into the payload |
| --- | --- | --- |
| `task.handoff` | `manager.handoff` only | `triggered_by, ball, ball_reason, ball_prompt` |
| `task.closed` | `manager.close_task` (incl. `/reject`, `DELETE` archive) | `triggered_by, outcome` |
| `task.question` | `add_log_entry(type=question)` | `triggered_by, body` |
| `webhook.test` | the broken test route | `task: {}, triggered_by: system, action: test` |

Every task event carries `task.model_dump(mode="json")` — the entire record, log
and all (`webhooks.py:249-253`). `docs/webhooks.md:20-43` describes this accurately
for `task.handoff`, and does not list the metadata for the other two.

---

## What I did not get to

- **`api/routes/web.py`** (legacy Jinja, 374 lines) and `spa.py` beyond confirming
  they are `include_in_schema=False`. Not read for correctness.
- **`api/models.py` field-by-field** against `models_v2.Task` — `TaskRead` is
  `Task` plus computed fields and is *not* in `components` (only `Task` is); whether
  the generated TS types therefore lack `actionable`/`unmet_needs`/`open_children_count`
  I did not verify.
- **Which `QueueCorruptionError` paths reach routes other than `/next`**: I asserted it
  surfaces as a 500 elsewhere from the code comment at `tasks.py:178-181`; I did not
  trace `list_tasks()` to confirm it can raise.
- **`expected_revision` round-trip** through the TS client (string vs datetime) —
  `check_revision` (`operations.py:147-175`) parses ISO strings; I did not confirm the
  frontend sends `updated` unmodified.
- **Dispatch routes' error bodies** beyond `_refusal_error` and `cancel` — the
  `/dispatch` endpoint's 20-row status map was read, not exercised.
- **Concurrency of `_webhook_manager_for`'s `lru_cache`** with `reset_dependency_cache` —
  noted, not analysed.
- I did not `POST` anything to the live server, so every mutation-path claim rests on
  code reading plus the one out-of-process reproduction for the P1.

## Questions for other auditors

- **12 (security):** with no API auth (`api-reference.md:10`), `GET /api/webhooks`
  returning `secret`, `POST /api/webhooks` accepting any `HttpUrl` (localhost, link-local
  metadata, a tailnet peer — no SSRF check anywhere in `webhooks.py`), and every
  delivery carrying the full task record including log bodies and attachment paths:
  what can a tailnet peer do with the webhook surface alone? Also: `webhooks.yaml`
  cleartext at rest.
- **4 (storage/manager):** the replay-refires-webhook defect is yours from the other
  side ("replay must not re-execute side effects"). Does `_mutate` have any other
  post-write side effect outside the mutator? `_store_attachments` runs *inside*
  `apply`, so it is safe on replay — is `maybe_auto_dispatch` (`tasks.py:616`) gated
  on a real write, or does an approve replay start a run?
- **8 (MCP):** `mutation_tools.py:272` translates `TaskClientError` by `code`. Routes
  in the `{"detail"}` row above have no `code` — what does an MCP caller see for a
  `POST /tasks` operation conflict or a 404 from a read?
- **9 (frontend):** `readRefusal` returns `null` for every `{"detail"}`-shaped error.
  Which user-visible actions lose their error message as a result? (`archive`, task
  create on a bad field, anything from the human-action routes.)
- **2 (docs):** the `api-reference.md` drift above; also `docs/performance.md:59`
  documents the headers — it is accurate.
- **10 (dispatch):** `serving_api_base` uses `scope["server"]` (`status.py:567-580`);
  behind the tsnet proxy that is the loopback socket, which is what a local runner
  needs — confirm a remote runner would be handed an unreachable address.
- **11 (gate):** `check:api-schema` imports `agentjobs.api.main` under `poetry run
  python` from `frontend/` — in a worktree, is that the worktree's interpreter after
  task-210's `VIRTUAL_ENV` disowning, or does the check silently export the main
  clone's schema?
