# 12 — Security & exposure

Auditor 12 of the Big Dawg Audit (task-242). Read-only; nothing was mutated, no
`scripts/check.py`, no task claims. Every live observation below is a GET against the
server already running on `127.0.0.1:8876` (commit `17fdfd0`) or against the tailnet
hostname that proxies it. Every code citation is against `main` at `79ea3b8`.

**Handling note for the synthesis session and for Jeff:** this file names the tailnet
hostname, the peer count, and the exact request shapes that work against the live
deployment. `audits/` is already on the public remote (`git log origin/main -- audits`
shows three commits). Do not push this file as-is; PLAN.md's own closing note
anticipated this. See finding S-9.

---

## Threat model

### Assets

| Asset | Where it lives | Who can reach it today |
|---|---|---|
| The task corpus of five projects, including `job-hunting` and `product-strategy` (the repo whose standing rule is "local only, never push") | `C:/projects/<project>/tasks/` on disk, served by the process on 8876 | Anyone on the tailnet, unauthenticated (S-1, S-8) |
| Dispatch transcripts — everything a Claude session printed while working | `~/.agentjobs/runs/<run>/transcript.log`, 61 run dirs | Anyone on the tailnet, via `/dispatch/runs/{id}/output` (S-8) |
| The repos on disk, and Jeff's machine generally | Written by dispatched agents under `--permission-mode auto` | Whoever can cause a dispatch and author what the agent reads (S-2) |
| Webhook receivers and their trust in the HMAC | `<project>/.agentjobs/webhooks.yaml` (gitignored) | Whoever can read or write `/api/webhooks` (S-4, S-5) |
| Jeff's identity in the record | `default_user` in each project's config | Anyone who reads `/api/projects` (S-10) |

### Attackers, as briefed

- **(a) A device on the tailnet.** Today that is four peers (`tailscale status --json`:
  2 Windows, 2 Android). The tailnet ACL is the *only* thing between a peer and every
  endpoint in this API. A stolen phone, a shared family device, or a guest node that
  was ever approved is this attacker.
- **(b) Malicious task content.** Authored by an agent that was itself injected, by an
  MCP client, by a `git pull` of a task file, or — combined with (a) — by anyone on the
  tailnet through `PATCH /tasks/{id}` or the review endpoints.
- **(c) A misbehaving dispatched agent.** Runs as Jeff's user with `auto` posture and a
  pre-approved allow-list that includes `git add`, `git commit`, `git merge`.

### The trust boundary the code believes in, versus the one deployed

The dispatch design doc's Safety section opens with: *"Dispatch converts an
unauthenticated localhost HTTP API into remote code execution on Jeff's machine"*
(`docs/agent-dispatch-design.md:1238`). Every gate that follows — master switch, runner
must be machine-local, per-project enablement, sentinel — is a defence against **a
repository** choosing what executes. None of them is a defence against **a network
peer**, because the doc's model is "localhost". `docs/mobile-access.md:3` and
`docs/api-reference.md:10-12` say plainly that there is no authentication and the
server must be kept on loopback "unless a private HTTPS proxy and access policy are in
place". The proxy is in place; the access policy is tailnet membership, full stop.

The tsnet proxy (`scripts/tailscale-service-host/main.go:56-63`) is
`httputil.NewSingleHostReverseProxy(backend)` with no path filter, no identity header,
no WhoIs. It forwards **everything**. Verified:

```
GET https://agentjobs.tailfed1df.ts.net/api/version                        -> 200 228B
GET https://agentjobs.tailfed1df.ts.net/api/projects/product-strategy/tasks -> 200 9266B
GET https://agentjobs.tailfed1df.ts.net/docs                               -> 200 937B
GET https://agentjobs.tailfed1df.ts.net/api/all/tasks                      -> 200 3904970B
```

That last line is every task of every registered project, 3.9 MB, to any tailnet peer.
Two `tailscale-service-host.exe` processes are running (`tasklist`), launched by
`C:/ai/shared/launchers/ensure-tailscale-service.ps1:34-44` — one fronting 8876
(this app) and one fronting 8766 (`jobsearch`, out of scope but on the same footing).

So the honest statement, as the brief asked for it: **tailnet membership is the entire
auth story, and the design's four gates are all on the wrong side of it.**

---

## Findings

Severity: P1 bites now · P2 should fix · P3 improvement · P4 observation.

### S-1 (P1) — The whole API is on the tailnet with no authentication, and identity is a body field

**Evidence.**
- No auth dependency, middleware or token anywhere: `grep -rn "APIKey|HTTPBearer|Authorization|TrustedHost" src/agentjobs` matches only the CORS block at `api/main.py:202-218` and a dispatch error class.
- Review endpoints require the `user` in the body to equal the project's `default_user`
  (`api/routes/tasks.py:96-118`), and `GET /api/projects` hands out `default_user` for
  every project (`api/routes/projects.py:88-94`, live: `"default_user":"Jeff Posey"`).
  So "must be attributed to the configured user" is satisfied by reading one GET.
- Agent verbs accept any configured actor id (`api/routes/status.py:182-205`), which the
  same GET lists.
- `POST /tasks/{id}/dispatch` with `{"user": "Jeff Posey"}` writes a human-authored
  note and dispatches on it (`status.py:583-665`, `guards.py:693-704, 759-775`). The
  human-clocked rule is structurally sound against *agents*; it is a no-op against a
  peer who can type the human's id.
- `POST /dispatch/enable` flips per-project enablement from HTTP (`routes/dispatch.py:404-416`)
  — gate 3 of the design's four.
- Live state: `dispatch.yaml` has `enabled: true`, projects `agentjobs`, `job-hunting`,
  `mastercalls` enabled, posture `auto`, `max_concurrent_runs: 3`.
  `GET /api/projects/agentjobs/dispatch` answers `"can_dispatch":true`.

**What a tailnet peer can do, enumerated** (all without credentials; only GETs were
exercised, the rest is read from the routes): read every project's tasks and search
them; read dispatch transcripts; create tasks, PATCH any task's spec/branches/
dependencies/parent; claim, hand off, close, archive, promote, queue-move as any actor;
approve / request-changes / answer / redirect / hold / resume / reject as Jeff; start a
Claude session on any task (costs money, runs with write access); cancel runs; enable
or disable dispatch per project; create, delete, test webhooks; register any directory
on the machine as a project or initialise one (S-6). OpenAPI: 88 paths, 99 operations,
61 of them mutating (`frontend/openapi.json`).

**Fix.** Two layers, either alone is a big improvement:
1. **Identity at the proxy.** The tsnet proxy can call `server.LocalClient().WhoIs(remoteAddr)`
   and set a header (`X-Tailscale-User`); the app trusts that header *only* when the
   request arrived on the loopback socket from the proxy, maps it to a configured actor,
   and refuses a `user`/`actor` body field that does not match. That turns "tailnet
   member" into "this person", which is what the review endpoints already pretend to know.
2. **Path allow-list at the proxy.** The phone needs `/app/*`, `/api/projects/*` reads,
   and a short list of review mutations. It does not need `POST /api/projects/init`,
   `/dispatch/enable`, `/webhooks`, `/queue/repair`, or `/docs`. Deny those at the
   proxy; they stay reachable on loopback.

Until then, a cheaper mitigation: `DISPATCH_DISABLED` sentinel when the phone is not in
use, and `auto_dispatch` stays off (it is).

### S-2 (P1) — Prompt injection chain (a)+(c) is open, and the pre-approved allow-list is what makes it bite

The brief asked: a task created over the tailnet whose content carries instructions —
what breaks the chain? **Nothing does.** Three content paths reach a dispatched agent:

1. **Cold dispatch.** The prompt stub is a pointer (`dispatch/runner.py:115-126`): it
   tells the agent to read the record and follow it. The record's `spec.description`,
   `constraints`, `context` are writable by `PATCH /tasks/{id}` (`api/models.py:349-367`)
   and at creation. The agent is instructed to obey them.
2. **Wake.** The ball prompt is delivered **verbatim** into a resumed session, up to
   4000 characters, framed as *"A human has moved the ball back to you. What they said:"*
   (`dispatch/wake.py:39-53, 70, 225-252`). `POST /tasks/{id}/request-changes` writes
   `payload.feedback` straight into `ball_prompt` (`api/routes/tasks.py:716-728`). So a
   peer's text becomes, literally, the human's instruction to a session that already has
   a worktree and a branch.
3. **Log entries.** `POST /tasks/{id}/log` with any configured actor, including the
   reserved `dispatcher`/`finisher` ids (`actors.py:175-178, 202-203`), so an injected
   "decision" can be attributed to AgentJobs itself.

The posture is `auto`: a classifier reviews each action. But the `--settings` blob
pre-approves, with no classifier round-trip, `git add`, `git commit`, `git merge`,
`npm run`, `poetry run pytest` (`dispatch/runner.py:233-244, 265`), for both `Bash` and
`PowerShell`. `npm run` runs whatever `package.json` scripts say. A session told, as
Jeff, to "merge feat/x into main and commit" does so inside the allow-list. The
project's own memory calls `git merge` under the classifier "a coin flip"; pre-approval
removes the coin.

`record_can_brief` (`guards.py:303-329`) keys on `spec.description` being non-empty,
which the attacker supplies. `assert_human_clocked` is satisfied by S-1.

**Fix.** S-1 closes the network half. For the content half, independent of auth:
- Drop `git merge` (and arguably `git commit`, `npm run`) from `ALLOW_PREFIXES`; the
  design's own merge gate says a human approves per task, so a pre-approved merge
  contradicts it. Keep the test/lint prefixes.
- Make the wake stub frame the ball prompt as quoted data, and have the agent re-read
  the entry's *actor* from the record — today the stub asserts "a human" on the strength
  of a body field.
- Consider `require_clean_tree: true` for dispatched projects (it is not set), so an
  injected run cannot commit on top of in-flight work.

### S-3 (P2) — Cross-site requests from Jeff's own browser can drive the loopback API

Server-side half verified; browser half **not** exercised (no mutations permitted).

- No `Host`/`Origin` validation: no `TrustedHostMiddleware`, and CORS
  (`api/main.py:202-218`) only governs whether a *response* is readable — it does not
  stop the request.
- FastAPI parses a body **with no `Content-Type` at all** as JSON
  (site-packages `fastapi/routing.py:336-338`, fastapi 0.119.1). A `fetch` with a typeless
  `Blob` body sends no `Content-Type`, and `mode: "no-cors"` lets it fly without preflight.
- Several mutations need **no body**: `POST /dispatch/disable`, `DELETE /webhooks/{id}`,
  `DELETE /tasks/{id}` (archive → closes as cancelled), and `POST /tasks/{id}/dispatch`,
  whose payload defaults to `DispatchRequestBody()` (`status.py:591`) — a bare POST
  starts a paid session if the newest entry is a human's.

A page Jeff visits can therefore `fetch("http://127.0.0.1:8876/api/projects/agentjobs/tasks/task-241/dispatch", {method:"POST", mode:"no-cors"})`.
DNS rebinding reaches the same place with a `Host` header the server never checks.

**Fix.** Require a custom header (e.g. `X-AgentJobs-Client`) on every non-GET; a browser
cannot add one cross-origin without a preflight, which the explicit CORS origin list then
denies. Add `TrustedHostMiddleware` with loopback and the tailnet hostname. Both are
one-line changes in `api/main.py`.

### S-4 (P2) — Webhook HMAC secrets are returned by the list and get endpoints

`Webhook` carries `secret: str` (`webhooks.py:33`) and both `GET /api/webhooks` and
`GET /api/webhooks/{id}` use it as `response_model` with no exclude
(`api/routes/webhooks.py:26-46`). A reader of the API can forge correctly-signed
deliveries to any receiver. Latent today: all five projects answered `[]` live. Nothing
in `tests/test_webhooks.py` asserts the secret is withheld.

**Fix.** A `WebhookRead` model without `secret`, or `Field(exclude=True)`.

### S-5 (P2) — Webhooks are an SSRF and an exfiltration channel for full task records

- `url: HttpUrl` accepts loopback, link-local, RFC1918, anything (`webhooks.py:31`).
- Every delivery body is `task.model_dump(mode="json")` — the whole record — plus
  metadata (`webhooks.py:242-255`). `docs/webhooks.md:43` confirms "actual deliveries
  contain the whole record".
- `POST /webhooks/{id}/test` fires synchronously on request (`webhooks.py:183-198`).
- Combined with S-1: a peer registers `https://attacker/x` on `job-hunting` for
  `task.handoff` and receives every handoff thereafter. Creation is not logged on any
  task and nothing in the UI lists webhooks.

Verified clean: redirects are not followed (`httpx.AsyncClient().follow_redirects` →
`False`), timeout 10 s, failures logged not retried. Signature is HMAC-SHA256 over the
exact body (`webhooks.py:257-264`) — but there is **no nonce and no receiver-side
freshness rule**: `timestamp` is a field, the docs never tell receivers to check it, so
a captured delivery replays forever (P3, folded here).

**Fix.** Behind S-1 first. Then: refuse non-global targets by default (allow loopback
only via an explicit config flag), and write a `note` entry on the project or a line in
the server log when a webhook is created or tested.

### S-6 (P2) — Any directory on the machine can be registered, initialised, or probed over HTTP

- `POST /api/projects` registers any existing dir that has a config (`projects.py:204-218`).
- `POST /api/projects/init` **writes** `.agentjobs/config.yaml`, a `tasks/` directory and
  a `.mcp.json` into any existing directory (`projects.py:221-246`,
  `project_setup.py:108-113, 176`). The caller-supplied `user` becomes a `kind: human`
  actor and `default_user` (`project_setup.py:54-58`) — so the new project is
  dispatchable as that "human" from birth.
- `POST /api/projects/inspect` is a filesystem existence oracle (`projects.py:249-271`).
- `GET /api/projects` discloses every registered root path.

Chain: init `C:/Users/jpose` (or any repo) → `POST .../dispatch/enable` → create a task
with a description → `POST .../dispatch` with `user` → Claude session with that cwd.
`contain_directories=True` correctly keeps `tasks_directory` *inside* the chosen root,
which does not help when the root is the attacker's choice.

**Fix.** These three endpoints exist for the onboarding page; they have no business
being reachable from the proxy (S-1 fix 2). Server-side, restrict `init`/register roots
to an allow-listed parent (`C:/projects`) declared in `~/.agentjobs/`, the same
machine-local place runners live.

### S-7 (P2) — Stored XSS in the legacy Jinja/Alpine pages, plus third-party script with no SRI

- `x-markdown` does `el.innerHTML = marked.parse(content)` (`api/templates/base.html:40-54`).
  `marked@11` has no sanitiser; raw HTML in markdown passes through.
- Rendered on `task.ball_prompt`, `spec.intent/description/constraints/out_of_scope`,
  and every log `entry.body` (`api/templates/task_detail.html:70, 151, 251, 287, 296, 304, 370-394`).
  `| tojson` correctly escapes the *attribute*; the damage is the innerHTML step.
- Alpine and marked load from `cdn.jsdelivr.net` with no `integrity` attribute
  (`base.html:30-32`): a CDN compromise runs in a page that can call the API as Jeff.
- The legacy router is still mounted (`api/main.py:291-292`) and proxied to the tailnet.

Any author of task content — an agent, an MCP client, a tailnet peer — can run script
in whoever opens `/p/<project>/tasks/<id>`. **The primary React UI is clean**: no
markdown library in `frontend/package.json`, no `dangerouslySetInnerHTML` or `innerHTML`
in `frontend/src`, Starlette's Jinja `autoescape` is on by default
(`starlette/templating.py:114`).

**Fix.** ENGINEERING.md already calls Jinja "legacy … not the primary or recommended
UI". Delete `web_router`/`web_legacy_router` and the templates. If they must stay, add
DOMPurify and SRI hashes. This is an argument for auditor 2's "abandoned" column.

### S-8 (P2) — The local-only repositories and the run transcripts are served to the tailnet

- `~/.agentjobs/projects.yaml` registers `product-strategy` (`C:/projects/product-strategy`)
  and `job-hunting`. `C:/projects/AGENTS.md` marks product-strategy "Local only — no
  remote, never push". The "never push" rule guards the public remote; the tsnet proxy
  publishes the same records to every tailnet device, and `/api/all/tasks` aggregates
  them (live: 200, 3.9 MB through the hostname).
- `GET /dispatch/runs/{id}/output` returns up to 512 KB of the session transcript
  (`routes/dispatch.py:480-498`) — whatever the agent echoed, including any secret it
  happened to print. 61 run directories exist.

**Fix.** Short term: a per-project `expose_on_proxy: false` honoured by the proxy's
path allow-list, defaulting product-strategy and job-hunting to off. Or unregister them
from the *served* registry and run a second loopback-only server for them.

### S-9 (P2) — The public remote carries machine and network identifiers; `audits/` is already pushed

Repo is public (`gh repo view`: `"visibility":"PUBLIC"`), and `tasks/`, `audits/`,
`.agentjobs/config.yaml` and `.vscode/` are on `origin/main`.

Found on the remote (grep of the working tree, all tracked):
- `C:\Users\jpose\...` paths in roughly thirty `tasks/agentjobs/*.yaml` files (dispatch
  `argv` records `C:\Users\jpose\AppData\Roaming\npm\claude.CMD`; `log_path` fields).
- The tailnet hostname `agentjobs.tailfed1df.ts.net` in `dispatch/address.py:88`
  (docstring), `task-108`, `task-154`, `task-240`, and `audits/2026-08-21/PLAN.md`.
- Tailnet IPs `100.126.67.115`, `100.90.111.6`, `100.89.165.121` and the hostname
  `jobsearch.tailfed1df.ts.net` in `task-086-live-updates.yaml:395-433`.
- `.agentjobs/config.yaml` is tracked despite `.gitignore` listing `.agentjobs/` (added
  before the ignore); it names the human actor. Harmless on its own.

**Not found** (read-only `git log -p --all` over the whole history, 1147 commits): no
`tskey-`, `ghp_`/`github_pat_`, `AKIA`, `sk-ant-`, Slack `xox`, or private-key blocks.
The historical `plugins/agentjobs/.mcp.json` held only `http://127.0.0.1:8765`.
`TS_AUTHKEY` appears only as a placeholder in docs and the launcher README pointer.

Hostnames and tailnet IPs are not credentials — the ACL still gates access — but
together with S-1 they are a target list, and task-155 ("scrub personal references")
evidently did not reach the dispatch `argv` records that every run appends.

**Fix.** (1) Do not push `12-security.md`; hold `audits/` or gitignore it as PLAN.md
suggested. (2) Have the dispatcher record `argv[0]` as the bare program name and keep
the resolved path in `meta.yaml` only. (3) Decide whether `tasks/` belongs on a public
remote at all; the product pitch is git-friendly YAML, and the corpus is also a log of
one person's machine.

### S-10 (P3) — Attribution is unverified in three places the actor model is supposed to cover

- `PATCH /tasks/{id}?actor=` is passed through unvalidated (`tasks.py:439-459` →
  `manager.update_task` records `actor or "system"`).
- Reserved ids `dispatcher` and `finisher` are accepted from any caller
  (`actors.py:202-203`), so a caller can write "AgentJobs finisher merged …" entries.
  The human-clocked guard correctly refuses to *dispatch* on them, but the record lies.
- `default_user` is disclosed by discovery and required by review routes, which is
  identity by announcement.

**Fix.** Validate `actor` on PATCH like the six verbs do; refuse reserved ids from HTTP
and MCP (the dispatcher and finisher call the manager directly, not the API).

### S-11 (P3) — Branch names from the task record reach git argv without `--`

`finish.py:640` runs `git merge --no-ff --no-edit -m <message> <plan.branch>` and
`finish.py:516` runs `git rebase <plan.base>`, where `plan.branch` comes from
`task.branches[]` (`finish.py:264-266`) — a field `PATCH /tasks/{id}` sets. A name
beginning with `-` is parsed as an option. `record_commit.py:173, 191` gets this right
with `--`. Latent: `finish:` is not configured on this machine (`dispatch.yaml` has no
such key), and the worktree is looked up from `git worktree list`, not composed. Not
exercised.

**Fix.** Validate branch names with `git check-ref-format --branch` on write, and put
`--` before every ref argument in `finish.py`.

### S-12 (P3) — No request limits: size, count, or rate

Descriptions and log bodies are unbounded strings; attachments are capped at 5 MiB
*each* (`attachments.py:32`) with no cap on how many per entry; `/search` and
`/all/tasks` scan the corpus per call; no rate limiting anywhere. From the tailnet this
is a trivial disk-fill (attachments are content-addressed and committed forever, by
design) and CPU exhaustion. P3 because S-1 dominates.

### S-13 (P3) — `_validated_bind_host` defends against the wrong exposure

`cli.py:399-419` refuses `0.0.0.0` "because AgentJobs has no authentication", and
`docs/mobile-access.md:140-142` repeats it. The guard protects the LAN case and says
nothing about the deployment actually in use, which routes around it by design. Asked
ENGINEERING.md's question — *what would this have caught?* — it would have caught a
typo; it cannot catch the documented setup. Keep it, but the docs should stop implying
loopback-plus-proxy is a security posture rather than a transport one.

### S-14 (P3) — The design's Safety section describes a localhost API that no longer exists

`docs/agent-dispatch-design.md:1236-1266` is the best-written security argument in the
repo and it is arguing against the wrong attacker. Gate 3 is HTTP-flippable
(`POST /dispatch/enable`) and the HTTP is on the tailnet. The doc should gain a
"network peer" row, and the four-gates claim "each independently sufficient" should be
re-examined: gates 1, 2 and 4 hold against a peer; gate 3 does not, and S-6 shows a
peer can manufacture a project that passes 1–3.

### S-15 (P4) — Observations, no action required by themselves

- `/docs` and `/redoc` are served to the tailnet; `server: uvicorn` banner; no
  `X-Content-Type-Options`, CSP, or frame headers on any response (live headers).
- Every dispatched session runs `claude --bg --remote-control` (`dispatch.yaml`): a
  second control plane for those sessions exists through Jeff's Anthropic account,
  entirely outside the tailnet. Not a defect, but the threat model should list it.
- `ball_prompt` in the wake stub is truncated at 4000 chars with a pointer; fine.
- Two `tailscale-service-host.exe` processes run as Jeff's user from
  `C:/ai/shared/services/bin`; state in `C:/ai/shared/services/state`. Those node
  identities are as good as the auth key that minted them — protect that directory.

---

## Examined, nothing found

- **YAML safety.** Every load site in `src/`, `scripts/`, `tests/` and `migration/`
  uses `yaml.safe_load` or `SafeLoader` (`grep` of `yaml.load|unsafe_load|FullLoader`
  across `*.py`: 70 hits, all safe). `storage.load_yaml` is `CSafeLoader` with a
  `SafeLoader` fallback (`storage.py:44-78`) and `tests/test_yaml_loader.py:79` asserts
  it is not `CLoader`. The live server reports `"yaml_loader":"libyaml (yaml.CSafeLoader)"`.
- **Path traversal.** Task ids go through `contained_path` (`storage.py:232-252`,
  `projects.py:285-297`), which resolves and checks containment — correctly handling
  the Windows `base / "C:/x"` absolute-join trap. Project ids are an exact registry
  lookup after a `^[a-z0-9][a-z0-9._-]*$` regex (`projects.py:30, 180-192`). Attachments
  are resolved only via a log entry that references them, then contained, then
  hash-checked (`tasks.py:339-382`, `attachments.py:95-105, 143-157`); only PNG/JPEG/WebP
  by magic number, so no SVG script vector. SPA assets are Starlette `StaticFiles`
  mounts (`spa.py:26-35`), which normalise and refuse `..`. Tests:
  `tests/test_projects.py:136, 171` cover traversal-shaped ids and hostile paths.
  Registry `root` fields are machine-local and hand-edited, as intended — the hole is
  the HTTP writer (S-6), not the reader.
- **Command composition.** No `shell=True` anywhere in `dispatch/`; argv is a list with
  placeholders substituted per element (`runner.py:449-460`); `resolve_executable`
  avoids the `shell=True` workaround on Windows. `env:` from runners is merged into the
  child environment and never written to `meta.yaml` (`runner.py:1027-1053`; live
  `meta.yaml` keys: argv, caused_by, …, no env).
- **Dispatch auth (`dispatch/auth.py`).** Despite the filename, this module is an
  *auth-failure detector*: it tails the Claude session transcript for
  `"error": "authentication_failed"` lines belonging to the run's session id
  (`auth.py:228-246`). It authenticates nothing and grants nothing. What a "valid
  credential" proves in dispatch is therefore: Claude Code's own OAuth token in
  `~/.claude`, which every dispatched session inherits from Jeff's environment. The
  only AgentJobs-side credential is `TS_AUTHKEY`, used once to mint the tsnet node
  identity, never stored by this repo.
- **Webhook signing primitive.** HMAC-SHA256 over the exact bytes sent, hex, in
  `X-Hub-Signature-256` (`webhooks.py:223-226, 257-264`); docs tell receivers to use
  `hmac.compare_digest` (`docs/webhooks.md:50-58`).
- **Secrets in git history.** Clean — see S-9.

## What I did not get to

- **No mutation was exercised**, so S-3 (CSRF), S-5 (SSRF), S-6 (init into an arbitrary
  dir) and S-7 (XSS) are verified from code and server-side behaviour, not by a
  successful exploit. Each names the exact request; a sandbox server on its own port
  with throwaway data (per GLOBAL-AGENTS.md's review-server rule) would confirm them in
  minutes.
- **The MCP server surface** (`src/agentjobs/mcp/`) — whether it adds any write path the
  REST routes lack, and the plugin hooks under `plugins/agentjobs/hooks/`. Auditor 8 owns it.
- **`finish.py` in full** (1500 lines). I read the git argv composition and the restart
  plumbing only. Its `restart` command comes from machine-local config, which is the
  right place; I did not trace what it does with `verify_base`.
- **The `jobsearch` proxy on 8766** and `C:/ai/shared/launchers/` beyond the one grep.
  Same exposure shape, different repo.
- **Tailnet ACL contents.** I counted peers; I did not read the policy. Whether
  `svc:agentjobs` is reachable by all four or a subset is the single most important
  number I do not have.
- **Windows file ACLs** on `~/.agentjobs/` and `C:/ai/shared/services/state`.
- **Dependency CVEs** (`poetry.lock`, `package-lock.json`). Not attempted.

## Questions for other auditors

- **Auditor 10 (dispatch):** does `poller.py` or `ledger.py` ever *act* on a `meta.yaml`
  field a run wrote about itself (a run directory is writable by the agent inside it,
  since it runs as the same user)? If the reconcile path trusts `status:` from a file
  the agent can edit, (c) can declare itself finished and slip the live-run lock.
- **Auditor 8 (MCP):** the REST layer lets any caller write as `dispatcher`/`finisher`
  (S-10). Does the MCP `actor` validation share `validate_actor`, inheriting the same hole?
- **Auditor 7 (API/webhooks):** S-4 and S-5 are yours from the contract side — does
  `openapi.json` document `secret` in the webhook read schema (it should not)?
- **Auditor 2 (docs):** `docs/agent-dispatch-design.md` §6 and `docs/mobile-access.md`
  are both accurate sentence by sentence and wrong as a whole (S-14). Which column is that?
- **Auditor 9 (frontend):** the React app acts as `default_user` (`App.tsx:147, 484`).
  Is there any client-side notion of "who is holding this phone", or is every device
  Jeff by construction? That decides whether S-1 fix 1 needs UI work.
- **Auditor 4 (storage):** `/api/all/tasks` parsed 3.9 MB of YAML for one GET through
  the proxy. Is that one corpus walk per project or per request, and is `X-Task-Parses`
  on it what you expect?
