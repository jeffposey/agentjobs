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

### Sharing a clone

Working alone in your own clone, `git checkout -b` is fine and nothing below applies.

It stops being fine the moment something else is working the same clone — a second
person, or an agent. A clone has one working tree and one `HEAD`, so a checkout replaces
the files under whoever else is in there. **When you are not alone in a clone, take a
worktree instead of checking out:**

```bash
git worktree add ../aj-045 -b feat/task-045-subtask-support
git worktree remove ../aj-045      # after the branch merges
```

Agents in this repository are required to do this — see
[ALLAGENTS.md](ALLAGENTS.md#task-lifecycle) — because several of them routinely run
against one clone and none of them can see the others.

One consequence is worth knowing whoever you are, because it looks like a bug and is
not: **tasks are YAML files in this repository, so whichever branch is checked out
decides what the dashboard shows.** A task handed to you for review on a branch does not
exist as far as `main` is concerned. If the GUI is missing something you expect, check
what is checked out before filing anything.

### Commit Hygiene
-   Stage explicit paths. `git add -A` commits whatever happens to be in the tree, which
    is your own mess when you are alone and someone else's work when you are not.
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

### Task files live on `main`, always

**Everything under `tasks/` is committed directly to `main`, never to a feature branch.**
Creating a task, grooming the backlog, claiming, logging progress, handing off, closing —
all of it, using a `chore(tasks):` or `chore(task-nnn):` commit. A feature branch carries
code and docs. It does not touch `tasks/`.

This used to be the opposite: the exception was narrow and explicitly excluded a task's
own status transitions, on the reasoning that they belonged beside the work they
described. That reasoning was wrong, and the failure is not subtle.

**The dashboard reads one working tree.** A handoff committed to a branch is invisible
to the person it is addressed to — they open the GUI, see the task still `ready`, and
conclude nothing is waiting for them. The merge gate depends on a human seeing a review
request, so recording that request somewhere the human cannot see it defeats the gate
entirely. Worktrees make it airtight: the shared clone is then *never* on the review
branch. Observed 2026-08-11, repeatedly, before the cause was understood.

Keeping task files on `main` also removes task-file merge conflicts as a category. Two
agents on two branches can no longer produce two divergent versions of the same task
record, because neither branch contains one.

Practically: write task updates through the API or the manager, which resolve the project
root from the registry and therefore land in the `main` clone's working tree even when
your own work is happening in a worktree. Then commit them there:

```bash
git -C <path-to-main-clone> add tasks/agentjobs/task-045-*.yaml
git -C <path-to-main-clone> commit -m "chore(task-045): hand off for review"
```

The cost, stated so nobody rediscovers it as a bug: a task's record and the code it
describes are no longer one atomic commit, and checking out an old revision will not show
you the task state as it was then. `main`'s history has it. That is a fair trade for a
review request the reviewer can actually see.

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
