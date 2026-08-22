# Auditor 2 — `docs/` vs reality

Big Dawg Audit, task-242. Read-only; this file is the only write. HEAD at audit time:
`2b6c89f` (2026-08-22). Evidence is `path:line` in this repository or a command and its
output. Four read-only subagents covered the four largest documents (dispatch design,
loops + playbooks designs, MCP set, queue + schema set); every P1/P2 they surfaced was
re-checked by me against the cited lines before it was rated.

Severity: **P1** bites now · **P2** should fix · **P3** improvement · **P4** observation.

---

## 1. Classification — every file in `docs/`

| File | Class | One-line reason |
|---|---|---|
| `index.md` | **drifted** | Indexes 12 of 19 top-level docs; labels the shipped queue design "proposed" (`:54`); says dispatch design is "clearly labelled where implementation is still pending" (`:53`) — it is labelled the opposite. |
| `installation.md` | **drifted — P1** | Clone recipe (`:6-11`) ends at a 404: `frontend_dist/` is gitignored and never built by the recipe. |
| `quickstart.md` | **drifted — P1** | Same cliff (`:10-16` then `:27-31`). Every CLI flag and client call it shows is otherwise correct. |
| `agent-workflow.md` | **accurate** | Every client signature, enum value, CLI flag and anchor checked resolves. Closing recipe (`:592-595`) repeats the P1 cliff. |
| `api-reference.md` | **drifted** | 20 routes missing; one false claim about optional `actor`; "approval does not run git" now false on a finish-enabled machine. |
| `webhooks.md` | **accurate** | Events, header, HMAC, payload keys match `manager.py` / `webhooks.py`. Two undocumented metadata keys (P4). |
| `mcp.md` | **accurate** | 15/15 tools, instruction text, `.mcp.json` generation, version rule all match. One false `replayed` claim (P3). |
| `mcp-clients.md` | **accurate** | Every config snippet matches `project_setup.py:117-131`; one step omits `expected_revision` (P4). |
| `mcp-integration-design.md` | **accurate design record** (self-labelled), behind by 2 tools / 3 reasons / 1 error code | Index says "the reference pages describe what shipped" (`index.md:49-50`), which is true. One present-tense falsehood about CI (P3). |
| `agent-dispatch-design.md` | **drifted — P1-class** | Header still says "Nothing here is implemented" (`:3-4`) over ~11.7k shipped lines; inside, three features described present-tense are **unbuilt**, two claimed absences are **built**. The dangerous kind in both directions. |
| `agent-loops-design.md` | **aspirational** (correctly labelled) | `:3` "Nothing here is implemented" — true. Zero code. Six derived tasks `ready`, unclaimed, positions 5800-8600. |
| `playbooks-design.md` | **aspirational** (label now wrong in one clause) | `:3-5` "held as drafts until this design is approved" — children 214-219 are `ready` since merge `25427f1`. Zero code. Not in mkdocs nav. |
| `task-selection-design.md` | **accurate, mislabelled** | Fully shipped (task-081 + 204-209 closed/completed). Header `:1` "design proposal", index "proposed design". §1 and §14 describe the *old* behaviour in present tense. |
| `schema-design.md` | **historical** (labelled) | Banner `:3-5` is honest; body has ~8 present-tense statements now false (rule numbering, lock-free storage, vanishing broken tasks, example without `queue_position`). |
| `schema/understanding.md` | **drifted** | Lists 8 of 11 log types (`:127-128`); closing tip (`:186-191`) compares two files that are now the same schema. |
| `schema/v1-erd.md`, `schema/v1/` | **retired, accurate** | Generated Aug 10; v1 is frozen. |
| `schema/v2-erd.md`, `schema/v2/` | **accurate content, stale banner** | Generated output is current (BallReason 13/13, `queue_move`, `queue_position` present). But `v2/index.md:3-4` says "PRESCRIPTIVE … nothing here is implemented yet", inherited from `schema/agentjobs-v2.yaml:6-13`. |
| `task-schema.md` | **accurate** | 25/25 top-level fields; all 8 enums identical across doc / `models_v2.py` / LinkML. Lists 5 rules where the model enforces 6 (P3). |
| `migration-guide.md` | **accurate** | All four flags exist (`cli.py:1669-1727`); "all-or-nothing" is `migrate_schema.py:831`. |
| `mobile-access.md` | **accurate** | Wildcard-host refusal list matches `cli.py:399-415`; `stop --port` exists; SW `/api/` network-only rule at `frontend/src/service-worker.js:29-30`. |
| `performance.md` | **drifted (minor)** | "`--port N` default 18950" (`:114`) — the default is derived from the checkout path (`scripts/bench.py:92`, task-187). |
| `task-corpus-audit.md` | **accurate (dated report)** | A 2026-08-13 snapshot that says so. Not in `index.md`. |
| `integration/agentjobs-package.md` | **abandoned** | 11-line stub, self-described historical, orphaned from index/nav/README. |
| `integration/mcp-release-evidence.md` | **accurate (dated)**, orphaned | Says "fourteen tools", "1089 tests" — a 2026-08-17 snapshot; nothing links to it. |
| `img/task-063-schema-v2-detail.png` | **orphan** | `grep -rn task-063-schema-v2-detail docs README.md mkdocs.yml` → nothing. |

Relative-link check across all non-generated docs: **0 broken** (script over every
`](…)` target; anchors for `schema-design.md#the-resumption-contract`,
`mcp.md#what-protects-what`, `mcp.md#every-registered-project-declares-the-server`
confirmed against headings).

---

## 2. Findings

### P1 — The install/quickstart recipe ends at a JSON 404

**Evidence.**
- `docs/installation.md:6-11`, `docs/quickstart.md:10-16,27-31`, `docs/agent-workflow.md:592-595`, and `README.md:112-121` all say: `git clone … ; poetry install ; poetry run agentjobs open`.
- `.gitignore:10` → `/src/agentjobs/frontend_dist/`. `git log --oneline -- src/agentjobs/frontend_dist` → empty: the bundle has never been tracked.
- `frontend/vite.config.ts:9` `outDir: "../src/agentjobs/frontend_dist"` — it exists only after `npm run build` (or gate stage 9).
- `src/agentjobs/api/spa.py:77-87`: when `index.html` is absent `/app` and `/app/{path}` return **404** with detail "React frontend bundle is missing from the package; run `npm run build` in frontend/…".
- `cli.py:532-594` (`open`) opens the browser at `/app/` unconditionally; `grep -nE 'frontend_dist|index.html|bundle' src/agentjobs/cli.py src/agentjobs/api/main.py` → nothing. No warning at `serve`/`open` time.
- `scripts/bootstrap.py` does not build the frontend either (`grep -nE 'build|frontend_dist|vite'` → nothing).
- The recipe was written this way on 2026-08-13 (`fd0d2f5 docs: make AgentJobs React-first`) — the release-wheel contract (`installation.md:17-29`) is what makes it true, and no wheel is published.

**Who it bites.** Every new user following the top of `installation.md`, the top of
`quickstart.md`, or the README. They see a JSON error in a browser tab. The
`installation.md:31-40` contributor recipe only avoids it because `scripts/check.py`
happens to build as stage 9; the recipe never says so.

**Fix.** One line in all four places: `npm --prefix frontend ci && npm --prefix frontend run build` before `open`, or make `agentjobs open`/`serve` print the same sentence `spa.py:83-85` already prints when the bundle is missing, before opening the browser. Either; both is better.

### P1 — `docs/agent-dispatch-design.md` tells a zero-context reader the opposite of the truth, in both directions

**Evidence (banner).** `:3-4` "**Nothing here is implemented; implementation tasks are derived in §13.**" Every §13 item 1-7 is shipped: `src/agentjobs/dispatch/` is 13 modules, ~11.7k lines (`wc -l`), CLI `dispatch` sub-app `cli.py:933-1367`, routes `api/routes/dispatch.py:398-528`, `status.py:583`. `README.md:96-98` repeats "accepted, **not yet implemented**". `docs/index.md:52-53` says the doc is "clearly labelled where implementation is still pending" — the label is the inverse.

**Evidence (present-tense, unbuilt).**
- `:887-893` "**`difficulty` and runner groups are built**"; `:914-934` describes the field. `grep -rn difficulty src/agentjobs --include=*.py` → one comment, `dispatch/config.py:1042: *(unbuilt)*`. No field in `models_v2.py`.
- `:1052-1073` runners "may additionally declare optional descriptive `model` and `effort` labels" — `DispatchRunner` (`config.py:195-222`) has `name, argv, env, mode, actor` only.
- `:1041-1045` a per-profile `strict` setting "inverts that… off by default" — absent (`grep -in strict config.py` → docstring only); the doc's own open-question box at `:1123-1127` admits it.
- `:129-138` (amendment 2026-08-18) chain-aware cap semantics in `auto.py` — `auto.py:114-165` counts dispatches; no chain concept.

**Evidence (present-tense, claims absence of things that exist).**
- `:1796` sessions have "**No run directories, no stdout capture**" — `runner.py:1232` creates a `RunDirectory`, `:94-103,1426-1450` capture `transcript.log` every poll, served by `/runs/{id}/output` and `/tail` (`routes/dispatch.py:480-510`).
- `:656-659` Remote Control URL "neither is verified. Task-070 owns it" — built: `runner.py:484-487` regex, `:1606-1609` surfaces the link in the parked `ball_prompt`.
- `:1274` and `:1938` "on graceful shutdown every live run is cancelled" — `main.py:129-155` lifespan cancels only the poller; `stop_everything` is CLI-only (`cli.py:1183`); batch thread is `daemon=True` (`runner.py:1845`) so the child is orphaned, not signalled, and `reconcile` labels it `interrupted` next start (`ledger.py:786-798`).

**Numbers that differ.** Probe timeout "two second" (`:368`) vs `PROBE_TIMEOUT_SECONDS = 5.0` (`address.py:214`). Gate "355s solo" (`:1430`) vs ENGINEERING.md's 95.8s. Config example (`:285-326`) omits `posture`, `resume_sessions`, `finish`, runner `actor` (`config.py:202,368,369,384`). `dispatch` entry example (`:403-416`) lacks `mode`, `posture`, `session_id`, `selection` that `manager.py:2048-2066` writes.

**Not in the doc at all.** `agentjobs finish` CLI and exit codes (`cli.py:1833-1881`), `~/.agentjobs/finishes/` (`finish.py:351`), `phases.jsonl` / `AGENTJOBS_RUN_ID` / `AGENTJOBS_RUN_DIR` (`phases.py:37-48`), `scripts/run_report.py`, the `WAKE_STUB` resumed-session contract (`wake.py:45-52`) — the agent-facing "you may be woken" rules live only in ALLAGENTS.md. `dispatch auth-check`, `reconcile`, `status --live`.

**Why P1 rather than P2.** This is 2,177 lines, the longest doc in the repo, linked from README as the dispatch reference, and it is the one auditor-1 and auditor-10 will be sent to. Its status line is wrong, and a reader who trusts §4 would try to set `difficulty:` on a task and `model:` on a runner and get silent no-ops. The "designed in June, reads as present" failure the brief flagged hardest — here it is, plus its mirror image.

**Fix.** Replace `:3-4` with a status block that says what shipped (§13 items 1-7, with task ids) and what did not (§2a chains, `difficulty`, labels, `strict`). Mark each of the seven passages above with the same *(unbuilt)* / *(built, see …)* convention `config.py:1042` already uses. Fix the two numbers. Add a §5a paragraph for `finish` CLI / ledger and a §8 paragraph for `WAKE_STUB`.

### P2 — `docs/api-reference.md` documents 74 of 94 operations and makes one false contract claim

**Three-way diff** (doc tables vs `frontend/openapi.json` paths vs `@router` decorators in `src/agentjobs/api/routes/*.py`):

- `openapi.json` ↔ routes: **identical** for everything `include_in_schema`. Only the root `/health` (`main.py:279`, explicitly excluded) and the Jinja `web.py` routers are absent from the JSON, by design. The gate's `api` stage is doing its job; the drift is all in the prose doc.
- Doc ↔ openapi: **missing from the doc** (unscoped form; each also exists under `/api/projects/{id}`):
  - `GET /api/version` (`health.py:69`)
  - `GET /api/all/tasks`, `POST /api/projects`, `POST /api/projects/init`, `POST /api/projects/inspect` (`projects.py:186-274`)
  - The whole dispatch family — `GET /api/dispatch`, `POST …/enable`, `…/disable`, `GET …/runs`, `POST …/runs/{id}/cancel`, `GET …/runs/{id}/output`, `…/tail` (`dispatch.py:398-501`) and `POST /api/tasks/{id}/dispatch` (`status.py:583`)
  - `POST /api/tasks/{id}/promote` (`status.py:258`)
  - `POST /api/tasks/{id}/answer`, `/redirect`, `/hold`, `/resume` (`tasks.py:789-880`); `/approve`, `/request-changes`, `/reject` are mentioned in prose only (`:65`)
  - `GET /api/tasks/{id}/attachments/{filename}` (`tasks.py:339`)
- Doc ↔ doc: nothing in the doc that is not in the code.

**False claim.** `:83-85` "the state verbs above, where both [`actor` and `operation_id`] are optional". `operation_id` is optional (`api/models.py:212`); **`actor` is required** — `HandoffRequest:645`, `ReleaseRequest:660`, `PromoteRequest:667`, `CloseRequest:674`, `LogAppendRequest:683` all `Field(...)`. `ClaimRequest:636` takes `agent`, not `actor`. Only `TaskCreateRequest:263` has an optional actor.

**Stale claim.** `:65-66` "Approval records the human handoff back to `agent/work`; it does not run git or merge a branch." On a machine with `finish.enabled`, `tasks.py:after_human_handoff` (`:600-620`) calls `spawn_finish`, which rebases, gates, merges `--no-ff`, restarts and closes (task-241, ENGINEERING.md "Steps 3 to 6 may already have happened"). The route docstring (`tasks.py:650-652` "Nothing here merges anything") and the `APPROVAL_CLEARANCE` ball_prompt text (`:622-626` "No merge has happened yet: the UI records approval, it does not run git") carry the same now-conditional sentence — see Questions, for auditor 10.

**Verified correct** (so the next editor need not re-check): queue table `:75-81`, `409` on broken queue (`tasks.py:175-228`), `agentjobs next` exit 1 (`cli.py:1644-1647`), `queue check --strict` exit 1 and plain exit 0 (`cli.py:1551-1577`), every CLI flag in `:103-111` (`cli.py:1433-1625`), `GET /api/tasks` filters `lifecycle|ball|priority|parent` (`tasks.py:140-147`), `/next` params (`tasks.py:195-198`), DELETE archives via `manager.archive_task` (`tasks.py:476-480`), the client example (`client.py:365,473,482`).

**Fix.** Add a "Human review actions" table (approve/request-changes/answer/redirect/hold/resume/reject with the `ball_reason` each writes — `agent-workflow.md:462-467` already has that table), a "Dispatch" table, `promote`, `version`, `attachments`, and the project routes; correct `:83-85`; qualify `:65-66` with the finish condition.

### P2 — The queue design shipped in full and every label still says "proposed"

**Evidence.** `docs/index.md:54` "proposed design for the explicit work order"; `task-selection-design.md:1` "design proposal"; `mkdocs.yml:70`. Shipped: `queue.py` (746 lines, first commit `e6188d2 2026-08-21`), `manager.py:565-657,1603-1948`, routes `queue.py:56-140` + `status.py:378-452`, CLI `cli.py:1363-1669`, React `TaskList.tsx:49-52,314,472`, `QueueBroken.tsx`, `NextExplanation.tsx`. task-081 and 204-209 `closed`/`completed`. The index line was written 2026-08-20 (`e4862d1`), one day before the code landed, and not revisited.

**Inside the doc, present tense now false:** `:19-23` "`get_next_task()` … sorts `(priority_rank(), -updated)`" — now `order_key` (`queue.py:106-116`); `:45-52` "`buildTaskRows` sorts by `updated` first" — `TaskList.tsx:58` "There is no sort here, deliberately"; `:611-612` "today they would dispatch an agent onto whichever task was edited last".

**Code deviations a reader would trip on.** `:302,493` show `agentjobs queue compact` bare — it requires a band (`cli.py:1599`, `QueueCompactRequest.band` `api/models.py:490-493`). `:205-207` `create --before/--after/--top` — manager accepts `placement` (`manager.py:767,901-906`) but no CLI flag, API field, or MCP input exposes it. `:336-338` "a crash mid-renumber … `queue check` reports it; `queue repair` finishes" — `find_queue_problems` (`queue.py:480-515`) reports only missing/non-positive/duplicate, so odd-but-ordered numbers are reported by nothing and only `compact` tidies them.

**Fix.** Relabel in three places as "accepted 2026-08-20, implemented 2026-08-21 (task-081, 204-209)"; mark §1/§14 as the before-state; fix `compact`, `create --before`, and the renumber sentence.

### P2 — `docs/index.md` indexes 12 of 19 docs and mislabels two of the three design records

**Evidence.** Not indexed: `agent-loops-design.md` (in mkdocs nav `:68`), `playbooks-design.md` (in neither), `performance.md` (nav only), `task-corpus-audit.md` (README and nav only), `integration/*` (nowhere), `docs/img/`. Mislabels: `:52-53` dispatch "clearly labelled where implementation is still pending" (see P1); `:54` queue "proposed" (see P2). `mkdocs.yml:71` "Task schema reference (v1 + v2)" — `task-schema.md` has no v1 section (only heading is `## Schema v2`, `:30`; v1 appears as the "Gone from v1" list `:93-94`). `mkdocs.yml:1` `site_name: AgentJobs Schema` for a site whose nav is two-thirds project docs.

**Fix.** Index every file under a status word (shipped / design / historical / dated report); drop "(v1 + v2)"; add playbooks to nav or say why not.

### P2 — README contradicts the docs it points at

Out of `docs/` strictly, but it is the other entry point, so recorded once here.
`README.md:96-98` dispatch "**not yet implemented**"; `:100-103` "Agent loops are also **not implemented** … **No agent-loops design document exists yet**" — `docs/agent-loops-design.md` has existed since `df307c1` 2026-08-18, task-078 is closed, and the README links task-078 as "queued". `README.md:42` "Codex additionally gets a bundled plugin" — Claude Code has one too (`plugins/agentjobs/.claude-plugin/plugin.json`, `mcp.md:112-120`). `plugins/agentjobs/README.md:9,159-163` "fourteen tools", omits `task_queue_move` — contradicts `mcp.md:150`, `mcp-clients.md:4`, and `tests/test_mcp_protocol.py:310` ("Serving 15 tool(s)").

### P2 — Doc/code contradiction that may be a real race: reopen positions are computed under the queue lock and written outside it

**Evidence.** `task-selection-design.md:150,358-359` say reopen/band-change hold the queue lock so "a duplicate cannot be created by a race". `manager.py:1055-1056`: `with self.storage.queue_lock(): position = self._place(…)`; the lock exits at `:1057`; the write happens in `self._mutate` at `:1017`, under the per-task lock only. A `create` or `queue move` landing in that window computes the same `max+100`. Every other queue writer (`create :805`, `move :1649`, `reprioritize :1744`, `rebalance :1589`, `compact :1600`, `repair :1905`) writes inside the lock.

I did not construct the race; I am reporting that the doc's safety claim and the code's lock scope disagree. **Handed to auditor 5** (queue) as the owner of whether it is exploitable; auditor 4 (storage) for the lock semantics.

### P3 — `playbooks-design.md` header is one merge out of date, and the doc is unreachable from the site

`:3-5` "held as drafts until this design is approved" — task-214 log (`:97-100`) "Promoted by Jeff … the design was approved and merged in 25427f1"; 214-219 are `ready`; task-211 is at the head of the high band (position 31, log: "I want to start it very soon"). `grep -n playbooks mkdocs.yml` → nothing; `docs/index.md` → nothing. Zero code: `grep -rni playbook src/ scripts/ frontend/src/ tests/ plugins/ prompts/` → 0; `ls playbooks` → no such directory. `:897-899` claims the dispatch-model-profiles task-080 record "does not exist" — it does (closed; task-156's parent; one of the two task-080 ids the corpus audit deliberately preserved). One sentence fix, one nav line.

### P3 — `agent-loops-design.md` is correctly labelled but the schema it amends still says the opposite

`:94` "Decision L1. `check` is a new field. `verify` keeps its meaning [prose]" — `models_v2.py:416-419` still documents `verify` as "machine-checkable hint, e.g. a command to run", i.e. the executable meaning the design moves to `check`. Zero code for the design (no `check` field, no `check_result` type in `LogEntryType:255-268`, no `agentjobs check`, no `AGENTJOBS_TASK_ID`). Tasks 147-152 all `ready`, positions 5800-8600; 150/151 carry "do not start without Jeff's go-ahead". Decision: either `verify`'s docstring changes now (cheap, schema doc regen) or the design records that it does not.

### P3 — Generated v2 reference and LinkML source carry a "nothing here is implemented yet" banner

`docs/schema/v2/index.md:3-4` and `schema/agentjobs-v2.yaml:6-13`: "PRESCRIPTIVE schema: nothing here is implemented yet … intended input to task-050". Also `yaml:254-256,396-402,540` "task-050 …" future tense; `scripts/regen-schema-docs.sh:26` "the input to task-050, not yet wired in". The generated content is otherwise **current** — `BallReason.md` has all 13 values including the 2026-08-21 additions, `LogEntryType.md` has `queue_move`, `Task.md` has `queue_position`, `schema/generated/agentjobs-v2.schema.json` matches, `git status` clean under `docs/schema schema`. Nothing in the gate checks that regen output is committed (the regen is a bash script with a `poetry run linkml-*` chain; `scripts/check.py` has no stage for it — auditor 3's item 4; I confirm the absence from the stage list in ENGINEERING.md). Orphans in `schema/generated/`: `agentjobs-v1.dbml`, `agentjobs-v2.dbml` both 0 bytes and never written by the script; `agentjobs-v2.er.svg` there (174,471 B, Aug 10) is stale vs the one the script writes to `docs/schema/` (192,951 B, Aug 21).

### P3 — `task-schema.md` and its neighbours count the consistency rules three different ways

`task-schema.md:96-108` lists **five** rules; `models_v2.py:730` says "the six rules" and rule 6 (`queue_position` present iff open, `:805-822`) is described only inside the `queue_position` row (`:80`). `:207-209` "Only seven fields are required … `lifecycle` defaults to `draft`, which then requires a `ball`" — also requires `queue_position` (rule 6) and `ball_reason` (rule 2). `schema-design.md:307-314` has a different rule 5 ("every change appends a transition"). `docs/schema/understanding.md:127-128` and `schema-design.md:362-370` list 8 of 11 `LogEntryType` values (missing `dispatch`, `dispatch_result`, `queue_move`). `task-schema.md` dispatch example (`:260-274`) omits `session_id` and `selection` (`models_v2.py:562-575`). `understanding.md:186-191` tells the reader to compare the `schema/examples/` v2 example with the corpus record of the schema-design task "before task-050 starts" — that corpus file is `schema: 2` now; they are the same schema. LinkML encodes rules 1-4 (`yaml:404-508`) and has no rule for 6, though it could express it the way rule 1 is.

### P3 — `mcp-integration-design.md` has one present-tense CI claim that never happened; `mcp.md` has one false `replayed` claim

`mcp-integration-design.md:347-351` "exercised on Python 3.11 and 3.12, Windows and a POSIX runner" — no `.github/workflows`; `docs/integration/mcp-release-evidence.md:78-85` says only 3.13/Windows ran. The version-skew rule described there also differs from what shipped (`compat.py:47-59` compares `(0, minor)` below 1.0); `mcp.md:284-286` has the correct one. `mcp.md:207-209` and design `:169-170` say `replayed` "says which happened" — `mutation_tools.py:411-414` and `:619` hard-code `"replayed": False` for `task_create_*` and `task_update_content` with a comment that a replayed create is indistinguishable. Design table `:176-190` lacks `task_promote` and `task_queue_move`; `:238` agent handoff reasons `work|revise` vs shipped five (`mutation_tools.py:125`); `task_next` output lacks the required `queue` block (`read_tools.py:526-530`). These are expected for a design record and `index.md:49-50` says so; they are listed so auditor 8 does not re-derive them.

### P3 — `schema-design.md` is labelled historical and still makes eight present-tense claims that are false

`:61-183` the "complete example" has no `queue_position` — as written it fails `models_v2.py:818-822` on load. `:497-502` "`TaskStorage.save_task` does read-modify-write with no locking" — `storage.py:338-366,393-407,412-438`. `:613-615` "`load_task` swallows validation errors and the task silently vanishes" — `TaskLoadError`, `GET /api/tasks/broken` (`task-schema.md:438-441`). `:328` "Task-050 owes a regression test"; `:397-398` "once v2 lands"; `:416-432` API sketch with no queue routes and no 409. The banner at `:3-5` is honest; a one-line "examples below predate `queue_position` and the storage locks" under it would close the gap.

### P3 — `installation.md` contributor recipe is a second, weaker bootstrap

`installation.md:33-39` `poetry install; npm --prefix frontend install; npm --prefix frontend run install:e2e; poetry run python scripts/check.py`. ENGINEERING.md says run `python scripts/bootstrap.py` in any fresh checkout — it does `poetry install`, `npm ci` (not `install`), `playwright install chromium`, **and** verifies the environment imports this checkout's source (the task-194/210 hazard). The doc recipe skips the verification and uses `npm install`, which can rewrite `package-lock.json`. `:42-43` "one real-server Playwright path" — the gate runs 26 (ENGINEERING.md stage table). Point the recipe at `bootstrap.py`.

### P4 — Observations

- `performance.md:114` "`--port N` … default 18950" → `DEFAULT_PORT = checkout_port(ROOT)` (`bench.py:92`); the help string already says "derived from this checkout".
- `webhooks.md` payload: `task.question` also carries `body`, `task.closed` carries `outcome` (`manager.py:2018-2020,1437-1441`); undocumented. `task.handoff` keys match exactly (`manager.py:1292-1299`).
- `mcp-clients.md:189` loop step 7 "After approval: `task_close`" omits the `expected_revision` the tool requires (`mutation_tools.py:807-808`); step 6 mentions it for handoff.
- `mcp-release-evidence.md:10,20,61,70` "1089 tests", "fourteen tools", "94-record corpus" — a dated snapshot; accurate for its date, orphaned from every index.
- `docs/img/task-063-schema-v2-detail.png` (85 KB) referenced by nothing.
- `agent-dispatch-design.md:179-180` "Task-071 should pick a [recency] window and state it" — never picked, never closed (`grep -n "timedelta|recent|window" guards.py` → nothing). An open item with no owner is the same failure as a question with no `answer`.
- `api-reference.md:10-12` and `mobile-access.md:3` "AgentJobs has no authentication" — true for the REST API (`main.py` middleware is timing + CORS only, `:171-212`). Stated plainly, which is the right thing; auditor 12 owns whether that is acceptable.
- `mobile-access.md` / `quickstart.md` / `api-reference.md` all use port 8765; this machine runs 8876 via a launcher. The docs are generic and say so implicitly; no defect, but a reader on this machine copying `http://localhost:8765/docs` gets nothing.

---

## 3. Brief item 3 — quickstart and installation as a skeptical new user

Walked read-only, command by command.

| Step | Doc | Reality | Cliff? |
|---|---|---|---|
| `git clone; poetry install` | both | fine | no |
| `poetry run agentjobs open` | `installation.md:10`, `quickstart.md:28`, README `:121` | server starts, browser opens `/app/`, **JSON 404** (`spa.py:77-87`) | **yes — P1 above** |
| `poetry -P … run agentjobs init` in another project | `quickstart.md:16` | creates `.agentjobs/config.yaml`, tasks dir, registry entry, and `.mcp.json` (`project_setup.py:133-177`, called from `cli.py:169-194`) — exactly as `:19-23` says | no |
| `agentjobs create --title … --category … --priority high` | `quickstart.md:42-43` | all three options exist (`cli.py:594-627`) | no |
| `agentjobs list --lifecycle ready` | `:44` | `cli.py:627-668` | no |
| `agentjobs work --agent codex` | `:45` | `cli.py:754-832` | no |
| Python client block | `:50-70` | `get_next_task(agent=)`, `claim_task(id, agent=)`, `add_progress_update(id, agent=, summary=, details=)`, `handoff_task(id, actor=, ball=, ball_reason=, ball_prompt=)` — all match `client.py:365,473,564,482` | no |
| `from agentjobs import Ball, BallReason, TaskClient` | `:51` | `__init__.py` exports all three | no |
| `http://localhost:8765/docs` | `:34-35` | `docs_url="/docs"` (`main.py:163`) | no |
| `agentjobs stop` | `:79` | `cli.py:450` | no |
| Contributor setup | `installation.md:33-39` | works, but is the weaker bootstrap (P3) | soft |
| `pip install agentjobs` | `installation.md:23-26` | labelled "once a release is published"; honest | no |

One cliff, and it is the first thing a new user does.

---

## 4. Brief item 5 — `docs/index.md`

Covered in the P2 above. Additionally: `:57-62` says "Everything under `docs/schema/v1/`, `docs/schema/v2/`, and `schema/generated/` is generated. Regenerate with `bash scripts/regen-schema-docs.sh`" — true, and the script also validates the live corpus against v2 and exits 1 on failure (`regen-schema-docs.sh:119-150`), which the index does not mention and which makes it a useful check nobody runs in the gate.

---

## 5. Examined, nothing found

- `webhooks.md` events ↔ `manager.py:1294,1437,2018`, `webhooks.py:190`: exact. Header name `X-Hub-Signature-256`, HMAC-SHA256 over the exact body (`webhooks.py:227-262`), async delivery with warning-only failure (`:240-246`): as documented.
- `mobile-access.md` operational claims: wildcard refusal set (`cli.py:399-415` — `*`, `+`, empty, any unspecified IP incl. `0.0.0.0`/`::`), `stop --port`, `scripts/tailscale-service-host/` exists, SW precache-shell / `/api/` network-only (`service-worker.js:29-30`).
- `agent-workflow.md` in full: every signature (`TaskClient(base_url=, timeout=)`, `list_tasks`, `search_tasks`, `create_task(**kwargs)` forwarding via `_serialise_payload`, `add_log_entry(actor, type, body)`, `operations.queue_move(expected_revision=, top=, body=)`), every `BallReason` it names (`approval`, `revise`, `answer`, `redirect`, `hold`, `available` — `models_v2.py:127-141`), the `queue_broken` MCP code (`mcp/errors.py:30`), the anchor into `schema-design.md` §5.
- `migration-guide.md`: four flags (`cli.py:1669-1727`), all-or-nothing (`migrate_schema.py:831`), "v1 rejected by name" (`models_v2.py:966-993 check_schema_version`).
- `mcp.md` / `mcp-clients.md`: 15 tools registered = 15 documented (`inventory.py:24-27`, `read_tools.py:588-596`, `mutation_tools.py:723-913`); instruction text (`instructions.py:15-34`) matches the served text verbatim; `.mcp.json` entry shape matches `project_setup.py:117-131`; `agentjobs mcp` flags `--base-url`/`--timeout` and bounds (`config.py:14-24`).
- `task-schema.md` field table (25/25) and all eight enum vocabularies vs `models_v2.py` and `schema/agentjobs-v2.yaml`: identical.
- `openapi.json` vs routes: identical (see P2).
- Relative links in `docs/`: 0 broken.

---

## What I did not get to

- **Running anything.** No `mkdocs build` (writes `site/`), no `agentjobs --help` beyond the subagent's `agentjobs mcp --help`; every CLI claim was checked by reading the Typer signatures, not by invoking them. A flag that exists but errors at runtime would pass my check.
- **The generated schema pages themselves** (`docs/schema/v1/`, `docs/schema/v2/`, ~160 files): spot-checked `index.md`, `BallReason.md`, `LogEntryType.md`, `Task.md`, `queue_position.md`; did not read the rest.
- **`frontend/README.md`** (linked from `index.md:47`) and `plugins/agentjobs/README.md` beyond the tool count — not in `docs/`, not read in full.
- **Whether the reopen lock window (P2) is actually exploitable.** Reported the contradiction; did not trace `_mutate`'s own locking far enough to say a duplicate position can be produced.
- **`docs/agent-dispatch-design.md` §8 worktree evidence and §11 decisions** — subagent read them and reported no drift; I did not re-read those ~400 lines myself.
- **`mcp-integration-design.md` §9 test-inventory claims** (concurrency, kill-restart, webhook-warning tests) — existence of the test files confirmed, contents not.
- **Historical accuracy of `task-corpus-audit.md`** — treated as a dated report and not re-verified against the 2026-08-13 corpus.

---

## Questions for other auditors

- **Auditor 5 (queue):** `manager.py:1055-1063` computes a reopen/reband position under `queue_lock()` and writes it later in `_mutate` (`:1017`) outside it. Can a concurrent `create` or `move` produce a duplicate position? The design (`task-selection-design.md:150,358-359`) says it cannot.
- **Auditor 4 (storage):** same window — does the per-task lock `_mutate` holds give any ordering guarantee relative to `queue_lock()`, or are they independent?
- **Auditor 10 (dispatch):** `APPROVAL_CLEARANCE` (`tasks.py:622-626`) writes "No merge has happened yet: the UI records approval, it does not run git" into the agent's `ball_prompt` and then `spawn_finish` runs the merge. Does the finish rewrite that prompt before a woken agent can read it, or is there a window where a resumed session reads "not merged" about a merged branch? Also: `main.py:129-155` has no shutdown cancel for batch runs despite the design's `:1274,1938` claim — is the orphaned child harmful?
- **Auditor 10 / 1:** the `WAKE_STUB` contract exists only in ALLAGENTS.md and `wake.py:45-52`; the dispatch design never mentions it. Which of those is the canonical statement?
- **Auditor 3 (schema):** `schema/agentjobs-v2.yaml:404-508` has no LinkML rule for rule 6 (`queue_position` iff open) and the comment at `:396-402` predates it. Also `verify`'s docstring (`models_v2.py:418`) vs loops design L1.
- **Auditor 8 (MCP):** `replayed` hard-coded `False` for creates and `update_content` (`mutation_tools.py:411-414,619`) while `mcp.md:207-209` says it "says which happened" — is that a doc fix or a result-shape fix?
- **Auditor 11 (gate):** nothing in `scripts/check.py` runs or checks `scripts/regen-schema-docs.sh` output; the generated v2 docs happen to be current today by merge, not by check. Worth a stage, or a `--since-gate` rule that `schema/*.yaml` selects it?
- **Auditor 6 (CLI):** `agentjobs open` opens the browser before knowing whether `/app/` can be served (`cli.py:532-594`; `spa.py:77-87`). The missing-bundle sentence exists server-side; should `open`/`serve` print it?
- **Auditor 12 (security):** `mobile-access.md:3` and `api-reference.md:10-12` state "no authentication" plainly. The docs are honest; the question of whether tailnet membership is enough is yours.
- **Auditor 1 (context):** `README.md:96-103` and `docs/index.md:52-54` are the two entry points and both mislabel dispatch/loops/queue status in opposite directions. Does any static-context file an agent loads point at the dispatch design as current reference? (ALLAGENTS.md links it once, for the `-w` reproduction.)
