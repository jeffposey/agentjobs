# Engineering Guidance

This handbook is the canonical source for universal engineering practices across the AgentJobs project. It applies to both human and AI contributors.

## Project Mission
**AgentJobs** is a lightweight task management system designed for AI agent workflows.
-   **Core Philosophy**: "Git-Friendly" & "Lightweight".
-   **Data Source**: YAML files in the `tasks/` directory are the single source of truth.
-   **Interface**: CLI (`agentjobs`) and Web UI (`agentjobs serve`).

## Tech Stack
-   **Language**: Python 3.11+
-   **Web Framework**: FastAPI
-   **CLI Framework**: Typer
-   **Data Validation**: Pydantic v2
-   **Templating**: Jinja2
-   **Package Manager**: Poetry

## Development Workflow

### Setup
```bash
poetry install
poetry run agentjobs init  # If starting fresh
```

### Testing
-   Run tests before every commit:
    ```bash
    poetry run pytest
    ```
-   Ensure high test coverage for core logic (`manager.py`, `storage.py`).

### Code Style
-   **Formatter**: Black
-   **Linter**: Ruff
-   **Type Checking**: MyPy
-   **Pre-commit**:
    ```bash
    poetry run black .
    poetry run ruff check .
    poetry run mypy .
    ```

## Git Workflow

### Branch Naming
-   Branches **MUST** include the associated task identifier if applicable.
-   Format: `type/task-xxx-description`
-   Examples:
    -   `feat/task-004-add-pagination`
    -   `fix/task-012-resolve-race-condition`
    -   `chore/update-dependencies` (no task id)

### Commit Messages
-   Use [Conventional Commits](https://www.conventionalcommits.org/).
-   Format: `type(scope): description`
-   Examples:
    -   `feat(api): add webhook endpoints`
    -   `fix(storage): handle missing yaml files gracefully`
    -   `docs: update installation guide`

### Branch Lifecycle
-   Create the branch **before** marking the task `in_progress`, so no committed work
    exists outside a branch.
-   Record it in the task's `branches[]` field (`name`, `status: active`) as part of the
    same update that sets `in_progress`.
-   Branch from an up-to-date `main`.

### Commit Hygiene
-   One logical change per commit. If the commit message needs the word "and", it is
    probably two commits.
-   Tests pass before every commit, not just at the end of the branch.
-   Keep mechanical changes (reformatting, renames) in their own commits so they do not
    bury reviewable logic.
-   Explain *why* in the body when the change is not self-evident; the diff already
    shows *what*.

### The Merge Gate
Work does not merge itself. When a branch is complete and verified:

1.  **Stop.** Set the task to `waiting_for_human` through the status API, with a status
    update stating what was done and what needs review.
2.  Wait for **explicit** human approval. Absence of objection is not approval.
3.  On approval: rebase onto `main`, then merge with `--no-ff` (the merge commit is the
    reviewable unit of work, so fast-forward is not acceptable).
4.  Mark the branch `merged` in `branches[]` and set the task `completed`.
5.  Delete the local branch once merged.

Pushing to the remote is a separate act from merging; do not assume approval to merge
carries approval to push.

### Exception: task-metadata-only changes
Creating or grooming task YAML (writing a new task, re-sequencing the backlog, editing
a description) may be committed **directly to `main`** without a branch, using a
`chore(tasks):` commit. These records are the backlog itself rather than changes to the
software, and gating them behind review would mean a branch per idea captured.

The exception is narrow. It does **not** apply to a task's own status transitions made
while working it — those belong on that task's branch, alongside the work they describe.

## Safety Rails
-   **Never** delete user data without explicit confirmation.
-   **Always** use the `TaskStorage` abstraction; avoid direct file I/O on task files where possible.
-   **Verify** local server startup (`poetry run agentjobs serve`) after modifying API routes.
-   **Restart the server after changing models, storage, or task files.** A running
    `agentjobs serve` holds the imported code in memory. If the task files change
    underneath it — a schema migration, a checkout, a bulk edit — it reads new data with
    old code and every file appears corrupt. This is what a stale server looks like:
    dozens of validation errors naming fields that no longer exist. The application is
    fine; the process is old. `agentjobs restart` before concluding anything is broken,
    and never leave a stale server running for someone else to find.

## Verification
-   A passing suite is not evidence a feature works. Exercise the change the way a user
    would, against a freshly started server.
-   **Assert on rendered values, not on the presence of markup.** Checking that a page
    contains `data-ball=` passes while it emits `data-ball="Ball.HUMAN"` and every
    filter silently matches nothing. Assert the value a user's browser will act on.
-   When a check passes, ask what it would have caught. If the answer is "nothing that
    has ever gone wrong here", it is decoration.
