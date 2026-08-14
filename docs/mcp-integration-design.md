# AgentJobs MCP integration design

**Status:** accepted design for implementation planning
**Date:** 2026-08-14

## 1. Problem and decision

An agent created a task file directly with `lifecycle: active` and no `ball`. The write
bypassed `TaskManager`, so no transition was logged, model validation never ran, and the
task disappeared from normal listings as a broken file. AgentJobs already has the right
domain boundary: strict Pydantic models, locked manager verbs, project-scoped REST
routes, and `TaskClient`. The missing piece is an agent-native interface that makes that
boundary easier to use than editing YAML, plus enforcement for clients that still have
general file and shell tools.

AgentJobs will ship **both**:

1. a standalone MCP server in the AgentJobs Python distribution; and
2. an AgentJobs Codex plugin that packages that same server with an AgentJobs workflow
   skill and a Codex `PreToolUse` direct-write hook.

There is one MCP implementation, not a standalone implementation and a plugin fork.
The plugin is distribution and client-specific guidance around the protocol surface.
Other MCP clients install the standalone server and receive the same tools without
depending on Codex packaging.

The first supported transport is **local STDIO**. The MCP process is a thin facade over
the running AgentJobs HTTP service through an extended `TaskClient`:

```text
agent / plugin
    -> MCP over STDIO
        -> TaskClient
            -> project-scoped REST endpoint
                -> TaskManager verb
                    -> TaskStorage lock + strict Task validation
```

The MCP server does not import `TaskManager`, open task YAML, or reproduce lifecycle
checks. The existing REST/manager path remains authoritative. A later Streamable HTTP
MCP deployment may reuse the same tool service for remote clients, but is not in the
initial implementation. The existing REST server is not itself an MCP transport.

This choice follows the current MCP recommendation that clients support STDIO where
possible and fits local, Git-backed AgentJobs installations. It also keeps the HTTP
service as the single multi-project writer rather than starting one independent writer
per agent session.

## 2. Non-negotiable boundaries

- Task YAML becomes **generated state**. Read-only access remains supported; direct
  content edits are no longer a supported workflow.
- Every MCP write calls a project-scoped REST endpoint through `TaskClient`. No tool
  calls storage or manager classes directly.
- State axes are absent from generic content updates. `lifecycle`, `ball`,
  `ball_reason`, `ball_prompt`, `assignment.owner`, and `outcome` move only through
  create, claim, handoff, release, and close domain verbs.
- The server exposes no `set_status`, `set_lifecycle`, `save_yaml`, generic patch, or
  arbitrary task-record mutation tool.
- The full strict `Task` model is returned after every mutation. A successful tool
  result means the task was persisted and reloaded through the authoritative path.
- MCP errors never turn a broken or ambiguous record into an empty result.
- The MCP layer is not a security boundary. Authentication/authorization for a future
  network service, Codex tool approval, hook policy, and repository validation are
  distinct controls.

## 3. Project discovery and actor identity

### Project selection

`projects_list` is the only tool that does not require `project_id`. Every task tool
requires the exact ID returned by it, including single-project installations (where the
existing API may return `_local`). The server has no mutable "current project" and does
not select a project from the MCP process working directory. This prevents a session
that moves between repositories from silently writing to the last or nearest project.

`TaskClient` must gain project-scoped methods rather than teaching individual MCP tools
to construct URLs. Unknown or missing projects remain the REST layer's 404/409 errors,
including a list of valid project IDs when available.

### Actor attribution

Every mutating tool requires an `actor` string. The server does not infer actor identity
from the model name, operating-system user, MCP client metadata, or the human
`default_user`. The project actor vocabulary is returned by `projects_list`; when a
project defines actors, the existing actor validator rejects an unknown actor before
the manager writes anything. Projects without an actor vocabulary preserve current
compatibility and accept the supplied ID.

The actor written to the task log is the actor supplied to the tool and validated by
the authoritative service. MCP client/session identity may be added as non-authoritative
diagnostic metadata, but it must never replace the task actor.

## 4. Initial MCP contract

The server identifies itself as `agentjobs` with the installed package version. Its
initialization instructions begin with this self-contained rule:

> AgentJobs task YAML is generated state. Use these tools for every task mutation.
> Call `projects_list`, pass its `project_id` to every task tool, and use only claim,
> handoff, release, and close to move workflow state. Reading task YAML is allowed.

Tools use explicit JSON Schema input and output schemas. They return both
`structuredContent` and a short serialized/text summary for clients that do not consume
structured results. Read tools are annotated read-only and non-destructive. Mutation
tools are annotated non-read-only; `task_close` is destructive because it ends open
work even though the record remains recoverable in Git. Annotations are hints, never
authorization.

### Shared types

The following definitions are normative shorthand for the JSON Schemas. Objects use
`additionalProperties: false` unless a field explicitly says otherwise.

```text
ProjectId       = string(minLength=1)
TaskId          = string(minLength=1)
ActorId         = string(minLength=1)
OperationId     = string(format="uuid")
Revision        = string(format="date-time")  # Task.updated from a prior read
Lifecycle       = "draft" | "ready" | "active" | "closed"
Ball            = "agent" | "human" | "external"
BallReason       = "available" | "work" | "revise" | "spec" | "review" |
                   "decision" | "approval" | "input" | "dependency" | "service"
Outcome          = "completed" | "cancelled" | "superseded" | "duplicate"
Priority         = "low" | "medium" | "high" | "critical"
LogType          = "note" | "progress" | "decision" | "question" | "answer" |
                   "instruction"
DependencyType  = "needs" | "blocks" | "related"
```

`TaskDocument` is the current schema-v2 `Task` JSON emitted by the REST API. It is
generated from the Pydantic/OpenAPI model, not manually copied into MCP code.
`TaskSummary` is:

```text
{
  project_id: ProjectId,
  id: TaskId,
  title: string,
  lifecycle: Lifecycle,
  ball: Ball | null,
  ball_reason: BallReason | null,
  ball_prompt: string | null,
  outcome: Outcome | null,
  priority: Priority,
  category: string,
  parent: TaskId | null,
  owner: ActorId | null,
  updated: Revision,
  display_status: string,
  actionable: boolean,
  unmet_needs: string[],
  open_children_count: integer(minimum=0)
}
```

Every mutation returns:

```text
MutationResult = {
  project_id: ProjectId,
  operation_id: OperationId,
  replayed: boolean,
  task: TaskDocument,
  warnings: string[]
}
```

`replayed: true` means the same successful operation was already recorded and no new
write or log entry occurred. `warnings` is for post-commit side effects such as a
failed webhook delivery; it cannot turn a committed mutation into an error.

### Tools

| Tool | Exact input | Exact structured output | Domain behavior |
| --- | --- | --- | --- |
| `projects_list` | `{}` | `{projects: ProjectSummary[]}` | Lists every project served by the REST service, including `id`, `name`, `root`, `tasks_directory`, `task_count`, configured actors (`id`, `kind`, `display_name`), and `default_user`. No mutation. |
| `tasks_list` | `{project_id, lifecycle?, ball?, priority?, parent?, limit?}` where `limit` is 1–200, default 100 | `{tasks: TaskSummary[], broken: BrokenTask[], truncated: boolean}` | Lists one project. Broken files are returned beside valid tasks, never omitted. |
| `task_get` | `{project_id, task_id}` | `{project_id, task: TaskDocument, dependency_facts, subtasks: TaskSummary[]}` | Returns the zero-context resumption record plus computed dependency facts and children. |
| `tasks_search` | `{project_id, query: string(minLength=1), limit?}` | `{tasks: TaskSummary[], broken: BrokenTask[], truncated: boolean}` | Full-text search in one project. |
| `task_next` | `{project_id, actor, priority?}` | `{task: TaskSummary | null, explanation: string}` | Returns the next claimable task for the actor. It does not claim it. The explanation distinguishes no work from broken/cyclic/blocked work. |
| `task_create_draft` | `CreateTaskInput` | `MutationResult` | Creates only `draft / human / spec`; the server supplies those axes and a spec prompt. |
| `task_create_ready` | `CreateTaskInput` | `MutationResult` | Creates only `ready / agent / available`; the server supplies those axes. It does not claim. |
| `task_claim` | `{project_id, task_id, actor, operation_id}` | `MutationResult` | Calls the claim verb. Ready/dependency/eligibility/umbrella checks stay in `TaskManager`. |
| `task_release` | `{project_id, task_id, actor, operation_id, body?}` | `MutationResult` | Calls release; only active work returns to ready/available. |
| `task_handoff` | `HandoffInput` | `MutationResult` | Moves the ball through the handoff verb and appends the manager-owned handoff entry. |
| `task_close` | `{project_id, task_id, actor, operation_id, expected_revision, outcome, body?, archive?: boolean=false}` | `MutationResult` | Calls close; no generic lifecycle setter exists. |
| `task_log_append` | `{project_id, task_id, actor, operation_id, type: LogType, body: string(minLength=1), re?, data?}` | `MutationResult` | Appends an allowed authored log entry. `transition` and `handoff` are not members of `LogType`. `data` is a JSON object and may not override reserved operation metadata. |
| `task_update_content` | `{project_id, task_id, actor, operation_id, expected_revision, patch: ContentPatch}` | `MutationResult` | Updates only authoring content. State axes and log replacement are impossible in the schema. |

`BrokenTask` is `{task_id, filename, reason}`. `ProjectSummary` and computed dependency
facts are generated from REST response models so the REST, OpenAPI, Python client, and
MCP contracts cannot drift independently.

### CreateTaskInput

```text
{
  project_id: ProjectId,
  actor: ActorId,
  operation_id: OperationId,
  id?: TaskId,
  title: string(minLength=1),
  summary: string(minLength=1),
  description: string(minLength=1),
  intent?: string,
  constraints?: string,
  out_of_scope?: string,
  context?: {path: string(minLength=1), why: string(minLength=1)}[],
  priority?: Priority = "medium",
  category?: string = "general",
  eligible?: ActorId[],
  effort?: string,
  tags?: string[],
  parent?: TaskId,
  acceptance?: AcceptanceCriterion[],
  deliverables?: Deliverable[],
  dependencies?: Dependency[],
  links?: Link[]
}
```

The create schema has no lifecycle, ball, owner, outcome, branches, or log. A branch is
live work metadata and is added through `task_update_content` immediately before a
claim when repository policy requires it. Creation records the actor and
`operation_id` in a manager-owned creation log entry.

### HandoffInput

`HandoffInput` is a discriminated union. The schema makes invalid holder/reason pairs
unrepresentable rather than relying on a prose warning:

```text
{
  project_id, task_id, actor, operation_id, expected_revision,
  target:
    | {ball: "agent", reason: "work" | "revise", prompt: non-empty string}
    | {ball: "human", reason: "spec" | "review" | "decision" | "approval" |
                      "input", prompt: non-empty string}
    | {ball: "external", reason: "dependency" | "service", prompt: non-empty string},
  body?: string
}
```

`agent/available` is intentionally absent: returning work to the pool is
`task_release`, not a handoff alias.

### ContentPatch

At least one field is required. The allowed fields are exactly:

```text
title, priority, category, effort, tags, parent, spec, acceptance, deliverables,
dependencies, links, branches
```

Nested values use the existing strict Pydantic models. Whole nested collections are
replaced, matching the REST patch contract. The following are forbidden by schema:

```text
schema, id, created, updated, lifecycle, ball, ball_reason, ball_prompt, outcome,
archived, assignment.owner, log, display_status
```

## 5. Validation and errors

Validation occurs in layers, with one final authority:

1. MCP JSON Schema rejects malformed tool arguments before execution.
2. The MCP handler performs only protocol concerns: required project/actor/revision and
   result shaping.
3. `TaskClient` calls the project-scoped REST request model.
4. The REST route validates actor and cross-record inputs, then calls a manager verb.
5. `TaskStorage.mutate_task` locks, reloads, validates the complete strict task, writes
   atomically, and reloads the persisted result.

The MCP server must not translate a domain error into generic prose. Tool execution
errors use `isError: true` with structured content:

```text
{
  code: "invalid_input" | "unknown_project" | "unknown_actor" | "task_not_found" |
        "broken_task" | "invalid_transition" | "dependency_blocked" |
        "revision_conflict" | "operation_conflict" | "lock_timeout" |
        "service_unavailable" | "internal_error",
  message: string,
  retryable: boolean,
  project_id?: string,
  task_id?: string,
  current_task?: TaskDocument,
  field_errors?: {path: string, message: string}[],
  suggested_action?: string
}
```

Pydantic request errors are `invalid_input`; missing and broken tasks remain distinct;
manager `ValueError` messages map to stable domain codes; lock timeouts and connection
failures are retryable; validation and transition failures are not. A revision conflict
returns the current task so the agent can re-read and decide rather than blindly retry.

## 6. Idempotency, concurrency, and partial failure

Every mutation requires a caller-generated UUID `operation_id`.

- The authoritative REST/manager path records successful operation IDs in manager-owned
  log metadata and detects a replay inside the same task lock.
- Reusing an operation ID with the same normalized operation returns the persisted task
  with `replayed: true` and appends nothing.
- Reusing it for a different tool, task, actor, or payload returns
  `operation_conflict` and writes nothing.
- Creation uses a project-wide creation lock. A manager-owned creation entry stores the
  operation ID, so retrying an auto-ID create finds the original task rather than
  creating a second one.
- `task_update_content`, `task_handoff`, and `task_close` require
  `expected_revision=task.updated`. They reject stale decisions before mutation.
- Claim and release rely on their locked state preconditions. Log append is an atomic
  append and does not require a revision, so independent progress entries do not
  conflict.

One MCP call mutates at most one task. There is no initial batch tool and no
`create_and_claim` tool. An agent that wants new work creates a ready task and then
claims the returned ID. If the second call fails, the valid ready task remains visible;
the failure is explicit and no rollback pretends the first committed write vanished.

The task write commits before webhooks run, matching current behavior. A webhook or
notification failure does not roll back the task and is returned as a warning when the
authoritative layer can report it. MCP never promises delivery of a human notification.

## 7. Installation, packaging, compatibility, and upgrades

### Standalone

The core wheel adds the official Python MCP SDK and an `agentjobs mcp` command that
runs STDIO without writing non-protocol output to stdout. Configuration:

```text
--base-url / AGENTJOBS_URL       default http://127.0.0.1:8765
--timeout / AGENTJOBS_TIMEOUT   bounded client timeout
```

The HTTP service must already be available; the MCP child does not silently start or
own it. Startup probes `/health`, the AgentJobs version endpoint, and the project list.
It fails with an actionable stderr message when the service is absent or incompatible.
Logs go only to stderr.

The server and REST API expose semantic versions. Major-version mismatch fails startup;
minor skew is allowed only when the server can prove the required routes and response
fields. The MCP server advertises the installed AgentJobs version and supported schema
version. Dependency ranges are pinned in `pyproject.toml`/the lock file and exercised on
Python 3.11 and 3.12, Windows and a POSIX runner.

### Codex plugin

The repository ships one plugin directory with:

```text
.codex-plugin/plugin.json
.mcp.json
skills/agentjobs/SKILL.md
hooks/hooks.json
hooks/guard_task_yaml.py
```

The manifest version tracks the AgentJobs package version. `.mcp.json` launches the
installed `agentjobs mcp` STDIO command; it does not vendor a second server. The skill
teaches discovery, zero-context resumption, manager verbs, and the repository workflow.
The hook is separately reviewable and trusted by Codex, as current Codex behavior
requires. Installation docs cover the local marketplace, restart/new-session behavior,
health verification, upgrades, and rollback/disable steps.

The standalone server remains the supported integration for Claude, Gemini, IDEs, and
other MCP clients. Their client-specific setup snippets must name what protections they
do and do not receive.

## 8. Direct-write prevention and portable backstop

MCP makes the correct operation available; it cannot stop a model from choosing a shell
or file editor. Enforcement therefore has three layers.

### Codex pre-tool hook

The plugin bundles a synchronous `PreToolUse` command hook. It resolves every managed
task directory from AgentJobs configuration/registry, canonicalizes paths (including
Windows case and separators), then:

- rejects `apply_patch`, `Edit`, `Write`, or other local file-write tools whose target
  is a managed `*.yaml` task file;
- rejects shell redirection and write-capable commands targeting managed task YAML;
- covers PowerShell `Set-Content`, `Add-Content`, `Out-File`, `New-Item`, `Remove-Item`,
  `Move-Item`, `Copy-Item`, aliases, and redirection;
- covers common POSIX writers (`tee`, redirection, `sed -i`, `mv`, `cp`, `rm`) and
  interpreter/script invocations that explicitly target a managed task path;
- allows read-only commands (`Get-Content`, `rg`, `git diff`, parsers opened read-only)
  and AgentJobs MCP/CLI/API operations;
- returns a denial naming the project and directing the agent to the relevant
  AgentJobs tools.

Tests use real hook JSON fixtures for patch, shell, PowerShell, relative/absolute path,
case, quoting, redirection, and script examples. Ambiguous commands that explicitly
name a managed task path and invoke a general interpreter are denied conservatively.

The hook cannot observe every hosted or specialized tool, can be disabled, and must be
trusted after installation or change. It is a guardrail, not a sandbox or security
boundary. Read-only task access remains intentionally available.

### Managed-write receipts and local commit gate

Every successful `TaskStorage` write emits a machine-local receipt under
`.agentjobs/write-receipts/` containing project, task ID, resulting canonical file hash,
operation kind, actor when known, timestamp, and AgentJobs version. The directory is
gitignored and is not task state.

`agentjobs validate --staged` validates every staged task YAML and requires its staged
hash to match a current manager-produced receipt. A repository Git hook invokes this
command before commit. This catches a semantically valid direct edit that a schema-only
check cannot distinguish. Receipts are evidence of the supported write path, not a
cryptographic security claim; a hostile local process can forge local files.

An explicit, noisy recovery override exists for maintainers and migration tooling. It
must require a reason, print every bypassed path, and be unsuitable for unattended
agent defaults. Normal schema migration must use storage and therefore produces its
own receipts.

### Portable validation

`agentjobs validate` works without receipts and is the backstop for CI, manual editors,
Claude, Gemini, and MCP clients without compatible hooks. It checks:

- every YAML file parses and validates as strict schema v2;
- filename and stored ID agree;
- lifecycle/ball/reason/prompt/owner/outcome invariants;
- log ordering and references;
- configured categories and actor references where policy requires them;
- parent and dependency existence, self-links, and cycles;
- context/deliverable paths under repository policy;
- canonical AgentJobs serialization, reporting a diff when a file was hand-shaped.

CI cannot prove which local program made a valid canonical edit because local receipts
are intentionally not committed. It does prove the corpus is safe to load and makes
invalid direct writes loud. This limitation must be stated, not hidden behind the word
"enforcement."

### Cross-agent matrix

| Client/writer | MCP domain tools | Pre-tool prevention | Local commit receipt gate | Portable validation |
| --- | --- | --- | --- | --- |
| Codex with plugin | yes | yes, after hook trust | yes when installed | yes |
| Codex standalone MCP | yes | only if repo/user hook separately installed | optional | yes |
| Claude/Gemini/other MCP | yes | client-specific; not promised by this plugin | yes when using Git hook | yes |
| Manual editor/script | no | no | yes at commit | yes |
| CI/clean clone | no mutation | no | receipts unavailable | yes |

## 9. Tests and realistic evaluations

Implementation is not complete with unit tests alone.

### Contract and protocol tests

- Snapshot/validate `tools/list` names, annotations, input schemas, and output schemas.
- Start the packaged STDIO command and exercise MCP initialize, tools/list, tools/call,
  structured results, text fallback, invalid arguments, clean shutdown, and stderr-only
  logging with the official MCP client or inspector.
- Run against a real freshly started AgentJobs HTTP service, not only mocked
  `TaskClient` responses.
- Verify project isolation with colliding task IDs across two projects.
- Verify actor validation and every lifecycle/ball invariant through MCP.
- Race claims, content updates, log appends, and create retries; prove one winner, no
  lost logs, no duplicate creates, and deterministic replay.
- Kill/restart MCP between a committed mutation and its retry; prove durable
  idempotency.
- Verify a post-commit webhook failure returns a warning while the reloaded task is
  correct.
- Install the built wheel in a clean environment and launch the plugin command on
  Windows and POSIX.

### Agent-behavior evaluations

Use a fixed scenario corpus and record tool traces, final task state, and any attempted
filesystem write:

1. create a ready task, claim it, log progress, hand it to a human for review;
2. resume a zero-context active task from `task_get` and obey the latest handoff;
3. choose the correct project when two projects contain the same task ID;
4. handle two agents racing to claim one task;
5. retry after a simulated timeout without duplicating the task or log entry;
6. reject a prompt asking to set `lifecycle: active` directly;
7. attempt patch, shell redirection, PowerShell, and script-based task writes and observe
   the hook denial;
8. read task YAML successfully for review;
9. encounter a broken task file and report the concrete repair path instead of saying
   the task does not exist;
10. try to hand off `human/work` or close without an outcome and receive immediate,
    useful errors.

Release evidence includes the protocol transcript, eval pass/fail table, hook matrix,
and the repository's complete `poetry run python scripts/check.py` gate.

## 10. Documentation and discoverability

The README and docs index lead with the supported agent workflow: install AgentJobs,
run the HTTP service, connect the standalone MCP or install the plugin, call
`projects_list`, and use managed verbs. The task schema and agent workflow guides state
that YAML is readable generated state, not an authoring interface.

Client-specific pages cover Codex desktop/CLI, Claude, Gemini, and a generic MCP client.
Each page includes a health check, project-selection example, actor setup, a complete
handoff loop, upgrade instructions, and the enforcement matrix. Plugin/skill text stays
workflow guidance; it does not restate tool schemas that MCP already publishes.

Future push notification delivery remains outside MCP. A successful human handoff is
durable in the task and emits the existing HMAC-signed `task.handoff` webhook. A future
notification service subscribes there. MCP may return delivery warnings but does not
become an email, mobile-push, or account service.

## 11. Bounded Beads checkpoint

The current Beads project is a serious alternative, not a library that can be swapped
under AgentJobs without changing the product. Its current architecture uses Dolt as the
database, hash IDs, atomic claim, dependency-aware ready work, cross-machine Dolt
push/pull, agent setup, hooks, and an existing MCP server. Those features overlap
AgentJobs storage, locking, task IDs, dependencies, CLI/client integration, and parts of
the agent onboarding work.

### What Beads would replace

- YAML storage, atomic-file writes, and much of the single-writer lock machinery;
- sequential ID generation and Git merge collision handling;
- ready-queue/dependency graph basics and some hierarchy behavior;
- part of the CLI/MCP/bootstrap surface;
- optional synchronization infrastructure that AgentJobs does not currently provide.

### What remains custom

Beads' documented core status loop is `open -> in_progress -> closed`. AgentJobs would
still need a compatibility/domain layer for:

- baton ownership independent of lifecycle (`agent`, `human`, `external`);
- holder-scoped ball reasons and mandatory `ball_prompt`;
- AgentJobs' five lifecycle/ball/owner/outcome invariants;
- release, human review, approval, change-request, and external-block handoffs;
- the typed append-only resumption log and open question threads;
- AgentJobs acceptance criteria, deliverables, links, branch lifecycle, and current
  parent rule that an umbrella with open children is not claimable;
- exact REST/OpenAPI and React UI behavior, webhooks, task corpus compatibility, and the
  zero-context resumption contract.

Mapping those semantics onto Beads notes, comments, custom statuses, labels, or a
parallel AgentJobs table would preserve little of the promised simplification. The
existing Beads MCP tool list also exposes generic update/status operations that do not
by themselves enforce AgentJobs' ball contract.

### Would it materially simplify this work?

It would simplify MCP bootstrapping, atomic claims, ID collisions, and future
cross-machine synchronization. It would not remove the AgentJobs MCP domain adapter,
actor policy, direct-write enforcement, behavior evaluations, plugin packaging, UI,
or notification receiver. Push notification delivery is still an application concern;
Dolt sync is data synchronization, not a human alert.

Adoption would add a Dolt runtime and schema migration, replace human-readable YAML as
the source of truth with a database plus export, require a full corpus converter and
dual-read/rollback plan, and force the React/API/test suite to be rewritten or fronted
by a compatibility service. Beads itself documents schema-version and coordinated
upgrade procedures, embedded single-writer versus server modes, and sync/backup
operations. Those are useful capabilities, but they are also operational surface that
AgentJobs does not currently carry.

No current requirement is fundamentally blocked by Git-backed YAML. The observed
invalid task was caused by bypassing the manager, not by an inability of YAML to model
or validate the state. Existing locks already serialize claims and log appends; the
planned receipts, hook, and validation command address the direct-write gap. Git-backed
YAML may become the limiting architecture if AgentJobs needs sustained multi-machine
concurrent writes, large corpora with query latency, or automatic bidirectional sync.
That evidence does not exist today.

### Decision rule and conclusion

**Do not pause or replatform.** Proceed with the incremental MCP design.

Reopen the decision only with concrete evidence that all of the following are true:

1. an implemented prototype preserves the ball/reason/prompt, lifecycle, log,
   hierarchy, and zero-context contracts without a parallel AgentJobs state store;
2. at least two real AgentJobs requirements fail performance, concurrency, or sync
   targets because of YAML/Git rather than missing implementation;
3. the prototype deletes a material portion of manager/storage/API code and tests
   instead of moving that logic into adapters;
4. corpus migration, rollback, Windows packaging, UI compatibility, and upgrade tests
   pass with an agreed operational owner.

Until then, Beads is a considered alternative and a source of useful implementation
patterns, not the storage engine for this task graph. No Beads migration tasks should be
created.

## 12. Sources and existing-work boundary

Primary material consulted on 2026-08-14:

- [OpenAI: Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp)
- [OpenAI: Codex hooks](https://learn.chatgpt.com/docs/hooks)
- [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)
- [OpenAI: Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [MCP 2025-11-25 tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP 2025-11-25 transports specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Beads repository and current architecture](https://github.com/gastownhall/beads)
- [Beads MCP integration](https://github.com/gastownhall/beads/tree/main/integrations/beads-mcp)

Related AgentJobs work is reused, not reopened:

- task-049: broken task files are loud;
- task-052: manager/API state verbs are authoritative;
- task-055: per-task writes and claims are locked;
- task-057: project registry and scoped backend;
- task-064: actor identity vocabulary;
- task-100: dependency cycle visibility remains its own task;
- task-105: general automatic ID generation defect remains its own task;
- `src/agentjobs/webhooks.py`: future notification delivery extension point.

The MCP implementation must not absorb task-100 or task-105 merely because its tests
encounter those known defects. It may depend on their public behavior or add integration
coverage after they land.
