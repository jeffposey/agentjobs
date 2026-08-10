# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Task Lifecycle
1.  **Read**: Read the task YAML (e.g., `tasks/agentjobs/task-042-*.yaml`) — its `spec`
    (`summary` → `intent` → `description` → `constraints` → `out_of_scope` → `context`)
    is the specification, `ball_prompt` is what is needed *right now*, and `acceptance[]`
    is what "done" means. Read the `log[]` newest-first: the last `handoff`, and every
    `decision` and open `question` since. **Decisions are binding — do not relitigate
    them.** Check `dependencies[]` and confirm they are satisfied before starting.
2.  **Branch, then claim**: Create the branch first, then `claim` the task and record the
    branch in `branches[]` — in that order, so no work is ever committed outside a
    branch. See [ENGINEERING.md](ENGINEERING.md#branch-lifecycle).
3.  **Work**: Small, single-logical-change commits with tests green before each one.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works.
5.  **Hand off**: `handoff` to `human`/`review` with a `ball_prompt` saying what was done
    and what needs review. **Stop there** — do not merge.
6.  **On approval**: Rebase onto `main`, merge `--no-ff`, mark the branch `merged`, and
    `close` the task with `outcome: completed`.

State is four fields, not one (schema v2 — see [docs/task-schema.md](docs/task-schema.md)):
`lifecycle` (`draft`/`ready`/`active`/`closed`), `ball` (who acts next — `agent`/`human`/
`external`, required while open), `ball_reason` (scoped to the holder), and `outcome`
(set only when closed). `archived` is a separate flag.

The axes move **only** through the manager verbs — `claim`, `handoff`, `release`,
`close` — each of which appends its own log entry. Editing them directly, or by hand in
the YAML, skips the record of *why* they moved. Only `ready` tasks with no unmet `needs`
dependencies are returned by `get_next_task()`.

`ball_prompt` is required whenever the ball is set: a handoff without a stated ask is a
notification with no payload, and the schema rejects it.

### Logging Work to the Task
The task record — not the surrounding conversation — is the source of truth for where
work stands. A different agent, or the same one with no memory of this session, must be
able to read the task YAML alone and know what happened and what is next.

-   Log each working pass as a `progress` entry through the API, **whether or not a
    human is watching**. An interactive chat session is a convenience, not the system of
    record.
-   Say what was done, what was verified and how, and what remains. Write for a reader
    with zero context.
-   Record decisions as `decision` entries, with their reasoning **and the rejected
    alternative** — especially scope changes and anything deliberately *not* done. A
    later reader cannot recover that from the diff.
-   Raise unknowns as `question` entries. A question with no `answer` threaded to it is
    queryable as an open thread; a question asked only in chat is not.
-   Never report a task complete on the strength of a chat message alone; it must be
    closed through the API.

### Agent Handoffs
-   When pausing, blocking, or handing off, `handoff` the ball with a `ball_prompt`
    covering open questions, blockers, and next steps.
-   Move the ball to `external`/`dependency` (blocked on another task),
    `external`/`service` (blocked on a third party), or `human`/`decision` (needs a
    call) rather than leaving a task sitting with the agent while nothing happens to it.
    An open task always names who acts next — that is what the schema enforces.

## Reporting Standards
-   **Conciseness**: Be brief. Use bullet points.
-   **Evidence**: Link to artifacts, screenshots, or log files that prove success.
-   **Context**: When reporting errors, include the full stack trace and the command that caused it.

## Behavioral Guidelines
-   **Ask First**: If requirements are ambiguous, ask the user for clarification.
-   **Non-Destructive**: Do not delete files or wipe databases unless explicitly instructed.
-   **Self-Correction**: If a tool fails, analyze the error message, fix the input, and retry. Do not loop endlessly.
-   **Transparency**: Explain *why* you are making a change, not just *what* you are changing.
