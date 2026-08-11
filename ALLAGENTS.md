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
2.  **Worktree, branch, then claim**: `git worktree add ../aj-<nnn> -b <type>/task-<nnn>-<slug>`
    and work there — **this is your first act, before anything is written.** Then `claim`
    the task and record the branch in `branches[]`. In that order, so no work is ever
    committed outside a branch. See [Why you get your own worktree](#why-you-get-your-own-worktree).
    **Your task-record commits go to `main`, not to your branch** — see
    [Task files live on main](ENGINEERING.md#task-files-live-on-main-always). Your branch
    carries code; it never touches `tasks/`.
3.  **Work**: Small, single-logical-change commits with tests green before each one.
    Stage explicit paths — never `git add -A`.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works.
5.  **Hand off**: `handoff` to `human`/`review` with a `ball_prompt` saying what was done
    and what needs review, and **commit that to `main`** — a handoff sitting on your
    branch is invisible in the dashboard, so the human you are handing to will never see
    it. **Stop there** — do not merge.
6.  **On approval**: Rebase onto `main`, merge `--no-ff`, mark the branch `merged`,
    `close` the task with `outcome: completed`, and `git worktree remove` your worktree.

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

### Why you get your own worktree

**You are not the only agent in this repository, and you cannot see the others.** Several
run against one clone. A clone has one working tree and one `HEAD`, so `git checkout`
replaces the files under whichever peer is mid-task — you will not get an error, and
neither will they.

A human working alone does not need this; they have no peer to collide with. You do.

-   Create the worktree **before** the branch, the claim, or anything written to disk.
-   Name it for the task, beside the clone rather than inside it: `../aj-045`.
-   `git worktree remove` it once the branch is merged. `git worktree list` is the
    inventory, and a worktree for a closed task is litter.
-   **Never `git checkout` in the shared clone** to start work.
-   Committing task metadata straight to `main` (the narrow exception in ENGINEERING.md)
    does not need one. Anything that goes on a branch does.
-   Your CLI may do this for you — Claude Code takes `--worktree`.

Three failures on 2026-08-11, all in one afternoon, all from skipping this:

1.  An agent ran `git add -A` and committed a peer's uncommitted, in-flight files.
    Recovered only because it was noticed within a minute. **Recovery, if it happens to
    you:** `git reset --soft HEAD~1`, then `git restore --staged` their paths. Never
    `git checkout --` them — that destroys work you did not write.
2.  An agent checked out its own branch and replaced the tree under a peer mid-task. The
    peer's next commit would have gone to the wrong branch.
3.  An agent finished and left the clone on `main`. The owner opened the dashboard, did
    not see a task waiting for review, and reasonably concluded the product was broken.
    It was not: **tasks are YAML files here, so the checked-out branch decides what the
    GUI shows.** Before reporting that a task is missing, check what is checked out.

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
