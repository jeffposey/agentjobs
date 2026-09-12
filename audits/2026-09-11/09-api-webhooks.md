# 09 — REST API, client, webhooks, OpenAPI

Big Dawg Audit II, night of 2026-09-11. Auditor 09. Main clone on `main` at `096f33ae`.

**How this was done.** A throwaway server on `127.0.0.1:8909` with its own `AGENTJOBS_HOME`
(a temp directory, project `auditbox`, five seeded tasks), a webhook receiver on
`127.0.0.1:8910` that logs every delivery to a file, and a probe script driving both over
HTTP with `httpx`. Every "verified by running" below is a request made against that server
between 22:54 and 22:58 Central on 2026-09-11, plus one `pytest` run at 11:28 Central on
2026-09-12 after the session resumed. The live store on 8876 was never touched; the only
read of the live database was a read-only `sqlite3` connection to search the backlog.

**Incident to own.** While confirming finding 10 I started the repository's own
`scripts/version_skew_sandbox.py` on port 8911, which was not my port. Something else was
already listening there (a review-panel sandbox, `sandbox-panel`, belonging to another
session), my readiness check answered against *that* server, and my cleanup killed its
process (pid 345132) at about 22:56 Central. A new listener appeared on 8911 later, so the
owner may have restarted it. I am sorry for the disruption; the synthesis session should
treat any gap in that auditor's sandbox evidence around 22:56 as my doing.

**Backlog read first.** No `agentjobs search` command exists (see finding 16); the backlog
was searched read-only in SQLite. Records that bear on this system: task-250, task-255,
task-256, task-257, task-259, task-263, task-265, task-274 (all `draft`, all from the
first audit, none worked since 2026-08-22), task-372 (`ready`). Each finding below is
marked New / Confirms / Refutes against them.

---

## 1. A dispatched run can read every webhook secret back — P1, New

**Verified by running.** `tests/test_run_authorization.py:222-231` proves a run is refused
`POST /api/webhooks` with `capability_denied`, and its docstring says why: *whoever can
read a webhook back holds the HMAC every receiver trusts.* But only `create_webhook`,
`delete_webhook` and `test_webhook` are in `ROUTE_CAPABILITIES`
(`src/agentjobs/api/authorization.py:121-123`); `list_webhooks` and `get_webhook` are not,
and `Webhook.secret` is a plain field in the response model (`src/agentjobs/webhooks.py:33`,
`src/agentjobs/api/routes/webhooks.py:26,34`). So the read is open to exactly the
principal the write is closed to.

Reproduction: a test file using the repository's own helpers (`owner()` creates a webhook
with secret `the-hmac-key`; `dispatched()` mints a real run credential), then as the run:

```
RUN GET /api/webhooks      -> 200 [{"id":"wh_5a1b341cc9", ..., "secret":"the-hmac-key", ...}]
RUN GET /api/webhooks/{id} -> 200 {"id":"wh_5a1b341cc9", ..., "secret":"the-hmac-key", ...}
1 passed in 1.01s
```

`docs/authorization.md:47` lists `webhook.admin` as "create / delete / test" and calls the
table "complete by construction"; the two reads are simply absent from it. Task-332's
safety argument ("a run may not act on a task that is not its own") does not cover a run
learning the key that lets it forge a `task.handoff` for any task to any receiver.

**Fix.** Either gate `list_webhooks`/`get_webhook` under `WEBHOOK_ADMIN` too, or — better,
and what task-257 already asks for — stop returning the secret from any route
(`WebhookRead` without `secret`, or a fingerprint). Add the read half to
`test_it_cannot_touch_webhooks`, which currently asserts the docstring's premise for the
write only.

## 2. `POST /api/webhooks/{id}/test` is a 500 that delivers nothing — P1, Confirms task-250

**Verified by running.**

```
POST /api/projects/auditbox/webhooks/wh_c68ea827ea/test -> 500 text/plain "Internal Server Error"
deliveries after test route: 0
```

Server log: `RuntimeWarning: coroutine 'WebhookManager._dispatch' was never awaited`. The
cause is unchanged since August: the async route calls `webhook_manager.test_webhook`
(`routes/webhooks.py:85`), which ends in `asyncio.run(...)` inside the running loop
(`webhooks.py:198`), and the route catches only `ValueError` (`:87`). Zero tests touch any
of the five webhook routes (`grep -rl webhook tests/` finds only `test_webhooks.py`,
`test_model_timestamps.py`, `test_run_authorization.py`), and `tests/test_webhooks.py:144`
still ends in `assert True`.

**Fix.** As task-250 says: have `test_webhook` return the coroutine and `await` it in the
route, or route it through `_schedule` and return 202. One `TestClient` test per route.
Task-250 is accurate today and can be promoted as written.

## 3. Secrets out of the API, cleartext on disk, no target validation — P2, Confirms task-257

**Verified by running.** Create, list and get all return `secret` (finding 1 shows the
same bodies). On disk, `<project>/.agentjobs/webhooks.yaml` holds `secret: s3cret-value`
in cleartext. `url` accepts a link-local target:

```
POST /webhooks {"url":"http://169.254.169.254/latest/meta-data",...} -> 201
POST /webhooks {"url":"ftp://example.com/x",...}                    -> 400 "url: URL scheme should be 'http' or 'https'"
POST /webhooks {"events":[],"secret":"",...}                        -> 201
POST /webhooks {"events":["task.handoff","bogus.event"],...}       -> 201
```

An empty secret is accepted, so a receiver can be "signed" with an empty HMAC key; an empty
or unknown event list is accepted, so a subscription can be created that never fires and
nothing says so. The signature itself is correct (HMAC-SHA256 over the exact body,
`webhooks.py:257-264`; the server signs, so timing-safe comparison is the receiver's job
and `docs/webhooks.md:62-70` correctly tells them `hmac.compare_digest`). There is still no
delivery id and no timestamp-freshness advice, so a captured delivery replays forever.

**Fix.** Task-257's list stands, plus: `min_length=1` on `secret`, validate `events`
against the four known names, refuse non-global targets by default.

## 4. Replay re-fires the webhook, and the storage doc says it cannot — P2, Confirms task-255 (1); New for the doc

**Verified by running.** Same `operation_id` sent twice to `/handoff?envelope=true`:

```
B1 first  -> 200  replayed: False  log len 3
B2 retry  -> 200  replayed: True   log len 3
deliveries total: 2   (both task.handoff, byte-identical except timestamp, different signatures)
```

The verb writes once and fires twice, because `handoff` calls `_mutate` then `_fire`
unconditionally (`src/agentjobs/manager.py:1531-1540`), and `_fire` is fire-and-forget
(`:2814-2816`). This is on today's SQLite store, so task-255's finding survived the
storage migration.

**The new part.** `docs/storage-sqlite.md:268-271` states: *"Webhooks go through
`webhook_outbox`, written in the same transaction as the state change. A replayed operation
short-circuits before the transaction body and so enqueues nothing (audit F3), and a crash
between commit and send no longer loses the notification (task-047)."*
`src/agentjobs/sqlstore/connection.py:158,167` repeats "state, history and the outbox row
commit together." None of it is implemented: the only references to `webhook_outbox` in
`src/` are the importer's table list (`sqlstore/importer.py:274,295`), and after seven
deliveries the sandbox database reports `webhook_outbox rows: 0`. The live database has
the same table and 0 rows. Delivery is still the in-memory `WebhookManager` reading a YAML
file per event (`webhooks.py:171`), scheduled on the event loop with the task handle
discarded (`:214`), one attempt, no retry (`:227-237`).

So the document describing the storage that shipped 2026-09-07 records task-255's fix
and task-047's durability guarantee as done, while task-255 is still a draft and the
outbox is an empty table. A reader building a notification service on that paragraph
gets duplicate pushes on every retried handoff and a lost push on every crash.

**Fix.** Either implement the outbox (write the row inside `mutate_task`'s transaction,
drain it from the poller) or correct §6 to say what is true today and point at task-255.
Until then, `_mutate` returning `(task, wrote)` and firing only on `wrote` is the five-line
version.

## 5. Webhook delivery runs inside the mutating request — P2, New

**Verified by running.** `docs/webhooks.md:93-94`: *"Delivery failures are logged and do
not block the state-changing request."* Handoffs against a receiver that sleeps two
seconds before answering, and one that refuses the connection:

| handoff to task-003 | client ms | `X-Response-Time-Ms` |
|---|---|---|
| no matching subscription | 242 | 237.9 |
| one receiver, sleeps 2 s | 457 | 455.6 |
| two receivers, sleep 2 s each | 670 | 668.0 |
| one receiver refusing connections | 470 | 468.4 |

Each subscribed receiver adds about 215 ms to the caller's response, whether it is slow or
dead. The route is `async def` and calls the synchronous manager; `_schedule` puts the
delivery coroutine on the loop (`webhooks.py:214`), and it runs — client construction,
connect, request send — before the response is written, yielding only once it is waiting
on the receiver. The response is not held for the receiver's full latency, so the doc is
half right, but a subscriber on a slow network is a tax on every handoff, close and
question, and `ENGINEERING.md` names `task.handoff` as the notification extension point.

Also worth the storage auditor's eye: a handoff with **no** matching subscription costs
240 ms and a claim costs 11 ms on the same store (`X-Response-Time-Ms` 237.9 vs 11.6). That
is not webhooks; I did not find what it is.

**Fix.** Deliver from a worker (the outbox drain from finding 4 is the right shape), or at
minimum `asyncio.create_task` after the response via a background task. Keep the delivery
handles in a set so they cannot be collected mid-flight.

## 6. The error contract is still three shapes, and OpenAPI declares none of them — P2, Confirms task-256; Refutes one sentence of task-255

**Verified by running.** Same failure class, different envelope by route:

```
GET  /tasks/task-999                    -> 404 {"detail":"Task task-999 not found"}
POST /tasks/task-999/claim              -> 404 {"code":"task_not_found","message":...,"detail":...,"suggested_action":...}
POST /tasks/task-004/claim   (closed)   -> 409 {"code":"invalid_transition",...}
POST /tasks/task-004/approve (closed)   -> 404 {"detail":"Task 'task-004' is closed; the ball cannot move."}
POST /tasks/task-999/approve            -> 404 {"detail":"Task 'task-999' not found."}
POST /tasks/task-002/handoff  (no ball) -> 400 {"detail":"ball: Field required"}
POST /webhooks/nope/test                -> 404 {"detail":"Webhook 'nope' not found."}
DELETE /webhooks/nope                   -> 404 {"detail":"Webhook nope not found"}
POST /webhooks/{id}/test                -> 500 text/plain, no JSON
```

The human-action routes still map every manager `ValueError` to 404
(`routes/tasks.py:811-815, 904-905, 1080-1083, 1106-1109`), so "closed" is a 404 from
`/approve` and a 409 `invalid_transition` from `/claim`. `POST /tasks` still wraps
`OperationConflictError` in a bare `HTTPException(409)` (`routes/tasks.py:463-464`).
`readRefusal()` in the frontend returns `null` for any body without `code`
(`frontend/src/api/mutation-error.ts:26-31`), so the browser loses the sentence for all
of the `{"detail"}` rows.

**One thing improved since August.** `PATCH /tasks/{id}` with a stale `expected_revision`
now returns the structured body with `current_task`
(`routes/tasks.py:515-519`, verified: `409 {"code":"revision_conflict", "current_task":{...}}`).
Task-255's third point says PATCH conflicts "arrive as `invalid_transition` without
`current_task`"; that is no longer true for a revision conflict. It is still true for an
`OperationConflictError` on `POST /tasks`.

**OpenAPI** (`frontend/openapi.json`, fresh — see finding 12): 121 operations, `422`
declared on 108 of them (the validation handler in `api/main.py:327-345` actually returns
400), `ErrorBody` absent from `components.schemas` (115 schemas), and the only non-2xx
other than 422 anywhere is `202` on four operations. `DELETE /api/webhooks/{id}` is
declared `200` and returns `204` (verified). `POST /webhooks/{id}/test` is typed as a
free-form object. The generated TypeScript client therefore types every refusal as a
validation error, which is the situation `mutation-error.ts:1-14` documents as a comment.

**Fix.** Task-256 as written. Concretely: an `HTTPException` handler that renders
`ErrorBody`; `responses={404: {"model": ErrorBody}, 409: ...}` on the verbs so the
schema names them; `status_code=204` on the webhook delete decorator; a named response
model for the test route.

## 7. `/approve` on a ready, unclaimed task succeeds — P2, Confirms task-259

**Verified by running.** task-001 was `ready` / `agent` / `available`, never claimed, never
reviewed:

```
POST /tasks/task-001/approve {"user":"auditor","body":"ok"} -> 200
  lifecycle "ready", ball "agent", ball_reason "work",
  ball_prompt "Approved -- cleared to merge. Rebase onto main, merge --no-ff, ..."
```

It also fired a `task.handoff` webhook. A ready task now carries a merge instruction and
`ready`+`agent/work`, which task-259 identifies as a combination no verb was meant to
produce. Combined with task-355 (approve with no branch merges nothing silently) the
click has no precondition at either end.

**Fix.** Task-259's rule: approve/resume only on `active` with the ball at `human`.

## 8. Actor kind is not enforced anywhere over HTTP — P2, Confirms task-263

**Verified by running.**

```
POST /tasks/task-001/claim   {"agent":"auditor"}     -> 200, assignment.owner "auditor"   (auditor is kind: human)
POST /tasks/task-005/promote {"actor":"dispatcher"}  -> 200                              (reserved id, any caller)
DELETE /tasks/task-005                               -> 200, closed cancelled, archived, last log actor "system"
                                                        and a task.closed delivery with triggered_by "system"
PATCH /tasks/task-001/deliverables/nothing.md        -> 404, no actor asked for
POST /tasks/task-005/claim   {"agent":"nobody"}      -> 400 unknown_actor   (unknown ids are refused; kinds are not)
```

Python client: `TaskClient.add_progress_update(task_id, summary=...)` with the default
`agent=""` sends `"author": "system"` (`src/agentjobs/client.py:687-699`) and gets
`400 unknown_actor` from any project with an `actors:` block — verified against the
sandbox. The default is unusable on every project that configures actors.

**Fix.** Task-263 as written; the client default should be required, not `"system"`.

## 9. No Host or Origin check on mutations — P2, Confirms task-274 (server side)

**Verified by running.** A body-less POST with a foreign `Origin` and `Host`:

```
POST /api/dispatch/disable  Origin: http://evil.example  Host: evil.example  (no body)
  -> 409 {"code":"not_configured", ...}
```

409 rather than 403 or 400: the request reached the handler and was refused by dispatch
config, not by any host check. `api/main.py:258-274` is CORS only, and its `allow_origins`
still lists 8765 and 5173 rather than the 8876 deployment (a preflight from the server's
own origin, 8909, got `400` with no `access-control-allow-origin` — harmless for
same-origin fetches, wrong for anything else). The browser half of task-274 was not
exercised here.

## 10. Every review sandbox seeds a database the server never serves — P2, New (outside my brief; hit while building the sandbox)

**Verified by running.** `scripts/sandbox_store.py` resolves the store for an
*unregistered* directory to `LOCAL_PROJECT_ID` and a hash-named file
(`local-tasks-<hash>.db`); the server, after `ProjectRegistry(home).add(...)`, opens
`databases/<project_id>.db`. All nineteen `scripts/*_sandbox.py` call
`sandbox_store(project_root / "tasks")` with no `project_id` **before** registering
(e.g. `version_skew_sandbox.py:213-214`, `review_panel_sandbox.py:163,188`,
`answer_questions_sandbox.py:223,246`). Result, from the repository's own script on 8911:

```
GET /api/projects -> [{"id":"sandbox-skew", ..., "task_count":0, ...}]
```

and in my first attempt the same: `GET /api/projects/auditbox/tasks -> []`, with two
database files in `home/databases/` — `auditbox.db` (served, empty) and
`local-tasks-c892ca3470.db` (seeded, never opened). Passing `project_id=PROJECT_ID` to
`sandbox_store` fixed it. `sandbox_store.py`'s own docstring names this exact failure ("a
seed written anywhere else is a page with nothing on it") as the thing it prevents.

This matters beyond scripts: the standing review-server rule in GLOBAL-AGENTS.md depends on
these, and every UI review since task-402 (2026-09-07) that used one was looking at an
empty project unless somebody noticed.

**Fix.** Register before seeding, or make `sandbox_store` take the id every caller already
has. A test that starts one sandbox and asserts `task_count > 0`.

## 11. Measurement headers reach every route except the ones that fail — P3, New

**Verified by running.** `X-Response-Time-Ms` and `X-Task-Parses` were present on `/app/`,
`/docs`, `/openapi.json`, `/p/auditbox/`, a 404 from the SPA fallback, a 404 from `/api`,
every 400/404/409 above, and the 201/204 webhook responses. `X-Task-Parses` was `0` on all
of them, as `docs/performance.md:52-61` says it should be on a row-backed store. They are
**absent on the 500** from finding 2 (`text/plain`, no measurement headers): the
middleware sets them after `call_next` returns (`api/main.py:232-235`), and an unhandled
exception never returns. So the one class of response a performance investigation most
wants timed is the one without a timing.

**Inferred from reading:** middleware order puts `measure_request` innermost
(`main.py:213`, then `resolve_principal_for_request` at `:239`, then CORS at `:258`), so
credential verification and CORS handling are outside "wall time inside the application".
Small today; wrong if verification ever gets expensive.

## 12. OpenAPI freshness is real — P4, sound

**Verified by running.** `poetry run python scripts/export_openapi.py frontend/openapi.json --check`
printed that both `frontend/openapi.json` and `frontend/src/api/apiDigest.ts` match the
application, exit 0. The `api` gate stage runs exactly that plus
`npm run check:api-client` (`scripts/check.py:333-339`), and
`frontend/scripts/check-generated-client.mjs` regenerates `src/api/generated` and diffs it
against the working tree rather than HEAD. `/api/version` on the sandbox reported
`api_digest b64003c6…` and `bundle_id 769e350159a5`. I did not run the client check
itself because it rewrites `src/api/generated` in place (`check.py:426`), which is a write
into the checkout. The *honesty* of the document is finding 6; its freshness is fine.

## 13. Parity: what the manager can do that HTTP cannot, and vice versa — P3, New

**Inferred from reading** `manager.py` public verbs against the 121 operations, then
spot-checked by running.

- Every manager verb has a route except `record_dispatch` / `record_dispatch_result`,
  which `remote_manager.py:647-681` refuses on purpose with the reason written down. Fine.
- Webhooks exist only as HTTP routes: `TaskClient` has no webhook methods (verified:
  `[m for m in dir(client) if "webhook" in m.lower()] == []`), the CLI has none, MCP has
  none. Only `curl` can manage a subscription, and nothing in the UI lists them.
- The seven human actions take no `operation_id` and no `expected_revision`, so the
  one class of click a phone retries after a dropped connection is the one with no
  replay protection (task-256 notes this).
- `TaskClientError.code` is `None` for every `{"detail"}`-shaped refusal (verified:
  `get_task("task-999")` → status 404, code None), so a client cannot branch on a read
  failure the way it can on a verb failure.
- `POST /tasks/{id}/log` with `type: question` and no `body` is accepted
  (`LogAppendRequest.body` is optional) and fires a `task.question` delivery whose
  `body` is null — verified. A question with no text is a webhook with no payload.

## 14. `docs/api-reference.md` never says what an error looks like — P3, New

**Verified by grep.** The document has no occurrence of `ErrorBody`, `"code"`, or any
description of a refusal body; the only status codes it names are the 409 on `/next`,
the 404-vs-422 note on finishes and the playbook `409 target_mismatch`. Webhook `DELETE` is
listed with no status (`:499`). `tests/test_api_reference_coverage.py` asserts only that
every operation id is mentioned somewhere in the file, so a route can be "documented" by
appearing in a table with nothing about its inputs, outputs or failures. The webhook guide
(`docs/webhooks.md`) is accurate about events and signature and wrong about blocking
(finding 5).

## 15. `docs/analytics-design.md` is honest about what shipped — P4, sound

**Verified by reading.** Its status line says "§6 shipped, the page has not"; no analytics
route is in the OpenAPI document; task-372 is `ready`, not claimed; `tests/test_analytics_contract.py`
tests the store amendments of §6, not a route. Consistent.

## 16. The runbook names a CLI command that does not exist — P4, New

**Verified by running.** `PLAN.md`'s preamble tells every auditor to search the backlog
with `agentjobs search`; `poetry run agentjobs search` answers `No such command 'search'`.
The read-only alternatives are the dashboard, MCP `tasks_search`, or a read-only SQLite
connection. Worth fixing in the runbook before the next audit so fourteen auditors do not
each rediscover it.

---

## What I did not get to

- **The browser half of task-274** (a page on a foreign origin actually triggering a
  mutation). Server side only.
- **Task-255 point 2** (a replayed claim reporting `replayed: true` on a task another
  agent now owns). Inferred from `storage.py`'s replay path as the record describes it,
  not reproduced.
- **Task-265** (poller-written handoffs firing no webhooks). It is in my brief by
  adjacency and I read the record only; the dispatch auditor (04) is closer to it.
- **Delivery ordering and concurrency** — two handoffs seconds apart arriving out of order
  (task-257). Not attempted.
- **`npm run check:api-client`** — not run, because it writes into the checkout.
- **Tailnet-principal behaviour** on the webhook routes; only owner and run were exercised.
- **The 240 ms handoff baseline** (finding 5's aside). Measured, not explained.
- **`remote_manager.py` beyond reading it**: its round-trip tests exist
  (`tests/test_remote_manager.py`) and I did not re-run them.

## Questions for other auditors

- **Storage (02) / manager (08):** why does a handoff with no webhook subscriber cost
  ~240 ms on the sandbox store when a claim costs ~11 ms? Both are one `mutate_task`. Is
  it `synchronous=FULL` paying twice, a lock wait, or attachment bookkeeping?
- **Storage (02):** `docs/storage-sqlite.md` §6 describes a transactional
  `webhook_outbox` that nothing writes (finding 4). Does anything else in that document
  describe a property the schema has a table for but the code does not implement?
- **Authorization (03):** the capability table is called "complete by construction"
  (`docs/authorization.md:29-31`) yet two webhook reads are ungated (finding 1). Are there
  other read routes whose *body* is a credential — transcripts and run output are gated;
  is anything else that carries a token or key readable by a run?
- **Dispatch / finish (04):** the poller's `TaskManager` is built without a webhook
  manager (task-265). Still true? If so, finding 5's fix (deliver from a worker) is also
  the fix for that.
- **Frontend (whoever holds it):** `readRefusal()` returns `null` for `{"detail"}` bodies.
  What does the approve/reject/answer UI actually show a person when the server answers
  `404 {"detail": "... is closed; the ball cannot move."}`?
- **Scripts / tests auditor:** finding 10 is yours more than mine. Has any review sandbox
  been run since 2026-09-07 with a non-empty page, and if so how?
