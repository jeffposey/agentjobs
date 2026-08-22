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
    | 1 | `black` | Python formatting | 0.6s |
    | 2 | `ruff` | Python lint | 0.1s |
    | 3 | `mypy` | Python types | 1.5s |
    | 4 | `api` | `openapi.json` and the generated client both match the app | 4.2s |
    | 5 | `icons` | the committed PWA icons match `assets/app-icon.svg` | 2.8s |
    | 6 | `oxlint` | frontend lint | 0.6s |
    | 7 | `pytest` | 2608 Python tests, across every core | 52.1s |
    | 8 | `vitest` | 228 jsdom component tests | 5.2s |
    | 9 | `build` | `tsc --noEmit` and the production bundle | 3.7s |
    | 10 | `e2e` | 26 Playwright tests against a live server | 25.0s |
    | | | | **95.8s** |

    MyPy is the one stage whose cost moves: under two seconds against a warm cache, about
    nineteen seconds on the first run after a checkout. Nothing else in the cheap block
    varies enough to notice.

    **That pytest figure was 326.5s until task-233, and the gate's total was 365s.** Two
    changes account for the difference, both of them arrangements of how pytest is
    invoked rather than reductions in what it checks -- the same 2608 tests run, and the
    pass/fail counts were compared on the same commit before and after:

    | Configuration | Wall clock | Result |
    |---|---|---|
    | serial, with coverage -- what the gate ran until task-233 | 540.1s | 2538 passed |
    | serial, no coverage | 342.6s | 2538 passed |
    | `-n auto` across 32 cores, no coverage | **42.5s / 45.7s / 43.6s** | 2538 passed |
    | `-n auto --dist loadfile` | 54.9s | 2538 passed |

    Three consecutive `-n auto` runs are quoted because one green parallel run proves
    nothing about a suite's parallel-safety. The suite is safe because `tests/conftest.py`
    already gives every test its own project registry and its own Claude home and stubs
    the reachability probe, and because nothing in it binds a fixed port -- the four
    places that open a socket ask the kernel for port 0. One thing had to be fixed: a
    `parametrize` whose cases came out of a `frozenset`, which each xdist worker iterated
    in its own hash order, so the workers disagreed about what the test IDs were and the
    run aborted during collection.

    Coverage is off by default and available on request. It cost between 60 and 200
    seconds depending on what else the machine was doing, and wrote an HTML report that
    nothing reads before a commit.

    ```bash
    poetry run python scripts/check.py --coverage   # the gate, plus coverage and htmlcov/
    poetry run python scripts/check.py --serial     # one process, for readable output
    ```

    Use focused pytest or npm commands while iterating, but do not substitute them for
    the gate. A hand-run `pytest` is serial and measures no coverage: `addopts` is now
    empty, and `-n auto` is passed by the gate rather than configured globally, because
    xdist costs more than it saves on a small selection and its interleaved output is the
    wrong trade when you are reading one failure.
-   **The cheapest stage runs first, whatever the slowest one currently costs.**
    Format, lint and types catch what pytest never will, and there is no reason to wait
    on the test suite to be told about a misformatted file. Task-189 carried the same
    reasoning through stages 4 to 6, which used to run *after* pytest: together they cost
    8.2 seconds, and a session working task-188 paid four and a half minutes twice to
    reach one of them. Everything above the pytest line now costs 9.8 seconds together.

    The argument used to be stated as "seconds before minutes", and task-233 took the
    minutes away -- pytest is now under a minute. The ordering stays regardless: it costs
    nothing, and the gap it exploits reappears the moment a slow stage is added.
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
-   **`--since-gate` is the one sanctioned exception to the sentence above, and its
    boundary is narrow.** It answers "does this run need to happen at all" — the question
    task-221 asked after a rebase brought in a single task YAML and cost a full six-minute
    gate to re-establish something that could not have changed.

    ```bash
    poetry run python scripts/check.py --since-gate
    ```

    Four properties make it an exception a third party can check rather than a judgement
    call by whoever wants to skip the wait. Do not weaken any of them:

    1.  **It rests on a receipt the gate itself wrote, not on your assessment.** A green
        unqualified run on a clean tree records the commit it verified, in this
        checkout's git directory. `--since-gate` diffs the working tree against that
        commit. With no receipt it narrows nothing and runs every stage, saying so.
    2.  **The classification table is default-deny.** Exactly two families of path map to
        a reduced set of stages — task records under `tasks/`, and prose. Everything else,
        including anything nobody has classified yet, selects all ten. An incomplete table
        therefore costs time, never coverage. The table lives in `scripts/gate_scope.py`
        and each entry has to name what reads those paths.
    3.  **A stage whose inputs are not bounded by the diff still runs.** "It was only a
        task file" is not a safe skip: `tests/test_validate.py::TestRealCorpus` loads this
        repository's own records, so a task YAML genuinely can turn the suite red. That is
        why `tasks/` maps to `pytest` rather than to nothing.
    4.  **The output is the claim, in full.** A reduced run prints `NECESSITY RUN`, the
        commit it is diffing against, every changed path with the rule that matched it,
        and every stage it skipped. It never prints "Ran every stage". An unchanged tree
        prints `NOTHING CHANGED` and runs nothing.

    A `--since-gate` run that goes green on a clean tree issues its own receipt, recording
    which receipt it derived from, so a chain of them is auditable. `--only` and `--from`
    never issue one — a partial green is not the gate's green, which is the same rule
    `PARTIAL RUN` states.

    **This is worth much less than it was when task-221 was written, and it is kept
    anyway.** The full gate is now about a minute rather than six, so the rebase case
    saves under a minute. It stays because the reasoning is the durable part: the gate
    should be able to say what a change cannot reach, and once `pytest` is cheap the same
    machinery is what makes it safe to add an expensive stage later.
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
-   Budget **about a minute and a half when you have the machine to yourself** —
    95.8s for the table above. It was six minutes until task-233, and the gate is
    no longer the thing to plan a working session around.
-   **Budget longer when you do not, and do not read slow as hung.** Several agents work
    this repository at once and this machine now allows three dispatched runs, so gates
    overlapping is the normal case rather than an unusual one.

    The scaling figures previously recorded here — two simultaneous gates 388s, four
    411s, six 444s — were measured against the **serial** suite and are kept only as
    history. They do not describe the gate as it now runs, and **nobody has yet measured
    concurrent parallel gates**: `-n auto` asks for every core, so two of them are
    competing for the same 32 rather than each taking a core, and the honest statement is
    that the contended figure is unknown. Measure it before quoting one. What has not
    changed is the advice: degradation here has always been gradual with no cliff, so a
    gate that is taking longer than you expected is working, not stuck.

    Concurrent gates are only safe at all because each checkout derives its own
    Playwright and benchmark ports from its own path (task-187); if you see a port
    collision, that is a bug and not a reason to serialise.
-   Ensure high test coverage for core logic (`manager.py`, `storage.py`).

### Measuring performance
-   `scripts/bench.py` times the API, the CLI and the browser's open-a-task
    interaction. See [docs/performance.md](docs/performance.md).
-   `scripts/run_report.py` answers the other question: **where dispatched agent time
    goes.** It reads the run ledger in `~/.agentjobs/runs/` and prints total time, runs
    per task, the length distribution, and — for runs dispatched since task-233 — how
    much of each run was the gate and how much of that was gate runs that failed.

    ```bash
    poetry run python scripts/run_report.py --per-task     # every task, worst first
    poetry run python scripts/run_report.py --since 7      # the last week
    poetry run python scripts/run_report.py --task task-233
    ```

    The gate reports itself: `scripts/check.py` appends a `gate_started` and a
    `gate_finished` record to `phases.jsonl` in the run directory whenever it runs inside
    a dispatched run, and writes nothing at all when it does not. Dispatch puts
    `AGENTJOBS_RUN_ID` and `AGENTJOBS_RUN_DIR` in the session's environment, so anything
    downstream of the agent inherits them and can add a phase with
    `agentjobs.dispatch.phases.record_phase_from_env`.

    It also reads `~/.agentjobs/finishes/`, where a **scripted finish** (task-241)
    writes itself down. A finish is not a run — no agent, no session, no tokens — and it
    exists to remove the follow-on run this report was built to measure, so it is
    counted in its own block rather than folded in. Without that the saving would show
    up only as runs-per-task falling, with nothing to attribute it to.

    **Do not measure a run by grepping `transcript.log`.** It is a raw TTY capture, so a
    line appears in it as many times as the terminal repainted it and every count derived
    from it is an artefact of that. Task-233 is the incident; phase records exist so the
    question does not have to be asked that way again.
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

#### Steps 3 to 6 may already have happened before you read them

**On a machine with `finish.enabled` set for this project, clicking Approve runs steps
3 to 6 itself, with no agent anywhere in it** (task-241). It rebases, runs the full gate
in your worktree with your worktree's interpreter, merges `--no-ff`, rebuilds the
frontend if the merge touched it, restarts the server the way this machine's config says
it was started, proves the running process is serving the merge, closes the task and
removes your worktree. It takes about as long as the gate does.

Nothing above is relaxed by that: **a person still approves, per task, before anything
merges**, and the merge is still a `--no-ff` merge commit.

What changes for you is only what to do when you are woken after an approval. Read the
task record first. It says, unambiguously, whether `main` moved:

- **"The merge is done: `abc1234`"** — the merge is in and the *delivery* is not. The
  task is deliberately still open, and the prompt names what remains: the bundle was not
  rebuilt, or the server did not come back, or it came back on the old code. Finish that
  and close the task. Do not re-merge.
- **"Nothing was merged"** — the rebase conflicted or the gate went red. The escalation
  says which, and for a conflict it states whether the branch was restored to the commit
  it was on, having read it back and compared. Nothing was forced and nothing was
  guessed at.

You can run the same thing by hand, which is how a finish that escalated is retried once
its cause is fixed. It knows its own earlier merge and picks up from it rather than
refusing:

```bash
poetry run agentjobs finish task-241 --project agentjobs
```

Exit 0 means merged, closed and verified; 1 means it stopped and the task says where; 2
means the task was never a candidate and nothing happened. It declines rather than
guessing whenever the answer is a judgement — no active branch, two of them, a clone
with something else checked out, a missing or dirty worktree, or a branch somebody
already merged by hand.

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
-   **Do not set up the state your test is meant to be checking.** task-207 added a
    keyboard reorder to the task list and covered it in jsdom and in Playwright, both
    green. Both focused the row's handle before every keypress -- and the defect was that
    focus did not survive a keypress, because React reorders rows by moving their DOM
    nodes and a browser drops focus from a node it reinserts. So the feature worked
    exactly once per click, and the two tests written to prove it worked were the reason
    nobody could see that. Found by pressing the key twice in a browser.
-   **A Playwright keypress is not a person's keypress.** `page.keyboard.press` goes in
    through CDP, below the browser's own shortcut handling and below anything a real
    window does with focus. It is the right tool for driving a page and the wrong
    evidence for "this key combination works" -- for that, press it yourself.
-   **"I did it and nothing happened" is not a defect until you have proved the gesture
    reached the page.** It has two explanations -- the feature is broken, or your input
    never arrived -- and a browser-automation tool reports success either way. task-225
    is the incident: `mcp__claude-in-chrome`'s `left_click_drag` delivered **no events at
    all** to the task list, not even `mousedown`, with correct coordinates and a
    successful-looking tool result. A session read that as "drag-to-reorder does not
    work", filed it as reproduced fact, and an afternoon went into fixing something that
    was never broken. A hand on a real mouse moved the row first try.

    The check is one line, and it costs nothing next to what skipping it costs:

    ```js
    document.addEventListener("mousedown", (e) => console.log("got", e.target.id), true);
    ```

    Empty means the harness, not the application. Never write *reproduced* into a task
    record on the strength of an automated gesture alone -- name the instrument, and say
    whether the gesture landed. The same caution runs the other way: Playwright drives
    Chromium's drag through `Input.setInterceptDrags` rather than the operating system's
    drag loop, so it is good evidence that the handlers, the client call and the route
    work, and it is not a hand on a mouse. When the question is whether a *gesture*
    works, a person has to make it -- stand up a sandbox and ask, and have the page
    record what happened rather than expecting them to screenshot a panel before their
    next click clears it, which is how the first attempt at this lost its evidence.
    `scripts/review_queue_sandbox.py` is that sandbox and it keeps its traces now.
