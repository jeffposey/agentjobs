# 03 — Authorization, identity, and exposure

Auditor 03, Big Dawg Audit II, night of 2026-09-11. Main clone on `main` at `096f33ae`,
read-only. Everything below was checked against today's code; where a claim was
exercised it was exercised on a throwaway server bound to `127.0.0.1:8903`, serving a
temp project with its own `AGENTJOBS_HOME`, and torn down afterwards. The owner's server
on 8876 was not called.

**Headline.** The hole the first audit's P1 named (task-244: unauthenticated API on the
tailnet, identity as a body field) is closed for the path it named. A tailnet caller is
identified by the proxy, the identity header is believed only with the proxy's secret,
a forwarded request resolves to nobody, and a dispatched run holding a credential is
refused every review, dispatch, admin and cross-task verb. Verified by running, not by
reading. What remains is a different shape of hole: the boundary is enforced **on HTTP
only, for runs that hold a credential, over the verbs the table names** — and each of
those three qualifiers has a gap that a run with a shell or a PATCH can walk through.
Findings 1, 4 and 5 are those gaps.

Test suites covering this area were run and are green today (verified by running,
2026-09-12 02:48 UTC):

```
poetry run pytest tests/test_run_authorization.py tests/test_principals.py tests/test_front_door.py \
  tests/test_project_exposure.py tests/test_run_credentials.py tests/test_capabilities.py \
  tests/test_authorization.py tests/test_identities.py -q
215 passed in 13.55s
```

Backlog cross-reference was done against the live store (read-only SQLite query over
the `task` and `log_entry` tables, 142 open of 409 in project `agentjobs`). The records
this file touches: task-274, task-257, task-250, task-246, task-387, task-406, task-389,
task-318, task-335.

---

## 1. A run can raise its own task's posture to `autonomous`, and the next dispatch honours it

**Severity:** P2.
**Status:** New. The mechanism is acknowledged in a comment in the owner's private
`~/.agentjobs/dispatch.yaml` (lines 156–169, read on this machine) but appears nowhere in
`docs/authorization.md`'s "What this does not do" list, and task-332's own refusal
("a run may not approve a review") is what it bypasses.
**Verified by running** (the PATCH, the resolution order, the ceiling). The end-to-end
merge was not run — it would need a real dispatch, which this audit forbids — so the
last step is inferred from reading `guards.py` and `finish.py`.

**Evidence.**

- `capabilities.py:103-111` grants a run `TASK_EDIT`; `authorization.py:78` maps
  `update_task` to it, scoped to the run's own task. `api/models.py:387` puts `posture`
  on the PATCH body, described as "a request bounded by the project's machine-local
  ceiling, never a grant, so it needs no verb of its own (task-308)".
- Against the sandbox, holding a live run credential minted for `task-001`:

  ```
  PATCH /api/tasks/task-001 {"posture":"autonomous"}   -> 200, "posture":"autonomous"
  PATCH /api/tasks/task-001 {"parent":"task-002"}       -> 200, "parent":"task-002"
  ```
- `dispatch/guards.py:960-965` resolves the posture of the *next* dispatch as
  `resolve_posture(settings, task=task.posture, requested=..., inherited=epic_posture)`;
  `config.py:381-392` states precedence "most-specific-wins": dispatch-time choice >
  inherited-from-epic > **task record** > project default, then the ceiling.
- The live ceiling for this project is `max_posture: autonomous` (dispatch.yaml:169),
  with `posture: auto` as the default. The comment above it says, in as many words, that
  an agent writing `posture: autonomous` onto its own task gets it on the next dispatch,
  that this is auditable via `posture_source: task`, and "it is not prevented. It is
  prevented only by this line."
- `dispatch.yaml:149-152` has `auto_dispatch: true`; `routes/tasks.py:677-720`
  (`after_human_handoff`) dispatches on **any** human handback, and the docstring
  distinguishes "merge this" (approve) from "change this" (request changes) only by the
  `finishable` flag. A "request changes" is therefore a dispatch trigger.
- `guards.py:996-999`, on the authoriser: "Only a posture chosen *for this dispatch*
  counts as the human's choice. One inherited from the task record widens the envelope
  just as much, and is deliberately not attributed to them." The code knows the human
  did not choose it and runs at it anyway.

**The chain, as a run under `auto` would execute it.** Hand off to `human/review` after
PATCHing `posture: autonomous` onto the task. The human clicks *Request changes* — the
act that is supposed to keep the review gate closed. `after_human_handoff` dispatches;
the resolved posture is `autonomous` with `posture_source: task`; the new run's prompt
tells it (verbatim from today's ledger, `task_run` row for `run_82c8e3ab`) "Posture
`autonomous` releases the merge gate: this run merges its own work with no human
review", and `agentjobs finish --posture-release` merges on a green gate. No approval
was ever given. The walk's children are protected by `inherited` outranking `task`
(task-316); a task dispatched on its own is not.

This is the same class of act as `task.review`, reached through `task.edit`. The
capability table's own argument for scoping queue moves ("a run reordering other
people's work is the same class of act as closing it") applies with more force here.

**The fix.** One of, in order of preference:

1. In `authorize`, refuse `posture` (and arguably `parent`, which changes what the walk
   will run and in what order) on a PATCH from a `run` principal — a field-level
   exclusion inside `TASK_EDIT`, the way `require_human` is a stricter form of the actor
   check. `tests/test_run_authorization.py` gets one more test in `TestARunIsRefused`.
2. Or cap a task-record posture at the posture of the run that wrote it: a run at `auto`
   cannot leave behind a request for more than `auto`.
3. Or lower `max_posture` on this project back to `auto` and accept losing the
   dispatch-time pulldown. The comment in dispatch.yaml already says this is the only
   thing preventing it; the question for the owner is whether that was meant to be the
   permanent answer.

Whichever is chosen, add it to the "What this does not do" list until it is done.

---

## 2. Cross-site: a page on another origin drives the loopback API as the owner

**Severity:** P2.
**Status:** Confirms task-274 (draft, unspecced since 2026-08-22). Every claim in that
record's summary is still true on today's code, and the browser half — which task-274
says was "not exercised" — is now exercised.
**Verified by running**, with one stated limit on what the browser test proves.

**Evidence.** No `TrustedHostMiddleware`, no Origin check on non-GET, no custom-header
requirement (`api/main.py:258-273` is a CORS allowlist and nothing else). With `curl`
against the sandbox:

```
POST /api/dispatch/disable   Host: evil.example  Origin: https://evil.example   (no body)
  -> reached the handler (409 not_configured on the sandbox; 200 where dispatch is configured)
POST /api/tasks/task-002/log Origin: https://evil.example, JSON body, NO Content-Type
  -> 200, entry written
POST same with Content-Type: text/plain -> 400 (FastAPI only parses typeless or JSON)
DELETE /api/tasks/task-002   Host: evil.example -> 200 (archive; DNS rebinding shape)
GET /api/tasks               Host: evil.example -> 200
OPTIONS /api/tasks Origin: https://evil.example -> 400 (CORS refuses the *preflight*)
```

Then from a real Chrome page served on a **different origin** (`http://127.0.0.1:8904`)
running the shapes task-274 describes, against `127.0.0.1:8903`:

```
1 dispatch/disable no-cors:        type=opaque status=0   (request sent, no preflight)
2 log typeless-blob no-cors:       type=opaque status=0   (request sent, no preflight)
3 form POST submitted
4 whoami cors:                     TypeError: Failed to fetch  (response withheld)
```

Server side, immediately after: `POST /api/tasks/task-002/log 200 OK` and the task's log
now carries an entry whose body is `csrf-from-browser`; the form POST was the `400`. The
browser cannot *read* anything (test 4), which is all CORS was ever for; it can *write*.
And every one of those writes resolved as `owner via loopback` — the principal that holds
every capability including `dispatch.start`.

**The limit of the demonstration.** The initiating page was itself on a loopback origin,
so Chrome's local-network-access protection (the preflight or permission prompt Chrome
applies to public-site → private-address fetches) was not in the path. A page on a
public site is the case that matters and was not run: there is no public host to serve
one from inside this audit. So the finding is "the server refuses nothing; the only thing
between a public page and this API is the browser's own policy, which the server does
not participate in". DNS rebinding sidesteps that policy's origin check, and the `Host:
evil.example` result above shows the server would accept the rebound request.

**The fix** is task-274's, unchanged and still two lines: require a custom header on
every non-GET (the React client, `client.py` and the MCP server add it; a cross-origin
page cannot without a preflight, which the allowlist then refuses), and
`TrustedHostMiddleware` with loopback plus the tailnet hostname. Finish specifying
task-274 and promote it; it has sat as a draft for three weeks with the fix already
written in its body. While there, the allowlist names 8765 and 5173 and not the 8876
deployment (harmless today because the app is same-origin, but it is the list a reader
will trust).

---

## 3. Webhook HMAC secrets are readable on an unchecked GET, including by a run

**Severity:** P2.
**Status:** Confirms task-257 (draft, unspecced since 2026-08-22). Still true today.
**Verified by running.**

**Evidence.** `routes/webhooks.py:26-46` uses `Webhook` — whose `secret: str` field is
plain (`webhooks.py:33`) — as the `response_model` for list and get; neither route is in
`ROUTE_CAPABILITIES`, because reads are deliberately unchecked (`authorization.py:22-27`).
The capability docstring for `WEBHOOK_ADMIN` says it covers "read the HMAC secret back"
(`capabilities.py:86-87`); it does not, because the reads are not routed through it.

```
owner: POST /api/webhooks {"url":"http://127.0.0.1:9/hook","events":["task.handoff"],"secret":"hunter2-hmac-secret"} -> 201
run  : POST /api/webhooks ...                       -> 403 capability_denied (correct)
run  : GET  /api/webhooks                           -> 200
        [{"id":"wh_50ec26f4fb", ..., "secret":"hunter2-hmac-secret", ...}]
```

So a run holds `webhook.admin`'s one genuinely secret half — the key that lets it forge
signed deliveries — while being refused the harmless half. All five live projects
answered `[]` at the last audit; this is latent until somebody registers a hook, and
task-257 says so.

**The fix** is task-257's: a `WebhookRead` model without the secret for list/get/create,
and a test that asserts the secret is withheld. Add `list_webhooks`/`get_webhook` to the
table under `WEBHOOK_ADMIN` regardless, so the docstring stops being wrong.

---

## 4. The run boundary is HTTP-only; a run with a shell has the whole CLI, unprincipaled

**Severity:** P2 as a documentation and design gap; the behaviour itself is by design.
**Status:** New as a finding. `docs/authorization.md:151-157` states the fact ("Nothing
here reaches the CLI, which drives the manager directly and speaks no HTTP") as a
property, not as a limit, and the "What this does not do" list does not carry it.
**Inferred from reading**, with one thing verified by running: there is no guard in
`cli.py` keyed on `AGENTJOBS_RUN_ID` or `AGENTJOBS_RUN_CREDENTIAL` (grep, 2026-09-12).

**Evidence.** `agentjobs --help` lists `dispatch` (run, walk), `finish`, `queue` (move,
repair), `project` (register), `create`, `promote`, `redact`. Each drives `TaskManager`
against the SQLite file directly and resolves no principal. A dispatched run at posture
`auto` or `autonomous` has a shell (`dispatch.yaml` postures table). So every verb the
table denies a run over HTTP — `dispatch.start`, `queue.admin`, `project.admin`, acting
on another task — is one `poetry run agentjobs ...` away, and the `task_run` rows show
every run in the last two days was dispatched at `autonomous` with `bypassPermissions`,
where no classifier is in the path either. The same shell can read
`~/.agentjobs/front-door-secret` and present it (finding 9 shows the ACL), which yields
`tailnet` — every capability — over HTTP too; `front_door.py:14-27` says this honestly.

None of this is a defect in the code that exists. The defect is that ALLAGENTS.md:234
tells a run "you may not approve a review, dispatch anything, change dispatch
configuration ... A 403 naming wrong_task or capability_denied is that rule and not a
bug", and a reader will take the table for the boundary. The boundary is the posture's
permission classifier, and at `autonomous` there is none. "Did the machinery close the
hole or document it?" — for a run with a shell, it documented it, in a place the limits
list does not point at.

**The fix.** Two halves. State it: add a fifth limit to `docs/authorization.md` and one
sentence to ALLAGENTS.md saying the table binds HTTP and MCP only, and that the shell is
bounded by posture, not by this. Then close what is cheap to close: when
`AGENTJOBS_RUN_CREDENTIAL` is in the CLI's environment, have the CLI adopt the run
principal and apply the same `authorize()` — the rule is already transport-free by
design (`capabilities.py:32-35`), so this is wiring, not policy. task-318 shows `finish`
must keep working from inside a run, which is fine: `finish --posture-release` is the
run's own task. Direct SQLite access stays open and should be named as such.

---

## 5. A session a run spawns inherits the run id but not the credential — so it is the owner

**Severity:** P2.
**Status:** New; adjacent to task-318 (closed: a walk child inherited the parent's
`AGENTJOBS_RUN_ID`). Cause not established — see below.
**Verified by running** (the environment of this very session); the cause is inferred.

**Evidence.** This auditor is a background session spawned by the supervisor of
task-410, which is the dispatched run `run_991db1bf`. In this session's process
environment:

```
AGENTJOBS_RUN_ID=run_991db1bf
AGENTJOBS_RUN_DIR=C:\Users\jpose\.agentjobs\runs\run_991db1bf
AGENTJOBS_RUN_CREDENTIAL   (absent)
```

while `run_991db1bf/session-settings.json` — the document `session_env.deliver_identity`
wrote for exactly this run — carries all three keys under `env`, and the run's meta says
`session_env: delivered`. So two of the three values reached this process and the secret
one did not.

Consequences, both real:

- `session_identity.py:50-51`: a process with `AGENTJOBS_RUN_ID` set is "already a
  dispatched run" and is **not** registered as an interactive session, so no run record,
  no poller, no stall detection — task-320's failure mode, re-created for every session a
  run spawns.
- `client.py:26-40`: with no credential the MCP server and the Python client present
  nothing, so over HTTP this session is `owner via loopback` and holds every capability —
  including `task.review` on the supervisor's own task. The supervisor's guard is
  therefore weaker than its children's.

**What was not established.** Whether the supervisor's spawn stripped the variable, or
Claude Code drops a settings `env` key whose name looks like a secret before it reaches
tool subprocesses, or the `--bg` daemon path differs from the `--settings` path for that
one key. The supervisor's transcript (`b4d72a07…jsonl`) mentions
`AGENTJOBS_RUN_CREDENTIAL` only in source it read; the spawn-session skill's SKILL.md
does not mention it. Somebody with a dispatched session in hand should run
`env | grep AGENTJOBS` inside it and inside one child it spawns, and record both. If the
credential is missing in the *parent* too, finding 4's "HTTP-only" boundary is not merely
HTTP-only, it is absent, and task-406's observed `wrong_task` refusals need explaining
(they prove *some* dispatched session presented a credential on 2026-09-11).

Related and simpler: `agentjobs run register` (task-354) and every interactive session
mint no credential — `mint_run_credential` has three callers, all cold-start paths in
`runner.py` (`:1506`, `:2027`, `:3026`) — so a session Jeff starts by hand or through the
spawn-session skill is `owner` by construction. ALLAGENTS.md:234 ("Every request you make
over HTTP carries your run's credential") is true of dispatched runs only.

**The fix.** Decide what a spawned child *is*. If it is part of the run, it should get the
run's credential and the parent should not have stripped it; if it is its own thing, the
parent must clear `AGENTJOBS_RUN_ID`/`RUN_DIR` before spawning so the child registers as
an interactive session. Either way, `docs/principals-design.md` gets a paragraph on
sessions that are neither the run nor the person.

---

## 6. `docs/authorization.md` limit 1 describes a world before task-244

**Severity:** P3.
**Status:** New (documentation). The docs auditor should own the fix; recorded here
because it is the sentence a reader of the capability table will trust.
**Verified by reading** the file and **by running** the behaviour it says does not exist.

`docs/authorization.md:134-136`: "It is not authentication. A tailnet peer still reaches
this API without proving anything, and until task-244 sets the identity header at the
proxy, every such caller resolves as `owner` or as nothing at all." task-244 is closed
and its proxy is what serves the owner's phone today. On the sandbox, with the secret
configured:

```
X-Tailscale-User: alice@example.com                              -> owner via loopback (ignored)
X-Tailscale-User + X-AgentJobs-Front-Door: <secret>              -> tailnet (alice@example.com) via proven_header
X-Tailscale-User + wrong secret                                  -> owner via loopback
X-AgentJobs-Front-Door: <secret>, no identity                    -> no principal (no_proven_identity)
X-Forwarded-For: 203.0.113.9 (alone, or with secret + identity)  -> no principal; "arrived from 203.0.113.9"
```

The last line also confirms the `FORWARDING_HEADERS` comment (`principals.py:67-86`):
uvicorn's proxy-headers middleware really does rewrite the peer on the served process,
and the application's "any forwarding header means not local" rule catches it.

The same page's limit 3 ("an uncredentialed run is still the owner") is accurate and is
the sentence finding 5 turns on. Fix: rewrite limit 1 to say what is true now — remote
identity is the proxy's WhoIs, believed on the shared secret, and the residual is the
same-user secret `front_door.py` describes.

---

## 7. `visibility: local` hides a project from the owner's own tailnet login

**Severity:** P3 (the record already rates it).
**Status:** Confirms task-387 (ready, unclaimed). Reproduced on today's code.
**Verified by running.**

With `visibility: local` appended to the sandbox project's config and the sandbox
`identities.yaml` mapping `alice@example.com` to the project's one human actor:

```
owner   GET /api/projects                 -> [{"id":"_local", ...}]
tailnet GET /api/projects                 -> []
tailnet GET /api/projects/_local/tasks    -> 404 "Unknown project '_local'. Registered projects: none registered."
run     GET /api/projects/_local/tasks    -> 200
```

`exposure.py:102-115` defines "local" as `{OWNER, RUN}` and `OWNER` is a *socket*
property (`principals.py:477-479`). The record's diagnosis is right: on this machine the
person never arrives on the socket, so the control hides `job-hunting` and
`product-strategy` from the only reader they have. Nothing to add except that the
identity registry already knows this login *is* the owner (`identities.yaml: owner:`),
which is the fact `readable_by` should be consulting.

---

## 8. A run cannot act on any task but its own — including the playbook's targets

**Severity:** P3 (as filed).
**Status:** Confirms task-406 (ready, filed 2026-09-11 17:42 UTC).
**Verified by running.**

```
run (task-001) POST /api/tasks/task-002/queue-move -> 403 wrong_task
run (task-001) POST /api/tasks/task-002/claim      -> 403 wrong_task
```

The scoping is exactly as `capabilities.py:124-141` argues for, and task-406's point
stands: groom and reorder are dispatched *at* a run task and act *on* others, so under
this rule they cannot do their one job. That is a design decision to make, not a bug to
patch around; the record frames it correctly.

---

## 9. `0600` is decorative on Windows; a second group holds Modify on every secret file

**Severity:** P3.
**Status:** New. `front_door.py:14-27` and `credentials.py:15-22` already say the secret
is not unreadable by a same-user agent; this makes the claim concrete and finds a group
the docs do not mention.
**Verified by running** (`icacls`, 2026-09-12).

`docs/tailnet-front-door.md:111` says the secret is written "mode `0600`";
`docs/principals-design.md:126-127` says the digest and the session-settings document
are `0600`. On this machine:

```
C:\Users\jpose\.agentjobs\front-door-secret               CORUSCANT\CodexSandboxUsers:(I)(M)  ... CORUSCANT\jpose:(I)(F)
C:\Users\jpose\.agentjobs\runs\run_991db1bf\session-settings.json   (same ACL)
C:\Users\jpose\.agentjobs\runs\run_991db1bf\credential.sha256       (same ACL)
```

Every entry is `(I)`nherited; `os.chmod(path, 0o600)` (`credentials.py:116`,
`session_env.py:229`) changed nothing, as the code comments predict. What the comments
do not predict is `CodexSandboxUsers` with **Modify** — presumably the account group
Codex's Windows sandbox runs under. A Codex batch run is a configured runner here
(`dispatch config`: four `codex-*` runners). So a sandboxed Codex process can read the
front-door secret (become `tailnet`, every capability), read any *live* run's token out of
its `session-settings.json` (become that run), and overwrite either.

`session-settings.json` also survives the run: `revoke_run_credential` deletes the
digest (`ledger.py:1153-1156`, `runner.py:1085-1088`) but the document holding the
token stays. Dead, since verification refuses it (finding 10), but a directory that
"sits on disk for months" now holds the plaintext of a credential rather than its hash,
which is the opposite of what `credentials.py:24-31` set out to achieve.

**The fix.** Delete or truncate the settings document when the digest is revoked. Replace
the `0600` claims in both docs with the sentence `front_door.py` already has. And decide
whether `CodexSandboxUsers` having Modify under `~/.agentjobs` is intended; if not,
`icacls ... /inheritance:r` on that directory is the one-line change.

---

## 10. Credential lifecycle: sound where tested, with one open window

**Severity:** P4 (observations), one P3 question at the end.
**Status:** New as a consolidated account. task-389 (open) is the window.
**Verified by running** except where marked.

| Question | Answer | How |
| --- | --- | --- |
| Where it comes from | `mint_run_credential` before spawn; token `run_<8hex>.<43 urlsafe>`; digest only on disk | read `credentials.py:85-119`; a mint against the sandbox home produced `credential.sha256` of 64 hex |
| Presented as | `X-AgentJobs-Run` header; the run id in the clear selects the directory, the regex `\Arun_[0-9a-f]{8,}\Z` is the traversal guard | `run_../../home.nonce` → `unverified_run_credential`, never a file read |
| Forged | resolves **nothing**, never `owner` | `run_deadbeef.nonce` → whoami `unverified_run_credential`; POST → 403 same code |
| How long it is good | until the run's `status` is in `{finished, cancelled, failed}` — **not** `completed`, which is an outcome word; a first attempt with `status: completed` still verified, and that was the test's mistake, not the code's | `meta.yaml` edited to `finished` → whoami `expired_run_credential`; POST → 403; refused *with the digest still on disk*, so status is the enforcer and revocation is the belt |
| What revokes it | any terminal write through `update_meta` or `write_finish_fields` also unlinks the digest | read `runner.py:1085-1088`, `ledger.py:1153-1156` |
| Run dies holding one | the credential stays valid until something writes a terminal status; nothing in verification looks at the process | inferred. The sandbox run had no process at all and verified for as long as its meta said `running` |
| Second process presents it | yes, by design; the token is in the child's environment and `curl` presented it throughout | `credentials.py:15-22` says so plainly |

**The open window (P3).** task-389 documents a run whose meta lacked `dispatch_entry_id`
and therefore sat at `status: starting` "for ever", invisible to the poller. Such a run's
credential never expires: it verifies (`starting` is not terminal), names a real task,
and any process that reads its settings file — finding 9 says which — is that run
indefinitely. When task-389 is fixed, add "and revoke the credential" to whatever settles
an orphaned record.

---

## 11. Injection through task content: bounded, and the wake attributes correctly

**Severity:** P4.
**Status:** New (the first audit raised the question; this is the answer for today's
code).
**Inferred from reading**, with the resolution-order and attribution paths traced.

- The cold-start prompt is a pointer, not a copy (`runner.py:1331-1379`,
  `PROMPT_STUB`/`SUPERVISOR_STUB`): task id, project id, root, api base, run id, child
  ids, posture clause, playbook line. No task-authored text enters argv. The
  `str.format` calls substitute values only; braces in a title cannot reach the template.
- The wake prompt carries `ball_prompt` verbatim, up to 4000 characters, under the words
  "A human has moved the ball back to you. What they said:" (`wake.py:55-75`). That
  attribution holds because a wake fires only on a human handback (`auto.py:14`,
  `assert_human_clocked`), and each human route writes its own text into `ball_prompt`
  (`routes/tasks.py:839` request-changes feedback; `:1074` approve note). A run cannot get
  its own words fed back to itself as the human's.
- What remains is inherent: the record *is* the instruction, `task.create` is unscoped for
  runs (`capabilities.py:57-58`, and verified: a run filed a task), and a task one run
  files is what a human later dispatches an agent at. A `spec` that says "run X" is obeyed
  because that is what a spec is. The controls that exist — posture, `push: false`, the
  gate — are the right ones for that, and finding 1 is the one that undoes the first of
  them.
- One shape worth a test: `describe_children` and the policy clause are built from ids
  and config; `project_root` comes from the registry. All owner-controlled. Nothing found.

---

## 12. Smaller observations

**P4 — the actor check has one documented soft spot.** `capabilities.py:348-354`: a run
whose meta carries no `agent` may claim *any* agent id (never a human one). Verified the
refusals on the sandbox: a run claiming `Jeff Posey` → `actor_mismatch` naming it a
person; claiming `codex` while dispatched as `claude` → `actor_mismatch`; claiming
`claude` → 200. A human (`tailnet`) claiming an unconfigured person → `unknown_actor`
from validation; claiming an agent → 200, as designed. The soft spot is admitted in a
comment and only affects hand-written or pre-task-332 metas.

**P4 — public remote carries tailnet identifiers.** Extends task-246 (draft, decision
pending since 2026-08-22): `docs/tailnet-front-door.md:137-143`, added by task-244 on
2026-09-04, names the account domain, this machine's hostname, two device hostnames and
the service VIP; `audits/2026-08-21/12-security.md` is still tracked. Not credentials,
and the ACL is the gate, but task-246 asked for a decision before more of this landed and
more landed. `git grep` on 2026-09-12.

**P4 — reads are unchecked, and a run can read everything.** Verified: with a run
credential, `GET /api/tasks`, `/api/all/tasks`, `/api/dispatch/runs`, `/api/dispatch`,
`/api/queue` all 200. By design (task-333 answered the reader's question with exposure,
not capability). Worth one sentence in the table's docs so nobody assumes `wrong_task`
also hides other tasks.

**P4 — `/api/whoami` reports and refuses nothing**, which is correct and is what made
this audit cheap. Keep it.

**P4 — `POST /api/tasks` rejects an `owner` field** (`"owner: Extra inputs are not
permitted"`), so a run cannot file a task pre-attributed to a person. Good.

---

## What I did not get to

- **The Go proxy's own tests and deny-list edge cases.** `go` is not installed on this
  machine, so `frontdoor_test.go` was not run. The `normalizePath` matching
  (`frontdoor.go:236-250`) was read, not probed: a trailing slash, `//api/projects/init`,
  `%2F` in a segment, and a `Host`-only difference are all untested against the compiled
  proxy.
- **Chrome's local-network-access policy from a public origin.** Finding 2 was run from a
  loopback origin, so the one browser-side control that might stop a public page was not
  in the path. This needs a page served from a non-private address.
- **A real dispatched session's environment, end to end.** Finding 5 is one observation
  from inside a *child* of a run. Nobody checked the parent's own `env`, the MCP server's
  request headers, or a Codex batch run's `env` (`codex_app_server.py`, out of scope).
- **task-250 (`POST /webhooks/{id}/test` 500s).** Not exercised; it is auditor 08's, and
  finding 3 is the authorization half of that module only.
- **The transcript and tail routes as a leak channel.** `RUN_OUTPUT` scoping was verified
  (own run 200); whether any live transcript contains a printed credential or the
  front-door secret was not searched. `transcript.log` is a pty capture and would hold
  anything an agent `cat`ed.
- **`identities.yaml` edge cases.** Case-folding on login is tested upstream; duplicate
  logins mapping to two actors, and an `owner:` naming a retired actor, were not tried.
  task-335 (identity CLI) is open.
- **The attachments route** (`GET /api/tasks/{id}/attachments/{filename}`) for path
  traversal — a read, unchecked, and the filename is a path segment. Not looked at.
- **The legacy Jinja routes** (`/tasks`, `/p/{id}/…`) — task-275 wants them deleted; their
  principal handling was not examined beyond confirming they are GET-only.
- **Rate limiting, or any cost control on the API surface.** None exists; none was
  expected; not assessed.

## Questions for other auditors

- **Dispatch (auditor 05/06):** does `after_human_handoff` on a *request-changes* really
  reach `dispatch_task` with the task-record posture applied, or does the handback path
  (`handback.py`, wake-in-place) reuse the *previous run's* posture? Finding 1 reads
  `guards.py:960` as the chokepoint for both; if the wake resumes the old session at the
  old posture, the escalation needs the session to have gone away first, which narrows
  it without closing it.
- **Dispatch / ledger:** is the poller the only writer of terminal status? If a run can
  end without one (task-389), finding 10's window is the size of that bug.
- **Storage (auditor 04?):** the SQLite file under `~/.agentjobs/databases/` inherits the
  same ACL as finding 9's files, so a run's shell can write task rows directly. Is the
  write path locked against a second process, and is there any audit of a row change
  that did not come through the manager?
- **Frontend (auditor 07?):** when a dispatch resolves `posture_source: task`, does the
  Dispatch button or the run row show that the posture was *not* the human's choice? The
  ledger records it; finding 1 depends on whether a person would notice.
- **Docs (auditor 13?):** `docs/authorization.md` limit 1 (finding 6) and the two `0600`
  claims (finding 9) are the sentences to correct; `docs/api-reference.md` was not read
  here and may repeat the pre-244 wording.
- **Webhooks (auditor 08):** finding 3 covers only who may read the secret; task-257's
  SSRF and replay halves are yours.
