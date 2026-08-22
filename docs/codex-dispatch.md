# Codex dispatch rollout

Codex can use the same AgentJobs MCP server as Claude. The machine-wide Codex entry
belongs in `~/.codex/config.toml`; use one named `agentjobs`, point it at `agentjobs
mcp`, and set `AGENTJOBS_URL` to the running AgentJobs service. AgentJobs makes that
server required on every Codex dispatch, so a run fails before work begins if it cannot
record its task activity.

Codex dispatch is batch-only for now: each run uses `codex exec --json` and finishes in
the normal AgentJobs batch supervisor. Session, remote-control, and resume dispatch are
Claude-specific and are rejected for a Codex runner instead of being silently imitated.

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
```

Add the disabled candidates to the groups before turning either one on:

```yaml
runner_groups:
  default:
    members:
      - runner: claude-opus-5
      - runner: codex-terra
        enabled: false
        note: Enable after the first Codex batch smoke dispatch passes.

  big-dawg:
    members:
      - runner: claude-fable-5
      - runner: codex-sol
        enabled: false
        note: Keep Sol reserved for high-value, complex work.
```

## Safe rollout

1. Start AgentJobs and verify a fresh Codex desktop or CLI session can call
   `projects_list` through MCP.
2. Enable only `codex-terra` and run one human-triggered, ordinary task through an
   explicitly named test group. Confirm the dispatch record, its JSONL output, and the
   task handoff all appear in AgentJobs.
3. Switch `default` to `codex-terra` as its only enabled candidate. Do not leave a
   Claude fallback: a group must fail rather than quietly spend on a different model.
4. Enable `codex-sol` only in `big-dawg`, then run its own human-triggered smoke task.
   After it passes, disable the Claude member there too.
5. Keep Claude defined outside production groups only for a fresh-session MCP
   compatibility canary.

## Posture mapping

| AgentJobs posture | Codex flag | Use |
| --- | --- | --- |
| `read_only` | `--sandbox read-only` | Review and investigation |
| `auto` | `--sandbox workspace-write` | Normal workhorse dispatch |
| `autonomous` | `--sandbox danger-full-access` | Explicit, high-trust automation |
| `supervised` | refused | A batch run cannot answer interactive approvals |

The `default` recommendation is Terra with `high` reasoning. Sol with `xhigh` is the
Big Dawg choice: reserve it for hard architecture, risky changes, and work where the
extra weekly capacity is worth the expected quality gain.
