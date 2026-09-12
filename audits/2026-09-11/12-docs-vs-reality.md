# 12 — Documentation versus reality

Big Dawg Audit II, night of 2026-09-11. Auditor 12, read-only in the main clone at
`bb12f33f`. Method: every verdict below is marked **verified by running** or **inferred
from reading**; anything not checked is called unverified rather than rounded up to
accurate. The quickstart was executed end to end in an isolated `AGENTJOBS_HOME` on port
8912, which is where the two P2 findings came from. The documentation contract tests were
run (94 passed: `tests/test_api_reference_coverage.py`, `test_documentation_contract.py`,
`test_roadmap.py`, `test_export_roadmap.py`, `test_schema_generated_is_current.py`,
`test_context_budget.py`) so the reader knows which claims are already guarded and which
are not. Nothing was written to the live store; the sandbox homes under the job's scratch
directory were the only thing mutated, and the 8912 server was stopped afterwards.

Backlog records read before starting, via `agentjobs show` (read-only): task-413,
task-270, task-396, task-287, task-403, task-402, task-393, task-251, task-252.

## Summary table

| # | Severity | Finding | Status against backlog | How verified |
|---|---|---|---|---|
| 1 | P2 | The quickstart's `init` and `create` lines block on interactive prompts the docs never mention; with no terminal both abort | New | Running |
| 2 | P2 | `init --port` is written to the project config and to `.mcp.json`, but every CLI verb and `serve`/`open`/`stop` ignore it and dial 8765 | New | Running |
| 3 | P3 | `scripts/regen-schema-docs.sh` still validates a corpus of task files that task-380 removed, so it now always exits 1; `docs/index.md` still advertises that step | New (same shape as task-396) | Reading, plus a glob echo |
| 4 | P3 | README, installation.md and ENGINEERING.md say the gate is ten stages; it is eleven | New | Running (`check.py --list`) |
| 5 | P3 | README says fifteen MCP tools; sixteen are registered and the protocol test asserts sixteen | New | Reading (test assertion + registrations) |
| 6 | P3 | ENGINEERING.md's `--since-gate` section describes a task-directory-to-pytest rule that `gate_scope.py` says was removed with task-380 | New | Reading |
| 7 | P3 | ENGINEERING.md and ALLAGENTS.md still branch on "a project still on `files`", a backend task-402 deleted | New (task-393 missed it) | Reading |
| 8 | P3 | `agentjobs validate` is sold in README and mcp.md as "every client's portable backstop", but it now refuses to run without `--tasks-dir`, and a database project has no directory to give it | New | Running (`validate --help`) |
| 9 | P3 | ROADMAP.md read as a stranger: 121 flat entries, 13 "critical", five whose summary is their title, internal audit and owner-specific items on the public page | Confirms task-413 | Running (parsed the file) |
| 10 | P4 | mobile-access.md recommends the `tailscale serve` path, which is exactly the path the front-door identity guarantees do not cover, and does not say so | New | Reading |
| 11 | P4 | The CLI prints a full Rich traceback when the server is down, on a path where the docs promise a sentence | New | Running |
| 12 | P4 | Quickstart still says `work` "reads whichever backend is authoritative"; there is one backend | New | Reading |
| 13 | P4 | An empty, untracked project subdirectory under `tasks/` sits in the main clone that README says has no `tasks/` directory | New | Running (`ls`) |

Sound, with the specific claims checked listed under "Verified accurate" at the end: the
Python client snippet in quickstart and README, the API reference's coverage and its
no-authentication banner, the webhook event list, every relative link and anchor across
README, ROADMAP, ENGINEERING, ALLAGENTS and docs (1252 links, 0 broken), the `init` side
effects the quickstart lists, the `.mcp.json` it writes, the storage and project commands
README cites, and the index's status labels on the design documents.

---

## 1. P2 — The quickstart's `init` and `create` block on prompts the docs never mention

**Evidence.** `docs/quickstart.md:17` gives the install step as one bare line,
`poetry -P /path/to/agentjobs run agentjobs init`, and `docs/quickstart.md:59` gives the
create step as `agentjobs create --ready --title ... --category engineering --priority high`.
README.md:281 and :296 repeat both. Run in a fresh directory with stdin not a terminal
(the sandbox script, first attempt):

```
=== step 1: init
Prompts dir [prompts]: Aborted.
--- files created
.  ..
=== step 3: create --ready
Description []: Aborted.
exit=1
```

Source: `src/agentjobs/cli.py:351-360` prompts for project name, prompts dir, port and
user id unless each is passed as an option; `cli.py:965-967` prompts for the title and
the description. `--project-name` alone does not silence `init`: it still asks for the
prompts directory, the port and the user id.

**Verified by running.**

**Why it matters.** The quickstart is written for a person at a terminal and it works
for one, because the prompts have defaults and Enter accepts them. It fails for the two
readers the rest of the documentation is aimed at: an agent following the quickstart
(every dispatched session here is non-interactive), and anyone scripting the setup. The
abort leaves nothing behind, so the next line, `agentjobs open`, opens an unregistered
directory and serves it as `_local`. The second sandbox run showed what that looks like:
`/api/projects` answered `{"id":"_local","name":"qsproj",...}` for a project that was
never initialised, with no error anywhere.

**The fix.** Either say in the quickstart that `init` and `create` prompt, and show the
flag form once (`--prompts-dir`, `--port`, `--user`; `--description`), or make both
commands fall back to their defaults when stdin is not a TTY. The second is the one that
helps an agent; the first is a one-line docs change. Also worth stating: the quickstart's
`work --agent codex` line is interactive too (`cli.py:1138`, :1192) and was not exercised
here.

## 2. P2 — `init --port` is recorded and then ignored by every CLI verb

**Evidence.** `agentjobs init --port 8912` (second sandbox run, fresh home) wrote
`gui.port: 8912` into `.agentjobs/config.yaml`, wrote `.mcp.json` with
`AGENTJOBS_URL: http://127.0.0.1:8912`, and printed:

```
Start the server to work it: 'agentjobs open' (or 'agentjobs serve --port 8912').
```

`agentjobs create --ready ...` in that directory, with a server up on 8912, then dialled
8765:

```
ServiceUnavailable: service unavailable, nothing written: AgentJobs at
http://127.0.0.1:8765 did not answer after 6 attempts over 15.75s.
```

`list` did the same (`url = '/api/projects/qs/tasks/broken'` against 8765). Source:
`src/agentjobs/remote_manager.py:697` resolves the address as `base_url or
configured_api_base() or "http://127.0.0.1:8765"`, where `configured_api_base()` reads
`AGENTJOBS_URL`, `AGENTJOBS_API_BASE`, then `api_base:` in `~/.agentjobs/dispatch.yaml`
(`cli.py:232`). Nothing on that path reads the project's `gui.port`. `serve`, `open`,
`stop`, `status`, `restart` and the three `storage` commands all default to a literal
8765 (`cli.py:414, 731, 757, 772, 814, 2273, 2361, 2481`). The one reader of `gui.port`
is `project mcp-setup` (`cli.py:1290`).

**Verified by running.**

**Why it matters.** `init` teaches a model, "this project's server is on this port", that
the rest of the CLI does not implement. A user who answers the `Port` prompt with
anything but the default, or passes `--port`, gets a `.mcp.json` that works for agents
and a CLI that cannot see the same server. The quickstart never mentions `--port`, so a
reader following it verbatim survives only by taking the default; `init --help` describes
the option as "Default port for the web UI", which is what it is not. The error message
compounds it: it says to run `agentjobs serve`, which would bind 8765, so following the
advice starts a second server on a port the project's own config does not name.

**The fix.** Either make `_build_manager` consult the project's `gui.port` after the
environment and before the 8765 fallback, and make `serve`/`open`/`stop` default to it,
or remove `--port` from `init` and the `Port` prompt and let `init`'s closing lines say
that the port lives in `dispatch.yaml`. The docs then need one sentence in the quickstart
saying where the port comes from. Auditor 10 (CLI) will want this one too.

## 3. P3 — `scripts/regen-schema-docs.sh` validates a corpus that no longer exists, so it always fails

**Evidence.** `docs/index.md:132`: "That script also validates the live task corpus
against v2 and exits non-zero on failure, which makes it a useful check that no stage of
`scripts/check.py` runs." `scripts/regen-schema-docs.sh:127-130` loops over two shell
globs under the retired `tasks/` tree (this project's corpus and the test-data corpus)
and counts a failure for every path `linkml-validate` does not report clean on. Neither
directory has files since task-380, so an unmatched glob expands to itself: echoing the
loop variable from the clone root prints the two patterns literally, with the asterisk
still in them. The loop therefore runs twice on nonexistent paths, counts two failures,
and the script's last line (`if [ "$fail" -gt 0 ] ...; then exit 1`) fires every time.
The generated pages it writes first are fine; the exit status is now always red.

**Inferred from reading, plus the glob echo above.** The script was not executed because
it rewrites `docs/schema/v1`, `docs/schema/v2` and `schema/generated/`, which would be a
source write.

**Status.** New. task-396 is the same shape in `scripts/corpus_stats.py` (composes a
directory from config, measures a frozen copy) and is open; this is a second instance the
task-393 documentation pass did not catch. The index's sentence is also the wrong
recommendation now: with the corpus in a database, "validate the corpus" is
`tests/test_validate.py::TestRealCorpus`, or `agentjobs storage export` followed by
`agentjobs validate --tasks-dir`.

**The fix.** Drop the corpus loop from the script (keep the examples loop, which reads
`schema/examples/`), and cut the sentence at `docs/index.md:132-134`. Or fold it into
task-396 as a second acceptance criterion.

## 4. P3 — "Ten stages" is eleven

**Evidence.** `poetry run python scripts/check.py --list`:

```
black  ruff  mypy  api  icons  roadmap  oxlint  pytest  vitest  build  e2e
```

Eleven. README.md:406 ("It runs ten stages, cheapest first: formatting, lint, types, the
generated API contract, the generated PWA icons, the frontend linter, then the Python
suite, the Vitest component suite, the production build, and the Playwright suite") and
:410 ("The unqualified command runs all ten"), `docs/installation.md:61` ("The complete
check is ten named stages") and `ENGINEERING.md:40` and :96 all say ten. README's own
list omits `roadmap`, which README.md:389 describes two paragraphs earlier as a stage of
the same gate. task-392 added the stage on 2026-09-11 and the counts were not touched.

**Verified by running.**

**The fix.** Say "eleven" in the four places, or better, follow ENGINEERING.md's own rule
one paragraph down ("Quote a command and a date, never a bare count") and replace the
number with `scripts/check.py --list`.

## 5. P3 — README says fifteen MCP tools; there are sixteen

**Evidence.** README.md:177: "Fifteen tools cover discovery, the whole
claim/handoff/release/close loop, the queue, the append-only log, and zero-context
resumption." Registered tool names in `src/agentjobs/mcp/*.py` number sixteen
(`playbooks_list`, `projects_list`, `task_claim`, `task_close`, `task_create_draft`,
`task_create_ready`, `task_get`, `task_handoff`, `task_log_append`, `task_next`,
`task_promote`, `task_queue_move`, `task_release`, `task_update_content`, `tasks_list`,
`tasks_search`). `tests/test_mcp_protocol.py:314` asserts `"Serving 16 tool(s)"` on the
server's startup line. `docs/mcp.md`'s tool table (lines 174-218) has sixteen rows and is
held to the published list by `test_the_mcp_guide_names_every_published_tool`; README is
not.

**Inferred from reading** (registrations and the test assertion; the server's startup
line was not captured because `agentjobs mcp < /dev/null` exits before logging it).

**The fix.** Drop the number from README the same way as finding 4, or add README to the
test that already guards `docs/mcp.md`. The index already knows the count drifts: it
labels `integration/mcp-release-evidence.md` as stale for saying "fourteen tools".

## 6. P3 — ENGINEERING.md's `--since-gate` description names a rule that was removed

**Evidence.** `ENGINEERING.md:94-102`, property 2 of `--since-gate`: "Only task records
under `tasks/` and prose map to a reduced set", and property 3:
"`tests/test_validate.py::TestRealCorpus` loads this repository's own records, so a task
YAML genuinely can turn the suite red — which is why `tasks/` maps to `pytest` rather
than to nothing." `scripts/gate_scope.py:9-12`: "task-380 retired the frozen task records
from the checkout, so no diff carries one and a stray `tasks/` path is now unclassified
like any other." Its `CLASSES` table (`gate_scope.py:106-110`) has three rows,
`ROADMAP.md`, `docs/*` and `*.md`, and no row for the task directory; lines 79-84 say
that answer is gone and that whether `pytest` should join `UNBOUNDED_STAGES` is deferred
to task-409.

**Inferred from reading.**

**Why it matters.** This is the always-loaded bundle, read before every session's first
thought, and it describes the safety property of the one sanctioned gate shortcut in
terms of a mechanism that no longer exists. The property it argues for still holds
(default-deny), but a reader checking the claim against the file finds it false and has
no way to know which of the four properties to still trust. Auditor 13 owns the context
architecture; this is reported here because it is a sentence-level doc-vs-code mismatch.

**The fix.** Rewrite property 2 to name the three rows that exist, and property 3 to say
what `UNBOUNDED_STAGES` does for `roadmap` and that `pytest`'s corpus checks are the open
question (task-409).

## 7. P3 — Two always-loaded files still branch on "a project still on `files`"

**Evidence.** `ENGINEERING.md:291`: "On a project still on `files`, the checked-out
branch decides what the dashboard shows — check that before filing anything."
`ALLAGENTS.md:171`: "On a project still on `files`, **commit that to `main`** — a handoff
sitting on your branch is invisible in the React app." task-402 ("Delete the file backend:
one storage, no backend question") is closed `completed`; `agentjobs init --help` on
today's code says "the file backend was removed by task-402". `agentjobs storage status`
reports `files=0` for every registered project. There is no project a reader could be on
where either sentence applies.

**Inferred from reading**, with the storage status run for the second half.

**Why it matters.** Both are instructions, not background. The ALLAGENTS one tells an
agent to commit a handoff to `main`, which on today's product is a no-op at best and a
stray commit to the default branch at worst. task-393 ("Docs review and project hygiene
pass, after the SQLite cutover") closed completed and listed ENGINEERING.md:8 as a known
starting point; these two survived it.

**The fix.** Delete both sentences. `docs/quickstart.md:69` ("reads whichever backend is
authoritative for it") is the same drift in a softer form; finding 12.

## 8. P3 — `agentjobs validate` is not the backstop README and mcp.md say it is

**Evidence.** README.md:183: "Every client gets `agentjobs validate`, the portable
backstop." `docs/mcp.md:267` puts `agentjobs validate` as a column in the
what-protects-what table, and :286-287 describe the receipt gate as `validate --staged`
installed by `validate --install-hook`. `agentjobs validate --help` on today's code:

```
--tasks-dir  TEXT  Directory of task YAML to check. Required: records are rows, files are not.
```

and its own docstring says validating a project "would mean validating whatever frozen
copy its checkout still carries -- an answer that looks current and is not." A client
working a database project has no directory to give it, so the "backstop" for the
managed write path validates nothing about the records the client actually touches. The
same README paragraph says the plugin ships "a hook that refuses direct writes to task
files", which on a database project guards files that do not exist.

**Verified by running** (`validate --help`); the mcp.md table's other columns were not
checked.

**The fix.** README and the mcp.md table should say what `validate` is for now: a
pre-import check on a corpus of files, and a pre-commit receipt gate for a repository that
still carries one. The table's "what each layer prevents" should say that on a database
project the write path itself is the only layer, which is true and is the stronger claim.

## 9. P3 — ROADMAP.md read as a stranger (confirms task-413)

**Evidence.** Parsed `ROADMAP.md` at `bb12f33f`: 121 entries under four headings,
`## Critical (13)`, `## High (63)`, `## Medium (38)`, `## Low (7)`, and a closing line
that 28 further open tasks are drafts. Five entries print their title twice because the
summary is the title verbatim (task-385, task-389, task-342, task-353, task-381). The
first critical item is the audit that produced this file, naming an internal runbook path;
several summaries reference the owner ("The owner asked for the list to open from a burger
control") or dispatch runners by model name.

**Verified by running** (the parse). The generated-vs-store check is the `roadmap` gate
stage and was not re-run; `tests/test_roadmap.py` and `test_export_roadmap.py` passed.

**The impression.** A visitor learns that thirteen things are critical, that most of the
high band is one product's UI polish, and that the project is mid-audit. They do not
learn what AgentJobs is planning to become, which is what a roadmap is for. task-413
(active, ball `agent/revise`) says exactly this and proposes splitting the projection
into `docs/backlog.md` and an analysed `ROADMAP.md`; nothing here refutes it. Two
smaller points the task does not mention: the summary-equals-title entries would be
caught by a one-line check in `scripts/export_roadmap.py`, and the file's own preamble
says "what is planned, in the order it will be worked", which overstates a queue order
that reshuffles whenever somebody files a task.

## 10. P4 — mobile-access.md recommends the path the front-door guarantees do not cover

**Evidence.** `docs/mobile-access.md:3`: "AgentJobs has no authentication." Its
"Recommended private HTTPS setup" is `tailscale serve --bg http://127.0.0.1:8765`, a
machine-level proxy that forwards from loopback and presents no front-door secret. The
tsnet proxy with the secret is presented later as the option for "when AgentJobs must
coexist with another installed app". `docs/tailnet-front-door.md:32` says there is "no
unauthenticated remote caller in the finished" front door, and
`src/agentjobs/principals.py:356-380` treats a loopback request without the secret header
as the person at the keyboard. So a phone arriving through `tailscale serve` is served as
the machine owner, which `mobile-access.md:165` does say, but only at the bottom of the
direct-bind fallback section and never beside the recommended setup.

**Inferred from reading.** No tailnet setup was exercised.

**The fix.** One paragraph under "Recommended private HTTPS setup" saying that this path
identifies nobody and that the tsnet proxy in the next section is the one that does, with
a link to the front-door page. The two documents are not contradictory; they describe two
setups, and the newcomer-facing one does not say which it is.

## 11. P4 — A down server produces a 60-line Rich traceback where the docs promise a sentence

**Evidence.** Sandbox run, `agentjobs list` with no server on the resolved port: a full
Rich traceback through `cli.py:1007`, `remote_manager.py:220`, `client.py:396` and
`client.py:901`, with locals, before the one useful sentence at the bottom ("service
unavailable, nothing written ... Start it with 'agentjobs serve'"). `create` did the
same. `docs/quickstart.md:24-27` says the server "has to be running for the commands
below" and stops there.

**Verified by running.**

**The fix.** Catch `ServiceUnavailable` in the CLI commands that reach the server and
print its message. Then the quickstart can say what a reader sees when they skip step 2.
Auditor 10 (CLI) may already have this.

## 12. P4 — Quickstart: "whichever backend is authoritative"

**Evidence.** `docs/quickstart.md:69`: "`work` resolves the project through the registry
and reads whichever backend is authoritative for it, so it sees the same records the UI
does." There is one backend (task-402, closed). The sentence is true and misleading: a
reader infers a choice that no longer exists.

**Inferred from reading.**

**The fix.** "reads the project's database, so it sees the same records the UI does."

## 13. P4 — An empty project subdirectory under `tasks/` in the main clone

**Evidence.** `ls tasks` in the clone prints one subdirectory named for this project; it
holds zero files, git tracks nothing under it (`git ls-files tasks` is empty) and
`git status` does not show it because git ignores empty directories. README.md:369-370:
"So there is no `tasks/` directory here, and a clone does not arrive with the records."

**Verified by running.**

**Why it matters.** Only because two scripts (finding 3, task-396) still glob into it and
a stray directory makes their failure look like an empty corpus rather than a missing one.
A clone is clean; only this working copy has it. Delete it, or leave it and let the
scripts be fixed.

---

## Verified accurate

Claims checked against source or by running, listed so the synthesis can distinguish
"checked" from "not contradicted":

- **Quickstart step 4 and README's Python client snippet.** `from agentjobs import Ball,
  BallReason, TaskClient` resolves (`src/agentjobs/__init__.py:3-24`); `get_next_task(agent=)`,
  `claim_task(id, agent=)`, `add_progress_update(id, agent=, summary=, details=)` and
  `handoff_task(id, actor=, ball=, ball_reason=, ball_prompt=)` match the signatures at
  `client.py:486, 594, 687, 603`. The snippet ran against the sandbox server on 8912
  without error (it found no task because of finding 2, not because of the snippet).
  Verified by running.
- **What `init` creates.** `.agentjobs/config.yaml`, `.mcp.json` with `AGENTJOBS_URL`, a
  registry entry in `$AGENTJOBS_HOME/projects.yaml`, and a per-project database under
  `databases/<id>.db`; no task directory. Exactly what `docs/quickstart.md:21-25` says.
  Verified by running. Two small gaps: `_ensure_gitignore` wrote no `.gitignore` in the
  sandbox (unverified why; may be by design when no git repo exists), and
  `init --tasks-dir` still advertises "Relative path for task YAML files" for a field
  nothing writes to.
- **`open` and `serve` on a fresh server.** `/api/version` reports `frontend_bundle`
  `present`, `/app/` and `/docs` both 200, `stop --port` stops it and the port refuses
  afterwards. Verified by running on 8912. The missing-bundle branch of `open` was not
  exercised; `cli.py:582-591` and the `open` docstring describe it.
- **The API reference.** Its no-authentication banner is at the top and its coverage of
  every OpenAPI operation is enforced; both tests in `tests/test_api_reference_coverage.py`
  pass on today's `frontend/openapi.json` (121 operations). task-270's fix holds.
  Verified by running the tests; the prose of individual operations was not read.
- **Webhook events.** `docs/webhooks.md:11-16` lists `task.handoff`, `task.question`,
  `task.closed` and `webhook.test`; the manager emits exactly the three task events
  (`manager.py:1535, 1678, 2644`). Inferred from reading.
- **Links.** 1252 relative links and anchors across README, ROADMAP, ENGINEERING,
  ALLAGENTS and every file under `docs/` resolve (one false positive on a heading with
  an underscore). Verified by running a checker script.
- **README's command list.** `create --ready`, `list --lifecycle`, `show`, `next --why`,
  `work --agent`, `status`, `restart --reload`, `stop`, `project add`, `project list`,
  `storage status`, `storage export`, `branches` all exist with those flags. `storage
  status` prints a path per project as README.md:363 says. `project list` marks
  `local-only` in code (`cli.py:1243`) though no registered project is local, so the
  marking itself was not observed. The README is right that the CLI has no
  `claim`/`handoff`/`release`/`close` commands. Verified by running `--help` and the
  read-only commands.
- **Python 3.11 or newer** (`pyproject.toml:27`, `^3.11`). Verified by reading.
- **`docs/index.md` status labels.** The dispatch design's header (lines 1-60) does say
  "SHIPPED", lists the tasks that shipped each section, and names the four unbuilt items;
  the index's "Historical" and "Dated report" labels match the banners in
  `schema-design.md`, `mcp-integration-design.md` and `mcp-release-evidence.md`.
  `test_the_docs_do_not_present_unshipped_work_as_shipped` passes. task-252 holds.
  Verified by reading the headers only; the 4,000-line body was not audited.
- **`docs/mcp-clients.md`'s use of 8876** in examples is deliberate illustration of a
  machine-local port and says so (lines 12-24). Not a leak of the owner's setup.

## What I did not get to

- The bodies of the design records: `agent-dispatch-design.md` beyond its header,
  `analytics-design.md`, `playbooks-design.md`, `task-selection-design.md`,
  `agent-loops-design.md`, `principals-design.md`. Present-tense claims inside them
  about unbuilt behaviour were not hunted; only the headers and the index labels were.
- `docs/agent-workflow.md` (1,149 lines). Every `agentjobs` command it cites exists; the
  sentences about them were not checked. task-270's log says it was re-read at
  first-principles depth on 2026-08-23; what has changed since (SQLite, task-402, the
  run credential) was not re-checked against it.
- `docs/task-schema.md`'s field table against `models_v2.py`, and
  `docs/schema/understanding.md`. task-270 corrected the required-field count; not
  re-verified.
- `docs/storage-sqlite.md`'s operator sequences (import, backup, verify, restore,
  split). Auditor 2 owns storage.
- `docs/authorization.md`, `exposure.md`, `identity-registry.md` capability and refusal
  tables against `capabilities.py` and `principals.py`. Auditor 3 owns authorization.
- `docs/performance.md`'s figures and the `bench.py` / `run_report.py` contracts.
- `docs/tailnet-front-door.md` and the tsnet proxy's procedures; only its flag set was
  compared to `main.go`, where `-service`, `-hostname`, `-state-dir` and
  `-front-door-secret-file` exist and mobile-access.md shows only `-backend`.
- `docs/codex-dispatch.md` and `codex-dispatch-architecture.md`.
- `docs/context-budget.md` figures; `tests/test_context_budget.py` passed.
- The mkdocs site build (task-287 says Mermaid does not render there; not re-checked)
  and `frontend/README.md`.
- The `work --agent` interactive flow and `open`'s browser launch, both avoided because
  they prompt or open windows on the owner's machine.
- Whether `agentjobs` resolves on a stranger's PATH after a poetry-from-clone install,
  which decides whether the `.mcp.json` that `init` writes (`"command": "agentjobs"`)
  works for the quickstart's own install path. On this machine it resolves only because
  the shell's PATH includes the main clone's virtualenv; the repository's own root
  `.mcp.json` uses an absolute interpreter path, which suggests the owner hit this.
  Unverified and worth a look from auditor 10.
- The other auditors' files in `audits/2026-09-11/`. This session was resumed after a
  usage-limit stop with the findings already gathered, and the file was written before
  reading neighbours so that it landed; overlaps with 10 (CLI), 13 (context) and 14
  (corpus) are likely and are flagged per finding above.

## Questions for other auditors

- **Auditor 10 (CLI/MCP).** Finding 2: is there a reason `_build_manager` should not
  read `gui.port` after the environment and before the 8765 fallback? And does
  `init`'s `_ensure_gitignore` deliberately write nothing outside a git repo?
- **Auditor 10.** Finding 11: is the Rich traceback on `ServiceUnavailable` a deliberate
  choice for `list`/`create`, or just uncaught?
- **Auditor 9 (API).** `/api/projects` reports a `tasks_directory` for every project on
  a product with no task directories (seen on the sandbox, pointing at a path that does
  not exist). Is anything reading it, or is it a leftover field the generated client
  still carries?
- **Auditor 13 (context architecture).** Findings 4, 6 and 7 are sentences in the
  always-loaded bundle. The ablation suite in `evals/context/` decides what is
  load-bearing; do any of these three sentences have a verdict there, and would cutting
  them trip `tests/test_context_eval.py`?
- **Auditor 5 (gate receipts).** Finding 6: `gate_scope.py:79-84` defers to task-409
  whether `pytest` belongs in `UNBOUNDED_STAGES` because its corpus checks read the
  store. Until that is settled, is a `--since-gate` run over a docs-only diff skipping a
  stage the store can turn red?
- **Auditor 14 (corpus/process).** task-393 closed as a docs pass "after the SQLite
  cutover", and findings 3, 7 and 12 are the same drift in three places it did not reach.
  Was its acceptance list bounded to the files it named, or did it claim the whole tree?
- **Auditor 3 (authorization).** Finding 10: confirm that a request through a
  machine-level `tailscale serve` arrives as `LOOPBACK` in `principals.py` and is served
  as the configured user, with nothing in the record marking it as remote.
