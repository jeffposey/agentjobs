# 08 — MCP server

Auditor 8, Big Dawg Audit (task-242), 2026-08-22. System: `src/agentjobs/mcp/`, the
instruction text it serves, the plugin bundle under `plugins/agentjobs/`, and the two
registrations this machine runs (`agentjobs` from the repo's gitignored `.mcp.json`,
`plugin:agentjobs:agentjobs` from the Claude Code plugin cache).

Method: read every module in the package (~2,800 lines of source), the REST routes and
client code the facade depends on, the MCP tests and evals; ran an **offline** schema
probe (`build_registry` against a never-contacted `TaskClient`, then
`validate_arguments`) to capture exact refusal text; made **live read-only calls** on the
running server (8876) plus three mutation-tool calls that are refused before any write
(unknown project; unknown actor; nonexistent task id, which 404s inside the manager
before `mutate_task` opens anything). Nothing was written. `scripts/check.py` was not run.

Severity key: P1 bites now · P2 should fix · P3 improvement · P4 observation.

## Findings, ranked

| # | Sev | Finding |
|---|---|---|
| F1 | P2 | `task_next` reports its own winner as `actionable: false` — the summary invents `False` when the `/next` route omits the computed field |
| F2 | P2 | `task_create_*` and `task_update_content` bypass the structured error envelope: conflicts come back as `invalid_transition`, a stale revision returns no `current_task` |
| F3 | P2 | `replayed` is hard-coded `False` on the same two tools while the served instructions and `docs/mcp.md` promise it "says which happened" (Auditor 2's question — answer below: result-shape fix at the REST layer) |
| F4 | P2 | The installed Claude Code plugin is a hand-edited snapshot from 2026-08-17: its guard lacks the `d2ef033` fix, its skill predates the queue, its `.mcp.json` was edited in the cache to reach 8876, and nothing can detect the drift because the version has never left `0.1.0` |
| F5 | P3 | "Never adopt `default_user`" is requested in four places and enforced in none — `Jeff Posey` is accepted as the actor on a mutation tool |
| F6 | P3 | `operation_id` is documented as a UUID at every layer and validated as a UUID at none |
| F7 | P3 | `oneOf` refusals (`target`, `placement`) tell the agent only "is not valid under any of the given schemas" |
| F8 | P3 | A forged `data.operation` marker on `task_log_append` is refused with code `invalid_transition`; `_classify`'s "umbrella task" branch is dead code |
| F9 | P3 | The startup version probe compares a constant; the one field that would reveal skew (`source_commit`) is served and ignored |
| F10 | P3 | Doc drift: plugin README says fourteen tools / nine mutations; `docs/mcp.md` lists three `expected_revision` verbs where there are five; the leading instruction names four state verbs where there are five |
| F11 | P4 | Dual registration costs every session in this repo two tool lists (30 tools) and two instruction blocks |
| F12 | P4 | `tasks_list`/`tasks_search` fetch the whole project and truncate client-side; `limit` saves tokens, not work |
| F13 | P4 | The REST 400 from a Pydantic failure carries no `code` or `field_errors`; MCP classifies it `invalid_input` by status alone and loses the field path |

---

## 1. Tool inventory vs manager verbs

Fifteen tools registered, in this order (offline probe, matches `test_mcp_protocol.py:310`
"Serving 15 tool(s)"): `projects_list, tasks_list, task_get, tasks_search, task_next,
task_create_draft, task_create_ready, task_promote, task_claim, task_release,
task_handoff, task_close, task_log_append, task_update_content, task_queue_move`.

Parity against `TaskManager` (`manager.py`) and the REST routes (`api/routes/`):

| Manager verb | REST | MCP | Note |
|---|---|---|---|
| `create_task` | `POST /tasks` (bare Task, no envelope) | `task_create_draft` / `task_create_ready` | lifecycle pinned per tool; REST accepts `lifecycle` in body |
| `promote_task` | `POST /{id}/promote` | `task_promote` | |
| `claim_task` | `POST /{id}/claim` | `task_claim` | |
| `release_task` | `POST /{id}/release` | `task_release` | |
| `handoff` | `POST /{id}/handoff` | `task_handoff` | |
| `close_task` | `POST /{id}/close` | `task_close` (`archive` flag) | |
| `add_log_entry` | `POST /{id}/log` | `task_log_append` | |
| `add_progress_update` | `POST /{id}/progress` | — (use `type: progress`) | fine |
| `update_task` | `PATCH /{id}` (bare Task) | `task_update_content` | both allowlist; neither exposes `lifecycle`/`ball`/`queue_position` |
| `move` | `POST /{id}/queue-move` | `task_queue_move` | |
| `reprioritize` | `POST /{id}/reprioritize`, `client.operations.reprioritize` | — | MCP can only re-band via `patch.priority`, which the manager intercepts and drops at the bottom of the new band (`manager.py:1023-1068`) |
| `repair_queue` / `compact_band` | `POST /queue/repair`, `/queue/compact` | — | MCP's `queue_broken` action points at the CLI; acceptable |
| `archive_task` | `DELETE /{id}` | — (`task_close archive=true` covers open tasks) | archiving an already-closed task has no MCP path |
| `delete_task` | no route found | — | manager-only (`manager.py:1072`); who calls it is outside this brief |
| `mark_deliverable_complete` | `PATCH /{id}/deliverables/{path}` | — | |
| `record_dispatch*` | dispatch router | — | correct: manager-written |
| human review (`approve`…`reject`) | `POST /{id}/approve` etc. | — | correct: human identity |
| webhooks CRUD | `/webhooks` | — | correct |

**Anything MCP can do that the verbs forbid: none found.** Every MCP mutation lands on a
manager verb through the same REST route the CLI/GUI use; `test_mcp_server.py:548`
asserts the package never imports `TaskManager`/`TaskStorage`, and it does not.

**The opposite direction is where the gaps are**, and they are all on the two tools that
do *not* go through `status.py::_run` — see F2/F3.

One wording mismatch: the leading instruction says "its place in line moves through
`task_queue_move` or not at all" (`instructions.py:23-24`), but `task_update_content
{patch: {priority}}` moves a task to the bottom of another band. Design §5.3 sanctions
this; the sentence does not mention it. P4, folded into F10.

## 2. Contract enforcement — what the agent actually receives

| Case | Layer that refuses | Code | Text the agent sees | Would a confused agent know what to do? |
|---|---|---|---|---|
| wrong `project_id` | MCP `routing.resolve_project` (404 from `/projects/{id}`) | `unknown_project` | "Unknown project 'no-such-project'. Projects on this service: agentjobs, fantasy-football, job-hunting, mastercalls, product-strategy." + action "Call projects_list and use one of the ids it returns." | **Yes.** (live) |
| actor not in vocabulary | MCP `routing.require_actor` before any HTTP write; REST `acting_actor` is the authority behind it | `unknown_actor` | "'gpt-5' is not an actor in project 'agentjobs'. Configured actors: Jeff Posey, claude, codex." field_errors `[actor]`, action "Use the agent actor you are running as. Agents configured in 'agentjobs': claude, codex." | **Yes.** (live) |
| actor = `default_user` | **nobody** | — | The call proceeded to `task_not_found` | **No — it is not refused.** See F5. (live) |
| reused `operation_id`, different payload, on the 8 `_run` verbs | manager `replay_or_conflict` → `_classify` | `operation_conflict` 409 + action "Use a fresh operation_id, or resend the original request." (`status.py:133-140`) | Yes |
| same, on `task_create_*` / `task_update_content` | manager raises the same `OperationConflictError`; route wraps it in a bare `HTTPException(409, detail=...)` (`tasks.py:425-426, 468-469`) → client `exc.code is None` → `mutation_tools._service_error` falls through to `INVALID_TRANSITION` (`mutation_tools.py:320-325`) | `invalid_transition`, retryable false | The *message* still says "Nothing was written. Use a new operation_id, or resend the original request exactly" — an agent reading prose recovers; one branching on the code does the wrong thing. See F2. | Partly |
| stale `expected_revision` on the 8 verbs | `check_revision` → `_classify` | `revision_conflict` + `current_task` + "Re-read the task, decide again, and resend." | Yes |
| stale `expected_revision` on `task_update_content` | same manager check, same bare 409 wrap | `invalid_transition`, **no `current_task`** | `docs/mcp.md:213-216` says "the current task comes back so you can decide again". It does not, for this tool. | No |
| handoff with no `prompt` | MCP JSON Schema (`oneOf`) | `invalid_input` | "{'ball': 'human', 'reason': 'review'} is not valid under any of the given schemas" field `target`, action "Re-read the inputSchema…" (offline probe) | Only by re-reading the schema. See F7. |
| handoff `human/work` or `agent/available` | same | same opaque text | same |
| `placement: {position: 300}` | same | "{'position': 300} is not valid under any of the given schemas" | same |
| `patch: {lifecycle: closed}` | schema `additionalProperties: false` | `invalid_input`, "Additional properties are not allowed ('lifecycle' was unexpected)" path `patch` | Yes |
| `type: handoff` on log append | schema enum | lists the six authored types | Yes |
| `limit: 0` | schema | "0 is less than the minimum of 1" path `limit` | Yes |
| nonexistent task on claim | manager `_mutate` → 404 | `task_not_found` + "List the project's tasks to see the ids it holds." | Yes (live) |
| `data: {operation: …}` on log append | **schema passes**; `operations.stamp` raises `ValueError` → `_classify` catch-all | `invalid_transition` | Misclassified; see F8 |

### F5 (P3) — `default_user` adoption is requested, not enforced

Evidence: `task_claim(project_id=agentjobs, actor="Jeff Posey", task_id=task-999999-…)`
returned `task_not_found`, i.e. the actor passed both checks. `routing.require_actor`
compares against `project.actor_ids` (`routing.py:101-102`), which includes humans;
REST `acting_actor` explicitly documents "an agent verb need not match default_user.
Any configured actor may claim or log" (`status.py:192-194`); `validate_actor` has no
kind check (`actors.py:194-213`). The prohibition lives only in prose:
`routing.py:41-42` (schema description), `read_tools.py:196`, `SKILL.md`, and
`PROJECT_SUMMARY_SCHEMA.default_user.description`.

The consequence is the exact attribution failure `actors.py:7-13` was written to
prevent, and the log is append-only. Fix: in `require_actor`, refuse `kind == human`
for mutation tools (and for `task_next`, which takes `actor` to filter eligibility) with
`unknown_actor` plus an action naming the agent ids — and the REST layer should gain the
same check for the six agent verbs, since the MCP copy is advisory by its own docstring
(`routing.py:83-86`). Rejected alternative: leaving it to the skill text, which the
standalone-MCP and Gemini rows of the "what protects what" table never load.

### F6 (P3) — `operation_id` "UUID" is enforced nowhere

`OPERATION_ID_SCHEMA` says `"format": "uuid"` (`mutation_tools.py:36-45`);
`validate_arguments` builds `Draft202012Validator(schema)` with no `format_checker`
(`server.py:91`), so format is ignored — offline probe: `operation_id: "not-a-uuid"`
passed. REST: `SafeMutationRequest.operation_id: Optional[str]` and the queue models
`min_length=1` (`api/models.py:212, 382`). Live: `"not-a-uuid-at-all"` reached the 404.
Collision risk is bounded by the fingerprint check (a reused id with a different payload
conflicts), so this is honesty rather than safety: either add `FormatChecker()` and
accept the refusal, or drop `format` and say "opaque string, unique per attempt".

### F7 (P3) — `oneOf` refusals explain nothing

jsonschema's message for a failed `oneOf` is the literal instance plus "is not valid
under any of the given schemas". For `task_handoff.target` the agent is never told the
three holders, their reason vocabularies, or that `prompt` is required; for `placement`
it is never told the four shapes. Fix: when `problem.validator == "oneOf"` in
`validate_arguments`, substitute a message built from the branch `const` values and
required keys. The handler already has a good hand-written one for `placement`
(`mutation_tools.py:640-643`) — it is unreachable because the schema fires first. What
this would have caught: the first `human/work` attempt by every new agent.

### F8 (P3) — forged replay marker misclassified; dead branch in `_classify`

`task_log_append` schema allows any `data` object. `operations.stamp` refuses a
caller-supplied `operation` key with `ValueError` (`operations.py:134-141`);
`status.py::_classify` has no case for it and falls to `invalid_transition`
(`status.py:160`). Fix at the schema: `"data": {"type": "object", "properties":
{"operation": false}}` makes it `invalid_input` with a path. Separately,
`_classify` matches `"umbrella task"` in the message (`status.py:151`) to emit
`dependency_blocked`; task-164 removed that refusal and the string now appears only in
a field description (`models_v2.py:714`) — decoration, and substring-matching on
messages is the fragile pattern the rest of the error code avoids.

## 3. Instruction-text audit

Every claim in `instructions.py`, and the claims in `docs/mcp.md` that describe the
server's behaviour:

| Claim | Verdict | Evidence |
|---|---|---|
| "pass `project_id` to every task tool" | enforced | schema `required` on 14 tools; `require_project_id` (`routing.py:47-57`) |
| "use only claim, handoff, release, and close to move workflow state" | true but incomplete | `task_promote` also moves lifecycle and its own description calls itself "the only exit from draft" (`mutation_tools.py:753-757`). An agent obeying the leading rule literally will not promote. F10 |
| "actor from the project's configured vocabulary" | enforced, with two holes | humans accepted (F5); unconfigured project accepts any id (documented, `routing.py:88-90`) |
| "caller-generated `operation_id` UUID" | presence enforced; UUID not | F6 |
| "reusing an operation_id replays the original result instead of writing twice" | true for 8/10; **unreported** for 2/10 | `replay_or_conflict` in every verb; `_find_created_by` for create (`manager.py:821-841`); `update_task` replays via `apply → None → storage returns current` (`storage.py:462-463`). Neither of those two routes reports it. F3 |
| "There is no generic status, lifecycle, position or YAML setter, and none is coming" | true at both surfaces | `CONTENT_FIELDS` (`mutation_tools.py:96-109`) and `TaskUpdateRequest` (`api/models.py:345-365`) lack all three; probe confirmed `lifecycle` in patch is refused. Note for Auditors 3/4: `manager.update_task` still carries a lifecycle-reopen path (`manager.py:1026-1030`, "this generic patch is the only path that can produce one") that no HTTP route can reach today |
| "`task_next` returns the queue's answer, the band and position it won on, and every task passed over with the rule that excluded it" | **verified live** | winner task-214 at high/62; skipped: task-233 "not ready (active, held by agent)", task-211 "has 6 open children" |
| "Disagree with the order by calling `task_queue_move`" | exists; placement-only | `QUEUE_PLACEMENT_SCHEMA`; probe refused `{position: 300}` |
| "`task_get` returns the whole record … spec, current ball_prompt, binding decisions, open questions, dependencies" | by code | `task_document` strips only computed fields (`summaries.py:95-99`); log passes through. Not exercised live on a real task (see "did not get to") |
| `docs/mcp.md:24-26` "never opens a task file and never imports TaskManager or TaskStorage; a test asserts that" | true | `test_mcp_server.py:543-572` AST walk |
| `docs/mcp.md:29-31` "refuses to run against one that is missing or version-skewed" | missing: yes; skewed: vacuous | F9 |
| `docs/mcp.md:150` "Serving 15 tool(s)" | true | probe; `test_mcp_protocol.py:310` |
| `docs/mcp.md:207-209` "the result's `replayed` field says which happened" | false for 2/10 | F3 |
| `docs/mcp.md:213` "`task_handoff`, `task_close` and `task_update_content` also take `expected_revision`" | incomplete | `task_promote` and `task_queue_move` require it too (`mutation_tools.py:759, 889`). F10 |
| `docs/mcp.md:214-215` "the current task comes back" | false for `task_update_content` | F2 |
| `docs/mcp.md:277` "plugin version tracks the package version and a test enforces it" | vacuous | both `0.1.0` since the first commit; `test_claude_plugin.py:74` would fire only on a bump that has never happened |
| `docs/mcp.md:231` `operation_conflict` "With a new id" | unreachable for create/update | F2 |
| `plugins/agentjobs/README.md:9,166-170` "fourteen tools … nine mutation" | stale | 15 / 10; `task_queue_move` missing from the list. F10 |
| `docs/mcp.md:109-110` "the project-scoped one is the one whose address a machine can correct without editing an installed plugin's cache" vs `README.md:178-181` "override that variable in your client's MCP configuration" | contradict each other | the cache on this machine *was* edited (F4), which is the evidence for the first sentence and against the second |

### F9 (P3) — the compat probe compares a constant

`compat.check_version` reduces `0.1.0` to `(0, 1)` on both sides (`compat.py:47-59`).
The package has been `0.1.0` for its entire history, so the check has never had anything
to catch and the "names both versions" refusal in `docs/mcp.md:286` has never fired
outside its unit test. Meanwhile `/api/version` reports `source_commit` and
`started_at` (live: `17fdfd0`, `2026-08-22T04:36:42Z`) and the probe reads neither
(`compat.py:125-135`). Right now there is no skew — `git log 17fdfd0..HEAD -- src` is
empty — but the hazard is documented in this very corpus: task-233's `ball_prompt`
describes a server holding pre-merge `dispatch/runner.py` in memory, and
`client._parse_task`'s tolerant-enum path exists because task-024 hit exactly this
through MCP. Fix: the MCP process imports the same editable install, so it can compare
the service's `source_commit` to the importing tree's HEAD and warn on mismatch, and
refuse when `source_root` differs. Rejected alternative: bumping the version on every
merge — it would make the check real but nothing else in the release process wants
that yet.

## 4. Dual registration

What actually runs on this machine, from the two `.mcp.json` files, `pip show`, and the
plugin ledger:

| | `agentjobs` (project-scoped) | `plugin:agentjobs:agentjobs` |
|---|---|---|
| config | repo `.mcp.json` (gitignored, `.gitignore:74`) | `~/.claude/plugins/cache/agentjobs/agentjobs/0.1.0/.mcp.json` |
| command | `…/virtualenvs/agentjobs-KSKY4Ymk-py3.13/Scripts/python.exe -m agentjobs.cli mcp` | `agentjobs mcp` → `C:/Users/jpose/AppData/Local/Programs/Python/Python313/Scripts/agentjobs.exe` |
| source the process imports | the venv's editable install → `C:/projects/agentjobs/src` | global Python 3.13 editable install (`agentjobs.pth` = `C:/projects/agentjobs/src`, `direct_url.json` editable) → **same tree** |
| `AGENTJOBS_URL` | 8876 | **8876 in the cache; 8765 in the repo** (`plugins/agentjobs/.mcp.json`, unchanged since `c30ca8c`) |
| guard / skill | none (project-scoped entry is server only) | the cache's copies, dated 2026-08-17 |

So the **server** halves are the same code: two interpreters, one `src/`, both serving
whatever is on disk at spawn. Both instruction blocks in this session's system prompt
are byte-identical, which is consistent with that.

### F4 (P2) — the installed plugin is a stale, hand-edited snapshot, and nothing can tell

`installed_plugins.json`: installed 2026-08-17T20:18Z from `gitCommitSha c30ca8c`,
version `0.1.0`. `diff -rq` between `plugins/agentjobs` and the cache:

- `.mcp.json` differs: cache says 8876, repo and `c30ca8c` both say 8765. Somebody edited
  the cache (mtime 18:57, four hours after the install's 14:54 files). Nothing listens
  on 8765 (`curl 127.0.0.1:8765/api/version` → exit 7; task-154 made that port
  deliberately dead). **Without that edit the plugin server fails at startup** — the
  probe is correct to refuse, but the session then has only the project-scoped tools
  plus the skill and guard of a plugin whose server is dead.
- `hooks/task_write_guard.py` differs: the cache predates `d2ef033` "a redirect writes
  to its target, not to every path named", so the installed guard still denies
  `head tasks/x.yaml 2>/dev/null`. The fix on `main` is not what runs.
- `skills/agentjobs/SKILL.md` differs: no `task_queue_move` row, no "work what task_next
  returns", no `queue_broken` guidance, no `-w` warning — everything `628f124`/`cd3d2f7`
  added.

Nine commits since `c30ca8c` touch `plugins/` or `src/agentjobs/mcp/`. The version is
still `0.1.0`, the cache directory is keyed by version, and the equality test in
`test_claude_plugin.py:74` is the only "what keeps them from diverging" mechanism —
it is satisfied by two numbers that never move. The honest answer to brief item 4:
**the MCP server cannot diverge (editable installs on one tree); the guard and skill
have already diverged, and the only thing that would surface it is a version bump
nobody makes.**

Fix, cheapest first: (1) a startup stderr line from the MCP server naming
`CLAUDE_PLUGIN_ROOT` when set, and the cache's `gitCommitSha` vs HEAD, so drift is at
least visible; (2) bump the plugin version on every merge that touches `plugins/` —
`build_release.py` is the place, Auditor 11's system; (3) for the port: either have
`plugins/agentjobs/.mcp.json` omit `AGENTJOBS_URL` so the process falls back to
`AGENTJOBS_API_BASE`/`dispatch.yaml` (the CLI already resolves that in
`_mcp_base_url`, `cli.py:149-167`; `McpConfig.resolve` does not), or document that a
non-8765 machine must edit the cache and will lose the edit on update. I could not
verify whether Claude Code honours a per-plugin env override — the README asserts it;
the hand edit is evidence that on this machine it was not relied on.

### F11 (P4) — two servers, one tree, double the context

This session's tool list carries 30 `agentjobs` tools and two identical instruction
blocks (~1.2k chars each). `docs/mcp.md:106-110` calls that "not a conflict", which is
true, but every interactive session in this repository pays for the duplicate, and
`.claude/settings.json:10-19` allow-lists only the `mcp__agentjobs__*` names, so the
plugin-prefixed copies prompt. Recommend: in this clone, rely on the project-scoped
entry and the plugin's guard/skill only — which needs a way to disable one plugin MCP
server without uninstalling the plugin; if Claude Code has none, that is the reason to
keep the duplicate, and Auditor 1 should count it in the static bundle.

## 5. Schema quality — validation living in the wrong layer

| Place | Schema accepts | Handler/service rejects | Finding |
|---|---|---|---|
| `operation_id` | any non-empty string (`format` unchecked) | nothing rejects | F6 |
| `task_log_append.data` | `{operation: …}` | `operations.stamp` → `ValueError` → `invalid_transition` | F8 |
| `task_handoff.target`, `task_queue_move.placement` | — | hand-written refusals in the handlers (`mutation_tools.py:495-502, 637-658`) are unreachable: the schema fires first with a worse message | F7 |
| `task_get.task_id` | `""` (no `minLength`) | `_require_task_id` | trivial; add `minLength: 1` for consistency with every other tool |
| `task_create_*` | `dependencies[]`, `acceptance[]` etc. as arbitrary objects | REST `Dependency`/`Branch` models validate; a bad shape returns the F13 bare 400 | the agent gets "dependencies.0.type: …" as a message with no `field_errors` |
| `tasks_list.limit` | enforced in schema | re-enforced in `_limit` | duplicate but harmless |
| `MUTATION_RESULT_SCHEMA.replayed` | "True when this operation had already been applied" | never true for two tools | F3 |

The strong parts are real: `additionalProperties: false` everywhere; the
holder/reason discriminated union makes `human/work` unrepresentable (probe confirms);
`CONTENT_FIELDS` omits the axes rather than rejecting them; `AUTHORED_LOG_TYPES` is
derived from the model so a new manager-written type cannot become postable
(`mutation_tools.py:87-91`).

### F1 (P2) — `task_next` says its winner is not actionable

Live payload: `"task": {"id": "task-214", …, "actionable": false, "unmet_needs": [],
"open_children_count": null}` alongside `"explanation": "task-214 is ready, eligible
for claude, and has no unmet needs."` Cause: `GET /next` returns a bare stored `Task`
(`tasks.py:195-208`, `response_model=Optional[Task]`) with no computed fields;
`task_summary` then does `summary["actionable"] = value if value is not None else
False` (`summaries.py:51-52`). The comment nine lines below explains why
`open_children_count` was changed to pass `None` through — "this line invented a
plausible number for the missing field" — and `actionable` was left inventing one.
An agent that filters on `actionable` (the field's purpose) drops the queue's answer.
Fix: same treatment (`None` when absent, schema `["boolean","null"]`), or have
`/next` answer with `TaskRead` so the facts are computed. What would have caught it: a
test asserting on the *rendered* `actionable` of a `task_next` result — the existing
tests build summaries from hand-written records that already carry the field.

### F12 (P4) — `limit` saves tokens, not work

`tasks_list` and `tasks_search` call `read_tasks`/`read_search` for the whole project,
build every summary, then `limited()` slices (`read_tools.py:224-235, 359-365`); each
also makes a second round trip for `read_broken_tasks`. On a 240-task project that is
three parses-worth per call. Acceptable today; worth knowing before anyone cites `limit`
as a performance control.

## Answer to Auditor 2's question

> `replayed` is hard-coded `False` for creates and `update_content`
> (`mutation_tools.py:411-414, 619`) while `mcp.md:207-209` says it "says which
> happened" — doc fix or result-shape fix?

**Result-shape fix, at the REST layer; the doc is wrong today and should say so until
then.** The chain:

1. The manager detects replay for both: `create_task` returns the existing task from
   `_find_created_by` (`manager.py:800-804`); `update_task`'s `apply` returns `None` on
   `replay_or_conflict` and `storage.mutate_task` hands back the current task unchanged
   (`storage.py:454-463`). The conflict half is also detected (`OperationConflictError`
   from both).
2. The routes discard the knowledge: `POST /tasks` and `PATCH /{id}` return a bare
   `Task` and never go through `status.py::_run`, which is where `replayed` is
   *measured* (log length before/after, `status.py:220-256`) and where errors get the
   envelope. `api/models.py:580-583` says the envelope is "returned only when a request
   asks for it with `?envelope=true`" — these two routes do not accept it.
3. The client therefore types them as returning `Task`, with a docstring that
   rationalises it: "a retry that resolves to the original task is indistinguishable
   from the original call, which is the entire point" (`client.py:961-963`).
4. MCP hard-codes `False` with a comment repeating the rationale
   (`mutation_tools.py:411-414`), and `test_mcp_mutation_tools.py:355-371` asserts only
   that a retried create returns the same id — it never asserts `replayed`, so the
   test passes either way.

The rationalisation is wrong for the reason `api/models.py:582-585` gives for the
field's existence: "a caller retrying after a timeout has no way to tell 'I did that'
from 'you already had'", and a create is the case where that matters most — it is the
one whose id the caller does not know until the answer arrives. The cost of the fix is
small because the measurement already exists: accept `?envelope=true` on both routes,
run them through `_run` (for create, "replayed" is "`_find_created_by` hit", which the
manager can return as a flag or `_run` can measure by whether the returned task's
creation entry was already on disk), and have the client's `create`/`update_content`
return `MutationResult` like the other eight. That closes **F2** at the same time,
since `_run` is what produces `operation_conflict`/`revision_conflict` with
`current_task`. Until it lands, `docs/mcp.md:207-209` should read "eight of the ten
mutation tools; `task_create_*` and `task_update_content` always report `false`" —
a doc that promises a field the caller is meant to branch on is worse than one that
admits the gap.

## Examined, nothing found

- Stdout hygiene: `protocol_stdout` hands the real fd to the transport and points
  `sys.stdout` at stderr; `configure_logging(force=True)` + `captureWarnings`
  (`server.py:40-79`). Tests cover both directions.
- Startup never starts a service and never falls back to another URL
  (`compat.probe_service`).
- Timeout bounded at 300s with a refusal above it (`config.py:20-24, 74-81`).
- Error payloads omit absent keys rather than nulling them; `ERROR_SCHEMA` is validated
  against every code (`test_mcp_server.py:231`).
- `open_children_count` null-vs-zero: fixed in `461dccc` and the fix is in place.
- `task_next` on an empty answer distinguishes four causes (`_explain_no_work`). Not
  exercised live because the project had a winner.
- Ten accepted eval scenarios run against a real service
  (`tests/mcp_evals/scenarios.py`, `test_mcp_protocol.py:389-435`), including
  refuse-direct-lifecycle and invalid-handoff-and-close. They are the strongest tests
  in this system.
- The write guard's "prints nothing on allow" rationale (`README.md:55-63`) is correct
  about Claude Code's `permissionDecision` semantics.

## What I did not get to

- **Did not call `task_get` on a real task** or any mutation tool that could succeed;
  the resumption-payload claim is verified from `summaries.task_document` only.
- **Did not reproduce `operation_conflict` or `revision_conflict` live** (both need a
  prior real write). The code paths are traced; F2's misclassification is from reading
  `tasks.py` and `mutation_tools._service_error`, not from a captured response.
- **Did not verify whether Claude Code honours a per-plugin MCP env override** (the
  README's claim at `plugins/agentjobs/README.md:178-181`). Stated as unverified in F4.
- **Did not audit the guard's decision tables** (`task_write_guard.py`, 18k) beyond the
  one diff hunk; that is its own afternoon and was not in the brief.
- **Did not check `TaskLockTimeout` handling on `POST /tasks` / `PATCH /{id}`** —
  `status.py` maps it to retryable `lock_timeout` for the eight `_run` verbs; the two
  bare routes catch only `ValueError`/conflict types, so a lock timeout there is either
  a 500 or a `ValueError` subclass — not read.
- Codex plugin manifest/hook parity: read the manifests, did not diff Codex's entry
  point against Claude's.
- `tests/mcp_evals` artifact-reproducibility claim (`test_mcp_protocol.py:435`): not
  read.

## Questions for other auditors

- **Auditor 7 (API):** F2/F3 are really yours — `POST /tasks` and `PATCH /tasks/{id}`
  are the two mutation routes outside `_run` and outside the error envelope. Does
  anything else (the GUI's create form, the CLI) depend on their bare-Task shape such
  that adding `?envelope=true` must stay opt-in? Also: `handle_validation_error`
  (`main.py:258-275`) returns `{"detail"}` with no `code` — is that the "one consistent
  envelope" your item 1 asks about, or the exception?
- **Auditor 3 (schema):** `manager.update_task` keeps a lifecycle-reopen path no route
  can reach (`manager.py:1026-1030`). Is that the fifth way to move an axis without a
  verb, or is it dead? And `acting_actor` accepting humans on agent verbs (F5) — is
  "kind is resolved from config" (D4) meant to be checked anywhere?
- **Auditor 1 (context):** the dual registration costs 30 tool declarations plus two
  identical ~1.2k-char instruction blocks per session in this repo (F11). Did your
  static-bundle measurement include MCP tool schemas? They are not in the @-chain but
  every session pays them.
- **Auditor 10 (dispatch):** `.claude/settings.json` allow-lists `mcp__agentjobs__*`
  only, and `runner.py:356` passes `enabledMcpjsonServers` for the project-scoped
  server. In a `--bg` run, does the plugin's second server prompt, fail silently, or
  get used with the stale skill from F4?
- **Auditor 11 (gate/tooling):** is there any place a plugin version bump would happen
  (`build_release.py`)? F4 and F9 both reduce to "0.1.0 forever makes every version
  check vacuous".
- **Auditor 12 (security):** F5 in your framing — a tailnet peer can write log entries
  attributed to `Jeff Posey` through either MCP or REST with no check, and the log is
  append-only. Also F8: the schema lets a caller *attempt* to forge a replay marker; the
  manager refuses, but nothing records the attempt.
- **Auditor 5 (queue):** `task_next` returned `actionable: false` for its own winner
  (F1). Does `/next/explain` or `queue list` compute `actionable` differently, so the
  three surfaces disagree about the same task?
