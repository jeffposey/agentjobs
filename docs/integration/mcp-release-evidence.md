# MCP release evidence

What was verified for the AgentJobs MCP program (task-109), and what was not. Captured
2026-08-17 against AgentJobs 0.1.0 on Windows 11, Python 3.13.

## Verified

### The repository gate

`poetry run python scripts/check.py` passes: pytest (1089 tests), the generated
frontend API client check, lint, the Vitest component suite, the React build, and one
Playwright path against a real server.

### Protocol, against the packaged command

`tests/test_mcp_protocol.py` launches `agentjobs mcp` as a subprocess and drives it
with the official MCP client, against an AgentJobs service in a third process:

- initialize returns `agentjobs` / the installed version / the tools capability, with
  the accepted instructions in the first 512 characters;
- `tools/list` publishes exactly the fourteen tools, each with a description, a closed
  input schema, an output schema, and annotations;
- a call returns `structuredContent` and a text fallback;
- an unknown project, malformed arguments, and an unknown tool each return
  `isError` with a stable code — never an empty result;
- stdout carries only JSON-RPC; diagnostics go to stderr;
- the server exits 0 when stdin closes;
- a create and a claim over the pipe land in a real YAML file on disk.

### Agent behaviour

All ten scenarios from section 9 of the design pass and write a versioned artifact to
`out/mcp-evals/report.json` with every tool call, its outcome, and the final persisted
state of each task (41 recorded calls). The scenarios evaluate the interface rather
than a model: each performs the calls a correct agent would make, and for the refusal
cases the calls a confused one would make.

### Packaging

The wheel builds, installs into a clean virtual environment, and the installed
`agentjobs mcp` command completes a real MCP session against a running service.

### End to end, by hand

Against a scratch project through the packaged server: create → claim → retry the claim
with the same `operation_id` (`replayed: true`, nothing written) → append progress →
`task_get` → hand off to `human`/`review` → retry with a stale revision (refused,
`revision_conflict`, current task returned) → close. An invalid handoff target
(`human`/`work`) and a content patch setting `lifecycle` were both refused with
`invalid_input` and field errors.

### The direct-write guard

82 cases across the file-write tools, `apply_patch` bodies, five path spellings, shell
redirection, eleven PowerShell writers and aliases, nine POSIX writers, interpreter
one-liners, compound commands, and writing `git` subcommands — plus the hook's real
stdin/stdout protocol. Reads are never denied.

### Validation

`agentjobs validate` runs against this repository's own 94-record corpus. No file is
unloadable, no relationship points at nothing, and there is no dependency cycle.

## Not verified here

**Codex itself.** Two acceptance criteria need a Codex session and could not be closed
by an agent without one:

- task-116 ac-3: the plugin loading in a fresh Codex CLI session and a fresh Codex
  desktop session, both exposing the skill and all fourteen tools from one shared local
  configuration.
- task-117 ac-5: Codex routing a live direct-write attempt through the hook and denying
  it before the file changes.

Everything checkable without Codex is covered by `tests/test_codex_plugin.py` and
`tests/test_codex_task_write_hook.py`.

**Python 3.11 and 3.12.** Only 3.13 is installed on this machine, so the clean-install
and launch checks ran there. The package declares `python = "^3.11"` and uses nothing
version-specific; a version matrix belongs in CI, which this repository does not yet
have.

**POSIX.** The launch command is argv-only with no shell or quoting, so it is platform
neutral by construction, and a verification path is documented in
[Connecting a client](../mcp-clients.md#posix-verification). It has not been executed.

## Known drift, out of scope

`agentjobs validate` reports 95 findings on the existing corpus: 60 `unknown-actor`,
22 `unknown-category`, and 13 `non-canonical-serialization`. All are in historical
records — nothing written during this program is flagged — and they are config/corpus
drift rather than MCP defects. They belong to task-103 (corpus audit). The commit gate
is opt-in (`agentjobs validate --install-hook`), so this blocks nothing today.
