# The context budget

**What a session actually loads before its first thought, measured rather than estimated.**

Measured 2026-08-25 on this machine. Every figure below carries the instrument that
produced it. Claude Code 2.1.238 / `claude-opus-5`; Codex CLI 0.149.0-alpha.4.1 /
`gpt-5.6-sol`.

This document exists because task-269's compression proposals were sized against a
denominator that had never been measured. The audit that raised them
(`audits/2026-08-21/00-BIG-DAWG-REPORT.md` §5) listed the harness system prompt and the
MCP tool declarations as *"not examined at all, by anyone"*. They are examined here.

---

## 1. The instruments

Three, in decreasing order of directness. Prefer the first two; the third is only used
where nothing better exists, and every number derived that way is marked *derived*.

### A — first-request input tokens, from the session transcript

Claude Code writes a JSONL transcript per session under
`~/.claude/projects/<slug>/`. Each assistant message records its `usage`. The sum

    input_tokens + cache_creation_input_tokens + cache_read_input_tokens

on the **first** non-sidechain assistant message is exactly how many input tokens the
first API request carried: harness system prompt, tool schemas, skill list, agent-type
list, the `@`-chain, memory, and the first user message. Nothing is estimated.

```bash
h=$(head -c 400000 "$f")
echo "$h" | grep -o -m1 '"input_tokens":[0-9]*,"cache_creation_input_tokens":[0-9]*,"cache_read_input_tokens":[0-9]*'
```

Codex records the same thing in its rollout files under `~/.codex/sessions/YYYY/MM/DD/`,
as `last_token_usage.input_tokens` on the first `token_count` event.

### B — self-instrumentation: the production tokenizer, used directly

To attribute a *specific file* to a token count, emit it as a tool result of known byte
size and read the resulting step in the transcript's usage ladder:

    tokens(result of turn k) = input(k+1) − input(k) − output(k)

This is the real tokenizer on the real text, not a ratio. It was validated in the same
run by control turns: sub-1 KB tool results cost 177, 305, 352 and 359 tokens, which is
the right order and proves nothing is being silently dropped from the ladder.

The full ladder for run `run_dd327e59`, 2026-08-25 (input total / output → result):

| input | output | → result | what was emitted |
|---:|---:|---:|---|
| 126,963 | 1,926 | **4,301** | five small chain files, 10,785 B |
| 138,579 | 2,345 | **6,894** | `head -c 20000 ENGINEERING.md` |
| 147,818 | 113 | **6,745** | `tail -c 20699 ENGINEERING.md` |
| 154,676 | 114 | **5,344** | `head -c 16268 ALLAGENTS.md` |
| 160,134 | 115 | **5,357** | `tail -c 16268 ALLAGENTS.md` |
| 165,606 | 157 | **3,838** | `GLOBAL-AGENTS.md`, 10,788 B |
| 178,265 | 109 | **548** | `agentjobs/AGENTS.md`, 1,468 B |

### C — bytes ÷ a measured ratio

Only where A and B cannot reach. The ratio is **not** the audit's ÷4. Measured on this
repository's own prose with instrument B:

| file | bytes | tokens | bytes/token |
|---|---:|---:|---:|
| ENGINEERING.md | 40,699 | 13,639 | 2.98 |
| ALLAGENTS.md | 32,536 | 10,701 | 3.04 |
| GLOBAL-AGENTS.md | 10,788 | 3,838 | 2.81 |
| agentjobs/AGENTS.md | 1,468 | 548 | 2.68 |
| link-dense markdown (MEMORY.md, the projects table) | 9,275 | 3,737 | 2.48 |

**Use 3.0, not 4.0.** The ÷4 estimator in `audits/2026-08-21/01-context-architecture.md`
understates this repository's markdown by about a quarter — it is a rule of thumb for
running English, and these files are dense with backticks, tables, paths and em-dashes.
`wc -c` and `wc -m` differ by under 0.4% here, so bytes and characters are
interchangeable for this purpose.

**Tokenizers are not comparable across runners.** The same two files cost 4.4 bytes per
token under Codex's tokenizer and 3.0 under Claude's. Never carry a token figure from one
column of this document to the other; carry the bytes.

---

## 2. Claude Code, dispatched `--bg` — the shape most of this repository's work runs in

Run `run_dd327e59`, 2026-08-25T09:58Z, cwd `C:/projects/agentjobs`, posture `auto`.
**First request: 75,619 tokens** (instrument A).

| source | bytes | tokens | share | how |
|---|---:|---:|---:|---|
| Harness system prompt, built-in tool schemas, skill list, agent-type list, env + git status, dispatch prompt | — | **42,876** | **56.7%** | residual |
| `ENGINEERING.md` | 40,699 | 13,639 | 18.0% | B |
| `ALLAGENTS.md` | 32,536 | 10,701 | 14.2% | B |
| `C:/ai/shared/GLOBAL-AGENTS.md` | 10,788 | 3,838 | 5.1% | B |
| `MEMORY.md` (auto-memory index, 40 entries) | 6,621 | ~2,668 | 3.5% | derived |
| `C:/projects/AGENTS.md` | 2,654 | ~1,069 | 1.4% | derived |
| `agentjobs/AGENTS.md` | 1,468 | 548 | 0.7% | B |
| `# claudeMd` block headers | — | ~250 | 0.3% | derived |
| `agentjobs/CLAUDE.md` (what survives stripping) | 43 | ~14 | <0.1% | derived |
| `~/.claude/CLAUDE.md` + `C:/projects/CLAUDE.md` | 42 | ~16 | <0.1% | derived |
| **total** | | **75,619** | 100% | A |

The 42,876 harness figure is a residual: total minus everything attributed. It therefore
carries the full error of the derived rows, which is at most a few hundred tokens.

### Then the MCP surface arrives — and it is small

In 2.1.238 the MCP block is **not** in the first request. It is injected as a
system-reminder on the first tool call: roughly 140 deferred tool names across ten
servers, plus the `# MCP Server Instructions` text. Measured on the same run
(instrument B, requests 1→2, netting out a one-line `ls | grep` result):

> **3,795 tokens.**

That settles the question the task was created to answer. The audit's worry was that the
MCP surface might dwarf the `@`-chain and make prose compression the wrong lever. It does
not: **the two repository markdown files alone cost 6.4× the entire MCP surface.**

A deferred tool costs its name, not its schema. `ToolSearch` pulls schemas in on demand —
the AgentJobs server's 16 tools, Notion's 30, Gmail's 27, Chrome's 25, Slack's 20, and so
on. Only the built-in always-on tools (Bash, Read, Write, Edit, Glob, Grep, Agent,
Artifact, Workflow, PowerShell, SendUserFile, ScheduleWakeup, Skill, ToolSearch,
ReportFindings, AskUserQuestion, EnterWorktree, ListAgents) ship their full schemas in
request 1, inside the 42,876.

**Post-first-tool-call floor: 79,414 tokens.**

### The dual AgentJobs MCP registration does not reproduce here

Auditor 8's F11 reported the server registered twice — once from `.mcp.json` and once
from the plugin — costing 30 declarations and two identical instruction blocks. Both
registrations do exist:

| declared in | server name | URL |
|---|---|---|
| `.mcp.json` | `agentjobs` | `http://127.0.0.1:8876` |
| `plugins/agentjobs/.mcp.json` | `agentjobs` | `http://127.0.0.1:8765` |

But in this run the injected block contained **one** `## agentjobs` instruction block and
**16** `mcp__agentjobs__*` tool names, not 32. The names collide, so one registration
wins. What survives is not a token problem but a correctness one: the two entries name
different ports, and 8765 is the port `GLOBAL-AGENTS.md` explicitly warns serves stale
data. Filed separately rather than fixed here — this task changes no registration.

---

## 3. Claude Code, interactive

Same instrument, same cwd, Claude Code 2.1.238, 2026-08-24T00:39Z:
**69,846 tokens.** `ENGINEERING.md` was 36,685 B and `ALLAGENTS.md` 30,769 B at that
commit (`b0d506e`), so the repository's own share was ~22,993 tokens.

| shape | same day, same version | first request | this repo's share |
|---|---|---:|---:|
| interactive | 2026-08-24T00:39 | 69,846 | 22,993 — **32.9%** |
| dispatched `--bg` | 2026-08-24T04:48 | 74,087 | 22,993 — **31.0%** |

**A dispatched run pays ~4,240 tokens more than an interactive one for the same work.**
That is the background-session preamble, the auto-mode "prefer Bash" block, the
remote-control surface and the dispatch prompt. It is not a large number, but it is
larger than the entire MCP surface and nobody has ever counted it.

Older versions are quoted only as history; the harness prompt changes between releases
and cross-version comparison is not meaningful. For the record: the same dispatched shape
cost 49,396 tokens on 2.1.235 (2026-08-20) and 57,603 on 2.1.237 the same day.

---

## 4. Codex

`~/.codex/sessions/2026/08/25/rollout-2026-08-25T01-20-11-*.jsonl`, cwd
`C:/projects/agentjobs`, 2026-08-25T06:20Z. **First request: 19,095 tokens** — about a
quarter of the Claude figure, and measured with a different tokenizer.

Codex's context is built differently, and the difference is the whole story:

| source | bytes | tokens | share | how |
|---|---:|---:|---:|---|
| Codex tool schemas (11 enabled plugins), `<environment_context>`, user prompt | — | ~14,220 | ~74% | residual |
| `base_instructions` (the Codex system prompt) | ~17,800 | ~4,050 | ~21% | derived ÷4.4 |
| `~/.codex/AGENTS.md` | 2,157 | ~490 | ~2.6% | derived |
| `agentjobs/AGENTS.md` | 1,468 | ~334 | **~1.7%** | derived |
| **total** | | **19,095** | 100% | A |

The Codex system prompt is stored verbatim in the rollout's `session_meta.base_instructions.text`,
which is how its size is known exactly rather than guessed.

**Codex does not expand `@`-imports, so `ENGINEERING.md` and `ALLAGENTS.md` are not
auto-loaded.** Verified by position in the rollout: the `world_state.agents_md` payload
at line 7 carries only `~/.codex/AGENTS.md` and `agentjobs/AGENTS.md`. Two further facts
fall out of the same check — `C:/projects/AGENTS.md` is **not** loaded at all (Codex does
not walk up past the git root the way Claude Code walks up the directory chain), and
there is no `MEMORY.md` equivalent.

So `agentjobs/AGENTS.md` tells Codex to *read* the two big files, and it does, as tool
results. Measured from the same rollout's token ladder:

| request | input tokens | Δ | what arrived |
|---:|---:|---:|---|
| 1 | 19,095 | — | the table above |
| 2 | 28,225 | **+8,965** | `ENGINEERING.md` read as a tool result |
| 3 | 35,650 | **+7,367** | `ALLAGENTS.md` read as a tool result |

**A compliant Codex session reaches 35,650 tokens, of which this repository controls
16,666 — 46.8%.** The repository's share of a Codex session is either 1.7% or 47%
depending on one instruction being obeyed, and there is no middle state. That is a worse
position than Claude's steady 33%, not a better one: the same words cost more relative to
the total, and they arrive as an uncacheable tool result rather than as a stable prompt
prefix.

---

## 5. Is `CLAUDE.md`'s HTML comment stripped?

**Yes — confirmed, not assumed.** Claude Code 2.1.238, 2026-08-25.

`C:/projects/agentjobs/CLAUDE.md` is 776 bytes, of which about 700 is an HTML comment
explaining why the file exists. The comment's own last sentence asserts that it "is
stripped before the file enters context, so it costs no tokens." That assertion is the
thing under test, and it holds.

What reaches the prompt, quoted verbatim from this run's own `# claudeMd` block:

```
Contents of C:\projects\agentjobs\CLAUDE.md (project instructions, checked into the codebase):

@AGENTS.md
@ENGINEERING.md
@ALLAGENTS.md
```

43 bytes, ~14 tokens. The comment is absent. The instrument is the session itself: this
run read its own delivered system context, so this is the prompt, not a reconstruction
of it. For contrast, `C:/projects/CLAUDE.md` (11 bytes, no comment) appears in full,
which shows the block renders file contents literally and is not summarising.

The 700 bytes are therefore free. Nothing needs to change, and nobody needs to check
again — but note the answer is version-scoped: it was verified on 2.1.238 and the
stripping is harness behaviour, not something this repository controls.

---

## 6. What this repository actually controls

Across all three shapes, the files in *this git repository* that load automatically are
`CLAUDE.md`, `AGENTS.md`, `ENGINEERING.md` and `ALLAGENTS.md`.

| session shape | first-request total | repo-controlled | share |
|---|---:|---:|---:|
| Claude, dispatched `--bg` (2026-08-25) | 75,619 | 24,902 | **32.9%** |
| Claude, dispatched, after MCP loads | 79,414 | 24,902 | **31.4%** |
| Claude, interactive (2026-08-24) | 69,846 | 22,993 | **32.9%** |
| Codex, opening prompt (2026-08-25) | 19,095 | ~334 | **1.7%** |
| Codex, after reading what `AGENTS.md` orders | 35,650 | 16,666 | **46.8%** |

**Outside this repository's control**, named:

- The Claude Code harness system prompt, built-in tool schemas, the skill list and the
  agent-type list — 42,876 tokens, 57% of a dispatched session, by far the largest single
  block. Anthropic controls it and it moves between releases.
- The MCP surface — 3,795 tokens, injected on first tool use. Reducible by disabling
  servers, not by editing prose. Nine of the ten servers are global connectors (Gmail,
  Slack, Notion, Calendar, Drive, Indeed, Microsoft 365, Chrome, Uber) that have nothing
  to do with this repository.
- `C:/ai/shared/GLOBAL-AGENTS.md` — 3,838 tokens. Jeff's, cross-project, and `task-305`
  and its siblings must not edit it.
- `MEMORY.md` — ~2,668 tokens and growing one line at a time, 40 entries as of
  2026-08-25. Jeff's machine, not this repository.
- `C:/projects/AGENTS.md` — ~1,069 tokens.
- The dispatch prompt itself — ~180 tokens.

---

## 7. The growth rate is the finding

The single most useful number in this document is not a share. It is this: the *same
session shape*, on the *same harness version*, in the *same directory*, over four days.

| date | Claude Code | first request | ENGINEERING.md | ALLAGENTS.md |
|---|---|---:|---:|---:|
| 2026-08-21T15:47 | 2.1.238 | 63,008 | 20,951 B | 21,767 B |
| 2026-08-21T20:00 | 2.1.238 | 64,672 | 27,496 B | 23,142 B |
| 2026-08-22T05:06 | 2.1.238 | 68,997 | 29,977 B | 24,344 B |
| 2026-08-23T23:03 | 2.1.238 | 73,370 | 36,685 B | 30,769 B |
| 2026-08-25T09:58 | 2.1.238 | **75,619** | **40,041 B** | **32,032 B** |

*(sizes from `git cat-file -s $(git rev-parse $(git rev-list -1 --before=<date> main):<file>)`;
totals from instrument A)*

**+20% in four days**, with the harness held constant. `ENGINEERING.md` grew 91% and
`ALLAGENTS.md` 47% in that window. Nothing else in the prompt moved appreciably.

That reframes the question task-269 asked. The problem is not that the bundle is a
particular size today. It is that it has no ceiling, and every session that discovers
something writes it here — which is exactly the behaviour these files ask for.

---

## 8. Proposed budget

**The deep cut is justified, and a one-time cut alone is not.** Both halves matter.

Justified, because the repository's own markdown is 33% of a Claude session's opening
prompt — the largest block anyone here can do anything about, and 6.4× the entire MCP
surface. The audit's fear that "compressing `ENGINEERING.md` is optimising the wrong
half" is not borne out by measurement. It is the right half.

Not sufficient alone, because of the growth rate in §7. Between 2026-08-21T15:47 and
2026-08-25T09:57 the two files gained 29,355 bytes in 3.76 days — about **1,240 words a
day** at this repository's measured 6.31 bytes per word. At that rate a 3,900-word cut is
undone in roughly three days. That window is an upper bound, not a steady state: it
contains the big-dawg audit itself and the flurry of follow-up write-ups it produced. But
even at a quarter of that rate the cut is undone inside a fortnight, so a ceiling is what
makes the cut durable rather than seasonal.

### The numbers, for `task-305` to adopt without re-deriving

Stated in **bytes**, because bytes are checkable with `wc -c` in a test and need no
tokenizer. Two conversions, both measured on this repository's own files rather than
assumed:

- **3.0 bytes per token** (§1C).
- **2.11 tokens per word** — `ENGINEERING.md` is 6,453 words and 13,639 tokens. Use this
  to convert any word-count proposal into prompt cost; `wc -w` is the same instrument.

Task-269's fixes 4 and 5 propose removing ≈3,900 words. At 2.11 tokens/word that is
**~8,200 tokens, ~24,600 bytes** — which would take the bundle from 74,746 B to
~50,150 B. That is within 4% of the 52,000 B anchor derived independently below. The two
figures corroborate each other; **task-269's proposed cut is the right size.**

| file | today | cap | cut | tokens at cap |
|---|---:|---:|---:|---:|
| `ENGINEERING.md` | 40,699 B | **26,000 B** | −36% | ~8,700 |
| `ALLAGENTS.md` | 32,536 B | **23,000 B** | −29% | ~7,700 |
| `AGENTS.md` | 1,468 B | **2,000 B** | — | ~670 |
| `CLAUDE.md` (excluding the stripped comment) | 43 B | **200 B** | — | ~65 |
| per-file caps sum to | | 51,200 B | | ~17,100 |
| **bundle total cap** | **74,746 B** | **52,000 B** | **−30%** | **~17,300** |

The 800 B between the per-file sum and the bundle cap is deliberate slack, so adding a
paragraph to one file does not force a cut in another before the bundle is actually over
budget. Enforce the bundle total; the per-file caps are guidance for where to look.

That lands the repository-controlled share at roughly **23%** of a 75k dispatched prompt,
down from 33%.

**Why 52,000 B and not some other number.** It is where the bundle stood on
2026-08-21T20:00 (51,903 B), plus the ~1 KB `AGENTS.md` Codex section added since, which
is load-bearing and should not be cut. Nobody complained the bundle was thin that day,
several sessions worked correctly against it, and it is a state that actually existed
rather than an aspiration. An anchor a reader can check beats a round number.

**Enforce it the way the MCP leading rule is enforced.** `src/agentjobs/mcp/instructions.py`
carries a 512-character budget with a test that fails when it is exceeded. Copy that
pattern: a test that reads the four files, sums `len(bytes)`, and fails over 52,000. A
cap nothing enforces is a statement of intent, which is the same argument `ENGINEERING.md`
already makes for putting checks in the gate rather than in a list.

### What the cut must not do

The measurement says where the tokens are; it does not say which sentences are load-bearing,
and this task did not test that — `task-304` (context evals) is where that question is
answered. Two guards worth stating anyway:

- **Cut narrative, keep rules.** The growth is overwhelmingly incident write-ups,
  before/after benchmark tables and rationale for decisions already made. Those belong in
  `docs/`, which costs nothing until something reads it. `task-305` is scoped to exactly
  this move.
- **Do not cut to hit the number.** If 52,000 B cannot be reached without dropping a rule
  that has actually prevented a failure, report that and raise the cap with the reason on
  the record. A budget that forces a bad trade should lose the argument.

### Cheaper levers, for whoever wants the tokens back without touching prose

Ranked by tokens per unit of risk, none of them in this task's scope:

1. **~42,900 tokens** — the harness system prompt. Not ours; listed so nobody mistakes
   the repository bundle for the dominant cost. It is not.
2. **~4,240 tokens** — the dispatched-session premium over interactive (§3). Never
   examined. Larger than the entire MCP surface.
3. **~3,795 tokens** — the MCP surface. Nine of ten servers are global connectors
   irrelevant to this repository; per-project MCP scoping would recover most of it.
4. **~2,670 tokens** — `MEMORY.md`, growing without bound at one line per lesson.

---

## 8a. What task-305 actually reached (2026-08-27)

The cut landed and the cap is enforced, at **60,000 B rather than 52,000 B** (61,000 B
since task-376 on 2026-09-07, for a rule the commit names). §8 above
anticipated this and authorised it -- *"if 52,000 B cannot be reached without dropping a
rule that has actually prevented a failure, report that and raise the cap with the reason
on the record"* -- so this is that report.

| | before | after | change |
|---|---:|---:|---:|
| bundle bytes, newline-normalised | 78,055 | **58,557** | **−19,498, −25%** |
| `ENGINEERING.md` + `ALLAGENTS.md`, words | 12,260 | **8,902** | **−3,358** |
| tokens per session at 3.0 B/token | ~26,000 | ~19,500 | ~−6,500 |

Measured on `docs/task-305-context-budget-cut` against its branch point `a7852c5`, with
`read_bundle` from `agentjobs.contexteval.bundle` (which normalises CRLF, so a Windows and
a Linux checkout agree). The cap lives in `tests/test_context_budget.py` and runs in the
`pytest` stage of the gate.

**Why the number is 60,000 and not 52,000.** The word target was substantially met and the
byte anchor was not, because the anchor was a snapshot of a file that has since grown by
rules:

- §8 set the audit's target at **3,900 words** out of the two large files. Task-305
  removed **3,358**.
- The two files are now **185 words larger than they were on 2026-08-21**, the day the
  52,000 B anchor is taken from — while carrying six days of new rules that arrived in
  between (task-293's scripted-finish cleanup, task-303's `bgIsolation` finding,
  task-308's posture ceiling, the finish configuration, the queue move notices). None of
  those is narrative and none was cut.
- Closing the remaining ~6,500 B would therefore have meant compressing rules rather than
  moving reasons. §8's own guard says a budget that forces a bad trade should lose the
  argument.

**What was moved, and where it went**, all of it traceable from the commit that removed
it:

| Moved | To |
|---|---|
| the gate's measurement history — task-233's before/after, the xdist anecdote, the coverage cost, task-189's arithmetic, the contention figures | [performance.md](performance.md#what-the-gate-costs) |
| the whole `run_report.py` reference | [performance.md](performance.md#where-agent-time-goes) |
| the scripted finish's exit codes, decline conditions and records | [agent-dispatch-design.md](agent-dispatch-design.md#5a-what-ends-a-dispatch-the-scripted-finish-task-241-shipped) |
| the queue's call forms and broken-queue exceptions; "a move answers back" | [agent-workflow.md](agent-workflow.md#work-what-the-queue-says-is-next) |
| the supervisor protocol's four child states, retry mechanics and auth-expiry case | [agent-workflow.md](agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them) |
| the wake prompt's contents | `WAKE_STUB`, `src/agentjobs/dispatch/wake.py` — delivered to the session that is woken |
| incident reconstructions | the task records that found them: 186, 189, 194, 207, 210, 211, 225, 233 |

**The lever nobody has pulled** is still the one §8 ranked first among cheaper levers: the
dispatched-session premium and the MCP surface together are larger than everything cut
here, and neither has been examined.

---

## 8b. A subdirectory `CLAUDE.md` is deferred — but only the native file tools trigger it

Measured 2026-08-27 on **Claude Code 2.1.238**, because §6 lists what this repository
controls and this is a mechanism it controls and does not use: all 24 agent-context files
under `C:/projects` sit at project roots, none nested.

Two scratch trees, identical but for one file. `A/` has a one-line root `CLAUDE.md`; `B/`
has the same plus `B/sub/CLAUDE.md`, 8,648 bytes of a distinctively numbered marker rule.
Each was asked to quote "nested marker rule number 017" or reply NOT_LOADED, so the probe
reports whether the text reached the model rather than whether a number moved.

| What the session did | Nested file loaded? |
|---|---|
| nothing — answered from the prompt | **no** |
| `Read sub/thing.py` | **yes** |
| `Bash: cat sub/thing.py` | **no** |
| `Grep` under `sub/` | **yes** |
| `Write sub/scratch.txt` | inconclusive — a permission prompt blocked the tool |

Instrument A agrees. Doing nothing, both trees cost **34,053 tokens** (10 input + 11,876
cache creation + 22,167 cache read) — identical to the token, which an 8.6 KB file loaded
at session start could not produce. After the `Read`, `A` cost 68,497 and `B` 70,059:
**+1,562 tokens**, appearing only once the directory was touched.

The Bash result was checked rather than assumed, per ENGINEERING.md's rule about automated
gestures: the run was re-asked to name the function it had seen, answered `widget_alpha`,
and still answered NOT_LOADED. The read landed; the nested file did not load.

**So it is a real lever and it is not free to adopt.** Two things stand between it and a
recommendation, and both are about who would stop seeing the rule:

- Dispatched runs here are handed a preamble telling them to prefer Bash over `Read` and
  `Edit`. A rule in `src/agentjobs/dispatch/CLAUDE.md` would be invisible to exactly the
  sessions that do most of the work in this repository.
- Codex reads `AGENTS.md`, not `CLAUDE.md` (§4). Whether it defers a nested `AGENTS.md`
  is untested. A rule moved to a nested file could become invisible to Codex rather than
  deferred, which is a correctness question and not a budget one.

Neither is a reason to drop the idea. Both are reasons it needs its own task rather than
being folded into a compression pass — and neither changes what task-305 cut, which was
rationale and history rather than rules.

---

## 9. Reproducing this

Everything above comes from files already on the machine; no session was started to
produce it, and no API key was used.

```bash
# instrument A — first-request total for every Claude session in a project
for f in ~/.claude/projects/<slug>/*.jsonl; do
  head -c 400000 "$f" | grep -o -m1 \
    '"input_tokens":[0-9]*,"cache_creation_input_tokens":[0-9]*,"cache_read_input_tokens":[0-9]*'
done

# instrument A — Codex
grep -o -m1 '"last_token_usage":{"input_tokens":[0-9]*' \
  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

# instrument B — cost of one file, in the tokenizer that will actually see it
#   1. cat the file (chunk under ~30 KB or the harness persists it instead of loading it)
#   2. read the usage ladder from your own transcript
#   3. tokens = input(k+1) - input(k) - output(k)

# bundle size at a past moment
c=$(git rev-list -1 --before="2026-08-21T20:00" main)
git cat-file -s $(git rev-parse $c:ENGINEERING.md)
```

One trap worth knowing: `cat` of a file over roughly 30 KB is not loaded into context at
all. The harness persists it to `tool-results/` and substitutes a 2 KB preview — which
cost 916 tokens in this run, against 13,639 for the whole file. Useful in general;
fatal to this particular measurement, which is why the two large files were chunked.
