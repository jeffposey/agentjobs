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

### Codex

Install the bundled plugin from
[`plugins/agentjobs`](https://github.com/jeffposey/agentjobs/tree/main/plugins/agentjobs).
It carries the MCP wiring, the workflow skill, and the direct-write guard hook. One
local Codex configuration is shared by the desktop app, the CLI, and the IDE
extension, so install once and start a new session in whichever you use.

### Claude, Gemini, and any other MCP client

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

You get the same fourteen tools as Codex. You do **not** get a pre-tool hook: the one
that ships is written against Codex's hook protocol, and no equivalent is packaged for
other clients yet. Claude Code in particular *can* run one — it has `PreToolUse` hooks
— so this is unbuilt work rather than a platform limit. See
[what protects what](#what-protects-what).

### Verify

```bash
agentjobs mcp --base-url http://127.0.0.1:8765
```

It should sit there speaking nothing on stdout and log `Serving 14 tool(s)` to stderr.
From a client, `projects_list` should return your projects with their configured
actors.

## The tools

Five read, nine mutation. Their schemas are published in `tools/list` and that is the
authoritative reference — the fields, types and constraints live there, and a copy in
this page would go stale.

| Tool | What it is for |
| --- | --- |
| `projects_list` | Every project, with its actor vocabulary. The only tool with no `project_id`. |
| `tasks_list` | One project's tasks, filtered, with unreadable files reported alongside. |
| `task_get` | The complete record: spec, current ask, log, dependency facts, children. |
| `tasks_search` | Substring search within one project. |
| `task_next` | Suggests claimable work, and explains an empty answer. Never claims. |
| `task_create_draft` | New task, born `draft/human/spec`. |
| `task_create_ready` | New task, born `ready/agent/available`. Not claimed. |
| `task_promote` | The spec is finished: `draft` becomes `ready/agent/available`. The only exit from `draft`. |
| `task_claim` | Take a ready task. |
| `task_release` | Put a claimed task back in the pool. |
| `task_handoff` | Move the ball, with the ask that travels with it. |
| `task_close` | End the task with an outcome. |
| `task_log_append` | Append progress, a decision, a question, an answer. |
| `task_update_content` | Edit authoring content only. |

There is no `set_lifecycle`, no `set_ball`, no generic patch, no `save_yaml`, no batch,
and no `create_and_claim`. State moves through the verbs or not at all, and the schemas
make an invalid combination unrepresentable rather than warning about it: a handoff
target of `human`/`work` does not validate, and `lifecycle` is simply absent from the
content patch.

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
| `dependency_blocked` | Unmet needs, or an umbrella with open children. | No |
| `revision_conflict` | You decided against a stale read. Current task returned. | After re-reading |
| `operation_conflict` | That operation id was used for a different request. | With a new id |
| `lock_timeout` | Another writer holds the task. | Yes |
| `service_unavailable` | The AgentJobs service did not answer. | Yes |

## What protects what

Making the right thing easy is not the same as making the wrong thing impossible, and
the difference is worth being precise about.

| Client / writer | MCP tools | Codex pre-tool hook | Local receipt gate | `agentjobs validate` |
| --- | --- | --- | --- | --- |
| Codex with the plugin | yes | yes, once you trust the hook | yes, if installed | yes |
| Codex, standalone MCP | yes | only if you install the hook separately | optional | yes |
| Claude, Gemini, other MCP | yes | not shipped yet (buildable — Claude Code has hooks) | yes, via the git hook | yes |
| A text editor or a script | no | no | yes, at commit | yes |
| CI, or a clean clone | no writes | no | receipts are not committed | yes |

- **The MCP tools** make managed operations the obvious path.
- **The Codex hook** refuses `apply_patch`, shell redirection, PowerShell and POSIX
  writers, and interpreter one-liners aimed at managed task YAML. It is a guardrail,
  not a security boundary: hosted tools it never sees, an untrusted or disabled hook,
  obfuscation, and anything started outside Codex all get past it. It is currently the
  only pre-tool guard that ships; a Claude Code equivalent is possible and not yet
  built.
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
