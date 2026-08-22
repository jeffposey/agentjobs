# Codex dispatch rollout

Codex can use the same AgentJobs MCP server as Claude. The machine-wide Codex entry
belongs in `~/.codex/config.toml`; use one named `agentjobs`, point it at `agentjobs
mcp`, and set `AGENTJOBS_URL` to the running AgentJobs service. AgentJobs makes that
server required on every Codex dispatch, so a run fails before work begins if it cannot
record its task activity.

Codex supports both batch and session dispatch. Batch runs use `codex exec --json` and
finish in the normal AgentJobs batch supervisor. Session runs use the stable JSONL
`codex app-server` protocol: the first turn is started through App Server, and its
conversation is written to Codex's normal session store so Codex Desktop can open it.
AgentJobs keeps a diagnostic JSONL transcript, but the Desktop conversation is the
primary view for output, diffs, and approvals.

Codex Fast mode is a runner setting, not something AgentJobs should inherit from the
currently-open Desktop conversation. Add `-c service_tier="priority"` (or the documented
`"fast"` alias) to a Codex runner's argv when that runner should spend the extra credits
for roughly 1.5x speed. The session adapter forwards this value to App Server's
`thread/start`/`thread/resume` and `turn/start`; batch runners pass it directly to
`codex exec`. Claude runners have no corresponding speed setting.

When `resume_sessions: true` (the default), a later dispatch of the same task selects
the newest completed Codex session run, calls `thread/resume`, and injects the current
AgentJobs wake prompt—including the new ball prompt—through `turn/start`. This keeps
one conversation across a task's work/review/follow-up lifecycle. If Codex reports that
the persisted thread already has an active writer (for example, a stale desktop App
Server), AgentJobs starts a fresh app-visible session and records that fallback; other
resume errors remain hard failures rather than silently falling back to batch mode.

The current Windows CLI's `app-server daemon` and `remote-control` lifecycle commands
are Unix-only. That is separate from ChatGPT Remote: the supported Windows path is to
enable **Settings → Connections → Control this Mac or PC** in ChatGPT Desktop, approve
the connection, and then use the ChatGPT mobile app to follow the connected computer.

## Add the runners, disabled

Put the following under `runners:` in the machine-local
`~/.agentjobs/dispatch.yaml`. `driver: codex` makes AgentJobs translate the project's
posture into Codex's sandbox policy and finds Codex Desktop's active bundled CLI on
Windows, avoiding the non-spawnable Microsoft Store `codex` alias.

```yaml
  codex-terra:
    argv: ["codex", "exec", "--json", "--model", "gpt-5.6-terra",
           "-c", 'model_reasoning_effort="high"', "{prompt}"]
    driver: codex
    mode: batch
    actor: codex

  codex-sol:
    argv: ["codex", "exec", "--json", "--model", "gpt-5.6-sol",
           "-c", 'model_reasoning_effort="xhigh"', "{prompt}"]
    driver: codex
    mode: batch
    actor: codex

  codex-luna-session:
    argv: ["codex", "app-server", "--model", "gpt-5.6-luna",
           "-c", 'model_reasoning_effort="high"',
           "-c", 'service_tier="priority"', "{prompt}"]
    driver: codex
    mode: session
    actor: codex

  codex-sol-session:
    argv: ["codex", "app-server", "--model", "gpt-5.6-sol",
           "-c", 'model_reasoning_effort="high"',
           "-c", 'service_tier="priority"', "{prompt}"]
    driver: codex
    mode: session
    actor: codex
```

Add the disabled candidates to the groups before turning either one on:

```yaml
runner_groups:
  default:
    members:
      - runner: claude-opus-5
      - runner: codex-luna-session
        enabled: false
        note: Enable after a human-reviewed App Server session smoke passes.

  big-dawg:
    members:
      - runner: claude-fable-5
      - runner: codex-sol-session
        enabled: false
        note: Keep Sol reserved for high-value, complex work.
```

## Safe rollout

1. Start AgentJobs and verify a fresh Codex desktop or CLI session can call
   `projects_list` through MCP.
2. Enable only `codex-luna-session` and run one human-triggered, ordinary task through
   an explicitly named test group. Confirm the dispatch record, the diagnostic JSONL,
   and the same named conversation in Codex Desktop.
3. Switch `default` to `codex-luna-session` as its only enabled candidate. Do not leave a
   Claude fallback: a group must fail rather than quietly spend on a different model.
4. Enable `codex-sol-session` only in `big-dawg`, then run its own human-triggered smoke task.
   After it passes, disable the Claude member there too.
5. Keep Claude defined outside production groups only for a fresh-session MCP
   compatibility canary.

## Posture mapping

| AgentJobs posture | Codex flag | Use |
| --- | --- | --- |
| `read_only` | `--sandbox read-only` | Review and investigation |
| `auto` | `--sandbox workspace-write` | Normal workhorse dispatch |
| `autonomous` | `--sandbox danger-full-access` | Explicit, high-trust automation |
| `supervised` | `approvalPolicy: on-request` | Desktop can answer an interactive approval |

For the current $20 plan, the configured policy is Luna with `high` reasoning for the
default workhorse and Sol with `high` reasoning for Big Dawg. Terra remains available
as a disabled candidate; Sol is reserved for work where its extra weekly capacity is
worth the cost.
