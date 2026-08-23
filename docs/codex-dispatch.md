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
one conversation across a task's work/review/follow-up lifecycle. Resume outcomes are
separate, audited states: `resume_busy` means the persisted thread still has an active
writer, so AgentJobs parks the run, retains its task lock, and starts no second thread.
Only a positively classified missing, deleted, or unrecoverable thread may start a fresh
app-visible session; that run records both the replaced and new thread IDs. A generic
resume failure remains a hard failure rather than silently falling back to batch mode.

## Desktop observation is separate evidence

After a completed App Server turn, AgentJobs records `persistence_status: persisted`
only when a fresh App Server probe can both read the recorded thread and find it in
`thread/list` with the `appServer` source and project cwd. That proves the durable Codex
store retained the conversation after the launch child ended; it does not prove a
Desktop sidebar has indexed or displayed it.

For a controlled Desktop observation, wait for that persisted result, open Codex
Desktop, refresh its conversation list, and search for the recorded `AgentJobs
<project>/<task>` thread name or thread ID. Record `desktop_visible` only when a person
can open that exact conversation. If the check is not performed, or Desktop is not
available, retain `not_observed` or `unavailable`; neither result changes dispatch,
turn-completion, or resumability evidence.

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
    argv: ["codex", "app-server", "--model", "gpt-5.6-terra",
           "-c", 'model_reasoning_effort="high"',
           "-c", 'service_tier="default"', "{prompt}"]
    driver: codex
    mode: session
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
2. Enable only `codex-terra` in a named, isolated `codex-smoke` group and run one
   human-authorized task with `agentjobs dispatch run <task> --group codex-smoke`.
   Confirm the dispatch record, the diagnostic JSONL, and persisted-thread evidence.
3. Re-dispatch the same still-open task through the same named group. Confirm the run
   records `resumed: true` and preserves the original App Server thread ID.
4. Perform the separate Desktop observation procedure below. Do not treat a successful
   observation, or lack of one, as evidence about dispatch or resume.
5. Keep this acceptance group isolated. It does not select or change the machine default
   runner group; normal routing remains an independent operator decision.

## Controlled Terra acceptance

For the controlled acceptance run, use GPT-5.6 Terra at High reasoning and Standard
speed through the explicitly named `codex-smoke` group. Before launch, record the human
authorization on the task; the command intentionally refuses to invent a human actor.

1. Dispatch the open task once and retain its run ID, persisted thread ID, MCP preflight
   result, and turn-completion result.
2. Leave the task open, then issue a second authorized dispatch. It must resume the
   persisted thread rather than create a replacement thread.
3. Read each run's metadata. Record dispatch success, resumability, persistence evidence,
   and `desktop_visibility` separately.
4. For Desktop visibility, refresh Codex Desktop and open the exact recorded thread by
   its AgentJobs title or ID. A person records `desktop_visible`; otherwise preserve
   `not_observed` or `unavailable` without changing the other outcomes.

## Posture mapping

| AgentJobs posture | Codex flag | Use |
| --- | --- | --- |
| `read_only` | `--sandbox read-only` | Review and investigation |
| `auto` | `--sandbox workspace-write` | Normal workhorse dispatch |
| `autonomous` | `--sandbox danger-full-access` | Explicit, high-trust automation |
| `supervised` | `approvalPolicy: on-request` | Desktop can answer an interactive approval |

The controlled acceptance configuration uses Terra with `high` reasoning and Standard
speed in an explicitly named group. It is not a claim that the default runner group has
been changed, and it does not use Sol/Big Dawg.
