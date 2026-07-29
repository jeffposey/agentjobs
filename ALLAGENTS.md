# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Task Lifecycle
1.  **Read**: Read the task YAML (e.g., `tasks/agentjobs/task-042-*.yaml`) — its
    `description`, `success_criteria`, and `prompts.starter` are the specification.
    Check `dependencies[]` and confirm they are satisfied before starting.
2.  **Branch, then claim**: Create the branch first, then mark the task `in_progress`
    and record the branch in `branches[]` — in that order, so no work is ever committed
    outside a branch. See [ENGINEERING.md](ENGINEERING.md#branch-lifecycle).
3.  **Work**: Small, single-logical-change commits with tests green before each one.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works.
5.  **Hand off**: Set the task to `waiting_for_human` with a status update saying what
    was done and what needs review. **Stop there** — do not merge.
6.  **On approval**: Rebase onto `main`, merge `--no-ff`, mark the branch `merged`, set
    the task `completed`.

The valid statuses are `draft`, `ready`, `in_progress`, `blocked`, `waiting_for_human`,
`under_review`, `completed`, `archived` — see `TaskStatus` in `src/agentjobs/models.py`.
Only `ready` tasks are returned by `get_next_task()`.

### Logging Work to the Task
The task record — not the surrounding conversation — is the source of truth for where
work stands. A different agent, or the same one with no memory of this session, must be
able to read the task YAML alone and know what happened and what is next.

-   Log each working pass as a `status_updates` entry via the status API, **whether or
    not a human is watching**. An interactive chat session is a convenience, not the
    system of record.
-   Say what was done, what was verified and how, and what remains. Write for a reader
    with zero context.
-   Record decisions and their reasoning, especially scope changes and anything
    deliberately *not* done — a later reader cannot recover that from the diff.
-   Never report a task complete on the strength of a chat message alone; the status
    must be set through the API.

### Agent Handoffs
-   When pausing, blocking, or handing off, leave a status update covering open
    questions, blockers, and next steps.
-   Set `blocked` (external dependency) or `waiting_for_human` (needs a decision)
    rather than leaving a task `in_progress` while nothing is happening to it.

## Reporting Standards
-   **Conciseness**: Be brief. Use bullet points.
-   **Evidence**: Link to artifacts, screenshots, or log files that prove success.
-   **Context**: When reporting errors, include the full stack trace and the command that caused it.

## Behavioral Guidelines
-   **Ask First**: If requirements are ambiguous, ask the user for clarification.
-   **Non-Destructive**: Do not delete files or wipe databases unless explicitly instructed.
-   **Self-Correction**: If a tool fails, analyze the error message, fix the input, and retry. Do not loop endlessly.
-   **Transparency**: Explain *why* you are making a change, not just *what* you are changing.
