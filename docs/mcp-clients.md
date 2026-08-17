# Connecting a client

Per-client setup for the [AgentJobs MCP server](mcp.md). Every client gets the same
thirteen tools; they differ in packaging and in which extra protections apply.

All of them need a running service first:

```bash
agentjobs serve
```

## Codex

Install the plugin from
[`plugins/agentjobs`](https://github.com/jeffposey/agentjobs/tree/main/plugins/agentjobs)
via a local marketplace entry. It bundles three things: the MCP wiring, the AgentJobs
workflow skill, and the direct-write guard hook.

Codex will ask you to review and trust the hook before it runs. Read
`hooks/guard_task_yaml.py` first — it is dependency-free and offline by design, so
there is not much of it.

**Start a new session afterwards.** Plugins, MCP servers and hooks are read at session
start. One local Codex configuration is shared by the desktop app, the CLI, and the IDE
extension, so this is one install, not three.

Verify: ask "what should I work on in *project*?" — the skill should trigger, and
`projects_list` should return your projects.

Protection: MCP tools, plus the pre-tool hook once trusted, plus the receipt gate if
you install it, plus portable validation.

## Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "agentjobs": {
      "command": "agentjobs",
      "args": ["mcp"],
      "env": { "AGENTJOBS_URL": "http://127.0.0.1:8765" }
    }
  }
}
```

Protection: MCP tools and portable validation, plus the receipt gate if you install the
git hook. **No pre-tool hook** — that is a Codex plugin mechanism, and nothing here
prevents Claude from writing task YAML with its own file tools. Install the commit gate
if that matters to you:

```bash
agentjobs validate --install-hook
```

## Gemini

Same STDIO entry as Claude, in Gemini's MCP configuration. Same protection level: tools
and validation, no pre-tool hook.

## Any other MCP client

The server is a plain STDIO MCP server with no client-specific behaviour. Point your
client at `agentjobs mcp`, set `AGENTJOBS_URL` if the service is not on the default
port, and start a new session.

If your client cannot pass environment variables, use the flags instead:

```bash
agentjobs mcp --base-url http://127.0.0.1:8765 --timeout 30
```

## POSIX verification

The command is argv-only — no shell, no quoting — so it launches identically on Windows
and POSIX. Windows is what this repository's suite runs on. To verify a POSIX install:

```bash
python -m venv /tmp/aj && /tmp/aj/bin/pip install agentjobs
/tmp/aj/bin/agentjobs serve &
/tmp/aj/bin/agentjobs mcp --base-url http://127.0.0.1:8765 < /dev/null
```

The last command should exit 0 having written only JSON-RPC to stdout, with
`Serving 13 tool(s)` on stderr.

## A complete loop

The same sequence in any client. `task_get` before acting, and a fresh `operation_id`
per distinct operation:

1. `projects_list` → note the `project_id` and pick your agent actor.
2. `task_next` with that project and actor → it suggests, it does not claim.
3. `task_claim` → take it.
4. `task_get` → read the spec, the current `ball_prompt`, and the log newest-first.
5. `task_log_append` (`progress`, `decision`, `question`) as you work.
6. `task_get` again for a fresh `updated`, then `task_handoff` to `human`/`review`
   with `expected_revision` and a prompt saying what needs reviewing.
7. After approval: `task_close` with an outcome.

## Troubleshooting

**"is not reachable"** — the service is not running, or `AGENTJOBS_URL` points
somewhere else. Start `agentjobs serve`. The MCP server will not start one for you.

**"predates the /api/version endpoint"** — the service is running but is an older
AgentJobs. Upgrade it and restart it; a stale `serve` process holds the old code in
memory even after you upgrade the package.

**"version mismatch"** — package and service are different releases. Upgrade whichever
is older, then restart the service.

**The tools do not appear** — start a new client session. Configuration is read at
session start.

**`unknown_actor`** — the actor is not in that project's `.agentjobs/config.yaml`. The
error names the configured ones; add yours with `kind: agent`.
