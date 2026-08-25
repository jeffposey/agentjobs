# ALLAGENTS.md

Shared guidance for all AI agents working in this repository. Universal engineering standards live in [ENGINEERING.md](ENGINEERING.md).

## Task Management

### Work what the queue says is next

The backlog has a stored order, not a sort over timestamps. `agentjobs next` — or
`task_next` over MCP — is the answer, and `--why` explains it: the band and position the
winner stands at, and every open task the queue passed over with the claimability rule
that excluded each. Read that before concluding the order is wrong; a task missing from
the answer is usually blocked, claimed, or holding open children rather than mis-placed.

**If you think something else should be first, move it.**

```bash
agentjobs queue list                      # the reviewable order, band by band
agentjobs queue move task-045 --top       # or --before/--after <id>, or --bottom
```

Over MCP that is `task_queue_move`, with the `actor` and `operation_id` every mutation
carries. Either way the move is attributed and appends a `queue_move` entry, so the next
session inherits the decision instead of re-deriving it.

**A move answers back.** It always lands — nothing here refuses one — but if the order
you just wrote cannot execute, the reply says so: the task you promoted is one the queue
will skip, it now stands ahead of something it needs, what it displaced is gating other
work, the band came out as it went in, or the band is corrupt. Deterministic, immediate,
and silent on an ordinary move. Read it rather than assuming a clean exit code means a
useful reorder.

Three things not to do instead, each of which has a real cost:

- **Do not add a `needs` dependency to make one task come before another.** Dependencies
  are prerequisites. A false one is not a strong hint about order — it makes the task
  unclaimable until the other closes, deadlocks the graph if it ever points both ways,
  and lies to every reader who takes it at face value.
- **Do not hand-edit `queue_position`.** There is no `set_queue_position` for the same
  reason there is no `set_lifecycle`: the number is a consequence of a decision, and the
  decision is what the record should show. A number written by hand can also collide
  with another open task in the band, which is corruption the queue refuses to answer
  over rather than guess past.
- **Do not rely on an instruction given in chat to reorder work.** Chat does not survive
  the session. The queue does, and it is what the next agent will read.

If a tool reports `queue_broken` — or the CLI exits non-zero from `agentjobs queue
check` — the order itself is in doubt, so picking a task by hand is the one response
that cannot be right. `agentjobs queue repair` states everything it guessed, and what it
guessed is exactly what a human should look at afterwards.

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

    It does steps 3 and 4 below, one child at a time, and it blocks until it is done —
    which is the point, because a supervisor that ends its turn promising to check back
    is asleep. `--dry-run` says which child is next and starts nothing.
3.  Each child gets **its own session and its own worktree**, and the walk starts it as a
    real dispatch, on the authorisation the human gave *this* parent. You do not work a
    child yourself — see [You do not work the children](#you-do-not-work-the-children).
    Never merge a child yourself, and never on your own approval.
4.  The walk watches each child through its **task record**, and judges it on one thing:
    did it close `completed`. That means the child's own gate ran and, where its posture
    released the merge, its own merge happened. Anything else — parked for a human,
    closed unresolved, or a session that died twice — **stops the whole walk**, because a
    sibling that depended on that child would be building on a gap.

    **Retries are bounded and the bound is enforced, not promised**: two runs per child
    per human authorisation of the epic, and only a run that *died* ever spends one. A
    human who wants a third can authorise the epic again, which starts a fresh budget and
    records that somebody chose to.

    **At posture `autonomous` there is no review checkpoint in any of this** (task-021):
    a child merges its own work once its own gate is green. At `auto` and `supervised`
    the first child hands off for review and the walk stops there — correctly. The gate
    is standing, not broken.
5.  Exit 0 means no open child remains. **That is not the same as the parent being
    done**, and the walk deliberately never closes it: evaluate the parent's own
    acceptance criteria against the children's durable evidence, do any parent-level
    verification, and close it only where that evidence supports it. Exit 1 means the
    walk stopped for cause; the parent's record says which child and why.
6.  Stop only for a required review/approval gate, a genuine human decision or external
    blocker, a clean usage boundary, or completion of the parent.

The task graph defines scope. Do not absorb unrelated follow-ups merely because they
were mentioned during the loop; create or update a separate durable task only when the
user authorizes it.

### You do not work the children

**Whoever holds a parent task starts a separate session per child and stays running as
the supervisor.** This binds whether you are an interactive session someone told to
"work task-160" or a dispatched run — a dispatched run is told so in its prompt, because
dispatch reads it off the record: a task with an open child gets the supervisor prompt
and every other task gets the ordinary one.

**The threshold is: anything that takes a worktree gets its own session.** A child that
edits files, runs `scripts/check.py`, or produces a branch is a session. A child that is
a decision to record, a question to answer or a task to file is not — it takes no
worktree, and a session for it costs more than it saves.

That threshold rather than a size estimate, for two reasons. It is checkable: "does this
write code?" has an answer, where "is this big enough to be worth a session?" is a
judgement made by the party with an interest in saying no. And the worktree boundary is
already a session boundary in all but name — [a worktree exists](#why-you-get-your-own-worktree)
because this clone has one `HEAD`, and one session moving between two of them is exactly
the interleaving that isolation is for.

The reason is context, not parallelism; the loop is still one child at a time. A session
that works four children carries four children's worth of exploration by the fourth, and
the transcript a handoff should have replaced is precisely what the next session cannot
read. Task-060's own log says it outright: *"the previous conversation was very long and
is not available."*

**The supervisor is thin, and meant to be.** You read the child's record, its acceptance
statuses, its branch and its diff — not its transcript. You are checking that the child
reported and verified its work, not re-verifying it. A supervisor that re-derives each
child's context is a second agent doing the work, at the context cost this rule exists to
avoid.

**Thin enough that the loop itself is now a command** (task-022). `agentjobs dispatch
walk` picks the next eligible child from the queue, starts it, watches its record to a
terminal state, and stops on the first child that is not clean. Every one of those steps
is mechanical, and an agent performing them adds no judgement — only the chance of
getting one wrong at three in the morning with nobody watching. What is left for you is
the step that *is* judgement and which the walk deliberately does not take: whether this
parent's own acceptance criteria are met.

Two things do not change because a child is a session. Its **task records still go to
`main` in this clone**, never to its branch — see
[Task files live on `main`](ENGINEERING.md#task-files-live-on-main-always). And its
**merge gate stands or falls on the child's own terms**: a child merges on an explicit
human approval of *that child*, or — at posture `autonomous` — on its own green gate.
Never on yours. A supervisor approves nothing under either policy.

One thing does change, and it is what would otherwise sink the rule: a fresh worktree has
no virtualenv and no `node_modules`, so a child session **cannot run `scripts/check.py`
until it bootstraps** — `python scripts/bootstrap.py`, about 30 seconds, see
[Bootstrapping a worktree](#bootstrapping-a-worktree). A parent working children inline
paid this once for itself; a rule that gives every child a worktree pays it per child, so
it belongs in the child's first three commands rather than in a workaround the supervisor
performs by hand. A child that skips it either cannot verify its own work or borrows the
main clone's environment and tests the wrong source.

The full protocol — how to start a child, and what to do when one finishes, parks, dies,
or leaves you waiting — is in
[the workflow guide](docs/agent-workflow.md#working-a-parent-task-you-supervise-the-children-you-do-not-work-them).
Two rules from it are worth repeating here because both have already been got wrong:
**watching is a mechanism, not an intention** — a supervisor that ends its turn promising
to check back is asleep — and **the signal is the task record, not the process**, because
a child parked on review has a live process and is the one state that needs you.

### Task Lifecycle

This is the lifecycle for the task you are *working*. If your task has an open child you
are not working it — you are supervising, you take no worktree, and
[the section above](#you-do-not-work-the-children) is your lifecycle instead.

1.  **Read**: Read the task YAML (e.g., `tasks/agentjobs/task-042-*.yaml`) — its `spec`
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
    ```

    **Your task-record commits go to `main`, not to your branch** — see
    [Task files live on main](ENGINEERING.md#task-files-live-on-main-always). Your branch
    carries code; it never touches `tasks/`.
3.  **Work**: Small, single-logical-change commits with tests green before each one.
    Stage explicit paths — never `git add -A`.
4.  **Verify**: Run `poetry run pytest` and exercise the change the way a user would —
    a passing suite is not by itself evidence the feature works. While iterating on a
    late gate failure, `scripts/check.py --from <stage>` picks up where it stopped
    instead of paying for the stages that already passed; `--list` names them. **Neither
    that nor `--only` is the gate.** A partial run prints `PARTIAL RUN` and the stages it
    skipped, at the start and again at the end, precisely so its green cannot be reported
    as the gate's — before a commit, run `scripts/check.py` with no arguments.
5.  **Hand off**: **rebase onto `main` first** — see
    [How long a branch should live](ENGINEERING.md#how-long-a-branch-should-live); your
    branch has probably been open for hours and a conflict is far cheaper now, while you
    are in context, than at the merge where it stops a scripted finish. Then `handoff` to
    `human`/`review` with a `ball_prompt` saying what was done and what needs review, and
    **commit that to `main`** — a handoff sitting on your branch is invisible in the React
    app, so the human you are handing to will never see it. Make the review request
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

    Two things about it to carry in before you get there. The branch deletion is `-d`,
    **never `-D`**: `-d` refuses a branch `main` does not contain, and a refusal means
    your merge did not land the way you think it did, which is worth stopping over rather
    than forcing past. And you are not finished when the merge commit exists — you are
    finished when the change is live and you have checked, because leaving them on the
    version they just approved you to replace is the default outcome of skipping it, and
    they will find out before you do. The scripted finish does the whole of it for you
    (task-293); do it by hand only when you merged by hand, and `agentjobs branches`
    lists what got left behind either way.

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
  you decided and rejected. A merge nobody can review afterwards is worse than one
  nobody reviewed beforehand, and that log entry is the only review this work will get.
  Then run the command. It rebases onto `main`, runs the **full unqualified
  `scripts/check.py`** on the rebased branch, and merges only on a green one; a red gate
  or a conflicting rebase stops it and hands the ball back with what it got done written
  on the record. Exit 0 means merged, closed, delivered and verified.

**Two things have to hold, not one.** The gate is the objective floor and your own
judgement is the other half: if you found something genuinely wrong with the work, hand
off for review however green the gate is. "No serious issue detected by the agent" is you
grading your own homework, which is why it is never the only authority — and the reason
the merge goes through the finisher is that the finisher runs the gate itself rather than
taking your word for it.

**Never push**, whatever your posture, unless the prompt's push clause says this project
permits it. AgentJobs is configured `push: false` and always will be. Merging into a
local `main` is recoverable; publishing is not, and that recoverability is the whole
reason an unreviewed merge is acceptable here.

If you are supervising a parent task, the clause is phrased for you instead: it tells you
what the children you start will do, and you approve nothing yourself either way.

### The post-approval steps may be done before you wake up

**Where this machine has the scripted finish switched on, the approval runs them
itself** — rebase, gate, merge `--no-ff`, rebuild, restart, verify, close, remove the
worktree, delete the branch — with no agent in the loop at all (task-241). Those are
[the merge gate's steps 3 to 6](ENGINEERING.md#steps-3-to-6-may-already-have-happened-before-you-read-them),
which is the only place they are numbered. Most of the time you will simply never be
dispatched again, and the task will be closed by the time anyone looks.

You are woken only when it stopped, and then **the record tells you where, and whether
`main` moved**. Read it before acting on anything you remember:

- **"The merge is done: `abc1234`"** — the merge is in, the task is deliberately still
  open, and what is missing is the delivery. Do that and close it. Do not merge again.
- **"Nothing was merged"** — the rebase conflicted or the gate went red, and the entry
  says which. For a conflict it also says whether your branch was restored to where it
  was, having read the tip back rather than assumed it.

The scripted finish relaxes nothing about *who* authorises a merge; it only removes the
agent from the commands after the authorisation. At `auto` and `supervised` a person
still approves, per task, and step 5 above is still where you stop. At `autonomous` the
authority is the green gate and you invoke the same code yourself with
`--posture-release`. Either way `agentjobs finish <task>` is that code by hand, and is
how a finish that escalated is retried once its cause is fixed.

### You may be woken rather than restarted

**A second dispatch of your task may resume the session that worked it, not start a new
one.** When it does, your first prompt says so explicitly: it names this as the same
session, carries what the human just wrote, and tells you the run it resumed. Everything
you established still applies — the worktree you took, the branch you are on, what you
built and what you verified. Do not start over and do not take a second worktree.

This exists because the post-approval run — rebase, merge `--no-ff`, close, rebuild,
restart — averaged about eleven minutes, almost none of it those commands. It was a cold
agent working out which branch it owned. Resuming skips that and nothing else: whatever
authorises the merge for your posture is exactly what authorised it before — a human in
the GUI for `auto` and `supervised`, a green gate for `autonomous`.

Two things to do with it:

- **Check before you act on memory.** A resumed conversation is confident by
  construction. If your worktree is gone, your branch is not where you left it, or your
  account of the task no longer matches what is on disk, say so on the record and hand
  the ball back. Do not improvise a recovery.
- **Do not assume you were resumed.** A cold start is the fallback for every uncertainty
  and stays the ordinary case for a task's first run. The prompt is what tells you which
  one you are; if it did not say you were resumed, you were not.

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
-   Committing task metadata straight to `main` (the narrow exception in ENGINEERING.md)
    does not need one. Anything that goes on a branch does.
-   **Do not use Claude Code's `--worktree` / `-w` to get one.** It looks like the CLI
    doing this for you and it is not the same thing: a `-w` session is isolated by a
    guard that refuses *every* git operation aimed at the shared clone — `git -C` and
    `cd` alike — and the shared clone is where your task-record commits and your merge
    have to happen. You would do the work and then be unable to record or merge it.
    Take the worktree yourself with `git worktree add`, as above. Probed on Claude Code
    2.1.235, 2026-08-19; the reproduction is in task-186 and in
    [the dispatch design](docs/agent-dispatch-design.md).
-   **The harness may tell you the opposite, and this rule wins.** A background session
    is given a preamble instructing it to use `EnterWorktree`, and saying the instruction
    is enforced because edits in the shared checkout are rejected. In this repository that
    instruction is wrong for the reason directly above, and following it strands your work
    where you cannot record or merge it. Three auditors on 2026-08-21 each had a write
    refused and worked around it by writing to a temp directory and copying the file in;
    that is the correct improvisation if you hit it. **Do not silently give up on a write
    the guard refuses** -- say on the task record that it happened.

    A second refusal wears the same face and is a different thing: the task-write guard
    refuses any write whose *content* mentions a task file path, even when the file you
    are editing is documentation. It is a false positive, it has cost several sessions
    time, and task-276 is the fix. Build the path from pieces, or reword, and carry on.

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

**That is the whole of it: naming the interpreter is the only thing you have to do.**
You do not need to unset, re-export or otherwise manage `VIRTUAL_ENV` around a gate run.
The gate disowns a foreign one for every process it spawns, so the nested `poetry run`
calls inside it — the frontend's OpenAPI and icon checks, and the server Playwright
starts — resolve to this checkout too. Before task-210 they did not, and a worktree gate
run went green through Black, Ruff, MyPy, pytest, Vitest and the production build and
was then refused at the Playwright stage, six minutes in, for pointing at the main
clone. The hazard above is still real everywhere else; `poetry run` outside the gate
still prefers whatever your shell activated.

The same preference is why the bootstrap now tells you it is **ignoring** an activated
virtualenv that belongs to another checkout. Until task-194 it did not: a worktree's
`poetry install` rewrote the main clone's editable install, and the dashboard on 8876
began serving that worktree's unmerged branch — from the correct task files, with correct
`git log` output, saying nothing. It took a forensic session to find. You are not being
careless if you hit this; following these instructions verbatim is what used to cause it.

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
