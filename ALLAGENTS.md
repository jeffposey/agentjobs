# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Parent Task Loop

When asked to work or drive a parent task, treat the parent and its descendants as the
durable execution plan. The kickoff prompt should normally be no more than "work
task-NNN"; do not require it to repeat specifications, child order, verification, or
handoff rules already stored in task records and these process files.

1.  Read the parent completely, then inspect its open descendants and their
    `dependencies[]`, logs, decisions, acceptance criteria, and current ball state.
2.  Work exactly one eligible child at a time. Dependencies determine eligibility and
    ordering. If a required order is not represented durably, record it as task
    dependencies or a task decision instead of relying on chat or a long launcher.
3.  Follow the normal task lifecycle below. When a child reaches human review, hand it
    off and stop. Never merge without explicit approval for that child.
4.  Approval releases that checkpoint; it does not end the parent loop. Preserve the
    recorded approval, merge and close the child, clean up its worktree, then continue
    automatically with the next eligible child.
5.  When no unfinished child remains, evaluate the parent's acceptance criteria against
    durable child evidence, perform any parent-level verification, and close the parent
    when supported.
6.  Stop only for a required review/approval gate, a genuine human decision or external
    blocker, a clean usage boundary, or completion of the parent.

The task graph defines scope. Do not absorb unrelated follow-ups merely because they
were mentioned during the loop; create or update a separate durable task only when the
user authorizes it.

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
    Then **bootstrap it** — a worktree has no virtualenv and no `node_modules`, so it
    cannot verify anything until you do:

    ```bash
    python scripts/bootstrap.py     # ~30s; see Bootstrapping a worktree
    ```

    **Your task-record commits go to `main`, not to your branch** — see
    [Task files live on main](ENGINEERING.md#task-files-live-on-main-always). Your branch
    carries code; it never touches `tasks/`.
3.  **Work**: Small, single-logical-change commits with tests green before each one.
    Stage explicit paths — never `git add -A`.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works.
5.  **Hand off**: `handoff` to `human`/`review` with a `ball_prompt` saying what was done
    and what needs review, and **commit that to `main`** — a handoff sitting on your
    branch is invisible in the React app, so the human you are handing to will never see
    it. **Stop there** — do not merge.
6.  **On approval**: Rebase onto `main`, merge `--no-ff`, mark the branch `merged`,
    `close` the task with `outcome: completed`, and `git worktree remove` your worktree.

### The Resumption Contract

A task must be sufficient working memory for a new agent session with no access to the
chat that created it or to the session that last worked it.

-   `spec.summary` is one or two sentences that orient a zero-context reader. It is
    distinct from `spec.description`, which is the detailed working specification; do
    not make the summary a clipped first line or force a reader to parse the description
    merely to learn what the task is.
-   `ball_prompt` is the current holder's concrete ask. Keep it current; the spec says
    what the task is, while the prompt says what must happen next.
-   The newest handoff, every binding `decision`, every unanswered `question`, progress
    and verification evidence, branches, dependencies, acceptance criteria, and
    deliverables must let the next session reconstruct what is done and what remains.
-   Before ending a session, move any fact needed for resumption out of chat and into
    the task log. If a fresh reader would still need the transcript, the handoff is not
    complete.

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
3.  An agent finished and left the clone on `main`. The owner opened the React app, did
    not see a task waiting for review, and reasonably concluded the product was broken.
    It was not: **tasks are YAML files here, so the checked-out branch decides what the
    React UI shows.** Before reporting that a task is missing, check what is checked out.

### Bootstrapping a worktree

`git worktree add` copies tracked files. The Poetry virtualenv and
`frontend/node_modules` are not tracked, so a new worktree cannot run
`scripts/check.py` — the gate you are required to pass before every commit. One command
fixes that, from inside the worktree:

```bash
python scripts/bootstrap.py
```

It runs `poetry install`, `npm ci`, and `playwright install chromium`, then confirms the
environment imports the worktree's own `src/`. **30 seconds** in a brand-new worktree,
**13 seconds** to re-run in one that already has both — measured 2026-08-19, 21s of the
first figure being Poetry. That is not a reason to skip the worktree. It is longer on a
machine whose Poetry and npm caches are cold, because those caches fill on the way past.

**Do not borrow the main clone's virtualenv instead.** `poetry install` puts the *main
clone's* `src/` on that environment's path, so `pytest` run from your worktree against it
imports the code on `main`, not the code on your branch, and reports a green suite for
source it never executed. Nothing in the test output reveals this. `check.py` refuses to
run when the import resolves outside its own checkout, for exactly that reason.

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

-   At a human decision or review point, record the progress and evidence, then use the
    handoff API with `ball: human`, the precise reason, and a self-contained
    `ball_prompt`. Notify through the interactive channel available today (chat and,
    when available, push notification), but never put information only in the alert.
-   Durable notification delivery is future work. Schema v2's HMAC-signed
    `task.handoff` webhook is the extension point for a pluggable notification service;
    it replaces the v1 `task.status_changed` event for this purpose. Do not build or
    assume such a receiver as part of an ordinary handoff.

## Reporting Standards
-   **Conciseness**: Be brief. Use bullet points.
-   **Evidence**: Link to artifacts, screenshots, or log files that prove success.
-   **Context**: When reporting errors, include the full stack trace and the command that caused it.

## Behavioral Guidelines
-   **Ask First**: If requirements are ambiguous, ask the user for clarification.
-   **Non-Destructive**: Do not delete files or wipe databases unless explicitly instructed.
-   **Self-Correction**: If a tool fails, analyze the error message, fix the input, and retry. Do not loop endlessly.
-   **Transparency**: Explain *why* you are making a change, not just *what* you are changing.
