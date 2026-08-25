# Context-bundle ablation evals

**The question.** Every session in this repository loads `CLAUDE.md` and the three files it
imports before its first thought. Which of those words is doing work?

Audit 1 (task-242) answered by reading, and produced a keep/compress/move/delete table. It
is a good table and it is one person's opinion. This suite answers the same question by
measurement: run a scenario that a rule exists to prevent, once with the rule loaded and
once with it cut out of a copy of the bundle, and see whether the agent still does the right
thing.

Four answers come out, and the fourth is the one an over-eager harness would round away:

| verdict | what happened | what [task-305](../../tasks) may do with it |
|---|---|---|
| `load_bearing` | right with the rule, wrong without it | keep the section |
| `decorative` | right both ways | cut it, with evidence rather than nerve |
| `not_working` | wrong both ways | the rule is loaded every session and is not landing. Rewrite it |
| `inconclusive` | the sample, or the ablation, cannot support any of the above | run more; decide nothing |

## Running it

```bash
poetry run python scripts/context_eval.py --list          # the scenarios
poetry run python scripts/context_eval.py --dry-run       # render both arms, run nothing
poetry run python scripts/context_eval.py --tag quick     # the per-release subset
poetry run python scripts/context_eval.py --runs 3        # the full sweep
```

Useful flags: `--case <glob>` to run one scenario, `--model` to name the model under test
(default `claude-opus-5`), `--jobs` for concurrency, `--keep` to leave the sandboxes on disk
so you can go and look at one, `--out` for the results directory.

`--dry-run` is worth running on its own after any edit to the bundle. It re-renders both
arms and fails if a case's ablation has gone stale -- a heading that was renamed, a rule
that has since been restated somewhere the ablation does not reach -- which is the way this
suite rots, and it costs nothing to check.

### When to run it

- **On a new model release.** This is the point. A rule that is load-bearing for one
  generation may be redundant with the next model's defaults, and a newer model may need a
  rule an older one absorbed from the situation. Run `--tag quick` against the new model and
  compare against the newest file in `baselines/`.
- **Before cutting anything from the bundle**, so the cut has a measurement behind it.
- **After a substantial rewrite of `ENGINEERING.md` or `ALLAGENTS.md`**, at least
  `--dry-run`, because a rewrite is how the ablations go stale.

Not on every commit, and not in `scripts/check.py`. The gate is 96 seconds and this is
tens of minutes and real money; wiring it into the gate would get it disabled within a week.
What *is* in the gate is `tests/test_context_eval.py`, which re-renders every ablation
against the current bundle and fails when one stops matching -- the cheap half of the check,
where it belongs.

## What a scenario is

One directory, one `case.yaml`. The file is the whole specification, deliberately: the
rubric lives in the repository, not in a prompt, so a baseline stays readable a model
release later.

| field | what it does |
|---|---|
| `rule`, `home` | the rule in one sentence, and the section of the bundle that hosts it |
| `incident` | `what` went wrong and `where` it is written down. Required -- no invented scenarios |
| `ablate` | `remove_sections` by heading text, `remove_lines` by regex |
| `residual_forbidden` | patterns that must **not** survive the ablation. Without this the suite would measure the removal of one *statement* of a rule rather than of the rule |
| `fixture` | the throwaway git repository: `files` (committed), `uncommitted` (a peer's in-flight work), `worktrees` (with optional pre-committed `files` on the branch) |
| `prompt` | the scenario, as a session would receive it |
| `compliant_when` / `violated_when` | the checks. A violation beats a compliance |
| `runs`, `max_turns`, `tags` | per-case knobs. `quick` marks the per-release subset |

### Checks

All mechanical. There are **no LLM graders** in this suite -- not because a judge is always
wrong, but because a judge's verdict is not reproducible across the model releases this
suite exists to compare, which would make each baseline incommensurable with the next. If
one is ever added, its rubric goes in the repository beside the case.

| kind | reads |
|---|---|
| `command_matches` | every shell command in the trace (`Bash` **and** `PowerShell`) |
| `tool_input_matches`, `tool_used` | any tool call, optionally by name |
| `ref_changed_paths`, `ref_changed_only` | what commits on a git ref changed, against the fixture's base commit |
| `branch_exists`, `worktree_count_at_least` | the end state of the scratch clone |
| `file_matches` | a file in the sandbox |
| `result_matches` | the final message. Used only where the violation *is* a claim |

Any check takes `negate: true`.

## Why a run is trustworthy, and where it is not

**The two arms differ in exactly one thing.** Same fixture, same prompt, same model, same
flags; one has a rule, the other does not. The suite refuses to report a verdict when that
is not true: an ablation that matched nothing, or a rule that survived its own ablation,
forces `inconclusive` however clean the runs looked.

**Nothing touches the real workspace.** Each session runs in a fresh git repository under
the OS temp directory, behind a `PreToolUse` hook that denies any tool call naming
`C:/projects`, `~/.agentjobs`, or the dashboard ports. The hook is enforced under
`bypassPermissions` -- probed 2026-08-25 -- and any denial it issues is counted in the
report, so a run that *reached* for the real workspace is visible and not merely stopped.
`AGENTJOBS_HOME` points inside the sandbox, so the `agentjobs` CLI sees an empty registry;
`--strict-mcp-config` strips the account's Gmail, Slack and Notion tools, and every run
records how many `mcp__` tools it was offered so a flag that stops working is not invisible.

**A run that ran out of turns is never scored compliant.** It did not *decline* to merge;
it never got there. Such a run is `inconclusive` unless it had already violated.

Three limits, stated rather than buried:

1. **The private global file cannot be ablated.** `~/.claude/CLAUDE.md` and the
   `GLOBAL-AGENTS.md` it imports load in both arms and cannot be suppressed without breaking
   authentication. Every run greps it for the rule under test and the report says so; as of
   the first baseline it states none of them. A `decorative` verdict on a rule it *did*
   state would have to be read as conditional on it.
2. **Small samples.** Three runs per arm, no significance test, a crude half-the-runs
   threshold before a difference is called one. A p-value on three runs would dress up a
   coin flip.
3. **A scenario is a proxy for an incident, not the incident.** It is built from the
   incident's task record, and it is still a constructed situation. A `decorative` verdict
   means "this model did the right thing in this scenario without being told", which is
   weaker than "this rule is never needed".

## Baselines

`baselines/` holds one committed report per model, dated and naming the model id and the
bundle commit. That is the whole point of the exercise: without a baseline, the next release
has nothing to compare against. `results/` is where a fresh run lands and is gitignored.
