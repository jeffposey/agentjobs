# Durable Codex dispatch architecture

## Status and decision

**Status: shipped.** All five implementation children below closed completed —
task-281 through task-285 — and `src/agentjobs/dispatch/codex_app_server.py` carries
the preflight, persisted-thread probe and resume classification this document designs.
Kept for the reasoning; `codex-dispatch.md` is the operator's page.

This document is the design record for task-280.  It preserves the AgentJobs
task lifecycle, append-only audit log, task-named worktree isolation, permission
postures, and per-task human review gate.  It changes neither the active
runner-group selection nor the project's directed Terra execution policy.

**Decision:** AgentJobs owns a durable *coordinator*, not one long-lived shared
Codex App Server process.  The coordinator owns one App Server process for each
active Codex turn, persists the run/thread/turn state, and reconciles it after
the AgentJobs host restarts.  A completed turn may be resumed in a fresh App
Server process against the persisted Codex thread.

A shared process would couple unrelated tasks to one child failure, require a
multiplexed notification router, and turn a crashed process into an ambiguous
multi-task recovery event.  It also does not provide a recovery advantage:
Codex persists threads and `thread/resume` is the supported recovery boundary.

## Contracts

These outcomes are deliberately independent.  A run must not report one as a
proxy for another.

| Contract | Success signal | Failure result | Does not imply |
| --- | --- | --- | --- |
| Dispatch | Required MCP preflight passes; `thread/start` or `thread/resume`, then `turn/start`, return IDs; AgentJobs writes the dispatch audit entry | No agent turn starts; run records a classified diagnostic | The turn completed, the thread can later resume, or Desktop shows it |
| Resumability | A later App Server process can `thread/read` and `thread/resume` the recorded `thread_id`, then start the next wake turn on that same ID | Preserve the original thread/run evidence and hand off or require an explicit cold start | Desktop index/sidebar visibility |
| Desktop visibility | Persisted thread is found by App Server reading/listing, plus a separately recorded Desktop observation when a human desktop check is required | `not_observed` or `unavailable`, with no downgrade of dispatch/resume success | Dispatch or resume failure |

AgentJobs stores the identifiers Codex returns.  `thread_id` is the continuation
key; `session_id` is metadata and must be read from Codex rather than derived.

## Lifecycle and recovery

The coordinator records the following state in each existing run directory:

```text
preflight -> starting -> running_turn -> turn_completed -> resumable
                 |            |                |
                 v            v                v
              failed     host_restarted     terminal_failure

resumable -> resuming -> running_turn
resuming  -> resume_busy | resume_missing | terminal_failure
resume_missing -> explicit_fresh_start -> starting
```

`host_restarted` is reconciliation, not an instruction to spawn a second agent.
The coordinator reads persisted run metadata and asks Codex for the stored thread
before acting.  It never uses a PID alone as proof that the turn is gone.

`resume_busy` replaces the current automatic fallback for an "active writer"
error.  A busy original thread is still the task's continuation and may be open
in Desktop or owned by a surviving process.  The task lock remains held and the
record states how to observe or retry it.  Automatically creating a fresh
thread here can duplicate work and loses the continuity the wake path exists to
preserve.

Only a positively classified missing, deleted, or irrecoverable stored thread
may use `explicit_fresh_start`.  That transition writes the prior run/thread,
the classified reason, and the new thread ID to the run and task audit records.
Generic JSON-RPC errors are hard failures; error handling must use structured
codes/status where Codex provides them, never a message substring.

## MCP preflight

Preflight runs after AgentJobs has acquired the per-task run lock, but before a
task turn and before dispatch is reported successful:

1. Resolve the exact Codex executable and launch a diagnostic App Server child
   with the same environment, cwd, required-MCP override, permission posture,
   and service settings as the intended turn.
2. Complete `initialize` and `initialized`; distinguish spawn, EOF, malformed
   JSON, JSON-RPC error, and timeout failures.
3. Inspect `mcpServer/startupStatus/updated` and page
   `mcpServerStatus/list`.  Require the configured `agentjobs` server to be
   enabled, required, authenticated where applicable, and ready.  For Codex
   versions that omit startup status but report usable server information and
   tools, record that capability as the positive readiness evidence.  Capture
   the server name, failure reason, error, effective base URL/config source,
   and elapsed phase in run metadata.
4. Start/resume the real task thread only after a ready result.  The existing
   `mcp_servers.agentjobs.required=true` remains the fail-closed final guard:
   Codex must also refuse `thread/start` and `thread/resume` when required MCP
   initialization fails.

Preflight does not call any task-mutating MCP method and does not manufacture a
disposable task conversation.  It is a diagnostic readiness check, not task
work.  If startup status is unavailable in a supported Codex version, preflight
must fail closed with a versioned capability diagnostic rather than quietly
turning into the old smoke path.

## Desktop visibility

App Server can verify durable persistence with `thread/read` and a paged,
unfiltered `thread/list` scoped by `cwd` and the recorded ID or AgentJobs thread
title.  The list must not require an `appServer` source label: the controlled
Windows acceptance run persisted the App Server thread with source `vscode`,
which would otherwise create a false negative.  This is automated evidence that
the session store can find the conversation after the launching process exits.

Codex Desktop's sidebar/index has no documented acknowledgement API.  Therefore
AgentJobs records a second, independent status:

- `persisted`: the automated App Server check found the expected stored thread;
- `desktop_visible`: a human, or a future explicitly supported Desktop API,
  observed the named thread and could open it;
- `not_observed` / `unavailable`: the Desktop check did not happen or could not
  be performed.  This is operational evidence, not a dispatch failure.

The runbook must state the observation window and refresh/restart procedure
used for a human smoke run.  It must never tell an operator that a missing
sidebar row proves the agent did not start.

## Test matrix

| Scenario | Dispatch | Resume | Desktop |
| --- | --- | --- | --- |
| AgentJobs MCP cannot spawn, handshake, or become ready | failed with phase diagnostic; no turn | n/a | n/a |
| Fresh thread starts and completes | success with all IDs and append-only audit evidence | later same ID resumes | persisted check; human visibility recorded separately |
| AgentJobs host restarts while a recorded run exists | reconciled without duplicate spawn | same ID or explicit classified handoff | unchanged/independent |
| Resume reports active writer | `resume_busy`; task lock retained; no fresh thread | retry/observe original thread | may be visible while busy |
| Thread is positively missing/deleted | classified failed resume | explicit, audited fresh start only | new thread separately observable |

Unit tests use a scripted JSONL App Server fixture for every transition and
diagnostic.  A controlled Terra smoke is separate from unit tests and is the
only acceptance step that exercises actual account/Desktop behavior.  It keeps
the normal review gate intact and does not change runner selection.

## Implementation children

1. **MCP preflight and phase diagnostics** — launch/handshake/status client,
   structured errors, run metadata, and fixture coverage.
2. **Per-run Codex coordinator and reconciliation** — state model, durable
   ownership, host-restart reconciliation, and task-lock behavior.  Depends on 1.
3. **Resume versus fresh-session policy** — typed resume classification,
   `resume_busy`, explicit cold-start transition, and no-duplicate tests.
   Depends on 2.
4. **Persistence/visibility reporting** — App Server persisted-thread probe,
   independent Desktop observation fields and UI/API presentation.  Depends on
   2 and 3.
5. **Terra acceptance smoke and runbook** — controlled dispatch/resume evidence,
   Desktop observation procedure, and operations documentation.  Depends on 1–4.

Every implementation child uses the directed Terra High/Standard configuration
and retains the normal worktree, task-record, permissions, and human-review
protocol.
