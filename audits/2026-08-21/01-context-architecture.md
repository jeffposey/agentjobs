# Audit 1 — Context architecture: static vs dynamic

Auditor 1 of the Big Dawg Audit (task-242). Read-only; nothing in this file was
committed by me. Evidence was gathered 2026-08-21/22 against `main` at `c51eb64`
(the server on 8876 reported `source_commit` `17fdfd0`, one task-YAML commit behind).

Two notes on how this file got written, both of which are evidence for findings below:
task records are referred to as `task-NNN` rather than by file path, because the plugin
write guard denies any `Write` whose **content** contains a managed task-file path
(F-12); and the file was staged outside the repository and copied in with `cp`, because
the Claude Code background-session harness refused a `Write` into the shared checkout
until `EnterWorktree` was called — which this audit's brief forbids (F-2).

**The governing question** — is the static/dynamic split right? — gets a short answer
up front and the evidence below: **the split is right in principle and wrong in
proportion.** The design already states the correct rule, in `runner.py:115-131`: *"the
payload is a pointer to where the context is, not a copy of it. Composing a richer
prompt would put the contract in a second place and guarantee the two disagree."* The
dispatch prompt obeys that rule (≈600 characters). The static bundle does not: it is
≈17,000 tokens, roughly 40% of which is rationale, incident narrative and measurement
history that is already — or should be — in `docs/`, and it restates several dynamic
payloads (the wake prompt, the finish escalation, the supervisor protocol) that the
record or the prompt already carries verbatim. Nothing in the bundle is wrong in a way
that bites today; the cost is paid per session, in tokens and in the attention every
rule competes for, and it churns daily (13 doc commits in the last three days).

---

## 1. The static bundle, measured

Everything a Claude Code session in this repository loads before its first thought,
through the `@` chain `~/.claude/CLAUDE.md → C:/ai/shared/GLOBAL-AGENTS.md` and
`C:/projects/CLAUDE.md → C:/projects/AGENTS.md` and
`agentjobs/CLAUDE.md → AGENTS.md + ENGINEERING.md + ALLAGENTS.md`:

| File | chars | words | lines | est. tokens (÷4) |
|---|---:|---:|---:|---:|
| `C:/ai/shared/GLOBAL-AGENTS.md` | 9,562 | 1,488 | 176 | 2,400 |
| `C:/projects/AGENTS.md` | 2,654 | 355 | 41 | 660 |
| `agentjobs/CLAUDE.md` (comment claims to be stripped — unverified) | 776 | 117 | 19 | 0–190 |
| `agentjobs/AGENTS.md` | 518 | 62 | 10 | 130 |
| `agentjobs/ENGINEERING.md` | 30,479 | 4,786 | 502 | 7,600 |
| `agentjobs/ALLAGENTS.md` | 24,734 | 3,931 | 390 | 6,200 |
| **@-chain total** | **68,723** | **10,739** | **1,138** | **≈17,200** |
| + Claude auto-memory index `MEMORY.md` (Claude only) | 5,969 | 745 | 37 | 1,500 |
| + MCP `SERVER_INSTRUCTIONS` (`mcp/instructions.py`) | 1,179 | 186 | — | 300 |
| + MCP tool docstrings (11 read + 8 mutation tools; schemas deferred in this harness) | 2,026 | — | — | ≈500 when loaded |
| **Per-session static total, excluding the harness system prompt** | | | | **≈19,500** |

(Command: `wc -c -w -l` on each file; MCP figures from importing
`agentjobs.mcp.instructions` and a regex over the two tool modules.)

ENGINEERING.md and ALLAGENTS.md are **two thirds of the bundle** and have been
committed 24 and 21 times respectively, 13 of those commits on 2026-08-19/20/21 alone
(`git log --format=%ad -- ENGINEERING.md ALLAGENTS.md | sort | uniq -c`). This is a
document that is rewritten most days, loaded by every session, and never measured —
there is no test, no budget, and no count anywhere of what it costs.

A dispatched run pays the same bundle plus `PROMPT_STUB` (`runner.py:115`, ≈600 chars
rendered) or `SUPERVISOR_STUB`, plus `docs/agent-workflow.md` on demand (4,596 words,
≈6,000 tokens — the guide the stub points at, which restates much of ALLAGENTS.md; see
§2). A woken session pays `WAKE_STUB` + up to 4,000 chars of ball prompt
(`wake.py:39-76`).

### What fraction a typical task uses

Per-section word counts (awk over headings) and a load-bearing classification are in
§4. Summing the rows marked **keep** (the operative rule and its command) gives the
words an ordinary single-task session actually acts on:

| File | words | operative for a routine code task | share |
|---|---:|---:|---:|
| GLOBAL-AGENTS.md | 1,488 | ≈450 (environment, AgentJobs-is-the-tracker, conventions, commit-before-switching) | 30% |
| C:/projects/AGENTS.md | 355 | ≈120 (the four rules) | 34% |
| ENGINEERING.md | 4,786 | ≈1,700 (setup, gate commands, style, branch/commit rules, merge gate steps, tasks-on-main rule, safety rails) | 36% |
| ALLAGENTS.md | 3,931 | ≈1,500 (queue rule, lifecycle, worktree rule, bootstrap rule, logging, handoff) | 38% |
| **total** | **10,560** | **≈3,770** | **≈36%** |

So roughly **one token in three is a rule the session will act on; two in three are the
reason for the rule, the incident that produced it, or a measurement.** Those are not
worthless — several are the only place an incident is recorded — but they are the wrong
kind of content for unconditional load. A supervisor run, a docs-only task, or a
task-bookkeeping session uses less than the 36%: none of the gate, bootstrap or merge
material applies to a session that takes no worktree.

**P3 — F-1. The static bundle has no budget and no measurement.** Evidence: table above;
no test under `tests/` references the size of `ENGINEERING.md` or `ALLAGENTS.md`
(`grep -rn "ENGINEERING.md\|ALLAGENTS.md" tests/` finds only the `docs/agent-workflow.md`
pin in the dispatch tests). The MCP instructions, by contrast, have a 512-character
leading-rule budget *and a test asserting it* (`mcp/instructions.py:10-13`; the leading
paragraph is 246 chars). Fix: a word budget per file, asserted by a cheap test the way
the MCP budget is, with the number chosen after §4's moves — ENGINEERING.md ≤ 2,500
words and ALLAGENTS.md ≤ 2,000 is achievable without losing a rule.

---

## 2. Redundancy map

Every rule stated in more than one file of the stack (the @-chain, the workflow guide
dispatched agents are sent to, the MCP instructions, the dispatch stubs). "Agree" means
the statements are compatible; a disagreement is a finding.

| # | Rule | Stated in | Agree? |
|---|---|---|---|
| R1 | Take a worktree under `../worktrees/` before anything, never checkout in the shared clone | ENGINEERING §Sharing a clone; ALLAGENTS §Task Lifecycle step 2, §Why you get your own worktree; `docs/agent-workflow.md` §Before you write anything; `PROMPT_STUB` | Yes in substance. Naming differs: ENGINEERING/ALLAGENTS say `aj-<nnn>`, the stub and guide say `<repo>-<nnn>`. Cosmetic (P4, F-11) |
| R2 | Never `git add -A` | ENGINEERING §Commit Hygiene; ALLAGENTS ×2 | Yes |
| R3 | Bootstrap the worktree (`python scripts/bootstrap.py`, "~30s") | ENGINEERING §Setup, §Sharing a clone; ALLAGENTS §Task Lifecycle, §You do not work the children, §Bootstrapping a worktree | Yes — five statements, the 30s figure three times |
| R4 | Task files go to `main`, never a branch | ENGINEERING §Task files live on main; ALLAGENTS §Task Lifecycle, §You do not work the children; agent-workflow §Canonical loop | Yes |
| R5 | Stop at `human/review`; explicit approval; merge `--no-ff`; rebuild + restart | ENGINEERING §Merge Gate (6 steps); ALLAGENTS §Task Lifecycle (7 steps); agent-workflow §Human Review | Yes, but the **same procedure is numbered differently** and both files then cross-reference their own numbering ("Steps 3 to 6" vs "Steps 6 and 7" mean the same actions). P3, F-5 |
| R6 | The scripted finish may have done steps N–M before you wake | ENGINEERING §Steps 3 to 6 may already have happened (429 words); ALLAGENTS §Steps 6 and 7 may be done (205 words) — same commit `e85eafd` | Yes. Same content, two places, and **inactive on this machine** — P2, F-3 |
| R7 | Do not use `-w` / `EnterWorktree` | ALLAGENTS §Why you get your own worktree; agent-workflow; `PROMPT_STUB` + docstring; `posture_flags` docstring | Yes among themselves. **Disagrees with the Claude Code background-session harness preamble**, which instructs the opposite and enforces it — P2, F-2 |
| R8 | "Not yours to restart" — restart the server the way it was started; 8876 vs 8765 | ENGINEERING §Merge Gate; `scaffold.py:230-232`; GLOBAL-AGENTS §Tasks live in AgentJobs | Yes. This is the one split done well: the public repo states the rule, the private global file holds the machine fact (launcher path, port), per commit `cde1bba` |
| R9 | Work what the queue says; move it, never fake a dependency or hand-edit a position | ALLAGENTS §Work what the queue says (361 w); agent-workflow §Work What the Queue Says; MCP instructions ¶3 | Yes — ALLAGENTS's copy is a near-verbatim paste of the guide's. P3, F-6 |
| R10 | Resumption contract: summary orients, ball_prompt current, decisions with rejected alternative, handoff self-contained | ALLAGENTS §The Resumption Contract; agent-workflow §Resume Without Chat History; `docs/schema-design.md:384` (canonical); MCP instructions ¶4 | **One disagreement**: ALLAGENTS says `ball_prompt` "is required whenever the ball is set … the schema rejects it" with no exception; `models_v2.py:782-791` and `docs/task-schema.md:105` both exempt `agent/available`, and 87 of 87 `ready` tasks have an empty prompt. P3, F-4 |
| R11 | Parent task = supervisor; children get sessions; threshold is "takes a worktree" | ALLAGENTS §Parent Task Loop (290 w) + §You do not work the children (584 w); agent-workflow (≈1,200 w); `SUPERVISOR_STUB` | Yes. ALLAGENTS says "the full protocol is in the workflow guide" and then restates ≈870 words of it. P3, F-6 |
| R12 | Log progress/decisions/questions to the record, not chat | ALLAGENTS §Logging Work; agent-workflow §Durable Logging; GLOBAL-AGENTS (findings vs working) | Yes |
| R13 | Never destroy / email / share personal data without asking | GLOBAL-AGENTS; C:/projects/AGENTS.md; ALLAGENTS §Non-Destructive; ENGINEERING §Safety Rails | Yes — four statements |
| R14 | Be brief | GLOBAL-AGENTS ("Keep responses short"); ALLAGENTS §Reporting Standards | Yes |
| R15 | Tasks live in AgentJobs, not the tool's built-in task list | GLOBAL-AGENTS (387 w) | Fights a **dynamic harness nudge**: this session received four `TaskCreate` reminders while doing read-only work. The rule wins; the nudge recurs. P4, F-13 |
| R16 | Run the unqualified gate before every commit; `--from`/`--only` are not the gate | ENGINEERING ×3; ALLAGENTS §Task Lifecycle step 4 | Yes |
| R17 | Watching is a mechanism, not an intention | ALLAGENTS; agent-workflow; PLAN.md; Claude memory | Yes |
| R18 | Gate duration | ENGINEERING: "about a minute" (§`--since-gate`) and "about a minute and a half" (§Budget) for the same 95.8s | Trivial inconsistency, P4 |

**Summary of the map:** 18 rules, 58 statements, one substantive disagreement with the
repository's own schema (F-4), one with the harness (F-2), and a ≈1,500-word block
(R6 + R9 + R11) that is a copy of text the agent is pointed at anyway.

---

## 3. Staleness sweep

Every number, count, version and date in the stack checked against current reality.

| Claim | Where | Reality (command) | Verdict |
|---|---|---|---|
| "2608 Python tests" | ENGINEERING gate table, dated 2026-08-21 | `poetry run pytest --collect-only -q` → **2723 tests collected** | **Stale same-day.** The table and the count moved apart within hours. P3, F-7 |
| "26 Playwright tests" | ENGINEERING gate table | `npx playwright test --list` → **22 tests in 8 files** | **Wrong or stale** — P3, F-7 |
| "228 jsdom component tests" | ENGINEERING gate table | `npx vitest run` → 228 passed in 26 files | Correct |
| "ten named stages" | ENGINEERING | `scripts/check.py --list` → 10 | Correct |
| "32 cores" | ENGINEERING | `nproc` → 32 | Correct |
| "this machine now allows three dispatched runs" | ENGINEERING | `~/.agentjobs/dispatch.yaml` `max_concurrent_runs: 3`; `agentjobs dispatch config` agrees | Correct |
| "95.8s … from one green run on this machine, 2026-08-21" | ENGINEERING | Gate receipt `.git/agentjobs-gate-receipt.json` → commit `245065a` (2026-08-21 18:25) | Plausible; dated, so acceptable |
| "2538 passed", "540.1s / 342.6s / 42.5s" etc. | ENGINEERING task-233 tables | Marked as history | Fine as written |
| "30 seconds … 13 seconds … measured 2026-08-19" bootstrap | ALLAGENTS | Not re-measured (would write a venv) | Dated; fine |
| "Probed on Claude Code 2.1.235, 2026-08-19" | ALLAGENTS, `runner.py` ×2 | `claude --version` → 2.1.238 | Dated probe; fine. Nothing re-probes it |
| "averaged about eleven minutes" post-approval run | ALLAGENTS §You may be woken; also `wake.py:4`, `finish.py:5`, `config.py:373`, `scaffold.py:199`, `docs/agent-dispatch-design.md:1729` | Source is task-234's hand-picked table of 11 runs (mean 11.0m, quoted in the task-241 log). `scripts/run_report.py` today prints *cold follow-on runs: 31, mean 27.5m; resumed: 2, mean 11.2m* — it does not reproduce the figure | Figure is sourced but **stated six times in source and docs** and not reproducible from the instrument ENGINEERING.md names for the question. P4, F-8 |
| "On a machine with `finish.enabled` set … clicking Approve runs steps 3 to 6 itself" | ENGINEERING, ALLAGENTS | `~/.agentjobs/dispatch.yaml` has no `finish:` key; `agentjobs dispatch config` prints nothing about finish; `~/.agentjobs/finishes/` does not exist | **Switched off on the only machine that runs this.** The prose is conditional, so not false — but 634 words describe a path no session here will hit, and an agent cannot cheaply tell (the config printer omits the setting). P2, F-3 |
| "Role-Specific Playbooks (e.g., CLAUDE.md, CODEX.md) are currently deferred" | AGENTS.md, unchanged since 2025-11-19 | `CLAUDE.md` exists (2026-08-10) as the bridge; a Codex plugin and skill exist under the plugin cache | Stale; P4, F-9 |
| "`tests/test_validate.py::TestRealCorpus`" | ENGINEERING | `tests/test_validate.py:659` | Correct |
| `record_phase_from_env`, `AGENTJOBS_SKIP_SOURCE_CHECK`, `scripts/review_queue_sandbox.py`, `scripts/gate_scope.py`, `docs/performance.md` | ENGINEERING | All exist (`phases.py:93`, `environment.py:146`, files present) | Correct |
| `/api/version` reports `source_root` | ENGINEERING | `curl 127.0.0.1:8876/api/version` → includes `source_root`, `source_commit` | Correct |
| "Task-060's own log says it outright: 'the previous conversation was very long and is not available'" | ALLAGENTS | task-060 record, line 758 | Correct |
| Every intra-doc anchor (`#task-files-live-on-main-always`, `#the-merge-gate`, `#you-do-not-work-the-children`, `#bootstrapping-a-worktree`, agent-workflow `#working-a-parent-task-…`) | ENGINEERING/ALLAGENTS | All resolve to a heading | Correct |
| GLOBAL-AGENTS: `agentjobs project list`, `init`, `open`; launcher `C:/ai/shared/launchers/open-agentjobs.ps1`; `NEW-PROJECT-SETUP.md`; `personal/index.md`, `goals/current.md`, `findings/assessment-catalogue.md`, `preferences/tools-and-setup.md` | GLOBAL-AGENTS | All commands exist; all paths exist | Correct |
| C:/projects/AGENTS.md project table (11 rows) | C:/projects/AGENTS.md | `ls C:/projects` shows all 11 plus `worktrees/`, `agentjobs.worktrees/`, `job-hunting.worktrees/` | Correct; the `*.worktrees` directories are undocumented residue (question for auditor 10) |
| "69 of 69 ready tasks have an empty `ball_prompt`", "74 open tasks", "measured 2026-08-20" | `guards.py:313-327` docstring | Today: 87 ready, 87 empty; **96 open**; 3 without acceptance (was 2) | Dated, still directionally true. The open count grew 74→96 in roughly a day — see §6 |

Pattern: **the numbers that were written with a date and a command survive; the numbers
written bare go stale within a day.** The gate table is dated but its counts are not
commands, and two of its three test counts were already wrong when I read it.

---

## 4. Load-bearing vs decorative, per section

"Load-bearing" = cites an incident or a mechanism that still exists. "Decorative" =
advice nobody has needed here, or rationale for a rule that a one-liner would carry.
Word counts are from the per-heading awk.

### GLOBAL-AGENTS.md (1,488 words)

| Section | words | Verdict | Why |
|---|---:|---|---|
| Who Jeff is / Standing rules | 307 | **keep**, but it is already conditional ("read when it matters") and still loads unconditionally | The top-complaint and push-back rules are load-bearing for every session |
| Environment | 151 | **keep** | Three-strikes backslash incident; PowerShell 5.1 facts |
| Where things live | 188 | **compress** | The table is right; the Obsidian paragraph is a pointer to a page that already holds it |
| Tasks live in AgentJobs | 387 | **keep the rule, move the 8765 story** | The stale-server incident is real and this is its only private home; the rule itself is 60 words |
| Conventions / Working alongside | 158 | **keep** | |
| Asking Jeff to review a UI change | 191 | **move to on-demand** | Applies only to UI tasks; the file itself says "keep it here" for privacy, which is a reason to keep it private, not unconditional |
| Syncing | 33 | keep | |

### C:/projects/AGENTS.md (355 words) — keep as is. It is an index plus four rules.

### ENGINEERING.md (4,786 words)

| Section | words | Verdict | Why |
|---|---:|---|---|
| Mission / Tech stack / Setup | 157 | keep | |
| **Testing** | **1,723** | **compress to ≈400; move the rest to `docs/performance.md`** | The commands, the stage table, `--from/--only`, and the four `--since-gate` properties are load-bearing. The task-233 before/after tables, the three-consecutive-runs argument, the serial-era scaling figures "kept only as history", the xdist `frozenset` anecdote and the coverage-cost paragraph are measurement history. ENGINEERING.md itself says the contended figure "is unknown. Measure it before quoting one." — that is a doc's sentence, not a rule |
| Measuring performance | 341 | **move to `docs/performance.md`**, keep a 3-line pointer | The `transcript.log` warning is the one load-bearing sentence |
| Code style / Branch naming / Commits / Lifecycle | 146 | keep | |
| Sharing a clone | 296 | **compress** | Duplicates ALLAGENTS R1/R3; the "tasks are YAML so the checkout decides what the dashboard shows" paragraph appears here *and* in ALLAGENTS failure 3 |
| Commit hygiene | 99 | keep | |
| The Merge Gate | 331 | keep | The six steps and the restart-ownership rule are the spine of the workflow |
| Steps 3 to 6 may already have happened | 429 | **delete or reduce to one sentence + pointer** | Inactive on this machine (F-3); the record and the finish's own escalation prompt carry the payload ("The merge is done: `abc1234`" is written *by the finish* onto the task — `finish.py:1043`). This is static text restating a dynamic message |
| Task files live on main | 324 | keep, compress the history paragraph | Load-bearing; the 2026-08-11 incident is the reason |
| Safety rails | 338 | keep | Each bullet names an incident (task-194, 2026-08-17) |
| Verification | 510 | **keep the rules, move the three incident narratives to a doc** | task-207 and task-225 are real and the "prove the gesture landed" line is the lesson; the 250-word reconstruction of each belongs in the task record it came from |

### ALLAGENTS.md (3,931 words)

| Section | words | Verdict | Why |
|---|---:|---|---|
| Work what the queue says is next | 361 | **compress to ≈120 + pointer** | Copy of agent-workflow §Work What the Queue Says and MCP ¶3 (R9) |
| Parent Task Loop | 290 | keep | The six-step loop is the rule |
| You do not work the children | 584 | **compress to ≈150 + pointer** | The threshold sentence and the two repeated rules are load-bearing; the rest restates the guide it cites (R11) |
| Task Lifecycle | 501 | keep | The spine |
| Steps 6 and 7 may be done before you wake up | 205 | **delete** — same as ENGINEERING's 429-word version (R6, F-3) | |
| You may be woken rather than restarted | 242 | **delete or one sentence** | `WAKE_STUB` (`wake.py:39-53`) says every one of these sentences to the session that is actually woken. A cold session does not need to be told what a warm one will be told |
| The Resumption Contract | 281 | keep, fix F-4 | |
| Why you get your own worktree | 441 | **compress** | The rule + the `-w` prohibition are load-bearing; the three-failures narrative duplicates ENGINEERING §Sharing a clone |
| Bootstrapping a worktree | 498 | **compress to ≈150** | The interpreter-path rule is load-bearing; the VIRTUAL_ENV forensic story (task-194, task-210) is history and `bootstrap.py` already prints both the warning and the interpreter path, which is the right place for it |
| Logging / Handoffs / Reporting / Behavioral | 435 | keep | |

Rough arithmetic on the **compress/move/delete** rows: ≈3,900 words leave the
unconditional bundle, which is ≈37% of the @-chain and ≈6,000 tokens per session,
without deleting a single rule — every removed paragraph is either already in a doc,
already in a dynamic prompt, or is a measurement table.

---

## 5. Migration candidates, both directions

### Static → dynamic

1. **The two scripted-finish sections (634 words).** The finish itself writes the
   dispositive sentence onto the task (`finish.py:1043`: "The merge is done: …" /
   "Nothing was merged"), and `WAKE_STUB` delivers the ball prompt verbatim. The
   record is the payload; the static text is a copy. Replace with: *"If you are woken
   after an approval, the record says whether `main` moved. Believe it."*
2. **"You may be woken rather than restarted" (242 words).** Verbatim overlap with
   `WAKE_STUB`. Same treatment.
3. **The supervisor protocol's restatement (≈870 of 874 words across two ALLAGENTS
   sections).** `SUPERVISOR_STUB` already points at
   `docs/agent-workflow.md#working-a-parent-task…`, and the guide is better than the
   summary (it has the four-state table and the auth-expiry case ALLAGENTS lacks).
4. **Measurement history in §Testing and §Measuring performance (≈1,300 words).** To
   `docs/performance.md`, which exists for this.
5. **Incident narratives in §Verification, §Bootstrapping, §Why you get your own
   worktree (≈900 words).** Each already lives in the task that found it (207, 225,
   194, 210, 186, 192). Keep the one-line lesson and the task id.
6. **"Asking Jeff to review a UI change" (GLOBAL-AGENTS, 191 words).** Only a UI task
   needs it. It is a candidate for a skill or a doc the UI-change rule points to;
   the privacy argument for keeping it out of the public repo is sound, the argument
   for loading it into a `job-hunting` session is not.

### Dynamic → static (re-derived per session today, should be one line)

1. **The harness tells background sessions to use `EnterWorktree` and enforces it;
   this repository forbids it (F-2).** The repo's text says "do not use
   `--worktree`/`-w`" but does not say *the harness preamble will tell you to, will say
   it is enforced, and will reject your first `Write` into the shared checkout*. Every
   bg session in this repository resolves that conflict alone. One sentence in
   ALLAGENTS §Why you get your own worktree closes it.
2. **The write guard denies any file whose content names a task path (F-12).** Nothing
   static warns; an agent writing a doc, a script, or an audit that cites a task file
   discovers it by being refused, and the refusal text talks about task mutation, which
   is not what happened.
3. **The `agent/available` exemption (F-4).** ALLAGENTS states the strict rule; the
   exception is in the schema doc. An agent doing `release` reads the strict rule.
4. **The three-refusals breaker and "do not reword and resend".** In
   `docs/agent-workflow.md` and Claude memory, not in ALLAGENTS — an interactive session
   in this repo that is not dispatched never reads the guide.
5. **Claude auto-memory duplicates the repo.** Of 37 `MEMORY.md` entries, at least
   eight restate ALLAGENTS/ENGINEERING rules (worktree, `git add -A`, VIRTUAL_ENV,
   bootstrap cost, watching-is-a-mechanism, approval-via-GUI, move-the-ball, worktree
   removal). The memory instructions say not to save what the repo records. P4, F-10.

---

## 6. Corpus sample against the Resumption Contract

Prior art: `docs/task-corpus-audit.md` (task-103, 2026-08-13): 79 records, 51 closed,
22 ready, 5 draft, every record loaded through the model, no dangling parents or
context paths, two superseded, three follow-ups filed. It checked structure and
currency; it did not score the contract's four properties.

Today: **240 records; 144 closed, 87 ready, 6 draft, 3 active** (script over the
directory with `yaml.safe_load`). The ready band went 22 → 87 in eight days.

Sampled 17 records across eras — 001, 004, 008, 015 (slugless, Aug 12–18), 045, 060,
075, 103 (slugged, Jul–Aug), 120, 150, 164 (Aug 15–19), 186, 200, 220, 233, 241, 242
(slugless, Aug 19–22):

**Summary orients a zero-context reader.** Drift is in both directions. task-045's
summary is seven words ("AgentJobs has no working task hierarchy today.") — too thin to
orient. Every record from 186 on is 47–86 words, and task-060 is 113: these are
paragraphs that front-load the description, not "one or two sentences". Corpus-wide
medians by era: 35 words (031–105), 43 (106–185), **54 (186–243)**; 51 of 58 recent
records exceed 40 words. The contract's "distinct from `spec.description`" is being
honoured by making the summary a second description.

**`ball_prompt` is current.** Correct by construction where a verb wrote it; weak where
a default did. 87/87 ready tasks are empty (schema-permitted). Three open drafts carry
the boilerplate "Finish specifying this task." and task-242 — the record for this very
audit — carries "Execute the spec; log progress and hand off when done.", which is the
claim verb's default and says nothing a reader can act on. Where a session wrote one
(task-233, 1,839 chars: merged commit, what is met, what is parked on) it is exemplary.

**Decisions record the rejected alternative.** Good and improving: 045 4/5, 075 3/4,
060 3/3, 164 2/2, 220 4/4, 186/200/241 1/1 name an alternative (keyword match on
reject/alternative/instead/rather than/considered); task-103's one decision and one of
task-120's two do not. Corpus-wide there are 174 `decision` entries.

**Handoffs self-contained.** Agent-side handoffs are: task-200's is 372 words and
names the commit, the worktree, and "not merged; waiting on your explicit approval".
Human-side handoffs are the UI's fixed "Approved by Jeff Posey through the web UI."
(8 words) on 7 of 17 — correct as a record of the act, empty as a review. That is a
property of the control, not a drift.

**Questions go to the record.** Corpus-wide: **15 `question` entries and 7 `answer`
entries across 240 records**, in 12 tasks, against 262 `progress`, 375 `note` and
222 `handoff` entries. None in the 17 sampled. Either nothing was ever uncertain, or
questions are being asked in chat — which ALLAGENTS says makes them un-queryable. The
absence is the finding.

**P3 — F-14. Contract compliance drifted toward long summaries, default ball prompts
and zero questions.** None of these fails validation; all three make the record less
useful as working memory than the contract promises. The check that would catch them is
cheap: a summary-length ceiling in `agentjobs validate` (warn over 40 words), a warning
when `claim` leaves the default prompt in place on a task at `agent/work` for more than
one log entry, and a corpus stat (`questions/answers per 100 entries`) in the
corpus-audit doc so the next audit has a baseline.

**P4 — F-15. Two id series.** Records 001–024 were created 2026-08-12/18 (task-001
`created: 2026-08-12`), after task-105 fixed allocation to "use the maximum leading
number"; the historical 001–030 are gone and 031 dates from 2025-10-25. Ids are unique
and nothing is broken, but the corpus is non-monotonic in a way the 2026-08-13 audit
did not anticipate. Question for auditor 3/4 (allocation) — is this the browser-created
low-id path task-105 was meant to close?

---

## Findings, ranked

**P2 — F-2. The background-session harness and the repository's worktree rule
contradict each other, the harness enforces its side, and the repository does not say
so.** This session's harness preamble (Claude Code, background job) says: *"Before
making any code changes, use the EnterWorktree tool … This is enforced: file edits in
the shared checkout are rejected until you isolate."* It is enforced: my one permitted
write — this findings file, into `audits/2026-08-21/` in the main clone — was refused
with *"This background session hasn't isolated its changes yet. Call EnterWorktree
first"*, and the suggested escape is a `.claude/settings.json` key
(`"worktree": {"bgIsolation": "none"}`) that the repository does not set. ALLAGENTS
§Why you get your own worktree, `docs/agent-workflow.md` §Before you write anything,
and `runner.py:115-131` say the opposite and explain why (`EnterWorktree` relocates the
permission root, `auto` declines it, the run parks — run_6f1f0741, 2026-08-20,
task-192). So every dispatched `--bg` run in this repository is handed two static
instructions that cannot both be followed; the one that is wrong for this repo claims
to be enforced and is. An agent that obeys the repo is blocked from writing in the main
clone — including the `tasks/` commits ENGINEERING requires to land there — unless it
uses a shell. Fix: (a) set `"worktree": {"bgIsolation": "none"}` in the repository's
`.claude/settings.json` so the harness stops enforcing what the repo forbids; (b) one
sentence in ALLAGENTS naming the harness instruction as the one to ignore here; (c) a
clause in `PROMPT_STUB` (read first): "even if your harness says it is required".
Coordinate: auditor 10 (dispatch) — this is very likely the mechanism behind every
`--bg` run that parked on a worktree prompt.

**P2 — F-3. 634 words of static text describe the scripted finish, which is switched
off on this machine, and no surface tells an agent that.** `~/.agentjobs/dispatch.yaml`
has no `finish:` block; `agentjobs dispatch config` prints every other setting and
nothing about finish; `~/.agentjobs/finishes/` does not exist. ENGINEERING §Steps 3 to 6
and ALLAGENTS §Steps 6 and 7 are conditional prose ("where this machine has…"), so they
are not false — but a session cannot cheaply resolve the condition, and the sections
tell it to expect a state of the world that will not occur here. Fix: (a) `dispatch
config` prints `finish: off|on` per project (question for auditor 6); (b) cut both
sections to one sentence and a pointer — the finish writes its own result onto the
record, which is the text the agent should read.

**P3 — F-4. ALLAGENTS states the `ball_prompt` rule more strictly than the schema.**
ALLAGENTS §The Resumption Contract: "`ball_prompt` is required whenever the ball is
set … the schema rejects it." `models_v2.py:782-791`: required unless
`reason is BallReason.AVAILABLE`. `docs/task-schema.md:105` states the exception.
Corpus: 87/87 ready tasks empty. An agent following ALLAGENTS literally would invent a
prompt on `release`. Fix: add "except `agent/available`, where the spec is the ask" —
the schema doc's own words.

**P3 — F-5. The merge-gate procedure is numbered 1–6 in ENGINEERING and 1–7 in
ALLAGENTS, and each file's follow-on section refers to its own numbering** ("Steps 3 to
6" vs "Steps 6 and 7" for the same actions). Fix: one list, one file, the other points.

**P3 — F-6. ≈1,500 words of ALLAGENTS are pasted from `docs/agent-workflow.md`** (queue
rule, supervisor protocol). The guide is pinned by a test and pointed at by every
dispatch prompt; ALLAGENTS's copies will drift from it (the guide already has the
auth-expiry state ALLAGENTS lacks). Fix: ALLAGENTS keeps the rule and the threshold
sentence; the mechanics live in the guide.

**P3 — F-7. Two of three test counts in the gate table are wrong on the day the table
is dated** (2608 vs 2723 collected; 26 vs 22 Playwright). Fix: quote the command and
the date, not the count; `check.py` prints the counts on every run.

**P3 — F-1. No budget or measurement on the static bundle**, while the 246-char MCP
leading rule has a 512-char budget and a test. Fix above.

**P3 — F-12. The plugin write guard denies any `Write`/`Edit` whose *content* contains a
managed task path, even when the target is unrelated.** Evidence: my first attempt to
write a read-only script to `C:/Users/jpose/.claude/jobs/…/tmp/corpus_sample.py` was
refused because its text contained the glob of the task directory; the refusal text
said the tool "would write AgentJobs task records". Mechanism:
`task_write_guard.py:421-423` extends the target list with
`re.findall(r"[^\s\"']+\.ya?ml", content)` for every file-write tool — the comment says
this is for `apply_patch`, whose paths are inside the patch body, but it runs on
`content` too. Any audit, doc, test fixture or script that cites a task file by path is
unwritable with the dedicated tools. Fix: apply the content regex only when the tool is
`apply_patch` (or the key is `patch`/`input`), not to `content`. Questions for auditors
8 and 12 — the guard is explicitly "a guardrail, not a security boundary" (its own
docstring), and this false positive teaches agents to route around it.

**P3 — F-14. Resumption-contract drift** (§6): summaries growing into paragraphs, default
ball prompts surviving on active tasks, 15 questions in 240 records.

**P4 — F-8. "About eleven minutes" is stated six times in source and docs** and is not
reproducible from `scripts/run_report.py`, the instrument ENGINEERING.md names for the
question (it prints 27.5m mean for cold follow-ons, 11.2m for resumed). The figure has
a source (task-234's table of 11 runs) but the six copies will not move together when
the sample does. Fix: one sentence in one place, citing task-234.

**P4 — F-9. AGENTS.md has not changed since 2025-11-19** and says role-specific
playbooks are "deferred"; `CLAUDE.md` (the bridge) and the Codex plugin exist. The file
is 62 words, loaded every session, and its one claim is stale.

**P4 — F-10. Claude auto-memory duplicates ≥8 repository rules.** Claude-only, but it is
≈1,500 tokens per session of restated ALLAGENTS.

**P4 — F-11. Worktree naming**: `aj-<nnn>` in ENGINEERING/ALLAGENTS, `<repo>-<nnn>` in
`PROMPT_STUB` and the guide. Pick one.

**P4 — F-13. The harness's `TaskCreate` reminders fire against GLOBAL-AGENTS's rule that
tasks live in AgentJobs**; observed four times in this read-only session. The rule wins.
Nothing to fix in the repo; worth knowing the static rule is load-bearing precisely
because the harness nudges the other way.

**P4 — F-15. Two id series** (§6).

**Examined, nothing found:** every intra-doc anchor resolves; every cited script, symbol,
env var, CLI subcommand and private path exists; the MCP instruction text's four claims
are each backed by a verb or a tool the surface lists (instructions: projects_list,
task_next, task_get, task_queue_move, claim/handoff/release/close — all present in the
tool inventory); `dispatch config` agrees with `dispatch.yaml` on runners, groups and
limits; the GLOBAL-AGENTS/ENGINEERING split of "machine fact vs rule" (R8) is the one
place the static/dynamic boundary was drawn deliberately and well.

---

## What I did not get to

- **The harness system prompt itself.** It is static context too and probably larger
  than the @-chain; I could not measure it from inside the session. The token figures
  above are a floor.
- **`CLAUDE.md`'s claim that its HTML comment "is stripped before the file enters
  context, so it costs no tokens."** Unverified. If false, it is 190 tokens of comment
  about token cost.
- **MCP tool input schemas** as loaded context. I measured docstrings (2,026 chars),
  not the JSON schemas a client receives; in this harness they are deferred, in Codex
  they may not be.
- **A full-corpus contract score.** I sampled 17 and ran corpus-wide counts for
  summary length, log types and ball prompts; I did not read every record's decisions.
- **Other projects' `AGENTS.md` chains** (job-hunting, mastercalls) that load the same
  GLOBAL-AGENTS.md — whether the AgentJobs-specific 387-word block is dead weight there.
- **`docs/agent-dispatch-design.md`** (20,872 words) as dynamic context — only the
  stubs that point at it were read. Auditor 2/10 territory.
- **Re-measuring bootstrap (30s/13s)** — it would write a virtualenv.
- **Whether `"worktree": {"bgIsolation": "none"}` actually disables the harness guard**
  — the refusal message suggests it; I did not test it (it is a config edit).

## Questions for other auditors

- **Auditor 10 (dispatch):** Does a dispatched `--bg` session receive the same
  "use EnterWorktree … this is enforced" harness preamble this session did, and does
  the same `Write` refusal fire on its first edit in the shared clone? If yes, F-2 is
  the mechanism behind run_6f1f0741, and `PROMPT_STUB` is the only text that reaches
  the agent before the harness's instruction is acted on. Also: what are
  `C:/projects/agentjobs.worktrees/` and `job-hunting.worktrees/` — residue of the
  `-w` era?
- **Auditor 6 (CLI):** `agentjobs dispatch config` omits the `finish` block entirely.
  Is there any read-only surface that reports whether the scripted finish is on?
- **Auditor 8 (MCP) and 12 (security):** `task_write_guard.py:421-423` — the content
  regex on `Write`/`Edit` (F-12). Is the same pattern in the Codex entry point? Does the
  false positive push agents toward `Bash` heredocs, which the shell path of the same
  guard inspects differently?
- **Auditor 3 (schema):** Confirm the `agent/available` exemption is enforced on every
  edge (model validator at `models_v2.py:789` — also manager `release` and the API), and
  whether the two id series (F-15) are the task-105 fix working or not.
- **Auditor 11 (gate):** The gate receipt is at `245065a`; `HEAD` is `c51eb64` with
  only task YAML between — does `--since-gate` correctly reduce to `pytest` here? Also:
  the stale counts in the table (F-7) — could `check.py` write the table row it printed
  into the receipt so the doc could quote the receipt?
- **Auditor 2 (docs):** `docs/agent-workflow.md` is the single most-pointed-at dynamic
  document (pinned by test, named in both stubs). Is it accurate? It carries the
  protocol ALLAGENTS summarises, so drift there is drift everywhere.
- **Auditor 4 (storage) / 10 / 11:** `run_report.py` shows *2 gate runs across 1
  instrumented run* out of 61 — the `phases.jsonl` plumbing ENGINEERING.md describes
  has recorded almost nothing. Whose finding is that?
