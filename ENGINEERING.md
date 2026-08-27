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
    cost. Costs below are from one green `poetry run python scripts/check.py` on this
    machine, 2026-08-21, nothing else competing for it; the history behind them is in
    [docs/performance.md](docs/performance.md#what-the-gate-costs):

    | # | Stage | What it checks | Cost |
    |---|---|---|---|
    | 1 | `black` | Python formatting | 0.6s |
    | 2 | `ruff` | Python lint | 0.1s |
    | 3 | `mypy` | Python types | 1.5s |
    | 4 | `api` | `openapi.json` and the generated client both match the app | 4.2s |
    | 5 | `icons` | the committed PWA icons match `assets/app-icon.svg` | 2.8s |
    | 6 | `oxlint` | frontend lint | 0.6s |
    | 7 | `pytest` | the Python suite, across every core | 52.1s |
    | 8 | `vitest` | the jsdom component tests | 5.2s |
    | 9 | `build` | `tsc --noEmit` and the production bundle | 3.7s |
    | 10 | `e2e` | the Playwright suite against a live server | 25.0s |
    | | | | **95.8s** |

    MyPy is the one stage whose cost moves: under two seconds against a warm cache, about
    nineteen seconds on the first run after a checkout.

    ```bash
    poetry run python scripts/check.py --coverage   # the gate, plus coverage and htmlcov/
    poetry run python scripts/check.py --serial     # one process, for readable output
    ```

    **Quote a command and a date, never a bare count.** A suite grows every week and a
    number in prose does not; three totals for this one sat in this file at once and two
    were wrong the day they were written. The gate prints what it ran; ask it, or ask
    `pytest --collect-only -q`.

    Use focused pytest or npm commands while iterating, but do not substitute them for
    the gate. A hand-run `pytest` is serial and measures no coverage: `addopts` is empty,
    and `-n auto` is passed by the gate rather than configured globally, because xdist
    costs more than it saves on a small selection and its interleaved output is the wrong
    trade when you are reading one failure.
-   **The cheapest stage runs first, whatever the slowest one currently costs.**
    Format, lint and types catch what pytest never will, and there is no reason to wait
    on the test suite to be told about a misformatted file. The ordering costs nothing,
    and the gap it exploits reappears the moment a slow stage is added (task-189,
    task-233).
-   The checks are *in* the gate rather than only in the pre-commit list below because a
    list nothing enforces is a statement of intent. Task-166 is the incident: a `mypy`
    that aborted before checking a file, and a Black drift on `main`, both long-lived.
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
        unqualified run on a clean tree records the commit it verified; `--since-gate`
        diffs the working tree against that. With no receipt it narrows nothing and runs
        every stage, saying so.
    2.  **The classification table is default-deny.** Only task records under `tasks/`
        and prose map to a reduced set; everything else, including anything nobody has
        classified yet, selects all ten. An incomplete table costs time, never coverage.
        It lives in `scripts/gate_scope.py`, and each entry has to name what reads those
        paths.
    3.  **A stage whose inputs are not bounded by the diff still runs.** "It was only a
        task file" is not a safe skip: `tests/test_validate.py::TestRealCorpus` loads this
        repository's own records, so a task YAML genuinely can turn the suite red — which
        is why `tasks/` maps to `pytest` rather than to nothing.
    4.  **The output is the claim, in full.** A reduced run prints `NECESSITY RUN`, the
        commit it diffed against, every changed path with the rule that matched it, and
        every stage it skipped. It never prints "Ran every stage". An unchanged tree
        prints `NOTHING CHANGED` and runs nothing.

    A green `--since-gate` on a clean tree issues its own receipt, naming the receipt it
    derived from, so a chain of them is auditable. `--only` and `--from` never issue one —
    a partial green is not the gate's green, which is the same rule `PARTIAL RUN` states.
-   **The gate runs before the commit, so no stage of it may require one.** The two
    generated checks — `openapi.json` and `src/api/generated/` — compare against **the
    working tree**, never `HEAD`: they ask whether the files on disk match what the
    application produces (task-189). So: **regenerate, run the gate, then commit.** The
    `api` stage names `frontend/src/api/generated` when those files are uncommitted, and
    does not fail — `git add` takes explicit paths here, and generated output is what
    that habit forgets.
-   Budget **about a minute and a half when you have the machine to yourself**, and
    **about two and a half when you do not** — several agents work this repository at
    once and this machine allows three dispatched runs, so overlapping gates are the
    normal case. Under three-way contention a gate has been measured at around six
    minutes. Degradation here has always been gradual with no cliff, so **a gate taking
    longer than you expected is working, not stuck**; the figures and how they were
    obtained are in [docs/performance.md](docs/performance.md#what-the-gate-costs).

    Concurrent gates are only safe at all because each checkout derives its own
    Playwright and benchmark ports from its own path (task-187); if you see a port
    collision, that is a bug and not a reason to serialise.
-   Ensure high test coverage for core logic (`manager.py`, `storage.py`).

### Measuring performance
-   Two tools, both documented in [docs/performance.md](docs/performance.md):
    `scripts/bench.py` times the API, the CLI and the browser's open-a-task interaction;
    `scripts/run_report.py` reads the run ledger and says where dispatched agent time
    goes, including how much of each run was the gate.
-   **A change that claims to be faster states a before/after pair from one of them**,
    and **a cycle-time claim is a before/after or it is an anecdote**. Prefer asserting
    on task files parsed rather than on wall-clock time: the parse count means the same
    thing on every machine, and a timing threshold does not. Every API response carries
    `X-Response-Time-Ms` and `X-Task-Parses`, so a slow request can be attributed
    without a profiler.
-   **Do not measure a run by grepping `transcript.log`.** It is a raw TTY capture, so a
    line appears in it as many times as the terminal repainted it and every count derived
    from it is an artefact of that. Task-233 is the incident; the phase records
    `scripts/check.py` writes exist so the question does not have to be asked that way
    again.

### Measuring this file

Every session loads `CLAUDE.md` and the three files it imports before its first thought,
and **the bundle is capped**: `tests/test_context_budget.py` fails when the four files
together exceed the budget task-301 measured. Cutting is therefore the normal way to make
room, and there are two instruments for doing it safely.

`scripts/context_eval.py` measures which of those words change what an agent *does*: it
runs a scenario twice, once against a copy of the bundle and once with one section cut
out, and scores each run on its action trace.

```bash
poetry run python scripts/context_eval.py --dry-run    # validate the ablations, run nothing
poetry run python scripts/context_eval.py --tag quick  # the per-release subset
```

**Run it on a new model release, and before cutting anything from these files.** A rule
that is load-bearing for one generation may be redundant with the next model's defaults,
which is why a verdict here carries a model id and a date. `evals/context/README.md` has
the scenarios, the scoring vocabulary and the limits; `evals/context/baselines/` holds one
committed report per model.

It is deliberately **not** in `scripts/check.py`: it costs tens of minutes and real money,
and a gate stage like that gets disabled within a week. The cheap half is in the gate --
`tests/test_context_eval.py` re-renders every ablation against the current bundle and fails
when a case's target heading has been renamed or its rule restated somewhere the ablation
does not reach, which is how the suite would otherwise rot.

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
-   Once it merges, `git worktree remove` the worktree and **then** `git branch -d` the
    branch. That order, because a branch checked out in a worktree cannot be deleted; and
    `-d` rather than `-D`, because `-d` refuses a branch `main` does not contain and a
    refusal means the merge did not land the way you think it did. The scripted finish
    does both (task-293) — do it by hand only when you merged by hand.
-   `agentjobs branches` says what was left behind: branches `main` already contains with
    no worktree, and the ones still in flight with how long each has really been open. It
    reports and deletes nothing, because several agents work this clone and a branch that
    looks abandoned from outside may be somebody's live work.

### How long a branch should live

**Short — and the lever is not the one people reach for.** Every rebase conflict in the
task-211 epic came from a branch living across another branch's merge; running the work
in parallel caused none of them. **Branch lifetime is not task size**: task-217's branch
was open nine hours and contained one hour of work, the rest being wait. So attack wait:

-   **Rebase onto `main` before handing off for review**, not only when the merge gate
    refuses. A branch open for hours is very likely behind, and rebasing at handoff moves
    the conflict to a moment when a session is already in context, where it costs minutes.
    Left until the merge, the same conflict stops a scripted finish and costs a cycle.
-   **Do not leave a ready branch sitting.** The half you control is handing off with a
    complete review request the *first* time, so the answer does not need a round trip to
    ask a question you could have answered.

**Bigger tasks are fine, and often better.** Every task boundary pays for a worktree, a
bootstrap, a full gate, a review round, a merge and a cleanup. The ceiling on a task is
not a size but the point where one session's context can no longer hold the work, or
where the acceptance criteria stop being independently verifiable. Below that, prefer
more per task rather than less.

**The parent-task rule is a session boundary, not a size limit**, and the two are
routinely confused. [ALLAGENTS.md](ALLAGENTS.md#you-do-not-work-the-children) says
anything taking a worktree gets its own session; that decides whose context carries the
work, and says nothing about how much work one task should contain.

### Sharing a clone

Working alone in your own clone, `git checkout -b` is fine and nothing below applies.

It stops being fine the moment something else is working the same clone — a second
person, or an agent. A clone has one working tree and one `HEAD`, so a checkout replaces
the files under whoever else is in there. **When you are not alone in a clone, take a
worktree instead of checking out:**

```bash
git worktree add ../worktrees/agentjobs-045 -b feat/task-045-subtask-support
cd ../worktrees/agentjobs-045 && python scripts/bootstrap.py   # ~30s; no venv or node_modules yet
git worktree remove ../worktrees/agentjobs-045      # after the branch merges
```

**They go in a `worktrees/` directory beside the clone**, not loose beside it and not
inside it; `git worktree add` creates that directory the first time.

The bootstrap is not optional politeness: a worktree that skips it cannot run
`scripts/check.py` at all, and borrowing the main clone's virtualenv to get around that
runs your tests against the main clone's source. See
[Bootstrapping a worktree](ALLAGENTS.md#bootstrapping-a-worktree).

Agents in this repository are required to do this — see
[ALLAGENTS.md](ALLAGENTS.md#task-lifecycle) — because several of them routinely run
against one clone and none of them can see the others.

One consequence looks like a bug and is not: **tasks are YAML files in this repository,
so whichever branch is checked out decides what the dashboard shows.** If the React app
is missing something you expect, check what is checked out before filing anything.

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
Work does not merge itself **unless the run's posture releases it**, and only one posture
does. Read [Posture decides this, not you](#posture-decides-this-not-you) below before
concluding either half applies to you; if you are a person, or a run whose posture was
not named there, the rule is unqualified and the release does not exist for you.

**This numbered list is the only one.** ALLAGENTS.md's task lifecycle used to restate
steps 3 to 6 under its own numbers 6 and 7, so "step 6" named two different actions
depending on which file the reader had open and each file's follow-on section then cited
its own numbering. Its lifecycle now stops at the approval and points here. Anything that
cites a step of the merge gate — prose, a docstring, a test — cites this numbering.

When a branch is complete and verified:

1.  **Stop.** Use the handoff API to set `ball: human` / `ball_reason: review`, with a
    `ball_prompt` and handoff log entry stating what was done and what needs review.
    Notify the human through whatever interactive channel is available (the chat reply
    and, when the host provides it, a push notification). The notification is only a
    wake-up signal; the task record must contain the complete review request.
2.  Wait for **explicit** human approval. Absence of objection is not approval, and
    neither is your own confidence that the work is good.
3.  On approval: rebase onto `main`, then merge with `--no-ff` (the merge commit is the
    reviewable unit of work, so fast-forward is not acceptable). A scripted finish holds
    this repository's **merge runway** across rebase, gate and merge, so a second one
    queues rather than gating against a base the first is moving; a finish that says it
    is queued is working (task-223).
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

#### Posture decides this, not you

**A dispatched run's posture carries two things** (task-021): what the process may
execute, and whether the run stops at the gate above. The second is derived from the
first — there is no separate switch.

| Posture | Executes | Merge | Push |
|---|---|---|---|
| `read_only` | no shell at all | no branch to merge | n/a |
| `auto` **(default)** | classifier-gated | **stop, hand off, wait for approval** | per project |
| `supervised` | allow-list, parks otherwise | **stop, hand off, wait for approval** | per project |
| `autonomous` | `bypassPermissions` | merges its own work | per project |

`auto` and `supervised` are deliberately identical here. They differ in how the process
is gated *while it runs*, which is a different question from who authorises the merge.

**An autonomous run does not run `git merge`.** It records its evidence on the task and
then runs `agentjobs finish <task> --project <id> --posture-release`, which routes it
through exactly the sequence a human approval takes and stops at the first step it cannot
complete. The posture is re-checked there, in code: `--posture-release` on a project
configured `auto` declines and touches nothing.

**The gate is the point.** "No serious issue detected by the agent" is the agent grading
its own homework and is the least reliable authority available, so it is not the one that
merges. `scripts/check.py` exit 0 is, and the finisher runs it rather than taking the
agent's word. Both have to hold: an agent that finds a serious problem stops and hands
off however green the gate was, and a green agent with a red gate merges nothing.

**Push is not a posture property.** It is per project, it defaults to `false`, and it is
`false` here. Nothing in AgentJobs runs `git push`. **That is the whole of the safety
argument**: a bad autonomous merge is caught by whoever next reads `main` and reverted,
because nothing left this machine. A project that both releases the merge gate and
permits pushing has given up that recovery, and should want a much stronger reason than
this one.

**An epic multiplies this, and the multiplication is the point** (task-022). Dispatching
a parent at `autonomous` and running `agentjobs dispatch walk` merges *every* child in
turn, unattended, on one click — each through `agentjobs finish --posture-release`, so
each merge still has a green unqualified gate on the exact commit under it, and the walk
stops outright on the first child that is not clean. Read
[the epic walk](docs/agent-dispatch-design.md#the-epic-walk-one-human-act-many-runs-task-022-2026-08-23)
before raising a project's posture: chains of unreviewed merges are recoverable only for
as long as nothing is pushed.

#### Steps 3 to 6 may already have happened before you read them

**Where a project has `finish.enabled`, clicking Approve runs steps 3 to 6 itself, with
no agent anywhere in it** (task-241) — rebase, the full gate in the branch's worktree,
`--no-ff` merge, rebuild, restart, verify, close, remove the worktree, delete the branch.
It relaxes nothing about who authorises a merge: a person still approves, per task.
`agentjobs dispatch config --project <id>` reports `finish=on` or `finish=off`, and
`agentjobs finish <task> --project <id>` is the same code by hand — which is how a finish
that stopped is retried once its cause is fixed.

**If you are woken after an approval, the record says whether `main` moved. Believe it**
— the finish writes either "The merge is done: `<sha>`" (deliver and close; do not
re-merge) or "Nothing was merged" (the rebase conflicted or the gate went red, and the
entry says which). The exit codes, the conditions it declines on rather than guessing,
and what it records are in
[the dispatch design](docs/agent-dispatch-design.md#5a-what-ends-a-dispatch-the-scripted-finish-task-241-shipped).

Pushing to the remote is a separate act from merging; do not assume approval to merge
carries approval to push.

### Task files live on `main`, always

**Everything under `tasks/` is committed directly to `main`, never to a feature branch.**
Creating a task, grooming the backlog, claiming, logging progress, handing off, closing —
all of it, using a `chore(tasks):` or `chore(task-nnn):` commit. A feature branch carries
code and docs. It does not touch `tasks/`.

**The dashboard reads one working tree.** A handoff committed to a branch is invisible
to the person it is addressed to — they open the React app, see the task still `ready`, and
conclude nothing is waiting for them. The merge gate depends on a human seeing a review
request, so recording that request somewhere the human cannot see it defeats the gate
entirely, and worktrees make it airtight: the shared clone is then *never* on the review
branch. Observed 2026-08-11, repeatedly, before the cause was understood. It also removes
task-file merge conflicts as a category, since neither branch contains a task record.

Practically: write task updates through the API or the manager, which resolve the project
root from the registry and therefore land in the `main` clone's working tree even when
your own work is happening in a worktree. Then commit them there:

```bash
git -C <path-to-main-clone> add tasks/agentjobs/task-045-*.yaml
git -C <path-to-main-clone> commit -m "chore(task-045): hand off for review"
```

The cost, stated so nobody rediscovers it as a bug: a task's record and the code it
describes are no longer one atomic commit, and checking out an old revision will not show
you the task state as it was then. `main`'s history has it.

## Safety Rails
-   **Never** delete user data without explicit confirmation.
-   **Always** use the `TaskStorage` abstraction; avoid direct file I/O on task files where possible.
-   **Verify** local server startup and the React `/app/` route (`poetry run agentjobs
    open`) after modifying API routes or frontend serving.
-   **A server that refuses to start because it "imported its own source from the wrong
    checkout" is telling the truth — do not work around it.** That virtualenv has an
    editable install pointing at a different checkout, so the process would read the right
    task files and run a different branch's code, and nothing else shows it: `git log` in
    the served clone is correct and so are the files on disk. The repair is printed in the
    error — `poetry install` from the clone that should be running, then a restart.
    `AGENTJOBS_SKIP_SOURCE_CHECK` exists for an unusual install layout, not for getting
    past this. Task-194 is the incident; `/api/version` reports `source_root`.
-   **Rebuild the frontend after merging front-end work, then restart.**
    `src/agentjobs/frontend_dist/` is gitignored, so merging a React change to `main`
    does **not** update the bundle a running server serves; the clone that serves the app
    needs `npm run build` in `frontend/` as well. Observed 2026-08-17: a merged fix
    appeared to have done nothing, because the browser was still being handed the
    pre-merge bundle.
-   **Restart the server after changing models, storage, or task files.** A running
    `agentjobs serve` holds the imported code in memory, so when task files change
    underneath it — a schema migration, a checkout, a bulk edit — it reads new data with
    old code and every file appears corrupt. Dozens of validation errors naming fields
    that no longer exist is what a stale server looks like: the application is fine, the
    process is old. `agentjobs restart` before concluding anything is broken, and never
    leave a stale server running for someone else to find.

## Verification
-   A passing suite is not evidence a feature works. Exercise the change the way a user
    would, against a freshly started server.
-   **Assert on rendered values, not on the presence of markup.** Checking that a page
    contains `data-ball=` passes while it emits `data-ball="Ball.HUMAN"` and every
    filter silently matches nothing. Assert the value a user's browser will act on.
-   When a check passes, ask what it would have caught. If the answer is "nothing that
    has ever gone wrong here", it is decoration.
-   **Do not set up the state your test is meant to be checking.** task-207's keyboard
    reorder was green in jsdom and in Playwright because both focused the row's handle
    before every keypress — and the defect was that focus did not survive a keypress. The
    two tests written to prove it worked were the reason nobody could see it did not.
-   **An automated gesture is not a person's gesture, and "I did it and nothing happened"
    is not a defect until you have proved the gesture reached the page.** A
    browser-automation tool reports success whether or not any event arrived: task-225
    lost an afternoon to `mcp__claude-in-chrome`'s `left_click_drag` delivering **no
    events at all**, with correct coordinates and a successful-looking result. Playwright
    is subtler — `page.keyboard.press` goes in below the browser's own shortcut handling,
    and its drag goes through `Input.setInterceptDrags` rather than the OS drag loop — so
    it is good evidence that handlers, client call and route work, and no evidence that a
    *gesture* works. Prove the input landed before filing anything:

    ```js
    document.addEventListener("mousedown", (e) => console.log("got", e.target.id), true);
    ```

    Empty means the harness, not the application. **Never write *reproduced* into a task
    record on the strength of an automated gesture alone** — name the instrument, and say
    whether the gesture landed. When the question really is whether a gesture works, a
    person has to make it: stand up a sandbox that records what happened rather than
    asking them to screenshot it (`scripts/review_queue_sandbox.py`).
