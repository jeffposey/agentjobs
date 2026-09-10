# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Work what the queue says is next

The backlog has a stored order, not a sort over timestamps. `agentjobs next` — or
`task_next` over MCP — is the answer, and `--why` explains it. Read that before
concluding the order is wrong; a task missing from the answer is usually blocked,
claimed, or holding open children rather than mis-placed.

**If you think something else should be first, move it** — `agentjobs queue move
task-045 --top`, or `task_queue_move` over MCP — so the next session inherits the
decision instead of re-deriving it. **Read what the move answers back**; a clean exit
code does not mean a useful reorder.

Three things not to do instead, each of which has a real cost:

- **Do not add a `needs` dependency to make one task come before another.** Dependencies
  are prerequisites. A false one makes the task unclaimable until the other closes,
  deadlocks the graph if it ever points both ways, and lies to every reader who takes it
  at face value.
- **Do not hand-edit `queue_position`.** The number is a consequence of a decision, and
  the decision is what the record should show. A hand-written number can also collide
  with another open task in the band, which is corruption.
- **Do not rely on an instruction given in chat to reorder work.** Chat does not survive
  the session. The queue does, and it is what the next agent will read.

If a tool reports `queue_broken` — or `agentjobs queue check` exits non-zero — the order
itself is in doubt, so picking a task by hand is the one response that cannot be right;
`agentjobs queue repair` states everything it guessed.

The full mechanics — the client and MCP forms, what `--why` returns, what a move answers
back, and which exception each caller sees on a broken queue — are in
[the workflow guide](docs/agent-workflow.md#work-what-the-queue-says-is-next).

### Parent Task Loop

When asked to work or drive a parent task, treat the parent and its descendants as the
durable execution plan. The kickoff prompt should normally be no more than "work
task-NNN"; do not require it to repeat specifications, child order, verification, or
handoff rules already stored in task records and these process files.

1.  Read the parent completely, then inspect its open descendants and their
    `dependencies[]`, logs, decisions, acceptance criteria, and current ball state.
2.  **Run the walk, and let it finish** (task-022):

    ```bash
    poetry run agentjobs dispatch walk <parent-id> --project <project>
    ```

    It starts **every** child whose dependencies are satisfied as a real dispatch on the
    authorisation the human gave *this* parent, up to this machine's
    `limits.max_concurrent_runs`, and starts each newly-freed child the moment its own
    needs close rather than at the end of a round. It watches each child's **task
    record** to a terminal state. The first child that does not close `completed`
    **grounds every further takeoff** — a sibling that depended on it would be building
    on a gap — while children already in the air are watched down rather than killed,
    none of them being able to have depended on it. **Takeoff and landing are different
    resources**: the work parallelises, the merge does not, so children queue for the
    repository's one merge runway inside their own finish. Retries are bounded at two
    runs per child per authorisation of the epic, and enforced rather than promised. It
    **blocks until it is done**, which is the point: a supervisor that ends its turn
    promising to check back is asleep. `--dry-run` says what would start now and starts
    nothing.
3.  Exit 0 means no open child remains. **That is not the same as the parent being
    done**, and the walk deliberately never closes it: evaluate the parent's own
    acceptance criteria against the children's durable evidence, do any parent-level
    verification, and close it only where that evidence supports it. Exit 1 means the
    walk stopped for cause; the parent's record says which child and why.
4.  Stop only for a required review/approval gate, a genuine human decision or external
    blocker, a clean usage boundary, or completion of the parent.

The task graph defines scope. Do not absorb unrelated follow-ups merely because they
were mentioned during the loop; create or update a separate durable task only when the
user authorizes it.

### You do not work the children

**Whoever holds a parent task starts a separate session per child and stays running as
the supervisor.** This binds whether you are an interactive session someone told to
"work task-160" or a dispatched run.

**The threshold is: anything that takes a worktree gets its own session.** A child that
edits files, runs `scripts/check.py`, or produces a branch is a session. A child that is
a decision to record, a question to answer or a task to file is not — it takes no
worktree, and a session for it costs more than it saves. **The reason is context, and it
decides nothing about ordering**: a session that works four children carries four
children's worth of exploration by the fourth, and the transcript a handoff should have
replaced is precisely what the next session cannot read. Three concurrent child sessions
leave your context exactly as small as three sequential ones — which is why the walk runs
independent children together (task-223).

**The supervisor is thin, and meant to be.** You read the child's record, its acceptance
statuses, its branch and its diff — not its transcript. You are checking that the child
reported and verified its work, not re-verifying it.

Three things do not change because a child is a session:

- Its **task record is written the same way yours is** — see
  [Where task records live](ENGINEERING.md#where-task-records-live).
- Its **merge gate stands or falls on the child's own terms**: a child merges on an
  explicit human approval of *that child*, or — at posture `autonomous` — on its own
  green gate. Never on yours. A supervisor approves nothing under either policy.
- A fresh worktree has no virtualenv and no `node_modules`, so a child **cannot run
  `scripts/check.py` until it bootstraps** — `python scripts/bootstrap.py`, about 30
  seconds, see [Bootstrapping a worktree](#bootstrapping-a-worktree). A child that skips
  it either cannot verify its own work or borrows the main clone's environment and tests
  the wrong source.

The full protocol — how to start a child, the bound and what spends it, and what to do
when one finishes, parks, dies, or leaves you waiting — is in
[the workflow guide](docs/agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them).
Two rules from it are worth repeating here because both have already been got wrong:
**watching is a mechanism, not an intention**, and **the signal is the task record, not
the process**, because a child parked on review has a live process and is the one state
that needs you.

### Task Lifecycle

This is the lifecycle for the task you are *working*. If your task has an open child you
are not working it — you are supervising, you take no worktree, and
[the section above](#you-do-not-work-the-children) is your lifecycle instead.

1.  **Read**: Read the task record (`task_get` over MCP, or `agentjobs show`) — its `spec`
    (`summary` → `intent` → `description` → `constraints` → `out_of_scope` → `context`)
    is the specification, `ball_prompt` is what is needed *right now*, and `acceptance[]`
    is what "done" means. Read the `log[]` newest-first: the last `handoff`, and every
    `decision` and open `question` since. **Decisions are binding — do not relitigate
    them.** Check `dependencies[]` and confirm they are satisfied before starting.
2.  **Worktree, branch, then claim**: `git worktree add ../worktrees/agentjobs-<nnn> -b <type>/task-<nnn>-<slug>`
    and work there — **this is your first act, before anything is written.** Then `claim`
    the task and record the branch in `branches[]`. In that order, so no work is ever
    committed outside a branch. See [Why you get your own worktree](#why-you-get-your-own-worktree).
    Then **bootstrap it** — a worktree has no virtualenv and no `node_modules`, so it
    cannot verify anything until you do:

    ```bash
    python scripts/bootstrap.py     # ~30s; see Bootstrapping a worktree
    poetry run agentjobs run register --task task-<nnn> --project agentjobs
    ```

    **Register always** — a dispatched session recognises itself and writes nothing, so
    there is nothing to judge. Every stall protection is keyed on a run record, and a
    session with none is polled by nothing: a permission park, an expired login or a
    silent stall then leaves the task reading `agent`/`work` while your supervisor waits
    on a process nobody is watching (task-320).

    **A task record is a row in a database outside your checkout, so it is not something
    you commit.** See [Where task records live](ENGINEERING.md#where-task-records-live).
3.  **Work**: Small, single-logical-change commits with tests green before each one.
    Stage explicit paths — never `git add -A`.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works. While a named stage is
    red, iterate with `scripts/check.py --only <stage>`; `--from <stage>` picks up where
    a late failure stopped instead of paying for the stages above it, and `--list` names
    them. **Neither is the gate.** A partial run prints `PARTIAL RUN` and every stage it
    skipped, at both ends, so its green cannot be reported as the gate's.

    **Then gate the branch once.** Commit, rebase onto `main` (step 5), and run
    `scripts/check.py` with no arguments on the result — one unqualified gate per task,
    before the handoff, never one per commit:
    [One gate per handoff](ENGINEERING.md#one-gate-per-handoff).
5.  **Hand off**: **rebase onto `main` first** — see
    [How long a branch should live](ENGINEERING.md#how-long-a-branch-should-live); your
    branch has probably been open for hours and a conflict is far cheaper now, while you
    are in context, than at the merge where it stops a scripted finish. Then `handoff` to
    `human`/`review` with a `ball_prompt` saying what was done and what needs review. On a
    project still on `files`, **commit that to `main`** — a handoff sitting on your branch
    is invisible in the React app, so the human you are handing to will never see it.
    Make the review request
    complete the first time: a round trip to answer a question you could have answered is
    the largest thing keeping your branch open. **Stop there** — do not merge.

    **Unless your dispatch prompt told you otherwise.** A run at posture `autonomous`
    is told, in its own prompt, that the merge gate is released for it; that sentence
    is the only authority for skipping this step, and if it is not in your prompt you
    do not have it. See [Your prompt says whether you stop here](#your-prompt-says-whether-you-stop-here).
6.  **On approval**: everything from here is
    [ENGINEERING.md's merge gate](ENGINEERING.md#the-merge-gate), steps 3 to 6 — rebase,
    `--no-ff` merge, mark the branch `merged`, close the task, remove the worktree, then
    delete the branch, then rebuild and restart so the person who approved the work is
    looking at the merged version. **That procedure is numbered there and nowhere else**,
    including which server is yours to restart and which is not; this list stops at the
    approval deliberately, so there is only ever one copy to keep current.

    Two things to carry in before you get there. Branch deletion is `-d`, **never
    `-D`** — a refusal means your merge did not land the way you think it did. And you
    are not finished when the merge commit exists, but when the change is live and you
    have checked. The scripted finish does the whole of it for you (task-293); do it by
    hand only when you merged by hand, and `agentjobs branches` lists what got left
    behind either way.

### Your prompt says whether you stop here

Step 5 is unconditional for a person and for every posture but one. **A dispatched run's
posture decides whether the merge gate stands for it** (task-021), and the decision
reaches you exactly once, in the prompt that started your run:

- *"Posture `auto` stops at the merge gate…"* — or `supervised`, or no clause at all.
  Step 5 as written. Hand off, stop, and let a human approve. This is the default and
  almost always what you have.
- *"Posture `autonomous` releases the merge gate…"* — you merge your own work, and the
  clause names the command. It is **not** `git merge`:

  ```bash
  poetry run agentjobs finish <task-id> --project <project> --posture-release
  ```

  Record your evidence on the task **first** — what you built, what you verified, what
  you decided and rejected. That log entry is the only review this work will get. Then
  run the command: it rebases onto `main`, runs the **full unqualified
  `scripts/check.py`** on the rebased branch, and merges only on a green one; a red gate
  or a conflicting rebase stops it and hands the ball back with what it got done written
  on the record. Exit 0 means merged, closed, delivered and verified.

**Two things have to hold, not one.** The gate is the objective floor and your own
judgement is the other half: if you found something genuinely wrong with the work, hand
off for review however green the gate is. "No serious issue detected by the agent" is you
grading your own homework, which is why the merge goes through the finisher — it runs the
gate itself rather than taking your word for it.

**Never push**, whatever your posture, unless the prompt's push clause says this project
permits it. AgentJobs is configured `push: false` and always will be; that
recoverability is the whole reason an unreviewed merge is acceptable here.

If you are supervising a parent task, the clause is phrased for you instead: it tells you
what the children you start will do, and you approve nothing yourself either way.

### Your API and MCP writes are scoped to your own task

Every request you make over HTTP carries your run's credential, so the server knows you
are a run rather than the person at the keyboard (task-332). You may work **your own**
task -- log, hand off, close, reorder it -- and file new tasks. You may not approve a
review, dispatch anything, change dispatch configuration, register a project, repair the
queue, or act on a task that is not yours. A 403 naming `wrong_task` or
`capability_denied` is that rule and not a bug; ask the human, or say so on the record.
The CLI speaks no HTTP and is unaffected, which is how the epic walk still starts
children. The table is in [docs/authorization.md](docs/authorization.md).

### If you are woken after an approval, read the record before you act

An approval may already have merged your branch without any agent: where a project has
the scripted finish switched on, clicking Approve runs
[the merge gate's steps 3 to 6](ENGINEERING.md#the-merge-gate) itself (task-241).
`agentjobs dispatch config --project <id>` says `finish=on` or `finish=off` for that
project; do not infer it from prose.

So **the record, not your memory, says whether `main` moved** — the finish writes one
of two sentences onto the task. *"The merge is done: `abc1234`"* means the merge landed
and only the delivery is missing: do that, close the task, do not merge again. *"Nothing
was merged"* means the rebase conflicted or the gate went red, and the entry says which.
`agentjobs finish <task> --project <id>` is the same code by hand, and is how a finish
that stopped is retried once its cause is fixed.

None of that changes *who* authorises a merge — see
[Your prompt says whether you stop here](#your-prompt-says-whether-you-stop-here).

A second dispatch may also **resume the session that worked the task** rather than start
a new one, in which case the prompt says so and repeats what still applies. Believe it,
and if what is on disk no longer matches your account of the task, hand the ball back
rather than improvising. If the prompt did not say you were resumed, you were not.

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
`close` — each of which appends its own log entry. Editing them directly skips the record
of *why* they moved. Only `ready` tasks with no unmet `needs` dependencies are returned by
`get_next_task()`.

`ball_prompt` is required whenever the ball is set, **except `agent/available`**, where
the spec is itself the ask. A handoff without a stated ask is a notification with no
payload, and the schema rejects it. The exemption is why a `ready` task can sit with an
empty prompt without being invalid -- most of them do.

### Why you get your own worktree

**You are not the only agent in this repository, and you cannot see the others.** Several
run against one clone. A clone has one working tree and one `HEAD`, so `git checkout`
replaces the files under whichever peer is mid-task — you will not get an error, and
neither will they.

A human working alone does not need this; they have no peer to collide with. You do.

-   Create the worktree **before** the branch, the claim, or anything written to disk.
-   Name it `<repo>-<nnn>` — the project's directory name and the task's number, so here
    `agentjobs-045` — and put it in the `worktrees/` directory beside the clone, not
    inside the clone and not loose in the workspace beside the projects:
    `../worktrees/agentjobs-045`. `git worktree add` creates that directory the first
    time. That is the one convention: it is what `docs/agent-workflow.md` states
    generically and what every dispatched agent's prompt tells it to run.
-   `git worktree remove` it once the branch is merged, then `git branch -d` the branch —
    the worktree first, because a branch checked out in one cannot be deleted, and `-d`
    rather than `-D` so an unmerged branch is refused instead of destroyed. `git worktree
    list` and `git branch --list` are the inventories; a worktree for a closed task is
    litter, and so is its branch. `agentjobs branches` reads both and names what is left.
-   **Never `git checkout` in the shared clone** to start work.
-   Working a task writes no files, so recording one needs no worktree. Anything that
    goes on a branch does.
-   **Do not use Claude Code's `--worktree` / `-w` or `EnterWorktree` to get one.** It
    looks like the CLI doing this for you and it is not the same thing: such a session is
    isolated by a guard that refuses *every* git operation aimed at the shared clone —
    `git -C` and `cd` alike — and the shared clone is where your merge has to happen. You
    would do the work and then be unable to merge it. Take the worktree yourself with
    `git worktree add`, as above. Probed on Claude Code 2.1.235, 2026-08-19; the
    reproduction is in task-186 and in
    [the dispatch design](docs/agent-dispatch-design.md).
-   **The harness tells background sessions the opposite, and this rule wins.** A `--bg`
    session is handed a preamble instructing it to use `EnterWorktree` and saying the
    instruction is enforced. In this repository that instruction is wrong for the reason
    directly above. Ignore it.

    **The enforcement half is switched off here, deliberately** (task-303).
    `.claude/settings.json` sets `"worktree": {"bgIsolation": "none"}` -- the escape the
    refusal message itself names -- so a background session's `Write` and `Edit` into the
    shared clone land instead of being refused. Probed on **Claude Code 2.1.238,
    2026-08-25**. `Bash` writes were never guarded either way.

    **If you are refused anyway** — *"This background session hasn't isolated its changes
    yet. Call EnterWorktree first"* — **put that on the task record before you work around
    it.** It means the key has stopped working, which is a finding about dispatch and not
    a personal obstacle. Write through `Bash` and carry on; do not call `EnterWorktree`.

    A second refusal wears the same face and is different: the task-write guard refuses
    any write whose *content* names a task file path, even when you are editing
    documentation. A false positive; task-276 is the fix. Build the path from pieces, or
    reword, and carry on.

Three agents skipped this in one afternoon on 2026-08-11 and each cost a peer real work;
two things from that are worth carrying:

-   **If you commit a peer's in-flight files** — which is what `git add -A` does here —
    recover with `git reset --soft HEAD~1`, then `git restore --staged` their paths.
    Never `git checkout --` them; that destroys work you did not write.
-   **Every branch shows the same backlog**, because the records are not in the checkout.
    So a task missing from the dashboard is never explained by what is checked out; look
    for it being closed, archived, or in another project.

### Bootstrapping a worktree

`git worktree add` copies tracked files. The Poetry virtualenv and
`frontend/node_modules` are not tracked, so a new worktree cannot run
`scripts/check.py` — the gate you are required to pass before every commit. One command
fixes that, from inside the worktree:

```bash
python scripts/bootstrap.py
```

It runs `poetry install`, `npm ci`, and `playwright install chromium`, then confirms the
environment imports the worktree's own `src/`. **About 30 seconds** in a brand-new
worktree and **13 seconds** to re-run in one that already has both — timed 2026-08-19,
longer on a machine whose Poetry and npm caches are cold. That is not a reason to skip
the worktree.

**Do not borrow the main clone's virtualenv instead.** `poetry install` puts the *main
clone's* `src/` on that environment's path, so `pytest` run from your worktree against it
imports the code on `main`, not the code on your branch, and reports a green suite for
source it never executed. Nothing in the test output reveals this. `check.py` refuses to
run when the import resolves outside its own checkout, for exactly that reason.

**Run the gate with the interpreter the bootstrap prints, not `poetry run`.** Its last
line is an absolute path to this worktree's Python:

```
Verify with: C:\...\virtualenvs\agentjobs-PLnZwjZ_-py3.13\Scripts\python.exe scripts/check.py
```

This matters because **your shell almost certainly has `VIRTUAL_ENV` set to the main
clone's environment** — a dispatched session inherits it — and Poetry prefers an
activated virtualenv over the one it keys on the project path. So `poetry run` from your
worktree resolves to the main clone's environment however many times you bootstrap, and
`check.py` correctly refuses each time. The printed path cannot be redirected.

**Naming the interpreter is the only thing you have to do** — no unsetting or
re-exporting. Since task-210 the gate disowns a foreign `VIRTUAL_ENV` for every process
it spawns, so the nested `poetry run` calls inside it resolve to this checkout too. The
hazard is still real everywhere else: `poetry run` outside the gate still prefers
whatever your shell activated.

That preference is also why the bootstrap tells you it is **ignoring** an activated
virtualenv belonging to another checkout. Until task-194 it did not, and a worktree's
`poetry install` silently rewrote the main clone's editable install. You are not being
careless if you hit this; following these instructions verbatim is what used to cause it.

### Logging Work to the Task
The task record — not the surrounding conversation — is the source of truth for where
work stands. A different agent, or the same one with no memory of this session, must be
able to read the task record alone and know what happened and what is next.

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

### Paraphrase a person, never quote them

This repository has a public remote, so a record says what somebody **meant**, not the
words they used. A quoted aside reads there as a characterisation of a real person
rather than as engineering.

-   **Quote only where the exact wording is the subject** — an API name, a spec sentence
    being disputed, a message being debugged.
-   **Never reproduce tone.** Drop the frustration, profanity or informality and keep
    the substance: "rejected the section outright" carries everything a reader needs.
-   Already written one? `agentjobs redact` replaces a field **or a log entry body** —
    the only verb that reaches the append-only log — and records that it did.
    `agentjobs quotations` finds them; the gate over `tasks/` and the SQLite importer
    both refuse a record carrying one.

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
-   **Durable notification delivery is future work**, so do not build or assume a
    receiver as part of an ordinary handoff. The extension point is schema v2's
    HMAC-signed `task.handoff` webhook — see [docs/webhooks.md](docs/webhooks.md).

## Reporting Standards
-   **Conciseness**: Be brief. Use bullet points.
-   **Evidence**: Link to artifacts, screenshots, or log files that prove success.
-   **Context**: When reporting errors, include the full stack trace and the command that caused it.

## Behavioral Guidelines
-   **Ask First**: If requirements are ambiguous, ask the user for clarification.
-   **Non-Destructive**: Do not delete files or wipe databases unless explicitly instructed.
-   **Self-Correction**: If a tool fails, analyze the error message, fix the input, and retry. Do not loop endlessly.
-   **Transparency**: Explain *why* you are making a change, not just *what* you are changing.
