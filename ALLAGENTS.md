# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Task Lifecycle
1.  **Planning**: Read the task file (e.g., `tasks/task-001.yaml`) to understand requirements.
2.  **In Progress**: Mark the task as `IN_PROGRESS` via CLI or API when starting work.
3.  **Verification**: Run tests and verify functionality manually (e.g., via browser).
4.  **Completion**: Update the task status to `COMPLETED` with a summary of work done.

### Agent Handoffs
-   When pausing work or handing off, leave a clear status update in the task file or `task.md`.
-   Note any open questions, blockers, or next steps.

## Reporting Standards
-   **Conciseness**: Be brief. Use bullet points.
-   **Evidence**: Link to artifacts, screenshots, or log files that prove success.
-   **Context**: When reporting errors, include the full stack trace and the command that caused it.

## Behavioral Guidelines
-   **Ask First**: If requirements are ambiguous, ask the user for clarification.
-   **Non-Destructive**: Do not delete files or wipe databases unless explicitly instructed.
-   **Self-Correction**: If a tool fails, analyze the error message, fix the input, and retry. Do not loop endlessly.
-   **Transparency**: Explain *why* you are making a change, not just *what* you are changing.
