# AgentJobs plugin

Packages the AgentJobs MCP server, a workflow skill, and a direct-write guard for
**Codex and Claude Code**. It contains no server code: `.mcp.json` launches the
`agentjobs mcp` command from your installed AgentJobs package, so the plugin and the
standalone integration are the same server and cannot drift apart.

Other MCP clients — Gemini, IDEs — install the standalone server instead and get the
same tools, without the skill or the guard. See
[docs/mcp-clients.md](../../docs/mcp-clients.md).

## What is in it

```text
.codex-plugin/plugin.json   Codex manifest; its version tracks the AgentJobs package
.claude-plugin/plugin.json  Claude Code manifest, over the same server and skill
.mcp.json                   launches the installed `agentjobs mcp` over STDIO
skills/agentjobs/SKILL.md   the workflow: discovery, the loop, retries, refusals
hooks/task_write_guard.py   the decision: which tools, commands and paths are writes
hooks/hooks.json            registers the guard on Codex's write-capable tools
hooks/guard_task_yaml.py    Codex entry point; serialises the decision Codex's way
hooks/hooks-claude.json     registers the guard on Claude's write-capable tools
hooks/guard_task_yaml_claude.py   Claude entry point; Claude's decision envelope
```

**One directory, two manifests.** The clients read different manifest paths and ignore
each other's, so both can sit here over one server entry, one skill, and one guard. A
marketplace installs a *directory*, so a shared module kept outside it would not be
there after install — which makes a single directory the only layout where the guard is
genuinely shared rather than vendored twice.

## The direct-write guard

`hooks/task_write_guard.py` holds the whole decision. It resolves the task directories
AgentJobs manages on this machine, then refuses a tool call that would write one of
their `*.yaml` records — `apply_patch`, `Edit`, `Write`, `NotebookEdit`, shell
redirection, PowerShell `Set-Content`/`Add-Content`/`Out-File`/`New-Item`/
`Remove-Item`/`Move-Item`/`Copy-Item` and their aliases, POSIX `tee`/`sed -i`/`mv`/
`cp`/`rm`, writing `git` subcommands, and an interpreter one-liner that names a
managed path. The denial says which file, which project, and which AgentJobs tool to
use instead.

Each client gets a thin entry point beside it that does nothing but serialise the
answer. There is one copy of the writer tables and one copy of the test matrix, because
two copies disagree and the disagreement shows up as a client quietly less protected
than this page claims.

**Reading is never blocked.** `cat`, `Get-Content`, `rg`, `git diff` and every read
tool pass through untouched, because reviewing a task means opening it.

One deliberate exception is worth knowing before it surprises you: an **interpreter**
that names a managed task path is refused even when it is only reading, because the
guard cannot tell a reading one-liner from a writing one from the outside. Expect the
occasional false refusal from `python -c` and similar; it names the file and says how
to proceed.

### Codex and Claude differ in one place

Codex expects a decision on every event, so its entry point answers `allow` explicitly.

Claude's entry point **prints nothing when it allows**, which is not an oversight.
Claude's `permissionDecision: "allow"` means "skip the permission system for this
call", not "this hook has no objection" — and the hook matches `Edit`, `Write`,
`NotebookEdit` and `Bash`, so emitting it on every event would silently auto-approve
nearly everything a session does. Exiting 0 with no output leaves normal permission
handling alone, which is what a guard with no opinion should do.

### What it is not

**It is a guardrail, not a security boundary**, and the distinction is the point:

- It only sees tool calls the client routes through it. A hosted or specialised tool it
  never observes gets past it.
- Both clients ask you to review and trust a plugin hook before it runs. Until you do —
  and any time you disable it — there is no hook.
- An obfuscated script, or a path assembled at runtime from variables, will not match.
- Any process started outside the client is entirely outside its view.

It catches the realistic accident: an agent reaching for `apply_patch` or `Edit`
because that is the tool it knows. It does not make direct writes impossible, and
nothing here should be read as claiming it does. The layers that catch what slips
through are `agentjobs validate` and the managed-write receipts (task-118).

An internal error in the guard **allows** the call and says so on stderr. A guard that
blocks tool use whenever it meets an event shape it does not recognise would be worse
than the accident it prevents.

### Trusting and disabling it

Both clients ask you to review and trust a plugin hook before it runs; read
`hooks/task_write_guard.py` and your client's entry point first — they are
dependency-free and offline by design, so there is not much of them. To disable the
guard, decline that prompt or remove the plugin from your client configuration, then
start a new session. The MCP tools are unaffected either way.

## Install

The MCP server talks to a running AgentJobs service; it will not start one for you.

```bash
pip install agentjobs
```

```bash
agentjobs serve
```

### Claude Code

The repository root is a plugin marketplace — `.claude-plugin/marketplace.json` lists
this directory. Add the marketplace, then install from it:

```bash
claude plugin marketplace add https://github.com/jeffposey/agentjobs
```

```bash
claude plugin install agentjobs@agentjobs
```

Working from a clone? Point the first command at your checkout instead of the URL —
`claude plugin marketplace add /path/to/agentjobs` — and it will pick up whatever is on
your current branch.

Useful neighbours: `claude plugin list` shows what is installed,
`claude plugin details agentjobs` shows the components and their token cost, and
`claude plugin marketplace list` shows which marketplaces are registered. `--scope`
takes `user` (default), `project`, or `local` on both `install` and `marketplace add`.

The same operations are available interactively as `/plugin` inside a session.

### Codex

The repository root is a Codex marketplace — `.agents/plugins/marketplace.json` lists
this directory. Add the marketplace, then install from it:

```bash
codex plugin marketplace add https://github.com/jeffposey/agentjobs
```

```bash
codex plugin add agentjobs@agentjobs-local
```

Working from a clone? Point the first command at your checkout instead of the URL —
`codex plugin marketplace add /path/to/agentjobs` — and it will install whatever is on
your current branch.

### Either client

**Start a new session afterwards.** Plugins, MCP servers and hooks are all read at
session start, so the session you install from will not see it — including the one that
ran the install command.

One local Codex configuration is shared by the desktop app, the CLI, and the IDE
extension. Install once; all three pick it up on their next new session. Claude Code
works the same way across its own surfaces.

Both clients ask you to review and trust the hook the first time it would run.

**Working from a clone rather than an install?** `.mcp.json` names the `agentjobs`
command, which a Poetry virtualenv does not put on `PATH`. See
[Developing from a clone](../../docs/mcp-clients.md#developing-from-a-clone) for the
invocation that works.

## Verify

**Ask the client whether the server is up before anything else.** A server that
cannot reach the service exits at startup, and every client renders that as its own
kind of failure rather than as a bad address — in Claude Code the plugin's entry simply
reads `Failed to connect`, and a session gets no tools from it while the plugin still
lists as installed and enabled. That is how a wrong port survived here unnoticed
(task-317).

```bash
claude mcp list        # plugin:agentjobs:agentjobs should read Connected
```

Then, in a new session, confirm the tools are present:

- `projects_list` should return your projects with their configured actors.
- The tool list should hold the `agentjobs` tools: six read (`projects_list`,
  `tasks_list`, `task_get`, `tasks_search`, `task_next`, `playbooks_list`) and ten
  mutation (`task_create_draft`, `task_create_ready`, `task_promote`, `task_claim`,
  `task_release`, `task_handoff`, `task_close`, `task_log_append`,
  `task_update_content`, `task_queue_move`). The server's own startup line on stderr
  names them, and is the answer that cannot go stale.
- Asking "what should I work on in <project>?" should trigger the AgentJobs skill.
- Asking your client to edit a task YAML file directly should be refused, with a message
  naming the file and the tools to use instead.

If the server fails to start, its message says why on stderr. The two common ones are
the service not running (start `agentjobs serve`) and a version mismatch between the
installed package and the running service (upgrade the older one and restart it).

## Configure

**`.mcp.json` names no address, deliberately** (task-317). It used to pin
`AGENTJOBS_URL` to `http://127.0.0.1:8765`, and a port in a file that ships to
strangers is a guess about their machine: on the machine this plugin is developed on
that guess was the one port documented as serving nothing, so the bundled server
refused at startup in every session and its tools were quietly absent while the plugin
looked installed and enabled.

With nothing set, the server resolves the address itself, in this order:

| # | Source | For |
|---|---|---|
| 1 | `AGENTJOBS_URL` in the server's environment | a client that wants to say it per server |
| 2 | `AGENTJOBS_API_BASE` in the environment | a shell that already knows |
| 3 | `api_base:` in `~/.agentjobs/dispatch.yaml` | **the machine's standing answer** |
| 4 | `http://127.0.0.1:8765` | a machine that has said nothing and runs `agentjobs serve` with no arguments |

Source 3 is the one to use. It is the same value AgentJobs' own dispatch resolves the
address it hands an agent from, so a machine serving on a non-default port states it
once and both stop being wrong:

```yaml
# ~/.agentjobs/dispatch.yaml
version: 1
api_base: http://127.0.0.1:8876
```

Codex deliberately does not let user configuration replace the command or environment
of a plugin-provided MCP server, which is what made the old hardcoded value awkward to
work around there. It no longer needs working around; if you would rather override it
per client anyway, keep the plugin enabled for its skill and guard, disable only its
bundled server, and add a standalone server entry in `~/.codex/config.toml`:

```toml
[plugins."agentjobs@agentjobs-local".mcp_servers.agentjobs]
enabled = false

[mcp_servers.agentjobs]
command = "agentjobs"
args = ["mcp"]
startup_timeout_sec = 30

[mcp_servers.agentjobs.env]
AGENTJOBS_URL = "http://127.0.0.1:8876"
```

The desktop app, CLI, and IDE extension all read that one Codex configuration. Start
a new session after changing it. Do not edit the cached plugin: the next upgrade would
replace that edit.

`AGENTJOBS_TIMEOUT` sets the request timeout in seconds (default 30, maximum 300).

## Upgrade and rollback

The plugin version tracks the AgentJobs package version, and a test enforces it for
both manifests. Both halves come from the same release, so upgrade them together:

```bash
pip install --upgrade agentjobs
```

Restart the service, then start a new client session. The MCP server checks the
service's version at startup and refuses to run against an incompatible one with a
message naming both versions — below 1.0 that means any minor difference, because
semver promises nothing across 0.x minors.

To roll back, install the previous version of the package and restart both. To disable
the plugin without uninstalling, remove it from your client's plugin configuration and
start a new session; the standalone `agentjobs mcp` command is unaffected either way.
