# Connecting a client

Per-client setup for the [AgentJobs MCP server](mcp.md). Every client gets the same
fourteen tools; they differ in packaging and in which extra protections apply.

All of them need a running service first:

```bash
agentjobs serve
```

## Developing from a clone

Every configuration below spells the command `agentjobs`, which is correct when you
installed the package — `pip install agentjobs` puts a launcher on `PATH`. **It is not
correct when you are working from a clone**, and that is the case for anyone
contributing to AgentJobs itself.

A Poetry virtualenv on Windows has no `agentjobs.exe`. It has a POSIX shebang script
with no extension and an `agentjobs.cmd` shim, and an MCP client spawns its server
through argv with no shell, so neither one launches. Call the interpreter instead and
let it find the module:

```json
{
  "mcpServers": {
    "agentjobs": {
      "command": "C:/path/to/virtualenvs/agentjobs-XXXXXXXX-py3.13/Scripts/python.exe",
      "args": ["-m", "agentjobs.cli", "mcp"],
      "env": { "AGENTJOBS_URL": "http://127.0.0.1:8765" }
    }
  }
}
```

`poetry env info --path` prints the directory. On POSIX the venv's `bin/agentjobs` is
directly executable, so `<venv>/bin/agentjobs` with `args: ["mcp"]` also works; the
interpreter form works on both and is the one to reach for if you are unsure.

The same applies to `agentjobs serve` and every other command in this file: from a
clone they are `poetry run agentjobs …`.

Client MCP configuration files are gitignored in this repository for that reason — the
working entry names one machine's virtualenv, so there is no committed version of it
that is right for two people at once.

## Codex

Install the plugin from
[`plugins/agentjobs`](https://github.com/jeffposey/agentjobs/tree/main/plugins/agentjobs)
via a local marketplace entry. It bundles three things: the MCP wiring, the AgentJobs
workflow skill, and the direct-write guard hook.

Codex will ask you to review and trust the hook before it runs. Read
`hooks/task_write_guard.py` and `hooks/guard_task_yaml.py` first — they are
dependency-free and offline by design, so there is not much of them.

**Start a new session afterwards.** Plugins, MCP servers and hooks are read at session
start. One local Codex configuration is shared by the desktop app, the CLI, and the IDE
extension, so this is one install, not three.

Verify: ask "what should I work on in *project*?" — the skill should trigger, and
`projects_list` should return your projects.

Protection: MCP tools, plus the pre-tool hook once trusted, plus the receipt gate if
you install it, plus portable validation.

## Claude Code

Install the same plugin directory,
[`plugins/agentjobs`](https://github.com/jeffposey/agentjobs/tree/main/plugins/agentjobs),
via a local marketplace entry. It carries a Claude manifest beside the Codex one over
one server entry, one skill, and one guard, so Claude gets all three: the MCP wiring,
the workflow skill, and a `PreToolUse` guard that refuses direct writes to managed task
YAML with `Edit`, `Write`, `NotebookEdit` or `Bash`.

Claude will ask you to review and trust the hook before it runs. Read
`hooks/task_write_guard.py` and `hooks/guard_task_yaml_claude.py` first.

**Start a new session afterwards.** Plugins, MCP servers and hooks are read at session
start.

Verify: ask "what should I work on in *project*?" — the skill should trigger. Then ask
Claude to edit a task YAML file directly; it should be refused, with a message naming
the file and the AgentJobs tools to use instead.

Protection: MCP tools, plus the pre-tool hook once trusted, plus the receipt gate if
you install it, plus portable validation. The same as Codex.

One difference from Codex is deliberate and worth knowing, because it reads as a
missing feature: when the guard allows a call, the Claude hook prints **nothing**.
Claude's `permissionDecision: "allow"` means "skip the permission system for this
call", not "no objection" — and the hook matches every file-writing tool, so emitting
it would auto-approve nearly everything a session does.

Expect the occasional false refusal. An interpreter that names a managed task path is
refused even when it is only reading, because the guard cannot tell the difference from
the outside. `Bash` is used heavily in Claude sessions, so this comes up more here than
it ever did in Codex.

## Claude Desktop, and any client without the plugin

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
git hook. A bare server entry carries no hook and no skill, so nothing stops the client
writing task YAML with its own file tools. Install the plugin above where your client
supports one, or install the commit gate, which is client-independent and catches the
same edits one step later:

```bash
agentjobs validate --install-hook
```

## Gemini

Same STDIO entry as the one above, in Gemini's MCP configuration. Same protection
level: tools and validation. **No pre-tool hook is shipped for Gemini yet**, so nothing
stops Gemini writing task YAML with its own file tools.

That is a gap in what AgentJobs ships rather than a limit of the client. The guard's
decision logic is client-agnostic and already serialises two clients' decision shapes
out of one module, so a third would be a third entry point, not a third guard.

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
`Serving 14 tool(s)` on stderr.

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
