# The AgentJobs MCP server

AgentJobs ships an MCP server so an agent can discover projects, read a task, and move
it through the workflow using validated domain operations instead of editing YAML.

**Task YAML is generated state.** Reading it is supported and always will be. Writing
it is not: a direct edit skips validation, skips the lock, and writes no log entry, so
it produces a record that looks correct and quietly is not. That is the failure this
whole interface exists to prevent — an agent once created a task with `lifecycle:
active` and no `ball`, which passed no validator, logged no transition, and vanished
from every listing as a broken file.

## Architecture

```text
agent
  -> MCP over STDIO           (agentjobs mcp)
    -> TaskClient
      -> project-scoped REST  (/api/projects/{id}/...)
        -> TaskManager verb   (claim, handoff, release, close, log)
          -> TaskStorage      (lock, strict validation, atomic write)
```

The MCP process never opens a task file and never imports `TaskManager` or
`TaskStorage`; a test asserts that by parsing the package. Everything it does is an
HTTP call to a service that was already the authority, which is why a write through MCP
is validated by exactly the same code as one from the CLI or the web UI.

The MCP server does **not** start the AgentJobs service. Run `agentjobs serve`
yourself; the server probes it at startup and refuses to run against one that is
missing or version-skewed, with a message naming both versions.

## Install and connect

```bash
pip install agentjobs
```

```bash
agentjobs serve
```

Then point your client at the STDIO command. Configuration is two environment
variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENTJOBS_URL` | `http://127.0.0.1:8765` | The running AgentJobs service. |
| `AGENTJOBS_TIMEOUT` | `30` | Request timeout in seconds; maximum 300. |

Both are also available as `--base-url` and `--timeout`.

`8765` is what `agentjobs serve` binds with no arguments, and it is the port the
bundled plugin and every example on this page assume. **If your service listens
elsewhere, say so once rather than in each client's config**, in either
`AGENTJOBS_API_BASE` or `api_base:` in machine-local `~/.agentjobs/dispatch.yaml`.
Dispatch already reads that value to tell a background agent where to find the service;
`agentjobs init` reads the same value when it writes a project's `.mcp.json`, so one
declaration keeps both from naming a dead port.

### Every registered project declares the server

`agentjobs init` — and project creation from the web UI — leaves the new project with a
`.mcp.json` naming this server:

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

The console script rather than an interpreter path, so the file survives a rebuilt
virtualenv and means the same thing on another machine. The address is the machine's
declared one if it has one, otherwise loopback on the port the project was configured
with. An existing `.mcp.json` is merged into and an existing `agentjobs` entry is never
rewritten — a project that pinned an interpreter, a port or a wrapper made a decision,
and AgentJobs does not quietly reverse it. AgentJobs' own clone is such a project: its
file names a venv interpreter and is gitignored, because that repository develops the
tool rather than consuming it.

Projects registered before this existed, and checkouts that never had the file, get it
from:

```bash
agentjobs project mcp-setup [path]      # --url to state the address yourself
```

Why it matters beyond convenience: a session with no MCP tools does the engineering and
then cannot record any of it, because the direct-write guard correctly refuses the YAML
edit it reaches for next. It presents as a tooling mystery rather than a missing file.

Two notes on what the file does and does not do:

- **It is not an approval.** Claude Code prompts the first time it sees a project-scoped
  server, and a `--bg` session has no terminal to answer with. Dispatch handles that by
  reading these names out of the file and passing them in `enabledMcpjsonServers`, which
  applies regardless of folder trust. An *interactive* session still gets the prompt
  once, which is the right default for a config file that says "run this program" —
  `init` does not write to your client's settings on your behalf.
- **It is separate from the plugin.** A project with both ends up with two server
  entries, listed by `claude mcp list` as `agentjobs` and `plugin:agentjobs:agentjobs`.
  They are independent processes serving the same fifteen tools, not a conflict, and
  the project-scoped one is the one whose address a machine can correct without editing
  an installed plugin's cache.

### Codex and Claude Code

Install the bundled plugin from
[`plugins/agentjobs`](https://github.com/jeffposey/agentjobs/tree/main/plugins/agentjobs).
It carries the MCP wiring, the workflow skill, and the direct-write guard hook, with a
manifest for each client over one server entry, one skill, and one guard. One local
Codex configuration is shared by the desktop app, the CLI, and the IDE extension, so
install once and start a new session in whichever you use; Claude Code works the same
way across its own surfaces.

### Gemini, and any other MCP client

Add a STDIO server entry:

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

You get the same fifteen tools. You do **not** get a pre-tool hook: the guard ships one
entry point per client protocol, and only Codex's and Claude Code's are written. The
decision itself is client-agnostic, so a third client needs a third entry point rather
than a third guard — unbuilt work rather than a platform limit. See
[what protects what](#what-protects-what).

### Verify

```bash
agentjobs mcp --base-url http://127.0.0.1:8765
```

It should sit there speaking nothing on stdout and log `Serving 15 tool(s)` to stderr.
From a client, `projects_list` should return your projects with their configured
actors.

## The tools

Five read, ten mutation. Their schemas are published in `tools/list` and that is the
authoritative reference — the fields, types and constraints live there, and a copy in
this page would go stale.

| Tool | What it is for |
| --- | --- |
| `projects_list` | Every project, with its actor vocabulary. The only tool with no `project_id`. |
| `tasks_list` | One project's tasks, filtered, with unreadable files reported alongside. |
| `task_get` | The complete record: spec, current ask, log, dependency facts, children. |
| `tasks_search` | Substring search within one project. |
| `task_next` | Suggests claimable work: first in the queue, with the band, the position, and everything passed over to reach it. Explains an empty answer. Never claims. |
| `task_create_draft` | New task, born `draft/human/spec`. |
| `task_create_ready` | New task, born `ready/agent/available`. Not claimed. |
| `task_promote` | The spec is finished: `draft` becomes `ready/agent/available`. The only exit from `draft`. |
| `task_claim` | Take a ready task. |
| `task_release` | Put a claimed task back in the pool. |
| `task_handoff` | Move the ball, with the ask that travels with it. |
| `task_close` | End the task with an outcome. |
| `task_log_append` | Append progress, a decision, a question, an answer. |
| `task_update_content` | Edit authoring content only. |
| `task_queue_move` | Change where a task stands in its band. The only way the order changes. |

There is no `set_lifecycle`, no `set_ball`, no `set_queue_position`, no generic patch,
no `save_yaml`, no batch, and no `create_and_claim`. State moves through the verbs or
not at all, and the schemas make an invalid combination unrepresentable rather than
warning about it: a handoff target of `human`/`work` does not validate, and `lifecycle`
is simply absent from the content patch.

`task_queue_move` follows the same rule about the order. It takes a **placement** — a
neighbour (`before`/`after`) or an end of the band (`top`/`bottom`) — and never a
position number, so a caller cannot choose a place without knowing what else is in the
band. The server does the arithmetic under the queue lock, where it is the only writer.
Use it when you disagree with what `task_next` returned: move the task, and the next
session inherits the decision. Do not express order with a `needs` dependency instead —
dependencies are prerequisites, and a false one makes the task unclaimable and lies to
every reader of the graph.

## Two rules every call obeys

**Name the project.** Every task tool requires an exact `project_id` from
`projects_list`, including on a single-project installation. Task ids are unique only
within a project, so an unnamed project is an unpredictable one — a session that moved
between repositories would otherwise write to whichever it happened to be standing in.

**Supply the actor.** Every mutation requires an `actor` from the project's configured
vocabulary. It is never inferred from the model name, the OS user, the MCP client, or
the project's `default_user`. `default_user` is the human; filing an agent's work under
them makes the record lie.

## Retries and conflicts

Every mutation takes a caller-generated `operation_id` (a UUID). Resending the same
request with the same id **replays** the original result rather than writing again, and
the result's `replayed` field says which happened. The marker is stored in the task
file, so replay detection survives the MCP process, the service, and the machine all
restarting — which is exactly when a client retries.

`task_handoff`, `task_close` and `task_update_content` also take `expected_revision`:
the `updated` value from your most recent read. If the task moved since, the call is
refused and the current task comes back so you can decide again. `task_log_append`
deliberately has no revision, because two agents writing independent progress entries
must not conflict.

Every failure carries a stable code:

| Code | Meaning | Retry? |
| --- | --- | --- |
| `invalid_input` | Arguments do not match the schema. | After fixing them |
| `unknown_project` / `unknown_actor` | Not configured. The message names the valid ones. | After fixing them |
| `task_not_found` | No such task in that project. | No |
| `broken_task` | The file exists and will not parse. Repair it. | No |
| `invalid_transition` | The move is not available from this state. | No |
| `queue_broken` | The stored order is not one selection can answer over: an open task with no position, two sharing one, or a position below 1. The message names them. `agentjobs queue repair`. | After repairing |
| `dependency_blocked` | Unmet `needs` dependencies. (An umbrella with open children is *not* this: it can be claimed, and the claim hands over supervision.) | No |
| `revision_conflict` | You decided against a stale read. Current task returned. | After re-reading |
| `operation_conflict` | That operation id was used for a different request. | With a new id |
| `lock_timeout` | Another writer holds the task. | Yes |
| `service_unavailable` | The AgentJobs service did not answer. | Yes |

## What protects what

Making the right thing easy is not the same as making the wrong thing impossible, and
the difference is worth being precise about.

| Client / writer | MCP tools | Pre-tool hook | Local receipt gate | `agentjobs validate` |
| --- | --- | --- | --- | --- |
| Codex with the plugin | yes | yes, once you trust the hook | yes, if installed | yes |
| Claude Code with the plugin | yes | yes, once you trust the hook | yes, if installed | yes |
| Codex or Claude, standalone MCP | yes | only if you install the hook separately | optional | yes |
| Gemini, other MCP | yes | not shipped yet (a third entry point, not a third guard) | yes, via the git hook | yes |
| A text editor or a script | no | no | yes, at commit | yes |
| CI, or a clean clone | no writes | no | receipts are not committed | yes |

- **The MCP tools** make managed operations the obvious path.
- **The pre-tool hook** refuses `apply_patch`, `Edit`, `Write`, `NotebookEdit`, shell
  redirection, PowerShell and POSIX writers, and interpreter one-liners aimed at managed
  task YAML. One module decides; each client gets a thin entry point that serialises the
  answer its own way, so Codex and Claude Code cannot drift into different coverage. It
  is a guardrail, not a security boundary: hosted tools it never sees, an untrusted or
  disabled hook, obfuscation, and anything started outside the client all get past it.
  It is also conservative in one direction on purpose — an interpreter naming a managed
  path is refused even when it is only reading — so expect occasional false refusals,
  more of them in clients whose shell tool is used heavily.
- **The receipt gate** (`agentjobs validate --staged`, installed with
  `agentjobs validate --install-hook`) refuses a commit whose staged task files do not
  match a recorded managed write. This is the only check that catches a *valid-looking*
  direct edit. Receipts are machine-local and never committed, so it works only on the
  machine that made the change. They are evidence, not cryptography — nothing signs them.
- **`agentjobs validate`** needs only the files, so it is the check CI and a clean clone
  can run. It proves the corpus is safe to load and internally consistent. It **cannot**
  prove which program wrote a file, because a careful hand edit produces a file that
  validates perfectly. That limitation is structural, and calling it "enforcement"
  would be wrong.

An emergency repair that no managed operation can express is possible, and deliberately
awkward: set `AGENTJOBS_ALLOW_DIRECT_WRITE_REASON` to a stated reason. It prints the
reason and every bypassed file, and the schema and relationship checks still have to
pass. It takes a sentence, never a flag, so a shell history shows why.

## Upgrading

The plugin version tracks the package version and a test enforces it. Upgrade both
together, restart the service, then start a new client session:

```bash
pip install --upgrade agentjobs
```

The MCP server checks the service's version at startup. Below 1.0 the check compares
major *and* minor, because semver promises nothing across 0.x minors; above 1.0 it
compares majors. A mismatch refuses to start and names both versions.

## Not shipped

Stated so nobody plans around them:

- **Remote transport.** STDIO only. A Streamable HTTP deployment could reuse the same
  tool service later; it does not exist.
- **Notification delivery.** A handoff to a human is durable in the task and emits the
  existing HMAC-signed `task.handoff` webhook. Nothing subscribes to it yet. MCP may
  return warnings but is not becoming an email or push service.
- **Public plugin marketplace publication.** The plugin installs from a local entry.
