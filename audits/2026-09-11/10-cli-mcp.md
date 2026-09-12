# 10 — CLI and MCP

Auditor 10, Big Dawg Audit II, night of 2026-09-11 (written 2026-09-12 morning).
Scope: `cli.py`, `src/agentjobs/mcp/`, `dashboard.py`, `docs/mcp.md`, `docs/mcp-clients.md`,
`docs/mcp-integration-design.md`, `docs/tasks-shell.md`, the installed Claude Code plugin.
Main clone on `main` at `bb12f33f`. Nothing in the live store was written; every mutation
below ran against a throwaway project under this session's job directory, served on 8910
by a copy of the `review_queue_sandbox.py` pattern (`AGENTJOBS_HOME` redirected before any
import; `~/.agentjobs/databases/` listed before and after and unchanged). The sandbox
server was stopped with `agentjobs stop --port 8910` at the end.

Backlog searched first (`agentjobs list` from the repo root, then `agentjobs show` on each
hit). Records this file touches: task-053, task-063, task-129, task-196, task-255,
task-261, task-263, task-266, task-271, task-276, task-301, task-317, task-319, task-332,
task-381.

Summary of the surface: the MCP mutation path is in good shape for the errors it was
designed around (`revision_conflict` carries the current record, `unknown_actor` names the
vocabulary, `queue_move` refusals explain the band rule). The three things that are not
sound are (1) every authorization refusal since task-332 reaches an MCP agent as
`internal_error`, (2) the instruction text and skill still describe a YAML-in-git product
four days after the cutover and the test pins the stale sentence, and (3) the CLI's
lifecycle commands still identify "the server" by port number alone.

---

## F1 — P1 — Every task-332 authorization refusal, and every credential problem, reaches an MCP agent as `internal_error`

**Verified by running.**

`docs/authorization.md` and ALLAGENTS.md tell a dispatched run that "a 403 naming
`wrong_task` or `capability_denied` is that rule and not a bug; ask the human". Over MCP the
run never sees either name. The REST layer sends `{"code": "wrong_task", ...}`
(`src/agentjobs/api/authorization.py:141-142,171-173`); the MCP classifier passes REST codes
through by constructing `ErrorCode(code)`, and `ErrorCode` (`src/agentjobs/mcp/errors.py:22-36`)
has no member for any of them, so the `except ValueError` branch — marked
`# pragma: no cover - a code this build does not know` at
`src/agentjobs/mcp/mutation_tools.py:414-419` — rewrites the code to `internal_error`.

Ran `_service_error` over every denial code the server can emit
(`C:/Users/jpose/.claude/jobs/a3ef0c61/tmp/classify.py`):

```
wrong_task                 -> internal_error       retryable=False suggested=None
capability_denied          -> internal_error       retryable=False suggested=None
actor_mismatch             -> internal_error       retryable=False suggested=None
wrong_run                  -> internal_error       retryable=False suggested=None
expired_run_credential     -> internal_error       retryable=False suggested=None
unverified_run_credential  -> internal_error       retryable=False suggested=None
no_principal               -> internal_error       retryable=False suggested=None
identity_unresolved        -> internal_error       retryable=False suggested=None
403 with no code -> invalid_transition
409 with no code -> invalid_transition
500 with no code -> invalid_transition
503 with no code -> invalid_transition
```

End to end, against the sandbox server, with a minted run credential whose run the server's
startup reconcile had already marked `interrupted` (so the credential was revoked — the
ordinary "my run ended" case): every mutating tool returned

```
{"code": "internal_error", "message": "A run credential was presented and did not verify. ...",
 "retryable": false, "project_id": "sandbox-mcp", "task_id": "task-005"}
```

The raw REST body for the same request was `{"code":"unverified_run_credential", ...}`.

Two consequences for a dispatched run. First, the code it is told to branch on
(`internal_error`) is the one whose documented remedy is "check the MCP server's stderr
log" — the least actionable of the thirteen. Second, a 5xx with no structured body maps to
`invalid_transition`, non-retryable: a server restart mid-request that gets as far as
answering is reported to the agent as "your transition is invalid". (A refused connection
is handled correctly as `service_unavailable`; only an answered 5xx is misfiled.)

No test covers any of this: `grep -rn 'wrong_task\|capability_denied\|unverified\|internal_error' tests/test_mcp_mutation_tools.py tests/test_mcp_protocol.py tests/mcp_evals` is empty. `docs/mcp.md` lists eleven codes and none of the authorization ones.

**New.** Not on the backlog; task-332 shipped the codes and nobody wired the MCP side.

**Fix.** Add the eight principal/authorization codes to `ErrorCode` (or let an unknown REST
code pass through verbatim instead of being rewritten), map `status >= 500` with no code to
`service_unavailable` with `retryable: true`, give the 403 family a `suggested_action`
("this task is not yours; work your own task or say so on the record"), and add one test per
denial code asserting the MCP code equals the REST code. Delete the `pragma: no cover`.

---

## F2 — P2 — The MCP instruction text, the plugin skill and the docs still describe task YAML in git; the test reworked on 2026-09-10 pins the stale sentence

**Verified by running** (the text is in this session's own system prompt, twice) **and by reading.**

`src/agentjobs/mcp/instructions.py:16-18`, loaded into every MCP client at initialize:
"AgentJobs task YAML is generated state. … Reading task YAML is allowed." The store moved to
SQLite on 2026-09-07 (ENGINEERING.md, "Data Source"); there is no task YAML to read for this
project and `tasks/` is empty. The instruction's last edit is `628f124c` (2026-08-21).

`tests/test_mcp_server.py:347-352` asserts `"task YAML is generated state"` and
`"Reading task YAML is allowed"` survive a 512-character truncation. That file was reworked
in `7e19d69d` on 2026-09-10 ("rework the suite off the deleted file backend") and the
assertion survived the rework — so the test now enforces that the leading rule stays wrong.
"What would this have caught?" — nothing; it is the thing preventing the fix.

Same claim elsewhere: `plugins/agentjobs/skills/agentjobs/SKILL.md:12` ("AgentJobs stores
tasks as YAML in a git repository. The YAML is generated state."), `docs/mcp.md:4,105,279`,
`docs/mcp-clients.md:132,177,189`, `src/agentjobs/mcp/read_tools.py:250-252` (tasks_list:
"Task files that exist but cannot be loaded are returned in `broken`"), `:382-383`
(tasks_search: "Unreadable task files are reported alongside"). The plugin README in the
installed cache still says "the same fourteen tools"; the registry serves sixteen.

**New** as a finding about the instruction text and the test. task-271 noted the skill was
stale in August for different reasons (no `task_queue_move`); this is a second, larger
staleness on top of it.

**Fix.** Reword the leading rule around the record rather than the file ("Task records are
managed state. Read them with `task_get`; every write goes through these tools; there is no
file to edit") and change the test to assert the new rule's key phrases. Sweep the
`read_tools` descriptions for "file". A client that truncates at 512 characters sees the
first paragraph only, so that paragraph is the one to get right.

---

## F3 — P2 — `task_release` lets any actor release any holder's claim, and `task_claim` accepts the human `default_user`

**Verified by running.**

Sandbox: task-002 seeded as claimed by `codex`. As `claude`:

```
task_release(project_id="sandbox-mcp", task_id="task-002", actor="claude", body="not mine")
    OK | Released: task-002 is now Ready.
```

`manager.release_task` (`manager.py`, `apply` closure) checks only `lifecycle is ACTIVE`;
it never compares `actor` with `assignment.owner`. Over HTTP the owner principal is
unscoped, so a person can do this deliberately, which is fine; over MCP an agent that
mis-typed a task id releases a peer's in-flight work, and the peer's next write goes
through anyway (release does not bump anything the peer's `expected_revision` would catch
on `task_log_append`, which takes no revision).

```
task_claim(project_id="sandbox-mcp", task_id="task-004", actor="Jeff Posey", ...)
    OK | Claimed: task-004 is now In progress (Jeff Posey).
```

`routing.require_actor` accepts any id in the vocabulary regardless of `kind`; the schema
text says "never send the default_user" and nothing enforces it. `agentjobs create --actor`
defaults to the project's `default_user`, so a CLI create with no flag files an agent's task
under the human. `agentjobs work --agent <any string>` claims and closes with no vocabulary
check at all (inferred from `cli.py` `work`: `manager.claim_task(task.id, agent=agent)` then
`manager.close_task(... actor=agent)`; not run, because there is no non-mutating way to run it).

**Confirms task-263** (draft, unspecced since 2026-08-22): "agents cannot act as
default_user … the CLI requires --actor". The release-anyone's-claim half is **new** and is
a question as much as a defect — release-by-anyone may be wanted for stuck tasks, in which
case the MCP tool should require `body` and say in its description that it can take a task
from under someone.

**Fix.** Refuse `task_release` when `actor != assignment.owner` unless the caller is the
owner principal (over MCP: never). Refuse `kind: human` actors on every MCP mutation, which
is one line in `require_actor` since `ProjectSummary.actors` already carries `kind`.

---

## F4 — P2 — Reusing an `operation_id` on a different task performs a second write

**Verified by running.**

The instruction text promises "reusing an operation_id replays the original result instead
of writing twice". Sandbox, one UUID `3654a719…` used three times:

```
3a task_claim(task-001, op X)            OK | Claimed: task-001 is now In progress (claude).
3b task_claim(task-004, op X)            OK | Claimed: task-004 is now In progress (claude).
3c task_claim(task-001, op X) again      OK | Already applied: task-001 is now In progress (claude).
```

task-004's log afterwards carries a `transition` entry under operation id `3654a719` with a
different fingerprint. The ledger is per task record (`replay_or_conflict(task, operation)`
inside each verb's `apply`), so `operation_conflict` — which exists at
`api/routes/status.py:147-153` and in `ErrorCode` — fires only when the same id lands on the
same task with different arguments. An agent that retries a failed call after "fixing" the
task id, keeping the id it already generated, gets two claims and a success message for
each. The promise as worded is false; as implemented it is "per task".

**Confirms task-255** ("Make the operation_id contract true …", draft since 2026-08-22).

**Fix.** Either state the scope in the instruction text and tool descriptions ("an
operation_id is replayed within one task") or key the ledger per project so a cross-task
reuse is `operation_conflict` with the suggested action the REST layer already writes
("Use a fresh operation_id, or resend the original request").

---

## F5 — P2 — `status`/`stop`/`restart` identify the server by port number alone; `open` is the only command that probes

**Verified by running** (status, stop, list) **and by reading** (restart — not run, on purpose).

```
$ agentjobs status                    ->  ❌ No server running on port 8765.   (exit 1)
$ agentjobs status --port 8876        ->  ✓ Server is running (PID 298608) on http://localhost:8876
$ agentjobs stop --port 8910          ->  Stopping server (PID 457328) on port 8910...
                                          SUCCESS: The process with PID 457328 has been terminated.
```

- All three default to `8765` (`cli.py:730,754,770`) and never consult `api_base:` in
  `~/.agentjobs/dispatch.yaml`, which on this machine says `8876` and which `McpConfig.resolve`
  and `_mcp_base_url` (`cli.py:228`) already read. So `agentjobs status` on the machine that
  runs the dashboard says no server is running.
- `stop` and `restart` call `_stop_server`, which on Windows is `taskkill /F /PID` on
  whatever `_find_process_by_port` returned (`cli.py:676-720`). Nothing asks `/api/version`
  whether the listener is AgentJobs, whose `source_root` it serves, or who started it —
  the "which server is yours to restart" rule in ENGINEERING.md is enforced by nothing.
  `probe_api_base` exists and `open` uses it (`cli.py:880-905`); the other three do not.
- `restart` then runs `uvicorn.run` in the foreground on the port it just freed, or on a
  port it never freed ("Warning: the old server is still listening" is printed and it
  starts anyway) — task-196's second-generation incident.
- `_find_process_by_port` on Windows matches `f":{port}" in line`, a substring test
  (`cli.py:650`). Port 876 matches a line for 8765. Low likelihood, inferred, not run.

**Confirms task-261** on three of its four halves (status names nothing; stop/restart
refuse nothing; defaults ignore `gui.port`/`api_base`). **Refutes** its fourth: running
`agentjobs list` in an empty directory outside any project printed `No tasks found.`,
exit 0, and created no `tasks/` directory — verified by listing the directory before and
after. That half can be dropped from the record when it is specced. **Confirms task-196**
by reading (`restart` still starts unconditionally).

**Fix.** Make `status`/`stop`/`restart` resolve the port the way `agentjobs mcp` does
(explicit flag, `AGENTJOBS_URL`, `api_base:`, then 8765); have `stop`/`restart` call
`probe_api_base` and refuse a listener that is not AgentJobs or whose `source_root` is not
this checkout without `--force`; have `restart` start nothing when nothing was stopped.
`status` should print `source_root`, `source_commit`, `started_at` from `/api/version`,
which is the whole of task-261's ac-1.

---

## F6 — P2 — Plugin drift: the installed plugin is still the hand-edited 2026-08-17 snapshot

**Verified by running** (diff, md5, mtimes, `claude mcp list`).

`~/.claude/plugins/installed_plugins.json`: installed 2026-08-17T20:18Z from
`gitCommitSha c30ca8c0`, version 0.1.0, never updated. `diff -rq plugins/agentjobs <cache>/0.1.0`:

```
.codex-plugin/plugin.json   differ   (HEAD uses ./-relative paths, task-124)
.mcp.json                   differ   (cache carries env AGENTJOBS_URL=http://127.0.0.1:8876;
                                      neither c30ca8c0 nor HEAD has it; file mtime 18:57,
                                      install 15:18 — hand-edited)
README.md                   differ   (cache: "fourteen tools", no task-317 section)
hooks/task_write_guard.py   differ   (cache predates the redirect fix task-271 names)
skills/agentjobs/SKILL.md   differ
```

`git diff --stat c30ca8c0 HEAD -- plugins/` is 141 insertions across those five files.
`tests/test_claude_plugin.py:72-74` asserts `MANIFEST["version"] == __version__`; both are
`0.1.0`, so the equality is vacuous and always will be until somebody bumps either.

What changed since task-271 was filed: task-317 (`5012377c`, 2026-08-27) removed the port
from the repo's `.mcp.json` and made `McpConfig.resolve` fall through to `api_base:`, so
the repo copy would now work here *without* the hand edit. The installed copy still works
only because of the hand edit, which is why nobody noticed the cache never updated.

`claude mcp list`: both `plugin:agentjobs:agentjobs` and `agentjobs` report `✔ Connected`.

**Confirms task-271** (draft, unspecced). Its fix item 3 has landed via task-317 in the
repo; ac-1 (visible drift) and ac-3 (probe compares `source_commit`) have not. The compat
probe still reduces both versions to `(0, 1)` (`compat.py:47-59`) and ignores the
`source_commit` that `/api/version` reports.

**Fix.** Unchanged from the record: print `CLAUDE_PLUGIN_ROOT` and the cache's commit
against HEAD at MCP startup; bump the plugin version on any merge touching `plugins/`;
have the probe warn on `source_commit` mismatch. Immediate one-liner for this machine:
reinstall the plugin from the directory marketplace so the cache matches HEAD.

---

## F7 — P3 — Duplicate registration is real, both halves connect, and task-301's "only one survives" is wrong today

**Verified by running.**

Two servers named `agentjobs` are declared on this machine: `C:/projects/agentjobs/.mcp.json`
(project scope, pre-approved by `enabledMcpjsonServers: ["agentjobs"]` in `~/.claude/settings.json`)
and the plugin. They surface as `mcp__agentjobs__*` and `mcp__plugin_agentjobs_agentjobs__*` —
different prefixes, so both survive. This session's deferred-tool list carries all 32 names
and the MCP instruction block appears twice, verbatim.

task-301's handoff (2026-08-25) says of auditor 8's F11: "it does not [cost 30 declarations]
— the names collide and only one survives". **Refutes** that sentence on today's client:
the prefixes differ and nothing collides. (Both pointed at 8876 only after task-317, so at
the time one of the two failed to connect, which may be what task-301 observed.)

Cost, measured: one server's full tool declarations are 63,692 bytes (`dump_schemas.py`,
`d.model_dump(exclude_none=True)` over `registry.declarations()`), roughly 16k tokens if a
client loads them in full. Claude Code 2.1.238+ defers them (task-301 measured 3,795 tokens
for the whole MCP surface), so the live cost here is the duplicated instruction text
(~1.6 KB each) plus 32 names, not 32k tokens. Codex or any client that does not defer
pays the full doubled figure.

Also: the user allow-list in `~/.claude/settings.json:10-37` names 14 of 16 tools for each
server; `task_queue_move` and `playbooks_list` are missing on both, so those two prompt.

**Confirms task-319** (ready, low, "retire the root .mcp.json"). Is it intended? task-319
says no. But note task-202 gave every registered project its own `.mcp.json` precisely so a
*dispatched* session, which has no plugin, gets the tools; the plugin is a user-scope install
that only this machine's interactive sessions see. Retiring the root file may take the tools
away from dispatched runs in this repository — a question for auditor 04.

---

## F8 — P3 — Three-way parity: the CLI has no workflow verbs at all, and the runbook told auditors to use a command that does not exist

**Verified by running** (`agentjobs --help` and every subcommand's `--help`) **and by
reading** (API route table, MCP registry, `manager.py` public methods).

| Manager verb | CLI | HTTP | MCP |
|---|---|---|---|
| claim | ✗ (only inside interactive `work`) | ✓ | ✓ |
| handoff | ✗ | ✓ | ✓ |
| release | ✗ | ✓ | ✓ |
| close | ✗ (only inside `work`, always `completed`) | ✓ | ✓ |
| add_log_entry / add_progress_update | ✗ | ✓ `/log`, `/progress` | ✓ `task_log_append` |
| search_tasks | ✗ (`list` has no text filter) | ✓ `/search` | ✓ |
| update_task (content) | ✗ | ✓ PATCH | ✓ |
| delete_task / archive | ✗ | ✓ DELETE | ✗ (archive only as a `task_close` flag) |
| mark_deliverable_complete | ✗ | ✓ | ✗ |
| answer a question | ✗ | ✓ `/answer` | partial (`log_append` type `answer` with `re`) |
| promote | ✓ | ✓ | ✓ |
| queue move | ✓ | ✓ | ✓ |
| queue reprioritize | ✓ | ✓ | ✗ (`task_queue_move` refuses cross-band and says so) |
| queue check / repair / compact | ✓ | ✓ `/broken`, `/queue/repair`, `/queue/compact` | ✗ (read-side `queue_broken` only) |
| redact | ✓ | ✓ `/redact` | ✗ |
| quotations | ✓ | ✗ | ✗ |
| finish | ✓ | approve triggers it; GET `/finishes/{task}` | ✗ (correct — capability-denied anyway) |
| branches | ✓ | ✗ | ✗ |
| storage status/backup/verify/restore | ✓ | ✗ | ✗ |
| dispatch * | ✓ | ✓ enable/disable/runs/cancel | ✗ by design |
| playbooks | ✓ list/show/run/init | ✓ list/get | ✓ list only (by design) |

The MCP gaps are deliberate and documented (`docs/mcp.md:196-210`). The HTTP gaps
(`quotations`, `branches`, `storage`) are operator commands and defensible. The CLI gap is
the one that matters: an agent in a terminal with no MCP client cannot claim, hand off, log
or close except through `work`, which claims and closes `completed` in one sitting with no
handoff and no review (see F3). The `agentjobs` CLI is the surface ALLAGENTS.md quotes for
`redact`, `quotations`, `branches`, `queue move` — every one of those exists — and the one
it does not quote for the workflow, which does not.

**Confirms task-053** (ready since August: "CLI mirrors — inbox, next, claim, handoff, log,
close"). **task-063** (its umbrella) is `ball: agent / answer` with an answer logged
2026-09-06 that reads, in effect, "nothing left — promote shipped, close it", and it is
still open. Somebody should close it or move the ball.

Also: this audit's runbook (`audits/2026-09-11/PLAN.md`, shared preamble) tells fourteen
auditors to "search it for your system — `agentjobs search`". There is no such command;
`agentjobs list` has no query flag and no `--project` flag. Every auditor either discovered
this or used the dashboard. **New, P4**, for auditor 14.

---

## F9 — P3 — Error ergonomics: the good, and the four refusals that leave an agent guessing

**Verified by running** against the sandbox, through `validate_arguments` + handler +
`failure()` — the same path `server.call_tool` takes minus the STDIO transport.

Good, and worth saying so in one line each: `revision_conflict` names both timestamps,
says "Nothing was written", and carries `current_task`; `unknown_actor` lists the
vocabulary and points at the agent ids; `unknown_project` lists the projects; `task_not_found`
says to call `tasks_list`; `task_queue_move` before-itself and cross-band refusals explain
the rule; `task_next` names every task passed over and why.

The ones that do not tell an agent what to do next:

1. **Invalid `(ball, reason)` pair on `task_handoff`**: `"{'ball': 'human', 'reason': 'work',
   'prompt': 'x'} is not valid under any of the given schemas"` — a `oneOf` failure that
   never names the five valid pairs. `suggested_action` says re-read the schema, which is
   6.7 KB. Print the pairs.
2. **Claim on a held task**: `invalid_transition`, "Task 'task-002' is not available to
   claim (it is in progress (codex), owned by codex)" — correct, no `suggested_action`
   ("call task_next"). Minor.
3. **Garbage `expected_revision`** (`"yesterday"`): `invalid_input` with the pydantic
   sentence and no `field_errors`, so the agent cannot see which field. Minor.
4. **`operation_id` is not validated as a UUID**: `"op-1"` was accepted and written to the
   log. The instruction text says "caller-generated `operation_id` UUID". Either enforce
   `format: uuid` or stop saying it.

Two schema facts an agent learns only by failing: `expected_revision` is required on
`promote`, `handoff`, `close`, `update_content` and `queue_move`, and `task_handoff` takes
`target: {ball, reason, prompt}` rather than the record's own `ball`/`ball_reason`/
`ball_prompt` names. Both are reasonable designs; neither is in the instruction text, which
is the only thing a truncating client shows. One sentence there would save every session
one failed call.

**New.** Ergonomics; no record.

---

## F10 — P3 — `task_close` closes a draft as `completed`

**Verified by running.** task-003 seeded as `draft`; `task_close(outcome="completed")`
succeeded: "Closed: task-003 is now Completed." A draft has by definition no finished spec,
so "completed" is a claim nobody made. `cancelled`/`superseded`/`duplicate` are the outcomes
that make sense from `draft`. Policy question rather than defect; the state machine in
`docs/task-schema.md` should say which outcomes each lifecycle may close to, and the verb
should enforce it. **New.**

---

## F11 — P3 — `agentjobs work` is a CLI path around the review gate

**Inferred from reading** (`cli.py` `work`: `get_next_task` → `claim_task(agent=…)` →
`typer.confirm("Close the task as completed?", default=True)` → `close_task(outcome=
completed)`). Any `--agent` string, no vocabulary check, no handoff, no `expected_revision`,
default answer "yes". It predates schema v2 and is the only claim/close the CLI has. When
task-053 lands, retire it or make it hand off instead of close. **New**; adjacent to
task-053 and task-263.

---

## F12 — P4 — Smaller observations

- `docs/mcp.md` error table lists 11 codes; `ErrorCode` has 13 (`unknown_actor`,
  `internal_error` undocumented) and none of the authorization family (F1). Reading.
- `docs/tasks-shell.md` is about the React Tasks surface (two regions, one route), not the
  shell. It belongs to auditor 11's brief; I did not audit it beyond confirming that.
- `dashboard.py` is a 234-line projection module shared by the React API and legacy Jinja
  views. Not audited beyond its header; nothing in this brief's questions touches it.
- The CLI's top level still carries YAML-era commands — `validate`, `migrate`,
  `migrate-schema`, `load-test-data` — beside `storage import`. `load-test-data` prints
  "These are files. To make them a backlog: 'agentjobs storage import …'", so it knows.
  task-381 is the neighbouring record. Reading.
- The plugin's `PreToolUse` guard (`hooks-claude.json`) protects `tasks/` YAML in a project
  whose records are rows. task-129 (guard protects nothing on an empty registry) and
  task-276 (content-based false positives) are both still open and both still describe the
  installed copy; I did not re-test the false positive, but this file was composed outside
  the repository and copied in on the runbook's advice, which is itself the workaround
  task-276 describes.
- The MCP process's startup probe (`compat.py`) still keys on `(major, minor)` only;
  `/api/version` reports `source_commit` and `api_digest` and the probe reads neither.
  Reading; task-271 ac-3.

---

## What I did not get to

- **The `wrong_task` path end to end over HTTP with a live credential.** The sandbox
  server's reconcile marks a run with no live session `interrupted` and revokes its digest;
  I re-minted twice and gave the run the server's own pid, and the server still refused the
  credential. The classifier result in F1 is verified by running the classifier on the real
  REST codes, and the end-to-end evidence is for `unverified_run_credential`; `wrong_task`
  itself is inferred through the same branch. Someone with time should reproduce it from
  `tests/test_run_authorization.py`'s `dispatched()` fixture rather than a hand-built run.
- **`agentjobs restart` and `agentjobs open`.** Neither was run: `restart` kills by port
  and `open` opens a browser tab. F5's `restart` claims are from reading.
- **The STDIO transport.** Every MCP call here went through the registry in-process. The
  protocol tests exist (`tests/test_mcp_protocol.py`); I did not re-run them.
- **Codex plugin and `docs/mcp-clients.md`'s Gemini/Cursor sections.** Not checked at all.
- **`docs/mcp-integration-design.md`** (629 lines). Read only the sections the code cites.
- **`agentjobs project mcp-setup`** and `_write_mcp_entry`. Read, not run.
- **The content-based guard false positive** (task-276). Not re-tested tonight.

## Questions for other auditors

- **03 (authorization):** does anything on the REST side test that a denial body's `code`
  survives to a client other than `TestClient`? F1 shows the MCP client discards all of them.
- **04 (dispatch/finish):** if task-319 retires the root `.mcp.json`, does a dispatched
  session in this repository still get MCP tools? `enabledMcpjsonServers` and task-202
  suggest the root file is what dispatch reads, and the plugin is user-scope.
- **04:** the sandbox server's startup reconcile revoked my run's credential within a
  second of starting because the run had no live session. Is that also what happens to a
  real run when the *server* restarts while the run is mid-task — does the run's next MCP
  call then fail with F1's `internal_error`?
- **09 (API/webhooks):** task-255 says a replay should report `replayed: true`; the
  cross-task reuse in F4 reported `replayed` false on a genuinely new write. Is the
  same-task different-arguments case (`operation_conflict`) covered by a test on your side?
- **14 (meta):** the runbook's `agentjobs search` (F8) — worth a line in the next runbook.
- **01 (regression sweep):** task-261, task-263, task-271, task-276 are all still unspecced
  drafts from the first audit and all four are confirmed tonight; task-301's "only one
  survives" is refuted.
