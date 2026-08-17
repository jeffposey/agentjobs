# AgentJobs Codex plugin

Packages the AgentJobs MCP server and a workflow skill for Codex. It contains no
server code: `.mcp.json` launches the `agentjobs mcp` command from your installed
AgentJobs package, so the plugin and the standalone integration are the same server
and cannot drift apart.

Other MCP clients — Claude, Gemini, IDEs — install the standalone server instead and
get the same thirteen tools. See [docs/mcp-clients.md](../../docs/mcp-clients.md).

## What is in it

```text
.codex-plugin/plugin.json   manifest; its version tracks the AgentJobs package
.mcp.json                   launches the installed `agentjobs mcp` over STDIO
skills/agentjobs/SKILL.md   the workflow: discovery, the loop, retries, refusals
```

The `PreToolUse` direct-write hook is **not here yet** — it is task-117. Until it
lands, this plugin makes the managed path the obvious one but does not prevent a
shell or file editor from writing task YAML. Do not read its absence as enforcement
that failed; read it as a layer not yet installed.

## Install

The MCP server talks to a running AgentJobs service; it will not start one for you.

```bash
pip install agentjobs
```

```bash
agentjobs serve
```

Then add the plugin from a local marketplace entry pointing at this directory, and
**start a new Codex session** — plugins and MCP servers are read at session start, so
an existing session will not see it.

One local Codex configuration is shared by the desktop app, the CLI, and the IDE
extension. Install once; all three pick it up on their next new session.

## Verify

In a new session, confirm the tools are present:

- `projects_list` should return your projects with their configured actors.
- The tool list should hold thirteen `agentjobs` tools: five read
  (`projects_list`, `tasks_list`, `task_get`, `tasks_search`, `task_next`) and eight
  mutation (`task_create_draft`, `task_create_ready`, `task_claim`, `task_release`,
  `task_handoff`, `task_close`, `task_log_append`, `task_update_content`).
- Asking "what should I work on in <project>?" should trigger the AgentJobs skill.

If the server fails to start, its message says why on stderr. The two common ones are
the service not running (start `agentjobs serve`) and a version mismatch between the
installed package and the running service (upgrade the older one and restart it).

## Configure

`.mcp.json` sets `AGENTJOBS_URL` to `http://127.0.0.1:8765`, the default service
address. If yours listens elsewhere, override that variable in your Codex MCP
configuration rather than editing the file here — a machine-specific path or port
committed to this repository would be wrong for everybody else.

`AGENTJOBS_TIMEOUT` sets the request timeout in seconds (default 30, maximum 300).

## Upgrade and rollback

The plugin version tracks the AgentJobs package version, and a test enforces it. Both
halves come from the same release, so upgrade them together:

```bash
pip install --upgrade agentjobs
```

Restart the service, then start a new Codex session. The MCP server checks the
service's version at startup and refuses to run against an incompatible one with a
message naming both versions — below 1.0 that means any minor difference, because
semver promises nothing across 0.x minors.

To roll back, install the previous version of the package and restart both. To disable
the plugin without uninstalling, remove it from your Codex plugin configuration and
start a new session; the standalone `agentjobs mcp` command is unaffected either way.
