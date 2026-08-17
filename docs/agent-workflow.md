# Agent Workflow Guide

AgentJobs is a durable handoff protocol for agents, humans, and external dependencies.
The task YAML is the source of truth. Chat can wake a participant or make an interactive
session convenient, but it is never required working memory.

The canonical contract is [schema design section 5](schema-design.md#the-resumption-contract).
This guide shows how to apply it with the schema-v2 Python client.

## Task YAML is readable generated state

Read the task files whenever you want; reviewing a task means opening it. But **do not
edit them**. Every change goes through a managed interface — the
[MCP tools](mcp.md), the REST API, the CLI, or the web UI — which all reach the same
code path: strict validation, a per-task lock, and a log entry recording who moved what
and why. A direct edit skips all three and produces a record that looks right and is
not. That is not hypothetical: a task once written directly with `lifecycle: active`
and no `ball` logged no transition, failed no validator, and disappeared from every
listing as a broken file.

If a managed operation fails, diagnose the error — every one carries a code and a
suggested action. A failing tool is not permission to edit YAML. Direct repair is an
emergency procedure for a maintainer, requires a stated reason, and is followed by
`agentjobs validate`.

Agents with MCP available should prefer it for every task read and write; the REST API
and CLI are the fallback when it is not.

## The Core Model

Schema v2 separates questions that v1 compressed into one `status` field:

| Field | Question | Examples |
| --- | --- | --- |
| `lifecycle` | Where is the task in its life? | `draft`, `ready`, `active`, `closed` |
| `ball` | Who acts next? | `agent`, `human`, `external` |
| `ball_reason` | Why do they hold it? | `work`, `review`, `decision`, `dependency` |
| `ball_prompt` | What must that holder do next? | A concrete, self-contained ask |
| `outcome` | How did a closed task end? | `completed`, `cancelled`, `superseded`, `duplicate` |

Every open task has a ball holder. Every non-available handoff has an ask. UI labels
such as "Needs review" or "Blocked" are computed from these fields; they are not stored
state.

## Resume Without Chat History

A fresh agent session resumes from the record alone, in the order defined by the
[resumption contract](schema-design.md#the-resumption-contract):

1. Read `spec`. `spec.summary` gives a one-or-two sentence orientation for a
   zero-context reader; `spec.description` is the detailed working specification.
2. Read the state axes and `ball_prompt` to learn who acts now and the immediate ask.
3. Read `log[]` newest-first: begin with the latest `handoff`, preserve every binding
   `decision`, and identify unanswered `question` entries.
4. Read `acceptance[]` to learn what done means and what has already been verified.

Also inspect `deliverables[]`, `dependencies[]`, `parent`, and `branches[]` when they
apply. Before ending a session, write every resumption-critical fact to the log and make
the `ball_prompt` current. A handoff is defective if the next participant needs the chat
transcript to discover what happened, why a decision was made, or what to do next.

## Canonical Agent Loop

```python
from agentjobs import Ball, BallReason, TaskClient

agent = "my-agent"

with TaskClient() as client:
    task = client.get_next_task(agent=agent)
    if task is None:
        raise SystemExit("No claimable task")

    task = client.claim_task(task.id, agent=agent)

    # Work in the task branch, record decisions, and verify the result.
    client.add_progress_update(
        task.id,
        agent=agent,
        summary="Implemented and verified the requested change",
        details="Changed src/feature.py. `poetry run pytest` passed.",
    )

    client.handoff_task(
        task.id,
        actor=agent,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Review branch feat/task-123-feature. Approve the merge or request "
            "specific changes. Tests: `poetry run pytest` passed."
        ),
        body=(
            "Implemented the feature and added regression coverage. The branch is "
            "complete; no merge has been performed."
        ),
    )
```

The claim is atomic: one eligible agent wins and other claimants receive an error.
`get_next_task()` returns only ready, eligible tasks with no unmet `needs` dependency
and no open child tasks.

For AgentJobs repository work, create the worktree and branch before claiming. Task
metadata is updated and committed on `main`; code and documentation stay on the task
branch. Repository contributors must also follow `ALLAGENTS.md` and `ENGINEERING.md`.

## Resume an Existing Task

Do not assume an open task belongs to the current conversation. Fetch it and reconstruct
the state from the record:

```python
from agentjobs import Ball, TaskClient

with TaskClient() as client:
    task = client.get_task("task-123-feature")

    if task.ball is not Ball.AGENT:
        raise SystemExit(
            f"Do not work yet: {task.ball.value} holds the ball. "
            f"Current ask: {task.ball_prompt}"
        )

    latest_handoff = next(
        (entry for entry in reversed(task.log) if entry.type.value == "handoff"),
        None,
    )
```

Then follow the reading order above. A human approval or change request is itself a
handoff entry, so a new session does not need the conversation in which it was given.

## State Verbs and Handoffs

Use a state verb for every ownership change. Do not patch `lifecycle`, `ball`,
`ball_reason`, or `outcome` directly; manager verbs enforce consistency and append the
transition history.

### Human Review, Approval, Input, or Decision

At any human-decision point:

1. Record what changed, decisions made, verification performed, and remaining risk in
   the task log.
2. Call `handoff_task()` with `ball="human"`, the precise reason (`review`, `approval`,
   `decision`, `input`, or `spec`), and a self-contained `ball_prompt`.
3. Commit the task-record update where the project workflow requires it.
4. Notify through whatever interactive channel is available today: the chat reply and,
   when the host provides it, push notification. The notification is only a wake-up
   signal; all substance belongs in the task record.
5. Stop. Do not merge or make the decision on the human's behalf.

The React UI can record approval or requested changes. Approval hands the ball back as
`agent/work` with instructions to rebase, merge, update branch metadata, and close.
Requested changes hand it back as `agent/revise`, with the feedback preserved in both
`ball_prompt` and the handoff log.

### External Block

If claimed work cannot proceed, hand off to `external/dependency` for another task or
`external/service` for a third party, outage, or provisioning step. State the exact
unblocking event in `ball_prompt` and record what was tried. A ready task with an unmet
`needs` dependency stays ready and is simply not claimable; do not duplicate that fact as
stored blocked state.

### Release or Close

- `release_task()` returns active work to `ready` / `agent/available` and clears the
  owner. Use it when bowing out, not when waiting on a named participant.
- `close_task()` ends the lifecycle and records an outcome. A closed task has no ball.
  Closing as completed follows verification and, where required, explicit approval.

## Durable Logging

The unified `log[]` replaces v1's status updates, comments, and follow-up prompts.

```python
from agentjobs import TaskClient

with TaskClient() as client:
    client.add_log_entry(
        "task-123-feature",
        actor="my-agent",
        type="decision",
        body=(
            "Used the existing cache abstraction because it preserves invalidation "
            "semantics. Rejected a second cache client because it would split policy."
        ),
    )
    client.add_log_entry(
        "task-123-feature",
        actor="my-agent",
        type="question",
        body="Should failed imports be retried automatically?",
    )
```

Use `progress` for work and verification, `decision` for a choice plus reasoning and a
rejected alternative, `question` and `answer` with `re` for open threads, and
`instruction` for a durable directive. State changes create their own `transition` or
`handoff` entries; callers cannot forge transitions directly.

## Querying the Queues

```python
from agentjobs import TaskClient

with TaskClient() as client:
    ready = client.list_tasks(lifecycle="ready")
    human_inbox = client.list_tasks(ball="human")
    externally_blocked = client.list_tasks(ball="external")
    high_priority = client.list_tasks(priority="high")
    task = client.get_task("task-123-feature")
    matches = client.search_tasks("cache invalidation")
```

The human inbox is `ball=human`, not a stored waiting status. The blocked list is
`ball=external`, not a stored blocked status.

## Creating a Self-Sufficient Task

```python
from agentjobs import TaskClient

with TaskClient() as client:
    task = client.create_task(
        title="Add bounded retry handling",
        summary=(
            "Import jobs currently fail permanently on transient upstream errors; "
            "add bounded retries while preserving non-retryable failures."
        ),
        description=(
            "Retry HTTP 429 and 5xx responses up to three times with capped backoff. "
            "Do not retry validation failures. Add deterministic tests."
        ),
        priority="high",
        category="infrastructure",
        lifecycle="ready",
        eligible=["my-agent"],
    )
```

`spec.summary` is not a role-specific "human field" and the description is not an
agent-only field. Both audiences use the same record: the summary provides orientation;
the description supplies detail.

## Notifications and Future Extension

AgentJobs currently relies on the active host's available channel--chat and, when
available, push notification--to alert a human after the durable handoff is written. It
does not yet provide a general email, SMS, mobile-push, desktop-toast, or accounts
service.

The intended extension point already exists in `src/agentjobs/webhooks.py`. Webhooks are
HMAC-signed, and schema v2 emits `task.handoff` with the ball holder and `ball_prompt`.
A future pluggable notification service can subscribe to handoffs where `ball=human` and
route them to configured channels. This is the schema-v2 replacement for the older
`task.status_changed` extension point; the receiver and account/channel model remain
explicitly out of scope here.

## Errors and Server Setup

```python
from agentjobs import TaskClient, TaskClientError

try:
    with TaskClient(base_url="http://localhost:8765", timeout=60) as client:
        task = client.get_task("task-123-feature")
except TaskClientError as exc:
    print(f"AgentJobs request failed: {exc}")
```

AgentJobs is not yet published to PyPI. Install it from a clone and open the primary
React application:

```bash
poetry install
poetry run agentjobs open
```

`agentjobs serve` is the foreground-server form. Both serve the packaged React app at
`/app/`; neither needs Node at runtime.

See the [task schema reference](task-schema.md), [API reference](api-reference.md), and
[schema-v2 design](schema-design.md) for the complete field and endpoint contracts.
