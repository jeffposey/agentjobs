# 06 — CLI

Auditor 6, Big Dawg Audit (task-242). Night of 2026-08-21 / morning of 2026-08-22.
Scope: `src/agentjobs/cli.py` (1881 lines, Typer) and the command examples in
`docs/quickstart.md`. Read-only; every command below that I ran is a read (`--help`,
`show`, `status`, `next --why`, `queue check`, `project list`, `validate`, a `work`
dry-run against a directory with no task files, and one `list` in my own scratchpad).
HEAD at time of audit: `f56c9ae`. The dashboard on 8876 reports `source_commit
17fdfd0`; the seven commits since are all under `tasks/` and `audits/`, so it is current.

## Summary

| Sev | # | Finding |
|---|---|---|
| P2 | 1 | `open` opens the browser before it knows `/app/` can be served — and a fresh clone following the quickstart cannot serve it |
| P2 | 2 | `restart`/`stop`/`status` identify "the server" by port only; with default flags `restart` on this machine starts the second-server-on-8765 that ENGINEERING.md names as an incident class |
| P2 | 3 | `agentjobs validate` exits 1 with 221 problems on this repository's own corpus; the gate's real-corpus test deliberately tolerates the same rules. The documented pre-commit hook would refuse every commit |
| P2 | 4 | Quickstart step 3 walks off a cliff: `create` makes a draft, `list --lifecycle ready` shows nothing, and `work` reads `./tasks` instead of the configured tasks directory |
| P2 | 5 | Read-only commands (`list`, `show`, `next`, `queue *`) silently `mkdir tasks/` in whatever directory they run in, and never say "this is not an AgentJobs project" |
| P2 | 6 | `promote`, `queue move`, `queue reprioritize` attribute an unnamed caller to `default_user` — a human — which is exactly what the MCP surface forbids an agent to do |
| P3 | 7 | `serve`/`open`/`status`/`stop`/`restart` default to 8765 and ignore the `gui.port` that `init` asked for and wrote |
| P3 | 8 | `_find_process_by_port` is a substring match: port 8765 matches a listener on 87650 or 18765 |
| P3 | 9 | Exit codes: `migrate` reports `Failed: N` and exits 0; `open` exits 0 after "Server may not have started" |
| P3 | 10 | No `agentjobs --version`, while `/api/version` and `probe_api_base` exist |
| P3 | 11 | The CLI has no `claim`/`handoff`/`release`/`close`/`log` verbs; the only CLI claim path is the interactive `work`, which a script cannot drive |
| P3 | 12 | `open` on Windows combines `CREATE_NEW_CONSOLE` with `CREATE_NO_WINDOW`; the server's stderr (including the wrong-checkout banner) goes to a console nobody is looking at |
| P3 | 13 | The `open` tests mock away the two things that can go wrong, so they could not have caught finding 1 |
| P4 | 14 | `work --priority` is free text with hand-listed choices; every other command uses the enum |
| P4 | 15 | `tasks/test-data/` is tracked (7 files) and invisible to the server; `load-test-data` defaults there |

Answer to Auditor 2's question is in its own section after the findings.

## 1. Command inventory and parity

Twenty-three top-level entries in `agentjobs --help` (live output, 2026-08-22), three of
them sub-apps with 4 + 11 + 6 commands. Mapped against `manager.py` public methods,
the `@router` decorators under `src/agentjobs/api/routes/`, and the MCP tool names in
`src/agentjobs/mcp/read_tools.py` / `mutation_tools.py`.

| CLI | Manager / module | REST | MCP |
|---|---|---|---|
| `init` | `project_setup.initialize_project` | `POST /api/projects/init` | — |
| `project add` | `ProjectRegistry.add` | `POST /api/projects` | — |
| `project list` | `ProjectRegistry.list_projects` | `GET /api/projects` | `projects_list` |
| `project remove` | `ProjectRegistry.remove` | **none** (no DELETE route in `projects.py`) | — |
| `project mcp-setup` | `project_setup.ensure_mcp_server_entry` | — | — |
| `serve` / `restart` / `stop` / `status` / `open` | process management only | `GET /api/health`, `GET /api/version` (not used by these commands) | — |
| `validate` | `validation.validate_corpus` | `GET /tasks/broken` covers only the "unreadable" subset | — |
| `mcp` | `mcp.server.run` | — | *is* the MCP server |
| `create` | `create_task` (lifecycle **draft**, no actor) | `POST /tasks` | `task_create_draft`, `task_create_ready` |
| `list` | `storage.load_all` + filter | `GET /tasks` | `tasks_list` |
| `show` | `get_task` | `GET /tasks/{id}`, `/detail` | `task_get` |
| `next [--why]` | `get_next_task`, `explain_next` | `GET /tasks/next`, `/next/explain` | `task_next` |
| `promote` | `promote_task` | `POST /tasks/{id}/promote` | `task_promote` |
| `work` (interactive) | `get_next_task` → `claim_task` → `close_task` | `/claim`, `/close` | `task_claim`, `task_close` |
| `queue list` | `queue_listing` | `GET /queue` (carries `problems`, `api/models.py:517`) | — |
| `queue move` | `move` | `POST /tasks/{id}/queue-move` | `task_queue_move` |
| `queue reprioritize` | `reprioritize` | `POST /tasks/{id}/reprioritize` | — |
| `queue check` | `check_queue` | `GET /queue` `.problems` | — |
| `queue repair` | `repair_queue` | `POST /queue/repair` | — |
| `queue compact` | `compact_band` | `POST /queue/compact` | — |
| `attachments [--orphans]` | `storage.attachments.orphans` | — | — |
| `load-test-data` | `storage.save_task` direct | — | — |
| `migrate-schema` | `migrate_schema.migrate_corpus` | — | — |
| `migrate` (Markdown→YAML) | `migration.migrate_tasks` | — | — |
| `finish` | `dispatch.finish.finish_task` | started by `POST /tasks/{id}/approve` when `finish.enabled` | — |
| `dispatch enable/disable` | `dispatch.config.set_project_enabled` | `POST /api/dispatch/enable` and `/disable` | — |
| `dispatch run` | `dispatch.guards.dispatch_task` | `POST /tasks/{id}/dispatch` (writes its own authorising entry; CLI requires a human's — documented at `cli.py:1001-1016`) | — |
| `dispatch status` | `ledger.list_runs/live_runs` | `GET /api/dispatch/runs` | — |
| `dispatch cancel` | `ledger.cancel` | `POST /api/dispatch/runs/{id}/cancel` | — |
| `dispatch stop/reconcile/reap/auth-check/config/example` | ledger / config | partial: `GET /api/dispatch` | — |

**Exists in only one surface:**

- *CLI-only:* `project remove`, `project mcp-setup`, `validate` (full rule set),
  `attachments`, `load-test-data`, `migrate-schema`, `migrate`, `finish` (by hand),
  `dispatch stop/reconcile/reap/auth-check/example`; `queue reprioritize` has REST but
  no MCP. Mostly operator tooling; reasonable.
- *Manager verbs with no CLI:* `claim_task` and `close_task` (only inside interactive
  `work`), `handoff`, `release_task`, `add_log_entry`, `add_progress_update`,
  `update_task`, `delete_task`, `archive_task`, `mark_deliverable_complete`,
  `search_tasks`. See finding 11.
- *REST-only:* approve / request-changes / answer / redirect / hold / resume / reject,
  webhooks, `/search`, `/dashboard`, `/revision`, deliverables PATCH, task DELETE,
  attachment GET, run tail/output.
- *MCP-only relative to CLI:* `task_handoff`, `task_release`, `task_log_append`,
  `task_update_content`, `tasks_search`.

## 2. Exit codes

Read every `raise typer.Exit` and every early `return` in `cli.py`. Failure paths exit
non-zero in the large majority; the table is the exceptions and the ambiguous cases.

| Command | Condition | Exit | Evidence |
|---|---|---|---|
| `migrate` | some files failed (`failed > 0`) | **0**, prints "✓ Migration complete!" | `cli.py:1821-1830` — no branch on `failed` |
| `open` | server never appeared after 10 s | **0**, warns, opens browser anyway | `cli.py:579-591` |
| `open` | server already running but stale / not AgentJobs / no bundle | **0** | no probe at all, see §3 |
| `restart` | kill of old PID failed | continues to `uvicorn.run`; bind fails inside uvicorn | `cli.py:517-518` |
| `stop` | nothing on the port | 0 | `cli.py:460-462` (fine for an idempotent stop) |
| `status` | nothing on the port | 1 | `cli.py:484-486`; confirmed live: `❌ No server running on port 8765.` exit 1 |
| `next` | nothing claimable | 0; broken queue → 1 | `cli.py:1645-1650`; confirmed live exit 0 with an answer |
| `queue check` | problems | 0 unless `--strict` | documented in the docstring, `cli.py:1557-1562` |
| `queue list` | problems | 0, renders + reports | documented, `cli.py:1451-1454` |
| `validate` | any finding | 1 | `cli.py:326-332`; confirmed live (finding 3) |
| `work` | no task / declined | 0 | `cli.py:790-792, 805-807` |
| `show` | unknown id | 1 | confirmed live: `Task 'task-999' not found.` exit 1 |
| `mcp` | config error | 2; otherwise `run_mcp`'s code | `cli.py:394-396` |
| `finish` | declined / escalated / done | 2 / 1 / 0 | `cli.py:1866-1877` — the one command with a documented three-way code |
| `dispatch auth-check` | a stall found | 1 | `cli.py:1157`; tested in `test_dispatch_cli.py:256-267` |
| wildcard `--host` | any server command | 2 (BadParameter) | tested, `test_cli.py:179-190` |

`migrate` is the only outright wrong one (P3, finding 9). `open`'s zero after a failed
start is the same defect as finding 1 seen from the exit-code side.

Test coverage of exit codes: 22 + 9 + 29 `exit_code` assertions across the three CLI
test files. `restart`, `stop` and `status` have no tests beyond the wildcard-binding
refusal (`grep "def test_" tests/test_cli.py` — 18 tests, none named for them).

## 3. `serve` / `open` / `restart` and the "not yours to restart" doctrine

**Finding 2 (P2). The CLI identifies "the server" by port number and nothing else.**

- `_find_process_by_port` (`cli.py:422-447`) parses `netstat -ano` / `lsof` and returns
  the first listening PID. It does not ask the PID what it is.
- `stop` (`cli.py:450-474`) and `restart` (`cli.py:491-529`) `taskkill /F` that PID.
  `agentjobs stop --port 8876` kills the dashboard the launcher started; `agentjobs stop
  --port 5173` kills a Vite dev server; neither prints anything other than "Stopping
  server (PID n)".
- `status` (`cli.py:477-488`) prints the PID and `http://localhost:{port}` and exits 0.
  Live: `agentjobs status --port 8876` → `✓ Server is running (PID 294284)`. It cannot
  distinguish a fresh AgentJobs, a stale one, one serving the wrong checkout, or a
  different program.
- **`restart` with default flags on this machine reproduces the incident ENGINEERING.md
  warns about.** Nothing listens on 8765 (confirmed: `status` exit 1), so `restart`
  skips the kill and runs `uvicorn.run(..., port=8765)` in the foreground of the calling
  shell (`cli.py:520-529`). That is the second server "nobody restarts" from
  GLOBAL-AGENTS.md and ENGINEERING.md's Merge Gate step 6 — documented as a hazard in
  two places, enforced nowhere. `restart --port 8876` is worse: it kills the
  launcher-managed dashboard (and, through the tsnet proxy, Jeff's phone UI) and
  replaces it with a process tied to the agent's shell.
- The probe that would fix all three already exists: `dispatch.address.probe_api_base`
  (`address.py:253-278`) GETs `/api/version` with proxies bypassed and reports
  *answered* and *is_agentjobs* separately; `dispatch config` uses it
  (`cli.py:1239-1269`). `/api/version` returns `source_root`, `source_commit`,
  `started_at` (live output above), which is enough to say "stale" too.

**Fix:** one helper, `_describe_server(host, port)`, that runs `probe_api_base` and
returns (pid, is_agentjobs, source_root, source_commit, started_at). `status` prints
it. `stop`/`restart` refuse when `is_agentjobs` is false unless `--force`, and print
`source_root` before killing so an agent sees it is about to kill a server it did not
start. `restart` with no listener on the port should say "nothing to restart on
{port}; `serve` starts one" rather than starting one — an operator who typed *restart*
did not ask for a new foreground server.

**`open` when the server is already running** (`cli.py:586-591`): prints the PID and
opens the browser. Stale, wrong checkout, non-default port, or not AgentJobs at all
are all invisible. See the Auditor 2 section for the start-up path.

## 4. Help text drift

Examined every `--help` (top level, three sub-apps, and eleven leaf commands, live).

- **Finding 7 (P3). The printed defaults are not the project's defaults.** `init` asks
  "Port" (`cli.py:219`) and writes `gui.port`; `serve`, `restart`, `open`, `status`,
  `stop` all print `[default: 8765]` and use it. The only command that reads
  `gui.port` is `project mcp-setup` (`cli.py:905`). A user who answered `9000` at init
  gets a server on 8765 and an MCP entry pointing at 9000. This is the literal
  "defaults printed that aren't the defaults" case from the brief.
- **Finding 14 (P4).** `work --priority` is `TEXT` with help "(high, medium, low,
  critical)" and a hand-rolled `Priority(priority.lower())` (`cli.py:761-786`); every
  other command declares `Optional[Priority]` and Typer prints the choices. Same
  command: `--storage-dir [default: ./tasks]` — see finding 4.
- `validate`'s docstring (`cli.py:288-291`): "it is the check that works in CI and in a
  clean clone". On this clone it exits 1 — finding 3.
- `create --help` does not say the task is born a draft. Combined with quickstart step
  3 this is finding 4.
- `serve`/`restart` `--host`/`--port` have no help string at all (`cli.py:253-254`,
  `493-494`); trivial, but the wildcard refusal (`cli.py:399-419`) is a rule a user will
  hit and the help does not hint that `0.0.0.0` is refused.
- `init --port` help says "Default port for the web UI" — true of the config file,
  false of the CLI (finding 7).
- Top-level help lists commands in registration order: `migrate` (the legacy
  Markdown→YAML importer) sits between `promote` and `finish`; `load-test-data`
  between `attachments` and `work`. No grouping, no "legacy" marker. P4, not tabled.
- **No `--version`.** Top-level options are `--install-completion`,
  `--show-completion`, `--help`. Finding 10.

## 5. Error text for the five likely mistakes

**a. Unregistered / wrong project directory.** Two behaviours depending on which half
of the CLI you hit.

- Registry-based commands (`dispatch run`, `finish`): `ProjectRegistry.resolve_default`
  (`projects.py:194-222`) raises with text that names the fix — "Run 'agentjobs project
  add <path>' or 'agentjobs init'" — and for the ambiguous case lists the known ids.
  Good.
- cwd-based commands (`list`, `show`, `next`, `promote`, `queue *`, `validate`,
  `create`): `_load_config` returns `DEFAULT_CONFIG` when there is no
  `.agentjobs/config.yaml` (`cli.py:111-117`) and `_resolve_tasks_dir` **creates**
  `tasks/` (`cli.py:137`). **Finding 5 (P2).** Demonstrated in my scratchpad: an empty
  directory, `agentjobs list` → `No tasks found.` exit 0, and a `tasks/` directory now
  exists. The user is told nothing; the next `create` will happily write a task into a
  directory no server serves. **Fix:** `_load_config` raises (or `_build_manager`
  refuses) when neither a config nor a registry match exists, with the same sentence
  `resolve_default` already uses; and read-only commands must not `mkdir`. Half the CLI
  also lacks `--project`, so an agent in a worktree under `C:/projects/worktrees/`
  (outside every registered root) gets the DEFAULT_CONFIG path for `next`, `queue`,
  `promote` — a silent empty answer rather than the worktree's project.

**b. Bad task id.** `show task-999` → `Task 'task-999' not found.` exit 1 (live).
`promote`/`queue move` catch `ValueError` (`TaskNotFoundError` subclasses it,
`manager.py:273`) and print the manager's sentence. Adequate; none of them suggest
`agentjobs list`. `create --id` accepts any string — no id-shape validation found in
`storage.py`/`models_v2.py` (`grep` for a pattern found only `generate_task_id`), so
`create --id foo` writes `foo.yaml`; whether that later validates is Auditor 3's area.

**c. Invalid transition.** `promote` on a non-draft: "Task 'x' is not a draft (it is
active); only a draft can be promoted." (`manager.py:1366-1369`), exit 1. Names the
state, names the rule. Good. `work` is the exception: `claim_task` and `close_task`
raise `ValueError` (`manager.py:1205-1218, 1412`) and `work` catches nothing
(`cli.py:809, 819`), so a dependency refusal arrives as a traceback. `work` also calls
`get_next_task` without catching `QueueCorruptionError` (`cli.py:788` vs `next`'s
handler at `1645`), so on a broken queue `work` tracebacks where `next` explains.

**d. Stale server.** Nothing in the CLI can see it. `status` says running;
`open` says "Server already running (PID n)"; ENGINEERING.md's prescribed diagnosis
("dozens of validation errors naming fields that no longer exist") is something the
user discovers in the browser. `/api/version` carries `started_at` and
`source_commit`; `status` could print both and a one-line "started before the newest
change under src/ — restart" the way the launcher's `Test-RestartNeeded` does
(`open-agentjobs.ps1`). Covered by the fix in §3.

**e. Wrong checkout.** The server refuses to start with a banner on stderr
(`api/main.py:103-126`). Via `serve` in a terminal the user sees it. Via `open` on
Windows the child is spawned with `CREATE_NEW_CONSOLE | CREATE_NO_WINDOW`
(`cli.py:563-568`); `open` polls the port for 10 s, prints "Warning: Server may not
have started successfully." and opens the browser on a connection-refused page
(`cli.py:577-591`). The banner — the one piece of text that names the fix — is in a
console window the user may never see. (Per the Win32 process-creation-flags
documentation `CREATE_NO_WINDOW` is ignored when combined with `CREATE_NEW_CONSOLE`;
I did not test which window appears on this machine. Either way `open` does not
capture or relay it.) Findings 1 and 12.

## 6. `agentjobs validate` on the real corpus — finding 3 (P2)

```
$ poetry run agentjobs validate
...
❌ 221 problem(s) across 240 task file(s).    exit=1
   124 [unknown-actor]   84 [unknown-category]   13 [non-canonical-serialization]
```

`tests/test_validate.py::TestRealCorpus` (line 659-671) runs the same
`validate_corpus` on the same directory and *deliberately filters* to structural rules,
with a docstring explaining the taxonomy drift is real but "fixing a hundred historical
records is its own task". The CLI has no such filter and no severity flag, so:

- The 12 doc references to `agentjobs validate` (docs/mcp.md, mcp-clients.md,
  task-schema.md, agent-workflow.md, README) all describe a command that is red on the
  project that ships it.
- `agentjobs validate --install-hook` writes a pre-commit hook that runs
  `validate --staged` — which runs the full corpus check first (`cli.py:309`) and only
  then the receipt check. On this repository the hook would refuse every commit. It is
  not installed (`.git/hooks/pre-commit` absent), which is presumably why nobody noticed.
- The 13 `non-canonical-serialization` files are all early records (task-031 … 057),
  consistent with hand edits before the managed-write gate existed — not evidence of a
  current writer drifting.

**Fix (pick one, record the decision):** (i) give `Finding` a severity and have the CLI
exit 1 only on structural findings by default, `--strict` for taxonomy — matching the
test; or (ii) add the missing categories/actors to `.agentjobs/config.yaml` and run a
one-off canonical rewrite of the 13 files so the CLI goes green as-is. (ii) is a task
file change and belongs to a task, not this audit. Either way the docstring's CI claim
should be true before it is quoted again.

## 7. Quickstart command examples — finding 4 (P2)

`docs/quickstart.md` step 3:

```bash
agentjobs create --title "Ship REST layer" --category engineering --priority high
agentjobs list --lifecycle ready
agentjobs work --agent codex
```

- `create` calls `manager.create_task` with no `lifecycle`, whose default is
  `Lifecycle.DRAFT` (`manager.py:762`). The next line lists `ready` and prints
  `No tasks found.`; `work` prints `No tasks available`. Nothing in the quickstart
  mentions `promote`. A new user concludes the tool is broken.
- `work` reads `--storage-dir` default `./tasks` (`cli.py:764-777`) and never consults
  `tasks_directory` from config, unlike every other task command which goes through
  `_resolve_tasks_dir` (`cli.py:132-146`). On this repository `tasks_directory` is
  `tasks/agentjobs` and `tasks/*.yaml` does not exist, so `agentjobs work --agent x`
  prints `No tasks available` (run live, exit 0) while `agentjobs next` on the same
  cwd answers `task-214`. Any project that took a non-default tasks dir at `init` gets
  the same contradiction.
- `--category engineering` is not in this project's category list, which `validate`
  would then flag (finding 3) — fine for a generic quickstart, but it means the
  quickstart's own example produces a record `validate` rejects.
- Step 2 `agentjobs open` after step 1's `poetry install`: see the Auditor 2 section —
  there is no bundle in a fresh clone.

**Fix:** `work` should use `_build_manager` like everything else; the quickstart should
either `create` then `promote`, or show `--lifecycle ready` if `create` grows one (MCP
already has `task_create_ready`; the CLI has no way to create a ready task).

## 8. Actor attribution — finding 6 (P2)

`_resolve_actor` (`cli.py:1739-1756`) falls back to `config.default_user` — here
`Jeff Posey` — for `promote`, `queue move`, `queue reprioritize`. The docstring argues
"an unattributed state change is worse than a refused one"; a *misattributed* one is
worse than both, and that is what an agent gets by omitting `--actor`. The MCP surface
says the opposite in its tool description: "Never adopt default_user as your own actor"
(`mcp/read_tools.py:194-196`; `mcp/routing.py:14, 42`). Dispatched agents are told to
use MCP, but the CLI is what an agent falls back to when the MCP entry is missing
(`cli.py:887-890` says exactly this). **Fix:** no fallback for agents — require
`--actor`, or fall back only when stdin is a TTY (a human at a keyboard), and print who
the action was recorded as. Auditor 8 owns whether MCP *enforces* its prohibition; the
CLI clearly does not.

## 9. Smaller findings

- **Finding 8 (P3)** `_find_process_by_port`: `f":{port}" in line and "LISTENING" in
  line` (`cli.py:434`). `python -c "print(':8765' in 'TCP 0.0.0.0:87650 0.0.0.0:0
  LISTENING 1')"` → `True`. Fix: split the line, take the local-address column,
  `rsplit(":", 1)[1] == str(port)`.
- **Finding 11 (P3)** The ALLAGENTS.md lifecycle is claim → log → handoff → close, and
  none of those exists as a CLI verb; `work` is the only claim/close path and it is
  gated on `typer.confirm` (`cli.py:805, 814`), so it cannot be scripted. Whether the
  CLI *should* be a full agent surface is a product decision (MCP is the intended one),
  but the quickstart presents the CLI as one, and `project mcp-setup`'s own docstring
  admits agents "quietly fall back to the CLI". Record the decision either way.
- **Finding 13 (P3, decoration)** `test_open_targets_react_app_on_existing_server` and
  `test_open_starts_installed_python_module_without_poetry` (`test_cli.py:149-176`)
  patch `_find_process_by_port` and `webbrowser.open`, and assert the URL string. What
  they would catch: a typo in `/app/`. What they cannot catch: everything in finding 1.
- **Finding 15 (P4)** `tasks/test-data/` is tracked (7 files) and, since `TaskStorage`
  globs non-recursively (`storage.py:575`) and config points at `tasks/agentjobs`, no
  server ever serves it. `load-test-data` defaults to writing there
  (`cli.py:703-706`). Looks like the deliberate outcome of task-042; if so a README line
  in that directory would stop the next auditor asking.
- `init` on Windows leaves the hook non-executable silently (`cli.py:358-361`); git on
  Windows runs hooks through sh anyway, so harmless, noted only.
- `load_test_data` and `work` bypass `_build_manager` and construct `TaskStorage`
  directly; `create` builds its own manager rather than calling `_build_manager`
  (`cli.py:606-609`) — three copies of the same four lines.

## Auditor 2's question: should `open`/`serve` print the missing-bundle sentence?

**Yes, both, and `open` should not open the browser until it has checked.** Evidence:

1. The sentence exists only inside three 404 handlers (`api/spa.py:37-87`): the user
   sees it as a JSON body in a browser tab, after the browser is already open. The
   asset mounts use `check_dir=False` (`spa.py:28, 33`) so startup is silent when the
   directory is absent; `api/main.py` never mentions the bundle.
2. `open` (`cli.py:532-591`) decides "ready" by `_find_process_by_port` — a socket is
   listening — then `webbrowser.open(app_url)`. It never requests anything.
3. The fresh-clone case is real, not hypothetical: `frontend_dist/` has 0 tracked files
   (gitignored); `poetry install` has no build hook (`pyproject.toml` has only the
   default `build-system` table); `docs/quickstart.md:10-28` goes clone → `poetry
   install` → `agentjobs init` → `agentjobs open` with no `npm run build` anywhere in
   the file. So the quickstart's second command lands on
   `{"detail":"React frontend bundle is missing from the package; run npm run build
   in frontend/ ..."}` — the right sentence, in the wrong place.
4. The same gap covers the wrong-checkout refusal (§5e): `open` cannot tell "server
   refused to start" from "server slow to start" and opens the browser either way.

**Concrete shape:**

- `spa.py`: `def bundle_status(dist) -> Optional[str]` returning the existing sentence
  when `index.html` is missing, used by the three handlers so the text stays single-
  sourced.
- `serve`/`restart` (and the `lifespan` in `main.py`): call it at startup and print the
  sentence to stderr as a warning. Do not refuse — the REST API and MCP are fully usable
  without a bundle, and refusing would break headless installs.
- `/api/version`: add `frontend_bundle: "present" | "missing"`.
- `open`: after the port appears, `probe_api_base(server_url)` (already written,
  `address.py:253`); if it does not answer → exit 1 with the stderr the child produced
  (capture it instead of `DEVNULL`/`CREATE_NO_WINDOW`); if it answers but
  `is_agentjobs` is false → exit 1 naming the port; if `frontend_bundle` is missing →
  print the sentence and exit 1 *without* opening a browser. Only then
  `webbrowser.open`. Same probe on the "already running" branch, so a stale or foreign
  server is named before the tab opens.
- Optional: `open` reads `gui.port` from the cwd's config as its default (finding 7),
  so the quickstart's `init` port and `open` port agree.

That turns finding 1, finding 2's `status` half, and the exit-0-on-failure half of
finding 9 into one change of roughly fifty lines, all in `cli.py`/`spa.py`/`health.py`.

## Examined, nothing found

- Wildcard-bind refusal (`cli.py:399-419`): correct for `0.0.0.0`, `::`, `[::]`, `*`,
  `+`, and tested for all three server commands.
- Output-encoding guard (`cli.py:52-78`): sound; `validate`'s 221 lines with emoji
  came through a pipe without a `UnicodeEncodeError`.
- `_scope_one_invocation` (`cli.py:89-108`): one corpus snapshot per invocation,
  closed via `call_on_close`; not measured, but the structure is right.
- `queue` sub-app: exit semantics are documented in each docstring and match the
  code; `next` exits 1 on a broken queue and names `REPAIR_COMMAND`; live queue is
  sound (`queue check` → no problems; `next --why` answered task-214 and explained two
  skips).
- `dispatch run`'s refusal of agent-clocked tasks and its "Agent told AgentJobs is at"
  line (`cli.py:1039-1044`): present and reasoned in the docstring.
- `dispatch example --write` refuses to overwrite; `dispatch stop` is argument-free as
  claimed.
- `finish` exit codes 0/1/2 match its docstring and ENGINEERING.md.
- `validate --install-hook` refuses to clobber an existing hook that lacks the line.

## What I did not get to

- I did not run `serve`, `open`, `restart`, or `stop` — they are mutating (they start or
  kill processes). Findings 1, 2 and 12 are from reading the code plus `status` and
  `/api/version` probes; the Windows creation-flag behaviour is from the documented flag
  semantics, not observed here.
- I did not run `promote`, `queue move`, `create`, `work` to a claim, `migrate`,
  `migrate-schema`, or `load-test-data` — all write. Error texts for transitions are
  quoted from `manager.py`, not from a terminal.
- `dispatch *` commands: inventoried and read, not exercised; Auditor 10 owns them.
- `client.py` parity (the Python client in quickstart step 4): listed its methods but
  did not map them; Auditor 7.
- The legacy `migrate` (Markdown→YAML) command's correctness beyond its exit code.
- I did not read `tests/test_cli_projects.py` beyond counting its assertions.
- Whether Typer's `Priority` enum options accept case variants on the shell (Typer
  enum matching is exact by default) — possible friction for `--priority High`,
  untested.

## Questions for other auditors

- **Auditor 3 / 4:** `create --id` accepts any string; is there any id-shape validation
  in storage or the model, or will `create --id foo` write `foo.yaml` that
  `filename-id-mismatch` later rejects?
- **Auditor 3:** the 13 `non-canonical-serialization` files (task-031 … 057) — is a
  canonical rewrite of them on anyone's list, and would a rewrite change semantics or
  only byte layout?
- **Auditor 8:** the MCP tool text says "Never adopt default_user"; is that enforced in
  `routing.py` (refuse `actor == default_user` from an MCP client) or only requested?
  The CLI does the opposite by default (finding 6), so if MCP only requests it, neither
  surface protects Jeff's name.
- **Auditor 7:** `/api/version` is the natural place for `frontend_bundle` and a
  "started before newest src/ mtime" hint; does anything else consume that model such
  that adding fields needs the generated client regenerated?
- **Auditor 11:** the gate never runs `agentjobs validate`; given finding 3, is that
  deliberate (the test is the gate's form of it) or an omission that would have caught
  the CLI/test divergence?
- **Auditor 2:** `docs/installation.md` "Contributor setup" never says `npm run build`
  either — `scripts/check.py` builds as a side effect of its `build` stage, which is
  the only reason a contributor ends up with a bundle. Worth a sentence there.
- **Auditor 12:** `stop`/`restart` will `taskkill /F` any PID on any port the caller
  names, from any cwd; combined with a dispatched agent's shell that is a one-line way
  for an agent to kill the dashboard or the tsnet proxy. Does the guard list in
  `dispatch/guards.py` cover `agentjobs stop`/`restart`?
