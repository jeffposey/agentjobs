---
name: agentjobs
description: >-
  Work with AgentJobs tasks: find projects and tasks, create work, claim it, log
  progress, hand off for review, release, and close. Use whenever the request
  involves an AgentJobs task, a backlog, a task id like task-042, "what should I work
  on", claiming or handing off work, or recording a decision or question on a task.
---

# Working AgentJobs tasks

AgentJobs stores tasks as YAML in a git repository. **The YAML is generated state.**
Read it freely; never edit it. Every write goes through the AgentJobs MCP tools, which
validate the change, lock the file, and record who did what and why. An edit that
skips them produces a record that looks fine and is not — that is the failure this
whole interface exists to prevent.

## Start here, every time

1. `projects_list` — the only tool that does not take a `project_id`. It returns each
   project's id, its configured **actors**, and its `default_user`.
2. Pass an exact `project_id` to every other tool. Do this even when there is only one
   project. Task ids are unique only *within* a project, so an unnamed project means an
   unpredictable one.
3. Pick your `actor` from that project's configured agent actors. It is written to a
   log nothing ever rewrites. **Never send a model name, an OS username, or the
   project's `default_user`** — `default_user` is the human, and filing your work under
   them makes the record lie.

## Before you touch a task, read it

`task_get` returns the whole record. It is designed so that a session with no other
context can pick the work up:

- `spec.summary` orients you; `spec.description` is the working specification.
- `ball_prompt` is what is wanted **right now**. It is not the same as the spec.
- Read `log` newest-first: the most recent `handoff`, then every `decision` and
  unanswered `question` since. **Decisions are binding — do not relitigate them.**
- `dependency_facts.unmet_needs` tells you whether the work is actually startable.
- `subtasks` matter: an umbrella task with open children is finished by its children,
  and cannot be claimed.

## The loop

| You want to | Call |
| --- | --- |
| see the backlog | `tasks_list` (filter by `lifecycle`, `ball`, `priority`, `parent`) |
| find a task by words | `tasks_search` |
| find work to do | `task_next` (suggests; it does **not** claim) |
| make a draft claimable | `task_promote` (the only exit from `draft`) |
| take it | `task_claim` |
| record what happened | `task_log_append` |
| pass it on | `task_handoff` |
| give it back | `task_release` |
| finish it | `task_close` |
| edit the spec | `task_update_content` |
| add new work | `task_create_draft` or `task_create_ready` |

There is no tool that sets `lifecycle`, `ball`, or `outcome` directly, and none is
coming. State moves through the verbs, or not at all.

**Handoff vs release.** A handoff names who acts next and why, and always carries a
prompt addressed to them. Putting work back in the pool is `task_release`, not a
handoff — `agent/available` is deliberately not a handoff target. Do not leave an open
task sitting with you while nothing happens to it; hand it to `human` for a decision
or `external` for a blocker instead.

**Log as you go, not at the end.** Progress, a `decision` with the reasoning *and the
rejected alternative*, an open `question`. A decision recorded only in chat is lost the
moment the session ends, and the next agent will make it again, differently.

## Retries and conflicts

- Every mutation needs an `operation_id`: a UUID **you** generate, one per distinct
  operation. If a call times out, **resend it with the same id** — the server replays
  the original result instead of writing twice. The result's `replayed` field tells you
  which happened.
- `task_handoff`, `task_close` and `task_update_content` also need
  `expected_revision`: the `updated` value from your most recent `task_get`. If the
  task moved since you read it, the call is refused and returns the current task. Read
  it, decide again, resend. Do not retry blindly.
- Every failure carries a `code`. Branch on it:
  `unknown_project` and `unknown_actor` mean fix your arguments; `invalid_input` means
  re-read the tool's schema; `revision_conflict` means re-read the task;
  `invalid_transition` and `dependency_blocked` mean the move is not available and
  never will be from here; `broken_task` means a file needs repair; `lock_timeout` and
  `service_unavailable` are the only two worth retrying unchanged.

## When a tool fails

Surface the error and diagnose it. **A failing tool is not permission to edit YAML.**
If the MCP tools are unavailable entirely, the managed REST API and the `agentjobs`
CLI reach the same authoritative code path and are the correct fallback. Hand-editing
a task file is a documented emergency-recovery procedure only: it needs an explicit
reason, and `agentjobs validate` afterwards.

A `broken_task` error means the file exists and does not parse. Report which file and
which field, and offer to repair it. Do not report it as a missing task. `tasks_list`
and `tasks_search` return these alongside the valid tasks, in `broken` — if that array
is non-empty, say so, because claimable work may be hidden inside those files.

## Working the AgentJobs repository itself

Two local rules, which the tools cannot enforce for you:

- **Take a git worktree before you start**, not a checkout. Several agents share one
  clone, and `git checkout` replaces the files under whichever one is mid-task. Take it
  with `git worktree add`, not with Claude Code's `-w`: a `-w` session is isolated by a
  guard that refuses every git operation aimed at the shared clone, and that is where
  the rule below requires your task-record commits to land.
- **Task records are committed to `main`, never to a feature branch.** A handoff
  committed to a branch is invisible to the person it is addressed to, who opens the
  dashboard and reasonably concludes nothing is waiting for them.

## Tool schemas

Read them from the tool list. They are published with every field, its type, and what
it is for, and they are the current truth; anything restated here would eventually be
a stale second copy.
