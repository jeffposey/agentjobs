# AgentJobs Codex plugin

Packages the AgentJobs MCP server and a workflow skill for Codex. It contains no
server code: `.mcp.json` launches the `agentjobs mcp` command from your installed
AgentJobs package, so the plugin and the standalone integration are the same server
and cannot drift apart.

Other MCP clients — Claude, Gemini, IDEs — install the standalone server instead and
get the same fourteen tools. See [docs/mcp-clients.md](../../docs/mcp-clients.md).

## What is in it

```text
.codex-plugin/plugin.json   manifest; its version tracks the AgentJobs package
.mcp.json                   launches the installed `agentjobs mcp` over STDIO
skills/agentjobs/SKILL.md   the workflow: discovery, the loop, retries, refusals
hooks/hooks.json            registers the guard on write-capable tools
hooks/guard_task_yaml.py    denies direct writes to managed task YAML
```

## The direct-write guard

`hooks/guard_task_yaml.py` is a synchronous `PreToolUse` hook. It resolves the task
directories AgentJobs manages on this machine, then refuses a tool call that would
write one of their `*.yaml` records — `apply_patch`, `Edit`, `Write`, shell
redirection, PowerShell `Set-Content`/`Add-Content`/`Out-File`/`New-Item`/
`Remove-Item`/`Move-Item`/`Copy-Item` and their aliases, POSIX `tee`/`sed -i`/`mv`/
`cp`/`rm`, writing `git` subcommands, and an interpreter one-liner that names a
managed path. The denial says which file, which project, and which AgentJobs tool to
use instead.

**Reading is never blocked.** `cat`, `Get-Content`, `rg`, `git diff` and every read
tool pass through untouched, because reviewing a task means opening it.

### What it is not

**It is a guardrail, not a security boundary**, and the distinction is the point:

- It only sees tool calls Codex routes through it. A hosted or specialised tool it
  never observes gets past it.
- Codex requires you to review and trust a plugin hook before it runs. Until you do —
  and any time you disable it — there is no hook.
- An obfuscated script, or a path assembled at runtime from variables, will not match.
- Any process started outside Codex is entirely outside its view.

It catches the realistic accident: an agent reaching for `apply_patch` because that is
the tool it knows. It does not make direct writes impossible, and nothing here should
be read as claiming it does. The layers that catch what slips through are
`agentjobs validate` and the managed-write receipts (task-118).

An internal error in the guard **allows** the call and says so on stderr. A guard that
blocks tool use whenever it meets an event shape it does not recognise would be worse
than the accident it prevents.

### Trusting and disabling it

Codex asks you to review and trust a plugin hook before it runs; read
`hooks/guard_task_yaml.py` first — it is dependency-free and offline by design, so
there is not much of it. To disable it, decline that prompt or remove the plugin from
your Codex configuration, then start a new session. The MCP tools are unaffected
either way.

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
- The tool list should hold fourteen `agentjobs` tools: five read
  (`projects_list`, `tasks_list`, `task_get`, `tasks_search`, `task_next`) and nine
  mutation (`task_create_draft`, `task_create_ready`, `task_promote`, `task_claim`,
  `task_release`, `task_handoff`, `task_close`, `task_log_append`,
  `task_update_content`).
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
