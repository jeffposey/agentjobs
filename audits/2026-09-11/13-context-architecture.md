# 13 — Context architecture and the agent contract

Auditor 13, Big Dawg Audit II, night of 2026-09-11 (written 2026-09-12 morning).
Repository at `bb12f33f` on `main`, clean apart from sibling auditors' untracked files.
Claude Code installed: **2.1.269**. Runner: `claude-fable-5-1`.

Scope: `CLAUDE.md` and its `@`-chain, `AGENTS.md`, `ALLAGENTS.md`, `ENGINEERING.md`,
`C:/ai/shared/GLOBAL-AGENTS.md`, the MCP server instruction text, the dispatch prompt
stubs, `docs/context-budget.md`, `tests/test_context_budget.py`, `contexteval/`,
`scripts/context_eval.py`, `evals/context/`.

Backlog read first. The brief's `agentjobs search` does not exist (see finding 14); I
used `agentjobs list | grep` and `agentjobs show`. Records nearest this system, all open:
task-323 (budget covers half the chain), task-319 (dual MCP registration), task-162
(dispatch prompt), task-411 (corpus checks inert), task-409 (`--since-gate` blind to the
store). Closed and load-bearing for context: task-301, 302, 304, 305, 376, 402, 403.

---

## 0. The measurement, up front

Instrument: `agentjobs.contexteval.bundle.read_bundle` (newline-normalised bytes, the
same reader the budget test uses), tokens at the 3.0 bytes/token ratio
`docs/context-budget.md` measured with the production tokenizer. **Verified by running.**

| file | bytes | ~tokens | words |
|---|---:|---:|---:|
| `CLAUDE.md` | 757 | 252 | 117 |
| `AGENTS.md` | 1,526 | 509 | 229 |
| `ENGINEERING.md` | 29,459 | 9,820 | 4,581 |
| `ALLAGENTS.md` | 28,854 | 9,618 | 4,573 |
| **budgeted bundle** | **60,596** | **~20,200** | 9,500 |
| budget (`BUNDLE_BUDGET_BYTES`) | 61,000 | | |
| **headroom** | **404** | | |

What the budget does not count but a session on this machine loads anyway (task-323's
point, confirmed):

| file | bytes | ~tokens |
|---|---:|---:|
| `C:/ai/shared/GLOBAL-AGENTS.md` (via `~/.claude/CLAUDE.md`) | 10,596 | ~3,770 |
| `C:/projects/AGENTS.md` (via `C:/projects/CLAUDE.md`) | 2,654 | ~1,060 |
| auto-memory `MEMORY.md`, 49 entries | 8,363 | ~3,370 |
| **unbudgeted** | **21,613** | **~8,200** |

So the `@`-chain plus memory that reaches a session's first thought is about
**82,000 bytes / 28,000 tokens**, of which the test governs 74%. Against task-301's
2026-08-25 figures (bundle 24,902 tokens = 32.9% of a 75,619-token first request) the
bundle is now ~20,200 tokens: task-305's cut held. Growth since the cap, from `git show`
at dated commits (LF bytes, three repo files, `CLAUDE.md` excluded):

| commit | date | sum |
|---|---|---:|
| `5365d58` | 2026-08-25 (pre-cut) | 77,298 |
| `d412da08` | 2026-08-27 (cap lands) | 57,800 |
| `cddbba62` | 2026-09-06 (paraphrase rule) | 60,057 |
| `4951239c` | 2026-09-07 | 60,176 |
| `ad6b4459` | 2026-09-11 | 59,839 |

About 150 bytes/day since the cap, against ~3,700 bytes/day (1,240 words/day) in the week
task-301 measured. **The ceiling works.** The cost is that it now has 404 bytes of room
and the next rule forces either a raise or a cut, and a cut is supposed to be preceded by
the ablation suite (finding 7).

---

## 1. `ENGINEERING.md` describes a `--since-gate` classification table that no longer exists

**Severity:** P2. **Status: New** (adjacent to task-409 and task-411, neither of which
names this prose).

**Evidence.** `ENGINEERING.md:94-102`:

> 2. **The classification table is default-deny.** Only task records under `tasks/` and
> prose map to a reduced set; everything else … selects all ten. …
> 3. … `tests/test_validate.py::TestRealCorpus` loads this repository's own records, so a
> task YAML genuinely can turn the suite red — which is why `tasks/` maps to `pytest`
> rather than to nothing.

`scripts/gate_scope.py:97-101`, the table itself, printed today:

```
CLASSES = (
    Class("ROADMAP.md", ROADMAP_STAGES, ...),
    Class("docs/*",     DOCS_STAGES,    ...),
    Class("*.md",       DOCS_STAGES,    ...),
)
```

No `tasks/` entry. `gate_scope.py:79` says in the past tense that ``tasks/ -> pytest``
*"let a record correction select the one stage"*. And `tests/test_validate.py:700-707`
(`corpus_or_skip`) skips with *"this repository's records have been retired from the
checkout"*; `git ls-files tasks | wc -l` prints `0`.

**Verified by running** (the table and the test source were read; the skip path and the
empty `tasks/` were executed/checked).

**Why it matters.** This is exactly the class task-302 fixed on 2026-08-27 — a bundle
sentence contradicting the code — reintroduced by the storage cutover. Property 3 tells
every session that a rule exists *because* a test loads the records; the test skips and
the rule is gone. A reader who trusts the bundle believes `--since-gate` has a safety
property it does not have (task-409 is the consequence: a store change moves no file, so
the diff is empty and every stage is skipped). The sentence also keeps the "ten" count
(finding 2) alive.

**Fix.** Rewrite properties 2 and 3 to the current table: ROADMAP.md → roadmap stage,
prose → docs-contract stages, everything else → all stages; and replace the
`TestRealCorpus` justification with task-409's point, that a store change is invisible to
a path-based classifier, which is the real reason `--since-gate` is narrower than it looks.

## 2. "The gate is ten named stages" — it is eleven

**Severity:** P2. **Status: New.**

**Evidence.** `ENGINEERING.md:40` *"The gate is ten named stages"*; `ENGINEERING.md:96`
*"selects all ten"*. `poetry run python scripts/check.py --list` today prints eleven:
black, ruff, mypy, api, icons, roadmap, oxlint, pytest, vitest, build, e2e. The `roadmap`
stage was added in `90b8a0d7` on 2026-09-11, the same day as the last edit to this file.

**Verified by running.**

**Fix.** Say "eleven", or better, say nothing numeric — `ENGINEERING.md:51-54` already
makes the argument *"Quote a command and a date, never a bare count"* about the test
suite, and then states a bare count of stages one paragraph earlier. `--list` is the
count.

## 3. The dual AgentJobs MCP registration is live, and the context-budget doc says it is not

**Severity:** P2. **Status: Confirms task-319; refutes `docs/context-budget.md` §2
("The dual AgentJobs MCP registration does not reproduce here").**

**Evidence.** This session's own deferred-tool listing carries both
`mcp__agentjobs__{playbooks_list … tasks_search}` (16 names) **and**
`mcp__plugin_agentjobs_agentjobs__{…}` (16 names), and the `# MCP Server Instructions`
block contains the `## agentjobs` text twice, byte-identical, once under each server name.
`docs/context-budget.md` lines under *"The dual AgentJobs MCP registration does not
reproduce here"* say *"the injected block contained **one** `## agentjobs` instruction
block and **16** `mcp__agentjobs__*` tool names, not 32. The names collide, so one
registration wins."* task-319's spec already explains the discrepancy: on 2026-08-25 the
plugin's server pointed at a dead port and never connected; since task-317 it does, and
Claude Code prefixes it differently so nothing collides.

**Verified by observation** of the loaded context in this session (Claude Code 2.1.269),
not by a separate measurement run. Cost is at least 16 tool names plus one 1,100-byte
instruction block per session; small in tokens, larger in the "two spellings of every
call" confusion task-319 names.

**Fix.** Do task-319 (retire the root `.mcp.json`, twenty minutes by its own estimate).
Add a dated correction paragraph to `docs/context-budget.md` rather than editing the
2026-08-25 measurement.

## 4. The MCP leading rule — the 512 characters a client is guaranteed to show — describes task YAML, and two tests pin the stale sentence

**Severity:** P2. **Status: New.**

**Evidence.** `src/agentjobs/mcp/instructions.py:16-18`:

> AgentJobs task YAML is generated state. Use these tools for every task mutation. …
> Reading task YAML is allowed.

Records for this project have been SQLite rows since 2026-09-07 (`ENGINEERING.md:9-11`);
there is no YAML to read. The sentence is the *leading rule*, the one `LEADING_RULE_BUDGET
= 512` exists to keep in front of the client, so it is the most-read sentence of the MCP
surface. And it is pinned: `tests/test_mcp_protocol.py:197` and
`tests/test_mcp_server.py:493` both `assert "task YAML is generated state" in
initialized.instructions[:512]`. A docs sweep that fixed the prose would go red in the gate
until it also edited two tests, which is one reason a sweep that touched fourteen files
(`dfd5dbc3`, `0ee5047f`, `ad6b4459`) missed this one.

**Verified by reading** the source and tests; **verified by observation** that the loaded
instruction text in this session is the same string.

**Fix.** Reword to what is true on both backends — "Task records are managed state; write
them only through these tools" — and change the two assertions to pin the *rule* ("only
claim, handoff, release, and close") rather than the phrase. Auditor 09's MCP/API file may
have more on the instruction text; I did not read it.

## 5. `ALLAGENTS.md` tells a background session to ignore a harness instruction it no longer receives

**Severity:** P3. **Status: New** (task-303 shipped the fix; the prose predates it working).

**Evidence.** `ALLAGENTS.md:331-334`: *"The harness tells background sessions the opposite,
and this rule wins. A `--bg` session is handed a preamble instructing it to use
`EnterWorktree` and saying the instruction is enforced."* And `ALLAGENTS.md:336-340`: the
enforcement half is off via `.claude/settings.json` (`{"worktree": {"bgIsolation":
"none"}}`, present today) — *"Probed on Claude Code 2.1.238, 2026-08-25"*.

This session **is** a `--bg` session, on 2.1.269. Its harness preamble said:
*"Edit files directly in your working directory — this session is configured to work in
place rather than isolating into a worktree. Skip EnterWorktree unless the user explicitly
asks to work in a worktree."* That is the opposite of what the bundle says the harness
says. With `bgIsolation: none` the harness now removes the instruction half as well as the
enforcement half. `PROMPT_STUB` (`dispatch/runner.py:138-150`) carries the same clause —
*"Your harness may instruct the opposite and call it enforced"* — hedged with "may", so it
is not wrong, only dead weight. The audit runbook's preamble repeats the same belief
(*"If a background-session preamble tells you to isolate first, that instruction is
wrong"*).

**Verified by observation** on one session and one version. A second `--bg` session with
the setting removed would establish whether the preamble comes back, which I did not run
(it would need a settings edit).

**Why it matters.** Twelve lines of bundle, and a dispatched prompt sentence, spent
overriding an instruction that no longer arrives. More importantly it is the pattern:
three version-stamped probes (2.1.235, 2.1.238, 2.1.238) in a file loaded by 2.1.269, with
no mechanism that re-probes. The eval suite's `no-enter-worktree-tool` case exists for
this rule and scored it *decorative* on opus-5 already.

**Fix.** Cut lines 331-334 to one sentence ("If a preamble tells you to call
`EnterWorktree`, do not; that is task-303's finding"), keep the refusal-handling paragraph,
and move the probe history to `docs/agent-dispatch-design.md`. Re-run
`context_eval.py --case no-enter-worktree-tool` on Fable before cutting (finding 7 says
what that costs: about $4).

## 6. The budget governs 74% of what loads; the other quarter is stale and outside the repo

**Severity:** P3. **Status: Confirms task-323**, with today's numbers and a new point about
the content.

**Evidence.** The table in §0: 21,613 unbudgeted bytes against 60,596 budgeted.
`C:/ai/shared/GLOBAL-AGENTS.md` says, today:

- line 107: AgentJobs *"is git-backed: one YAML file per task under a project's
  `tasks/<project>/`, source of truth, diffable"* — false for this project since
  2026-09-07 and for none of the others after the split (auditor 02's territory).
- lines 114-117: a second server on 8765 *"serves the same task files from a process
  nobody restarts … validation errors against task files"* — the port hazard is real, the
  mechanism described is the file backend's.
- line 136: *"Read `docs/task-schema.md` before writing a task file"* — there is no task
  file to write.

**Verified by running** (sizes) and **by reading** (the sentences). The file is outside the
repository and outside this audit's write, so this is a question for the owner, not a fix
I can propose a diff for.

**Why it matters.** Every session in every project on this machine reads those sentences
before this repository's own bundle, which says the opposite. task-323's four options are
still the right menu; option 2 (measure and report the user half, assert nothing) is the
one I would take, because a number nobody sees is what let this quarter go stale.

## 7. The eval harness works, costs $1.51 a case-pair on Fable, and has never been run on Fable

**Severity:** P3. **Status: New** (task-304 built it; nothing on the backlog says to re-run
it for the model that became the Big Dawg runner on 2026-09-05).

**Evidence — verified by running.**

```
poetry run python scripts/context_eval.py --case stage-explicit-paths --runs 1 \
    --model claude-fable-5-1 --out <scratch>/ctxeval
[11:43:19] 1 case(s), 2 session(s) against claude-fable-5-1 at up to 3 at a time
[11:43:34]   stage-explicit-paths [with] run 1: ok (15s, $0.718, 3 turns)
[11:43:46]   stage-explicit-paths [without] run 1: ok (27s, $0.788, 4 turns)
1 scenario(s), 2 session(s), $1.51, 0.5 min wall clock
| stage-explicit-paths | ENGINEERING.md#Commit Hygiene | 100% | 100% | inconclusive |
  why: only 1 scored run(s) with the rule and 1 without; 2 per arm is the floor
  guard_denials: 0 in both arms
exit=0
```

`--dry-run` over all six cases: 1.6 s, every ablation still matches (the
`test_context_eval.py` half also passes: 55 tests, 1.9 s, with `test_context_budget.py`).

What it told me:

- **It runs, end to end, on the current Claude Code and the current model**, writes
  `report.md`/`report.json`, and the sandbox guard is armed (0 denials because the run
  never reached; the counter is present).
- **Pricing.** Fable at ~$0.75/session. The `quick` tag is five cases × 3 runs × 2 arms =
  30 sessions ≈ **$23, ~5 minutes at `--jobs 3`**; all six cases ≈ $27. That is cheaper
  than the opus-5 baseline was ($30.34 for 42 sessions) and far below "tens of minutes
  and real money", the phrase `ENGINEERING.md:185` uses to justify raising the budget
  instead of running it (`tests/test_context_budget.py` docstring: *"Raised rather than
  paid for by a cut, because … that suite costs tens of minutes and real money"*).
- **`--runs 1` can never produce a verdict.** The floor is 2 per arm. The README's
  quick-start examples do not say so; a first-time user who tries the cheapest invocation
  gets `inconclusive` and may conclude the harness is broken.
- **No Fable baseline exists.** `evals/context/baselines/` holds only the two 2026-08-25
  files (opus-5, haiku-4-5). `evals/context/README.md#When to run it` says the point of
  the suite is a new model release; Fable 5.1 became the Big Dawg runner 2026-09-05 and
  Claude Code moved 2.1.238 → 2.1.269. The `--model` default is still `claude-opus-5`.

**Fix.** Run `scripts/context_eval.py --tag quick --model claude-fable-5-1` once (~$23),
commit the baseline, and change the budget-test docstring's cost claim to the measured
one so the next raise-versus-cut decision is priced correctly. Consider `--runs 1` refusing
with the floor message before spending anything.

## 8. Stale pointers inside the eval cases and its README

**Severity:** P4. **Status: New.**

**Evidence — verified by reading; the tests were run and pass, which is the point.**

- `evals/context/stage-explicit-paths/case.yaml:10` and
  `worktree-not-checkout/case.yaml:10` cite `incident.where: "ALLAGENTS.md, 'Three
  failures on 2026-08-11', failure N"`. That heading was moved out of the bundle by
  task-305; `grep "Three failures" ALLAGENTS.md` matches nothing (line 352 has a
  one-sentence residue). `tests/test_context_eval.py` validates `ablate` and
  `residual_forbidden` against the bundle but not `incident.where`, so the field rots
  silently. The README says *"`incident` … Required — no invented scenarios"*; a pointer
  to a heading that is gone undermines the property it exists for.
- `evals/context/README.md:49`: *"The gate is 96 seconds"*. `docs/performance.md:601`
  prices a 302-second gate and `docs/agent-dispatch-design.md:2315` says 169–285 s under
  load. The argument (do not put the suite in the gate) survives either number; the number
  does not.
- Both baseline reports still list `task-record-on-main` with home
  `ENGINEERING.md#Task files live on main, always` — correctly retained as history, and
  correctly footnoted in the README under task-403. Not a defect; noted so nobody files it.

**Fix.** Point `where` at the task ids (task-186/192 style, as the other four cases do)
and make `test_context_eval.py` assert that a `where` naming a bundle heading resolves.

## 9. The paraphrase rule overclaims what `agentjobs quotations` finds

**Severity:** P3. **Status: New** (task-376 shipped the rule and the tool).

**Evidence — verified by running.** `ALLAGENTS.md:439`: *"`agentjobs quotations` finds
them; the gate over `tasks/` and the SQLite importer both refuse a record carrying one."*

```
$ poetry run agentjobs quotations
✓ 417 task record(s) scanned; no quoted remarks.
$ poetry run agentjobs quotations task-162-author-the-dispatch-prompt
✓ 1 task record(s) scanned; no quoted remarks.
```

task-162's description opens: *Jeff, 2026-08-19: "you should be able to edit the prompt
yourself, when dispatching, …"* — the owner, named, dated, quoted verbatim, in a public
repository's backlog. `src/agentjobs/quotation.py:80-101,143-146` shows why it is clean:
the detector matches a *lexicon* (vulgarity, dismissal, informality) near an attribution
cue. It finds reproduced *register*, not quotation. That is a defensible design and the
module docstring says so; the bundle sentence does not. The same tool's `--help` still says
it *"fails the gate over `tasks/`"*, a gate that has nothing under `tasks/` to run over.

Also: `SUPERVISOR_STUB`'s docstring (`dispatch/runner.py:213-214`) quotes the owner by
name verbatim in source. The rule is scoped to records; the reason given for it (public
remote, characterisation of a real person) applies to source docstrings equally.

**Fix.** Change the bundle sentence to what the tool does ("flags quoted *tone*; a neutral
quotation is yours to catch"), or widen the detector to name-plus-date-plus-quote
attributions, which is the pattern in task-162. Fix the `--help` text.

## 10. Duplication between the two large files, and what is static that should be dynamic

**Severity:** P3. **Status: New** (task-305 did the last pass on 2026-08-27; the merge-gate
numbering is the one duplication it removed).

**Evidence — inferred from reading, with counts from grep.** Mentions per file
(E=ENGINEERING, A=ALLAGENTS):

| rule | E | A |
|---|---:|---:|
| bootstrap a worktree | 6 | 9 |
| `-d` never `-D` | 1 | 1 |
| never `git add -A` | 1 | 2 |
| one gate per handoff / `PARTIAL RUN` | 7 | 2 |
| `finish=on/off` | 2 | 1 |
| `--posture-release` | 3 | 1 |
| where records live | 1 | 2 |

The merge-gate numbering is now consistent everywhere I looked (`client.py:52`,
`finish.py:1,1464,1868`, `ALLAGENTS.md:182,247` all cite ENGINEERING's numbers). That
incident is closed. What remains is the same *argument* made twice: "Sharing a clone"
(ENGINEERING.md:262-293) and "Why you get your own worktree" (ALLAGENTS.md:299-361) both
explain the one-HEAD collision, both give the `git worktree add` command, both say
`worktrees/` beside the clone. The eval suite's `worktree-not-checkout` case has to ablate
three sections in three files (dry-run: 112 lines) to remove one rule, which is the
measurement of this duplication.

Static that should be dynamic (candidates, each with what would replace it):

- **`--since-gate`'s four properties** (ENGINEERING.md:78-110, ~33 lines). A rule for the
  one person who runs `--since-gate`, loaded by every session; two of the four properties
  are false today (finding 1). Keep the one-line "the unqualified command is the gate;
  `--since-gate` is the exception, documented in performance.md".
- **"Measuring this file"** (ENGINEERING.md:171-189) and **"Measuring performance"**
  (ENGINEERING.md:160-170): instructions for editing the bundle and for benchmarking,
  read by every session, acted on by a session a month. A pointer each.
- **Bootstrapping a worktree** (ALLAGENTS.md:362-408, ~45 lines including the interpreter
  paragraphs). Load-bearing only for a session that takes a worktree, and the dispatch
  stub already points such a session at `docs/agent-workflow.md` (62,590 bytes, which
  contains the same material). The eval scored `worktree-interpreter` decorative on opus-5
  and 5-of-6 violating on haiku, so it is a real rule for small models; the question is
  where it lives, not whether.

The reverse — load-bearing and buried: nothing I found in `docs/` that an agent needs
before its first act is missing from the bundle. The audit's own preamble rule about the
managed-path write hook (compose elsewhere, copy in) lives only in a runbook; task-276 is
still a *draft* (`agentjobs show task-276` → `lifecycle: draft`), and `ALLAGENTS.md:349`
says *"task-276 is the fix"* as if it were landing.

**Fix.** Not a patch, a decision: pick the three candidates above, run the ablation on
Fable for the sections that have cases, and move the rest to `docs/agent-workflow.md`
with a one-line pointer. That buys roughly 5–7 KB against a 404-byte headroom.

## 11. Five dated probes and timings in the always-loaded bundle

**Severity:** P4. **Status: New.**

**Evidence — inferred from reading.** `ALLAGENTS.md:328` (2.1.235, 2026-08-19); `:339`
(2.1.238, 2026-08-25); `:374-375` ("About 30 seconds … 13 seconds … timed 2026-08-19");
`ENGINEERING.md:450` ("Observed 2026-08-17"); `ENGINEERING.md:121-124` ("three dispatched
runs … 32 cores" — both still true: `dispatch.yaml` `max_concurrent_runs: 3`,
`os.cpu_count()` = 32). `ENGINEERING.md:51` states the principle: a count in prose goes
stale and the command does not. The bundle is loaded by 2.1.269 and none of the probes
have a re-probe trigger. The ablation README does this right (a verdict carries a model id
and a date and lives in `baselines/`, not in the rule).

**Fix.** Keep the rule, move the probe to the doc the rule links to. Same shape as
task-305's move of measurement history.

## 12. Stale file-era sentences in the dispatch stub's docstrings

**Severity:** P4. **Status: New.**

**Evidence — inferred from reading.** `dispatch/runner.py:162`: the `-w` guard refuses
git operations aimed at the shared clone, *"which is where this project requires task
records to be committed and where the merge gate runs"*. `runner.py:220-222`
(`SUPERVISOR_STUB` docstring): a supervisor in a worktree *"would then commit the parent's
task records somewhere the dashboard cannot see them"*. Neither is possible since
2026-09-07; the merge-gate half of each sentence is still the reason. The rendered stubs
themselves are clean (`build_prompt` output was not re-rendered; the string constants were
read). `PROMPT_STUB` still spends a sentence on the harness preamble (finding 5).

**Fix.** Trim both docstrings to the merge-gate reason.

## 13. Sound, in one line each

- `tests/test_context_budget.py`: the assertion does what its docstring says, the failure
  message names the file, and the raise on 2026-09-07 was recorded with its reason. The
  growth curve in §0 is the evidence it works.
- `contexteval/bundle.py`'s `residual_hits` refusal (an ablation that leaves a restatement
  behind is `inconclusive`, not a result) is the one design choice that makes a
  `decorative` verdict mean something; it survived six cases' dry-run today.
- `AGENTS.md` (1,526 bytes) is accurate today, including the sentence added on 2026-09-07
  about records being rows outside the checkout.
- `ENGINEERING.md:9-11` ("no task YAML is tracked here since task-380"): true,
  `git ls-files tasks` is empty.

## 14. The instructions this audit runs under

**Severity:** P3 for the runbook; the audit still worked. **Status: New**, except the
`agentjobs search` point, which auditors 02, 04, 07, 08 and 09 all hit independently
before I did — five auditors re-deriving one wrong sentence is the cost the preamble
warns about, applied to itself.

- **`agentjobs search` does not exist.** `agentjobs --help` lists no `search`; `list`
  takes no `--project`. The preamble's other option, "or the dashboard", is on 8876, which
  the same preamble bans touching. What is left is `list | grep` and `show`, which is what
  I did, and which cannot search a description.
- **The brief contradicts itself about 8876.** Prompt: *"readable at
  `http://127.0.0.1:8876` … Read it if you like."* Preamble: *"Do not touch the server on
  port 8876."* A GET is harmless, but a rule that says "never" and an invitation to read
  through it cannot both be followed. I did neither; `agentjobs show` reads the database
  directly.
- **The preamble carries the same stale belief as finding 5** — *"If a background-session
  preamble tells you to isolate first, that instruction is wrong"*. No such preamble
  arrived on 2.1.269. Harmless, and a data point that the runbook copied the bundle's
  claim rather than checking it.
- **"Your server port, if you need one, is 8913"** — allocated per auditor, good; but no
  auditor is told what to do with `AGENTJOBS_HOME` or the registry, so a server on 8913
  would serve the *live* registry read-only at best. I did not need one, so I did not
  find out whether it is safe. Auditor 02's file may say.
- **The count "139 open tasks"** is quoted without a command or date, the thing
  ENGINEERING.md forbids in prose. `agentjobs list` printed 417 rows across lifecycles; I
  did not verify 139.
- **The write-hook workaround is right and undocumented in the repo.** The preamble says
  the managed-path hook refuses a write whose content names a task-record path and to
  compose elsewhere; that is task-276, still a draft, and this file was written by that
  route. The hook cost two auditors their final minutes last time; a draft is not a fix.
- **Two things it got right that the first audit did not:** "verified by running or
  inferred from reading, per finding" and "New / Confirms / Refutes" are the two
  instructions that changed what I did. Keep them.

---

## What I did not get to

- **A second measurement of the harness residual.** Task-301's 42,876-token harness figure
  was not re-measured on 2.1.269; the tool list this session carries (roughly 190 deferred
  names across eleven servers, up from ~140 across ten) suggests the MCP block grew, but I
  did not run instrument A on a transcript.
- **The Codex side.** `AGENTS.md` tells Codex to read ENGINEERING and ALLAGENTS *in full*
  as tool output, which at 4.4 bytes/token is a different cost and a different failure
  mode (a truncated read). I did not measure a Codex session.
- **`docs/agent-workflow.md`** (62,590 bytes), the file the dispatch stub points every run
  at. I grepped it for file-era prose (one stale table row at line 1003 naming a gate over
  `tasks/`) and did not read it.
- **A full `--tag quick` sweep on Fable.** Priced (finding 7), not run; it is ~$23 and
  five minutes, and a synthesis session or the owner should decide to spend it.
- **Whether the preamble returns without `bgIsolation: none`** (finding 5) — needs a
  settings edit, which is a write.
- **`MEMORY.md`'s 49 entries** as content: some are certainly file-era ("Reading a task
  record with an interpreter is refused", "task YAML read guard"). Private, outside the
  repo, and outside my write.

## Questions for other auditors

- **02 (SQLite storage):** does `agentjobs quotations` with no arguments scan the SQLite
  store or the frozen files? It said 417 records, which matches `list`, so probably the
  store — but its `--help` and `--storage-dir` say YAML. Same question for the "SQLite
  importer refuses a record carrying one" clause in the paraphrase rule.
- **05 (gate and receipts):** is finding 1 the whole of task-409, or is there a path-based
  narrowing still live that `gate_scope.py`'s three-entry table performs? If the `docs/*`
  and `*.md` entries can skip pytest on a prose change, does the documentation-contract
  test that reads the bundle run in the stages `DOCS_STAGES` names?
- **04 (dispatch/finish):** the rendered `PROMPT_STUB` was read as a constant, not
  re-rendered with a live runner. Does a real 2.1.269 `--bg` dispatch still park on
  anything, given the harness preamble now says to work in place (finding 5)? If not, the
  stub's harness sentence and ALLAGENTS.md:331-334 can both go.
- **09 (API/MCP):** the two identical instruction blocks (finding 3) — is the 512-byte
  leading rule the only text a Claude Code client injects, or the whole
  `SERVER_INSTRUCTIONS`? My context shows the whole thing, twice.
- **01 (regression sweep) / 14 (meta):** the first audit's 01 file used a ÷4 token
  estimator that task-301 showed was 25% low. The numbers in §0 use the 3.0 ratio from
  the production tokenizer; if any auditor quotes a token count from the 2026-08-21
  files, it is the wrong denominator.
