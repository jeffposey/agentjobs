# Engineering Guidance

This handbook is the canonical source for universal engineering practices across the AgentJobs project. It applies to both human and AI contributors.

## Project Mission
**AgentJobs** is a lightweight task management system designed for AI agent workflows.
-   **Core Philosophy**: "Git-Friendly" & "Lightweight".
-   **Data Source**: YAML files in the `tasks/` directory are the single source of truth.
-   **Interface**: CLI (`agentjobs`) and packaged React Web UI (`agentjobs open`, or
    `/app/` on a running `agentjobs serve`).

## Tech Stack
-   **Language**: Python 3.11+
-   **Web Framework**: FastAPI
-   **CLI Framework**: Typer
-   **Data Validation**: Pydantic v2
-   **Web application**: React 19, TypeScript, Vite, Tailwind CSS, TanStack Query
-   **Compatibility surface**: Jinja2 remains only for legacy server-rendered routes;
    it is not the primary or recommended UI.
-   **Package Manager**: Poetry

## Development Workflow

### Setup
```bash
python scripts/bootstrap.py       # poetry install + npm ci + the Playwright browser
poetry run agentjobs init         # If starting fresh
```
Run the bootstrap in **any** fresh checkout — a clone or a worktree. It is what makes
`scripts/check.py` runnable, and it verifies that the environment imports this checkout's
source rather than a neighbouring one's.

### Testing
-   Run the complete repository check before every commit:
    ```bash
    poetry run python scripts/check.py
    ```
-   The gate is ten named stages run **cheapest first**, and it prints what each one
    cost. From one green run on this machine, 2026-08-21, nothing else competing for it:

    | # | Stage | What it checks | Cost |
    |---|---|---|---|
    | 1 | `black` | Python formatting | 0.5s |
    | 2 | `ruff` | Python lint | 0.1s |
    | 3 | `mypy` | Python types | 1.5s |
    | 4 | `api` | `openapi.json` and the generated client both match the app | 4.5s |
    | 5 | `icons` | the committed PWA icons match `assets/app-icon.svg` | 3.2s |
    | 6 | `oxlint` | frontend lint | 0.5s |
    | 7 | `pytest` | 2190 Python tests | 326.5s |
    | 8 | `vitest` | 164 jsdom component tests across 23 files | 4.7s |
    | 9 | `build` | `tsc --noEmit` and the production bundle | 3.6s |
    | 10 | `e2e` | 16 Playwright tests against a live server | 20.0s |
    | | | | **365.0s** |

    MyPy is the one stage whose cost moves: 1.5s against a warm cache, about nineteen
    seconds on the first run after a checkout. Nothing else in the cheap block varies
    enough to notice.

    Use focused pytest or npm commands while iterating, but do not substitute them for
    the gate.
-   **Anything that can answer in seconds runs before anything that takes minutes.**
    Format, lint and types catch what pytest never will, and there is no reason to spend
    five minutes finding a misformatted file. Task-189 carried the same reasoning through
    stages 4 to 6, which used to run *after* pytest: together they cost 8.2 seconds, and
    a session working task-188 paid four and a half minutes twice to reach one of them.
    Everything above the pytest line now costs 10.3 seconds together.
-   The checks are *in* the gate rather than only in the pre-commit list below because a
    list nothing enforces is a statement of intent. Task-166 found `poetry run mypy .`
    had been aborting on a module-name collision before it checked a single file, and a
    Black drift sitting on `main`, both surviving for exactly that reason.
-   Two orderings are real dependencies rather than preferences, and stay: `build` writes
    the bundle `e2e` drives, and `api` exports the OpenAPI document before anything
    compares a generated client against it. Every other stage's position is purely a
    question of what it costs.
-   **Resume; do not re-run.** A failure names the stage it happened in, and every stage
    is addressable:
    ```bash
    poetry run python scripts/check.py --list          # the stages, in order
    poetry run python scripts/check.py --from vitest   # this stage and everything after
    poetry run python scripts/check.py --only oxlint   # just these (repeatable, or a,b)
    ```
    **The unqualified command is what the commit rule above means, and the only thing
    that does.** `--from` and `--only` exist for the loop between a late failure and its
    fix. A partial run prints `PARTIAL RUN`, names every stage it skipped, and repeats
    both at the end — so a green from `--from e2e` cannot be reported as a green from the
    gate.
-   **The gate runs before the commit, so no stage of it may require one.** The two
    generated checks — `openapi.json` and `src/api/generated/` — compare against **the
    working tree**, never `HEAD`: they ask whether the files on disk match what the
    application produces. Until task-189 the client half asked instead whether they were
    committed, and reported the answer as staleness, so a client you had just regenerated
    failed the gate with a message telling you to regenerate it. That is the contradiction
    the old check created, and this is the direction it is resolved in: **regenerate, run
    the gate, then commit.** The `api` stage names `frontend/src/api/generated` when those
    files are uncommitted, and does not fail — `git add` takes explicit paths here, and
    generated output is what that habit forgets.
-   Budget **about six minutes when you have the machine to yourself** — 365s for the
    table above, of which pytest is 326s. The other nine stages come to 38s between
    them, so the gate's wall clock is the Python suite and almost nothing else.
-   **Budget longer when you do not, and do not read slow as hung.** Several agents work
    this repository at once and this machine now allows three dispatched runs, so gates
    overlapping is the normal case rather than an unusual one. Measured the same day, on
    the same machine, all green: **two simultaneous gates 388s (+9%), four 411s (+16%),
    six 444s (+25%)** — worst case per run, against the 355s above. The degradation is
    gradual and there is no cliff, so **a gate that has been quiet for eight minutes is
    working, not stuck.** This paragraph exists because the previous figure said five
    minutes flat, and an agent that believes five minutes is the whole story kills a run
    that was about to pass. Concurrent gates are only safe at all because each checkout
    derives its own Playwright and benchmark ports from its own path (task-187); if you
    see a port collision, that is a bug and not a reason to serialise.
-   Ensure high test coverage for core logic (`manager.py`, `storage.py`).

### Measuring performance
-   `scripts/bench.py` times the API, the CLI and the browser's open-a-task
    interaction. See [docs/performance.md](docs/performance.md).
-   Every API response carries `X-Response-Time-Ms` and `X-Task-Parses`, so a slow
    request can be attributed without a profiler.
-   A change that claims to be faster states a before/after pair from that tool.
    Prefer asserting on task files parsed rather than on wall-clock time: the parse
    count means the same thing on every machine, and a timing threshold does not.

### Code Style
-   **Formatter**: Black
-   **Linter**: Ruff
-   **Type Checking**: MyPy
-   **While iterating** — the gate runs all three for you, so these are for fixing
    rather than for checking:
    ```bash
    poetry run black .          # rewrites; the gate runs `black --check`
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
git worktree add ../worktrees/aj-045 -b feat/task-045-subtask-support
cd ../worktrees/aj-045 && python scripts/bootstrap.py   # ~30s; no venv or node_modules yet
git worktree remove ../worktrees/aj-045      # after the branch merges
```

**They go in a `worktrees/` directory beside the clone, not loose beside it.** A worktree
is transient and there are several at a time, so a listing of the workspace that mixes
them in with the projects stops being a listing of the projects. `git worktree add`
creates the directory the first time; nothing else is needed.

The bootstrap is not optional politeness: a worktree that skips it cannot run
`scripts/check.py` at all, and borrowing the main clone's virtualenv to get around that
runs your tests against the main clone's source. See
[Bootstrapping a worktree](ALLAGENTS.md#bootstrapping-a-worktree).

Agents in this repository are required to do this — see
[ALLAGENTS.md](ALLAGENTS.md#task-lifecycle) — because several of them routinely run
against one clone and none of them can see the others.

One consequence is worth knowing whoever you are, because it looks like a bug and is
not: **tasks are YAML files in this repository, so whichever branch is checked out
decides what the dashboard shows.** A task handed to you for review on a branch does not
exist as far as `main` is concerned. If the React app is missing something you expect, check
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

1.  **Stop.** Use the handoff API to set `ball: human` / `ball_reason: review`, with a
    `ball_prompt` and handoff log entry stating what was done and what needs review.
    Notify the human through whatever interactive channel is available (the chat reply
    and, when the host provides it, a push notification). The notification is only a
    wake-up signal; the task record must contain the complete review request.
2.  Wait for **explicit** human approval. Absence of objection is not approval.
3.  On approval: rebase onto `main`, then merge with `--no-ff` (the merge commit is the
    reviewable unit of work, so fast-forward is not acceptable).
4.  Mark the branch `merged` in `branches[]` and set the task `completed`.
5.  Delete the local branch once merged, and remove the worktree.
6.  **Put the merged code in front of the human.** Rebuild the frontend if the change
    touched it, then restart the server. Merging is not delivering: `frontend_dist/` is
    gitignored and a running server holds its code in memory, so until you do this the
    person who approved the work is still looking at the version they approved it to
    replace. Verify the change is actually live before you say you are done.

    ```bash
    cd frontend && npm run build     # only if the change touched the frontend
    ```

    Then restart. `agentjobs restart` is the right command when the CLI started the
    server. **When something else started it, it is not yours to restart with the
    CLI** — a deployment behind a proxy, on a non-default port, or launched by a
    wrapper script is not the process `agentjobs restart` would touch; that binds the
    default 8765 and leaves the real dashboard stale while appearing to succeed.
    Restart it the way it was started, and check the environment's own setup notes for
    that command rather than assuming the default. Either way the step is the same: the
    human ends up on the merged version, and you checked.

AgentJobs does not yet deliver durable out-of-session notifications. The intended
extension point is the existing HMAC-signed webhook system in
`src/agentjobs/webhooks.py`: schema v2 emits `task.handoff` with the new ball holder and
`ball_prompt` (replacing v1's broader `task.status_changed` event). A future pluggable
service can subscribe to human-directed handoffs and route email, SMS, mobile push, or
desktop alerts. Building that service is separate work.

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
to the person it is addressed to — they open the React app, see the task still `ready`, and
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
-   **Verify** local server startup and the React `/app/` route (`poetry run agentjobs
    open`) after modifying API routes or frontend serving.
-   **A server that refuses to start because it "imported its own source from the wrong
    checkout" is telling the truth — do not work around it.** The virtualenv on that
    interpreter has an editable install pointing at a different checkout, so the process
    would read the right task files and run a different branch's code. Nothing else shows
    it: `git log` in the served clone is correct and so are the files on disk. The repair
    is printed in the error, and it is `poetry install` from the clone that should be
    running, then a restart. `AGENTJOBS_SKIP_SOURCE_CHECK` exists for an unusual install
    layout, not for getting past this. Task-194 is the incident; `/api/version` reports
    `source_root` if you want to ask a running server the same question.
-   **Rebuild the frontend after merging front-end work, then restart.**
    `src/agentjobs/frontend_dist/` is gitignored, so merging a React change to `main`
    does **not** update the bundle a running server serves. `git pull` and a restart
    are not enough; the clone that serves the app needs `npm run build` in `frontend/`
    as well. Observed 2026-08-17: a merged performance fix appeared to have done
    nothing, because the browser was still being handed the pre-merge bundle.
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
