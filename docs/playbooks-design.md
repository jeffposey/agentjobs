# Playbooks — design proposal

**Task:** task-211. **Status:** design pass, awaiting review. Nothing here is
implemented; implementation tasks are derived in §12 and held as drafts until this
design is approved.

This document defines the **playbook**: a named, versioned brief for AI-judgment work —
grooming, ordering, spec-writing — checked into the repository and instantiated through
dispatch. It specifies four playbooks as its test cases (`groom`, `reorder`,
`flesh-out`, `queue-check`), and it generalises the bounds Jeff set for autonomous
reordering into rules any judgment playbook that writes must satisfy.

---

## 1. The gap

The product has three layers of "make work happen", and the middle one is missing.

1. **Deterministic verbs.** `queue move`, `close --outcome superseded`,
   `task_promote` — literal Python, no model, no judgment. These exist, they are the
   rails, and the task-081 program keeps adding to them.

2. **Dispatched AI work.** An agent session run against a brief. This exists
   (task-060's design, task-074's machinery) and it works — but every brief is written
   fresh. "Groom the backlog" means someone composes a prompt or a task from scratch,
   and the run's quality is capped by how good that one-off brief happened to be.

3. **Playbooks — the missing middle.** The judgment work between those layers is
   *recurring*: the backlog needs grooming more than once, the queue needs reordering
   whenever tasks arrive, thin tasks always need fleshing out. Today each recurrence
   pays the full cost of writing the brief again, and the lessons of the last run —
   the rule that was missing, the gate that was skipped — die with the session that
   learned them.

The repository already contains the fossil record of this gap: `prompts/` holds
thirteen one-off implementation briefs from the phased roadmap, each written once, run
once, and retired. They were good briefs. Nothing about them was reusable, because
nothing made them reusable — no name, no contract, no place for the next run to find
them.

The evidence that the middle layer is worth building is task-209: the first live
reorder of this backlog is being run from a brief that took several sessions of
discussion to get right — graded effort by queue depth, the ordering-is-not-grooming
constraint, the approval shape. All of that judgment is currently recorded as decisions
on one task. The second reorder should not have to excavate it.

**A playbook is that brief written once, well, with its rules and its human gates
stated — then run many times.** Given the product is called AgentJobs, a playbook is
literally a job definition an agent can be given repeatedly.

### What a playbook is not

- **Not a deterministic verb.** A playbook run exercises judgment; its output is an
  argument you can read and disagree with. Anything that can be decided without a
  model belongs in layer 1, not in a playbook.
- **Not a workflow engine.** A playbook does not sequence other playbooks, spawn
  children, or manage state machines. It is one brief for one kind of run.
- **Not a permission.** Naming a playbook never widens what may execute on a machine
  (§6). The dispatch gates are untouched.

---

## 2. Naming

**"Playbook" stands.** The candidates:

- **Routine** — rejected. It implies a schedule, and recurrence is exactly what this
  design defers (§8). It also collides with Claude Code's own "routines" (scheduled
  cloud agents), which would guarantee confusion in a product whose primary runner is
  Claude Code.
- **Job template** — rejected. It undersells the concept: a template parameterises
  task creation, but a playbook also carries permitted verbs, human gates, and a
  target shape. And "job" is the most overloaded word available — the product name
  already uses it to mean something else.
- **Skill** — rejected. Claude Code ships a first-class feature by this name; a
  dispatched Claude session would have two unrelated "skills" in scope at once.
- **Brief** — rejected as the product term, retained as the term for the playbook's
  prose body. The brief is the content; the playbook is the whole contract around it.

"Playbook" reads correctly to anyone who has met Ansible's usage — a named, versioned
procedure with rules — without colliding with anything in this stack. It becomes the
user-facing term everywhere: CLI (`agentjobs playbook`), REST, GUI, docs.

**Vocabulary used below:** a *playbook* is the versioned file; a *run* is one
instantiation of it; a *run task* is the task a batch run becomes (§4); a *check* is
the output of a reactive run (§5.4).

---

## 3. Anatomy: where playbooks live and what one contains

### 3.1 Storage

**Per-project, in the repository, in a `playbooks/` directory** named by
`playbooks_directory` in `.agentjobs/config.yaml` (default `playbooks`, exactly the
pattern `tasks_directory` and `prompts_directory` already follow). One file per
playbook, filename is the playbook name: `playbooks/groom.md`.

Versioned in git like everything else: no version field, no version suffixes. A run
records which version it read (§4.3), and `git log playbooks/groom.md` is the history
of how the groom brief improved — which is the auditable, improvable property this
task exists to create.

**AgentJobs ships reference playbooks; it never runs them implicitly.**
`agentjobs playbook init` copies the shipped `groom`, `reorder`, and `flesh-out` into
the project's `playbooks/` directory, refusing to overwrite anything already there.
From that moment the project's copy is authoritative and tunable — bands, categories
and thresholds differ per project, and a brief behind a name that lives in the
installed package would make a run's behaviour depend on the package version rather
than the repository. Copy-in also means `git blame` shows who tuned what, which an
implicit built-in can never show.

*Rejected: a machine-local playbook directory* (`~/.agentjobs/playbooks/`). It is the
wrong half of the `projects.py` split: a playbook is *what the work is*, which is a
project's property, not *what this machine will do about it*. Machine-local playbooks
would also be invisible to every other machine and agent working the project, which
defeats "written once, run many times".

*Rejected: playbooks inside `.agentjobs/config.yaml`.* Multi-paragraph prose in a
YAML string is unauthorable and undiffable, and the config file is loaded on every
request — a brief is read at instantiation time only.

### 3.2 Format: markdown with YAML frontmatter

The machine-read contract is frontmatter; the brief is the markdown body. This is the
natural authoring split — the contract is small and structured, the brief is prose an
agent reads — and it diffs well.

```markdown
---
name: groom                      # must match the filename stem
description: Find duplicate, superseded and stale tasks; propose closures;
  close only what a human approved.
target: project                  # project | task — what a run is aimed at
kind: batch                      # batch | reactive — see §4 and §5.4
difficulty: hard                 # routine | standard | hard — see §7.2
verbs: [close, log]              # declared contract, audited — see §6.2
gates:
  - before: close
    what: The full closure list, with a reason per item, approved by a human
      as recorded on the run task.
run_task:                        # defaults for the run task a batch run creates
  title: "Groom the {project} backlog"
  category: meta
  priority: medium
  tags: [grooming, playbook]
  acceptance:
    - text: The closure proposal was recorded on this task before anything closed.
    - text: Every executed close appears in the approved list, with the approved
        outcome (duplicate or superseded), and nothing else was closed.
    - text: No task file was deleted and no spec was rewritten.
---

# Groom

(the brief: what to look for, what counts as a duplicate, how to state a
proposal, what to do with the ambiguous cases …)
```

What each field is for:

| Field | What it decides |
|---|---|
| `name`, `description` | Identity and the one-line answer in `playbook list`. Name must equal the filename stem; validation refuses a mismatch. |
| `target` | Instantiation shape (§4): `project` runs create a run task; `task` runs dispatch the named task itself. |
| `kind` | Lifecycle shape: `batch` runs are tasks; `reactive` runs are checks (§5.4). |
| `difficulty` | The capability this work needs, in the task-156 vocabulary. Feeds runner selection (§7.2). |
| `verbs` | The manager verbs a run declares it will use — a contract, not a mechanical gate; §6.2 states the enforcement honestly. |
| `gates` | Where a run must stop for a human, stated declaratively so the UI can show them and the audit can check them. Enforced by the brief and the record, per §6. |
| `run_task` | Title/category/priority/tags/acceptance for the run task. Acceptance criteria here are the run's own definition of done — and the future hook for task-078's executable checks, if a playbook's run ever earns a `check:`. |

*Rejected: a structured rules DSL in frontmatter* (conditions, thresholds, per-band
policies). The brief is judgment work; encoding judgment in config fields is the
scoring mistake from task-selection-design §12 wearing a new hat. Rules live in the
prose where an agent can weigh them and a human can read them.

---

## 4. Instantiation: a run is a task

### 4.1 The rule, defended

**A batch playbook run is durable task state — a task, created or claimed at
instantiation, logged, handed off, closed.** This was the strong prior from the repo's
philosophy, and it survives examination on four grounds:

1. **The resumption contract holds for free.** A groom run parked on approval *is* a
   task with `ball: human` and a `ball_prompt` carrying the proposal. The session that
   proposed can die; any session can execute the approved closures from the record
   alone. No new resumption machinery, because the run's memory is the task record —
   the same argument that let dispatch refuse to compose prompts.
2. **Dispatch requires a task anyway.** The human-clocked rule needs a stored log
   entry to resolve; the claim, the run lock, the caps, the `dispatch` /
   `dispatch_result` entries all key on a task id. Run-is-a-task means playbooks add
   **zero** new execution machinery — they ride §5 of the dispatch design untouched.
3. **The audit is in git, next to the work.** Proposals, approvals, and executed
   closures land in a task log committed to `main`. "What did the March groom runs
   close, and why" is a question the corpus answers.
4. **The human gates are the existing ones.** A gate is `ball: human` with a
   `ball_prompt`; approval is the ball leaving `human` — the same mechanics as every
   review this repository performs, visible in the same dashboard inbox.

The two `target` shapes differ only in where that task comes from:

- **`target: project`** (groom, reorder): `agentjobs playbook run groom` **creates a
  run task** from the `run_task` frontmatter, then dispatches it. One run task per
  invocation, dated by creation, closed when the run concludes. Run tasks accumulate
  in the corpus and that is intended — they are the audit trail — with `archived`
  available if they ever crowd a view.
- **`target: task`** (flesh-out): the run **is a dispatch of the target task itself**,
  with the playbook supplying the brief. No run task exists, because the record
  belongs on the task being worked: a flesh-out of task-123 is ordinary work *on*
  task-123, and creating a second task to say so would split one story across two
  records. A task-targeted playbook run is exactly "dispatch with a named brief".

Reactive runs (`kind: reactive`) are the deliberate exception: they create no task and
are not dispatches in the §5 sense. §5.4 defines their record.

### 4.2 The prompt stays a stub

Dispatch's strongest rule — the prompt is a pointer, not a composition — survives
contact with playbooks. A playbook dispatch appends one line to the existing stub:

```
Your brief is the playbook `groom` at playbooks/groom.md (sha256 3f9c…, 12 chars
shown). Read the task record first, then the playbook; the playbook is the
specification for this run.
```

The brief is **not** copied into the prompt or into the run task's spec. Copying would
fork it: a playbook fix made mid-run would not reach the copy, and two sources of the
same brief will disagree eventually — the exact drift argument that rejected prompt
composition in dispatch §4. The run task's `spec.description` is a short pointer
("Run of playbook `groom`; the playbook file is the specification"), and its
`context[]` names the playbook path.

### 4.3 What the record pins

The `dispatch` log entry for a playbook run gains two fields in `data`:

```yaml
playbook: groom
playbook_hash: sha256:3f9c…      # the file content at instantiation
```

`git_head` is already recorded and usually suffices, but the hash is what makes the
record self-contained: it says exactly which brief ran even if the file was dirty or
the head moved, and it costs one hash of one file. A later reader diffing two runs'
behaviour starts by diffing the briefs those hashes name.

### 4.4 Rejected instantiation shapes

- **Ephemeral runs in `~/.agentjobs/runs/` only.** Breaks the resumption contract, is
  invisible to the dashboard and to every other machine, and puts human gates in a
  place no human looks. The run directory stays what it is today: machine-local
  scaffolding, with the durable record in the task.
- **A standing singleton task per playbook** ("the groom task", reopened per run).
  Tempting — no proliferation — and wrong three ways: reopening re-enters the queue at
  the bottom by design, so the standing task fights the queue invariants every cycle;
  one log accretes every run forever, burying the decisions the log exists to surface;
  and two overlapping runs would collide in one record. Per-run tasks keep each run's
  approval atomic and each record readable.
- **Run state inside the playbook file.** Turns a versioned brief into mutable state,
  makes every run a merge conflict, and puts agent writes in a file whose whole value
  is that humans author it.

---

## 5. The playbooks

Four, and they are the design's test cases rather than an exhaustive set. The first
three are batch; the fourth is the reactive category that log entry 6 on task-211
required this design to add or reject — it is added, with its own §.

A division of labour binds the first two, from the task-211 record: **groom prunes the
list, reorder orders what survives.** They meet at the tail of the queue from
different directions — reorder's triage scan asks "does anything down here belong near
the head?", groom asks "is anything down here dead?" — and neither does the other's
job.

### 5.1 `groom` — the proving case

**Target:** project. **Kind:** batch. **Gate:** mandatory human approval before any
close.

The brief, normatively:

- **Sweep the open corpus** for three conditions: *duplicates* (two tasks that are the
  same work), *superseded* (a task whose work another task or a shipped change now
  covers), and *staleness* (a task whose premise no longer holds — the feature it
  extends was rewritten, the problem it fixes was fixed another way).
- **Propose, never act first.** The run records a closure proposal on the run task —
  one item per task: the id, the proposed outcome, the reason, and the evidence (the
  duplicate's counterpart, the superseding task or commit). Then it hands off
  `ball: human` / `ball_reason: review` with the proposal as the `ball_prompt`, and
  stops.
- **Execute only what was approved.** Approval is recorded on the run task (Jeff's
  entry, or the ball leaving `human` with the approved list stated — partial approval
  is expressible by striking items, and what executes is the recorded approved list,
  nothing else). The run then executes each approved closure through the managed
  `close` verb with `outcome: duplicate` or `outcome: superseded`, each close's body
  referencing the run task by id, then closes its own run task with the tally.
- **The executable set is exactly {close-as-duplicate, close-as-superseded}.**
  A stale task that is neither is *surfaced*, not closed: `wont_do` is a human's own
  judgment about intent, and the playbook raises it as a question rather than
  proposing to make it. Groom also never deletes a file, never rewrites a spec, never
  reprioritises, and never reorders — anything it thinks about order it hands to
  reorder by raising it, not by moving it.

**Why groom's gate is mandatory when reorder's review is optional (§5.2), stated
because the asymmetry is the design:** a wrong close is silent and compounding — the
closed task leaves every list and is forgotten, which is precisely the failure the
queue program was built to end — and "these two tasks are the same" is exactly the
judgment humans contest. A wrong queue move is loud and cheap: the order is read every
time anyone asks "what is next", and the next run corrects it. Propose-then-approve
where errors hide; write-then-audit where errors show themselves.

### 5.2 `reorder` — task-209 generalised

**Target:** project. **Kind:** batch. **Gate:** none required — human review is
optional, per Jeff's decision of 2026-08-20 (task-211 log entry 3, overturning
task-selection-design §12's suggestion-only clause). At 75 open tasks the human is the
less-informed party at ordering time; requiring his click on every order makes the
order worse, not more legitimate.

The §12 rejection of **scoring** is untouched and binds every playbook: no numeric
ranking function over signals. A run's output is an argument you can read and disagree
with; a score is not.

The brief, normatively:

- **Graded effort by queue depth** (task-209's decision, generalised): the head of
  each band — roughly the next 5 — ordered exactly, with a reason per move; the next
  5–10 in rough buckets; the tail left as it stands, receiving only a fast triage scan
  whose single question is "does anything here belong near the head?". Precision deep
  in the queue decays before it is consumed; attention is spent where positions will
  actually be executed.
- **Writes through `queue move`, itself.** Every move is a managed `queue_move` entry
  whose body carries the *reason for the placement* — this is bound 1, **reasoning is
  recorded**, and it is what makes optional review safe rather than nominal: a
  drifted order is audited after the fact instead of having to be caught live.
- **Triggered, not continuous** (bound 2): a run happens on an explicit trigger — new
  tasks arrived, or a human asked — never on every dispatch and never on a schedule
  (§8). Continuous re-derivation is churn, and the queue must stay readable as a
  stable object.
- **A human move is sticky** (bound 3): see anchors, below.

**Anchors.** No new schema field. `queue_move` is a managed verb and its log entry
records its actor, so *"was this position last set by a human?"* is derivable from the
record as it stands — provenance that cannot drift from the truth because it *is* the
write history. The run derives, for every open task in the bands it touches, the actor
of the latest `queue_move`; a human actor makes that position an **anchor**:

- The run orders *around* anchors by default.
- Overriding an ordinary anchor is permitted only with an explicit statement in the
  run's output naming the anchor and the grounds — never by silently re-deriving the
  band.
- A **strong anchor** — a position a human kept after reading a `queue-check`
  objection (§5.4) — is not moved at all. The run may raise a question about it; it
  does not override it. The human who read the objection and kept the move was the
  informed party, which is the exact condition that legitimises human authority here.
- **Staleness:** an anchor expires when its task materially changes — it closes, its
  priority band changes, or a `needs` dependency resolves — at which point the
  position is the playbook's again. An anchor with no expiry would slowly freeze the
  queue into whatever the human last touched; this rule is the accepted form of the
  candidate stated in task-211 log entry 5, adopted unchanged.

*Rejected: a `queue_pinned` boolean.* Explicit and fast to read, but it needs a
clearing rule or every task is eventually pinned and the playbook has nothing left to
do — and it can drift from the history that justifies it. Provenance carries the same
information for free.

### 5.3 `flesh-out` — a full spec for a thin task

**Target:** task. **Kind:** batch. **Gate:** review-after-write — the target task is
left at `ball: human` / `ball_reason: review`, which keeps it unclaimable and
undispatchable until a human releases it.

The run is an ordinary dispatch of the target task with the playbook as brief. It
reads the thin record plus the repository context, and writes a full spec — summary,
intent, description, constraints, out-of-scope, acceptance — through the managed
`update_content` verb, directly onto the task. Then it hands off for review with a
`ball_prompt` naming what it wrote and what it was least sure of.

Write-then-review rather than propose-then-write, deliberately: the write is cheap to
reverse (the prior text is in git and in the update's log entry), the target is by
definition mostly empty so there is little to destroy, and a draft posted as a log
entry for later transcription would be a proposal formatted as a chore. What makes
this safe is the parked ball: a wrong spec cannot be dispatched against, because the
task is in review until a human says otherwise.

Scope rules in the brief: flesh-out never changes lifecycle, priority, queue position,
or dependencies — it writes spec fields and acceptance criteria, and raises anything
else as a question.

### 5.4 `queue-check` — the reactive category

Task-211 log entry 6 describes an interaction that the three cases above cannot
express, and requires this design to add or reject the category. **Added.** A
**reactive playbook** is triggered by a human action, scoped to that action, and
returns an objection or silence. It is cheap, fast, and will likely be the most-run
playbook in the product.

The concrete case: Jeff drags a task to a new position in the React queue. **The move
lands immediately and is authoritative** — never blocked, never awaiting a model,
durable the instant it happens. A playbook then checks it and objects *only if it has
grounds*. This inverts the earlier reason-at-drag-time proposal (task-211 log entry 5,
now the rejected alternative) for three reasons recorded there: an explanation shown
on every drag becomes wallpaper, it explains the grabbed task when the risk is usually
what got pushed *down*, and a prior justification cannot evaluate the queue as it now
stands — only a post-hoc check can.

Mechanics, each answering a consequence entry 6 required settled:

- **Trigger and debounce.** A reorder burst — consecutive human `queue_move`s in one
  project — is checked **once, after it settles**: the check fires when 120 seconds
  pass with no further human move in that project. One model call per burst, not per
  drop; Jeff reorders routinely and a call per drag is real latency and real money.
  The window is machine-local config with a stated default, not schema.
- **Shape of the run.** Not a task, not a session: a **batch-mode model call**
  (dispatch's `batch` runner mode — spend ceiling via `--max-budget-usd`, structured
  output, real exit code) whose input is the burst's moves plus the queue and the
  affected records, and whose output is silence or objections. The human's own
  `queue_move` entries are the human act this traces to; the check spends a bounded,
  machine-configured amount on the back of it.
- **Record.** An objection is appended to each objected task's log (a `note`-shaped
  entry by the reserved `dispatcher`-style playbook actor, referencing the
  `queue_move` entry it objects to). It lands on the record whether or not anyone is
  watching the browser, so a burst made from a phone still leaves its objection where
  the task page shows it.
- **Surface.** Non-blocking notice in the React queue — the objection text with
  `undo` and `keep`, one click each way. `undo` issues the inverse `queue_move`
  (logged, human actor). `keep` appends the human's kept-over-objection entry
  (`re:` the objection), which is what promotes the position to a **strong anchor**
  (§5.2). Ignoring the notice leaves an ordinary anchor. A blocking modal is
  rejected: it reintroduces the friction the post-hoc inversion removed.
- **Fail-open, structurally.** If dispatch is disabled, no runner is configured, the
  budget is exhausted, or the check errors — nothing happens and the move stands. The
  check is advisory by construction; a queue that cannot be reordered without a model
  on hand would be a worse product than one with no check at all.

Reactive playbooks create no run task. A task per drag would bury the corpus in
records of non-events; the objection belongs on the task it is about, threaded to the
move it answers. This is the scoped exception to §4.1, and the line is principled:
**work with a lifecycle gets a task; a bounded check with a one-shot output annotates
the record it checked.**

---

## 6. Guardrails

### 6.1 Playbooks act through the verbs, like everyone else

A playbook run is an agent session. It mutates state through the managed verbs —
`claim`, `handoff`, `close`, `queue move`, `update_content`, log appends — or not at
all. There is no bulk YAML path, no generic setter, and nothing about being "a
playbook run" relaxes a single rule in ALLAGENTS.md. The propose-then-approve gates in
a playbook are *additional* constraints on top of that floor, never substitutes for
it.

### 6.2 The verb contract is audited, not mechanically enforced — stated honestly

The `verbs:` list and `gates:` in frontmatter are a **declared contract**. They are
not a mechanical per-run permission boundary, because no per-run identity exists to
hang one on: runs act through shared actor names (`claude`), and the API cannot tell
one session's `close` from another's. Pretending otherwise would be the allow-list
mistake the loops design rejected — a control that stops nothing while making the real
gates feel less load-bearing.

What actually holds the line, in order of when it acts:

1. **The managed-verb surface.** The only writes possible are logged, attributed,
   idempotent domain operations. The contract's job is to say *which* of those this
   playbook uses; the surface guarantees nothing else is available.
2. **The human gates.** Groom's approval means an unapproved close is detectable by
   construction: the approved list is on the record *before* anything executes, so
   the audit is a set comparison, not a judgment.
3. **The record.** Every mutation logs actor and operation; the run task names the
   playbook and pins its hash. A run that acted outside its contract has documented
   itself doing so, in git, and the finding is grounds to fix the brief.

Mechanical per-run enforcement (per-run tokens the API checks verbs against) is
possible future work. **Reopen when a playbook run is first observed acting outside
its declared contract** — build it on evidence of the failure, not in anticipation.

### 6.3 A playbook cannot widen what executes

Dispatch gate 2 says a repository must never choose what runs on a machine. Playbooks
are repository content, so the same rule binds them, restated for this feature:

- **A playbook never names a runner, a runner group, or argv.** It declares
  `difficulty` — an abstract statement of the capability the work needs — and the
  machine's own config maps that to spend (§7.2). A repo file occupying rung 1 of the
  selection ladder would let a `git clone` choose what model of whose account burns.
- **A playbook run is subject to every dispatch gate unchanged**: master switch,
  machine-local runner, per-project enablement, sentinel, human-clocked authorisation,
  caps. `playbook run` introduces no new authorisation surface (§7.1).
- **The trust boundary is per-project dispatch enablement**, same as the loops design
  (L3): a playbook is instructions to an agent that already has that project's write
  access, so enabling a project for dispatch is what authorises its playbooks —
  strictly less new capability than the enablement already granted. A cloned repo full
  of malicious playbooks can do nothing on a machine that has not enabled it, and on a
  machine that has, it could already do the same through the task files themselves.
  Someone reviewing a PR that edits `playbooks/` should know they are reviewing
  instructions a dispatched agent will follow — the same standing this repo's
  `ALLAGENTS.md` already has.

---

## 7. Invocation surfaces and model selection

### 7.1 Surfaces

**CLI** — a `playbook` sub-app in the shape of `dispatch` and `queue`:

```
agentjobs playbook list                    # names + descriptions + kinds
agentjobs playbook show groom              # frontmatter contract + the brief
agentjobs playbook init [--force-refuse]   # copy the shipped references in; never overwrites
agentjobs playbook run groom [--group deep]
agentjobs playbook run flesh-out --task task-123
```

`run` composes existing machinery: for `target: project`, create the run task from
frontmatter, then dispatch it; for `target: task`, dispatch the named task with the
playbook pointer (§4.2). Authorisation follows task-188 exactly — the browser path
writes the clicking human's authorising entry; the CLI path requires the stored record
to satisfy the human-clocked rule, and `default_user` is never substituted. A run task
created by the human running the command carries that human's creation entry, which is
itself the authorisation. **No new authorisation surface exists.**

**REST** — `GET /api/projects/{id}/playbooks`,
`POST /api/projects/{id}/playbooks/{name}/run` (body: `user`, optional `task`,
optional `group` — the same fields the dispatch endpoint takes, because it is the
dispatch endpoint with a brief attached).

**GUI** — the playbook list with a Run button per playbook, riding the existing
dispatch dialog and its enablement/disable states; a running playbook is a running
dispatch and appears wherever runs appear.

**MCP** — `playbooks_list`, read-only, so agents can discover what exists.
**Deliberately no `playbook_run` tool.** An MCP mutation is callable by agents, and an
agent starting a playbook run is an agent causing a dispatch — the transition §2 of
the dispatch design exists to forbid. An agent that believes a groom pass is needed
raises it the way agents raise everything: a question or a handoff a human reads.

### 7.2 Model selection: playbooks declare difficulty, machines map it

Jeff's stated posture (task-211 log entry 3): judgment playbooks — reorder first, then
spec-writing and review — should go to a frontier runner, not the project default.
Today that is expressible only per-dispatch (`--group deep`), because the selection
ladder has no task-level rung: the difficulty → profile table is designed
(dispatch §4, "Model policy") and unbuilt, and `difficulty` itself is task-156, also
unbuilt.

The design therefore has a now and a later, and both are cheap:

- **Now:** the reference judgment playbooks declare `difficulty: hard` in frontmatter
  (display and orientation, like every `difficulty` until the table exists), and
  `playbook run --group` passes through to the dispatch that already accepts it. The
  human picks the big model per run, which is exactly today's per-dispatch state.
- **Later:** once task-156 lands, the run task **carries the playbook's declared
  difficulty as a real field**, and once the profile table is built, dispatch maps it
  — at which point "this playbook is hard work, give it the frontier model" is durable
  data instead of a choice remade at every dispatch. Playbooks are named consumer #1
  of that table, and strengthen its reopen trigger 3 (unattended dispatch picking its
  own group is the case a difficulty mapping exists for).

The dependency runs one way: playbooks want task-156; nothing in task-156 waits on
playbooks. It is carried as a `related` dependency on the instantiation child (§12).

### 7.3 The reactive lane and runner modes

`queue-check` runs batch mode by design (§5.4): it needs the spend ceiling and
structured output that batch has and sessions do not, and it needs no steerability —
there is nothing to redirect in a sub-minute check. This is the first consumer that
*prefers* batch since session mode became the default, which is worth noting as
vindication of keeping both modes.

---

## 8. Recurrence: deferred, and what "triggered" means meanwhile

**Decision: playbooks are on-demand in this design. No schedule, no standing queue.**

The reasoning is already written elsewhere and applies unchanged. Dispatch §2a permits
autonomous *chains* only with a human-authorized, machine-evaluable bound; a scheduled
groom is not a converging chain, it is "cron with judgement", which the loops design
explicitly names a different and smaller problem and declines to smuggle in. Deferring
costs almost nothing today — the human act that triggers a groom run is one command or
one click — and building recurrence before the playbooks themselves have run even once
would be automating an unmeasured process.

What is *not* deferred is being **triggered by events that are human acts**:

- `queue-check` fires on a human's own queue moves (§5.4) — the human act is the
  trigger, one bounded check per burst. That is inside the spirit and the letter of
  human-clocked dispatch.
- Reorder's stated trigger — "new tasks arrived" — is, in this design, still a human
  reading that state and running the playbook. Wiring the arrival event to the run is
  recurrence machinery and waits.

**Reopen when:** the manual cadence becomes the demonstrable bottleneck (groom runs
requested more than weekly, or the backlog demonstrably rots between requested runs) —
and when it reopens, it lands on task-078's authorization machinery: a
`chain_authorized`-shaped entry naming the playbook, a cadence, and bounds, so
recurrence arrives with the same evidence rules as every other autonomy in this
product. The `run_task.acceptance` field (§3.2) is the prepared hook: a playbook whose
runs carry acceptance criteria is a playbook a bounded loop can evaluate.

---

## 9. Rejected alternatives

Collected, including those argued inline, because the rejected list is what stops the
same conversation happening twice.

- **Routine / job template / skill / brief as the product term.** §2.
- **Machine-local playbooks; playbooks in `config.yaml`.** §3.1.
- **Implicit built-in playbooks run by name from the package.** §3.1 — behaviour would
  version with the install, not the repo; copy-in references instead.
- **A rules DSL in frontmatter.** §3.2 — judgment encoded as config is scoring with
  extra steps.
- **Ephemeral runs; a standing singleton task per playbook; state in the playbook
  file.** §4.4.
- **Copying the brief into the prompt or the run task.** §4.2 — the drift argument
  that rejected prompt composition in dispatch.
- **`queue_pinned` boolean for anchors.** §5.2 — needs a clearing rule or freezes;
  provenance is already the truth.
- **Reason-at-drag-time.** §5.4 — wallpaper, explains the wrong task, cannot see the
  new queue; superseded by post-hoc objection (task-211 log entries 5→6).
- **A blocking confirmation on reorder.** §5.4 — reintroduces removed friction.
- **A check call per drop.** §5.4 — latency and money per drag; debounced burst
  instead.
- **A run task per reactive check.** §5.4 — a corpus of non-events.
- **Mechanical verb enforcement now.** §6.2 — no per-run identity exists; a fake
  boundary weakens real ones. Reopen on first observed violation.
- **Playbooks naming runners or groups.** §6.3 — repo content choosing machine spend;
  difficulty is the abstraction that keeps gate 2 intact.
- **An MCP `playbook_run` tool.** §7.1 — an agent-callable dispatch trigger.
- **Scheduled/recurring playbooks in this pass.** §8 — deferred with a reopen
  condition and a named landing zone (task-078 authorization).
- **Groom closing stale tasks as `wont_do`.** §5.1 — intent is the human's to
  declare; the playbook surfaces, a human decides.
- **A parallel "suggestion" mode for reorder (propose-only).** Deliberately *not*
  built as a separate mode: optional review plus recorded reasons plus anchors is
  strictly more useful, and a propose-only mode would resurrect the §12 clause Jeff
  overturned. A human who wants to preview a reorder reads the run task before the
  run is dispatched — or reverts moves, which are cheap by design.

---

## 10. Decisions

- **P1.** The term is **playbook**. §2.
- **P2.** Playbooks are per-project repo files — `playbooks/<name>.md`, markdown with
  YAML frontmatter, configured by `playbooks_directory`. References ship in the
  package and are copied in by `playbook init`, never run implicitly. §3.
- **P3.** **A batch run is a task.** `target: project` creates a run task from
  frontmatter and dispatches it; `target: task` dispatches the target itself with the
  playbook as brief. Reactive runs create no task and record on the tasks they check.
  §4, §5.4.
- **P4.** The dispatch entry pins `playbook` and `playbook_hash`; the brief is
  pointed at, never copied. §4.2–4.3.
- **P5.** `verbs:` and `gates:` are a declared contract audited from the record; the
  mechanical guarantees are the managed-verb surface and the human gates. No per-run
  enforcement until a violation is observed. §6.2.
- **P6.** Playbooks declare `difficulty` and never name runners, groups, or argv;
  selection stays machine-local on the existing ladder, with task-156 + the profile
  table as the path to durable mapping. §6.3, §7.2.
- **P7.** Gates by playbook: **groom** proposes then executes only a recorded human
  approval, and may close only as `duplicate`/`superseded`; **reorder** writes
  itself under three generalised bounds — reasons recorded on every move, triggered
  not continuous, human anchors sticky (ordinary anchors overridable only with stated
  grounds; strong anchors not overridable; anchors expire on material change);
  **flesh-out** writes the spec and parks the target at `human`/`review`. §5.
- **P8.** The reactive category exists. `queue-check` is debounced per human reorder
  burst (120s default, machine-local), runs batch mode with a spend ceiling, records
  objections on the objected tasks, surfaces a one-click undo/keep, promotes
  kept-over-objection positions to strong anchors, and **fails open**. §5.4.
- **P9.** Recurrence is deferred with a stated reopen condition; triggering remains a
  human act. §8.
- **P10.** MCP exposure is read-only (`playbooks_list`); running a playbook is
  human-gated on every surface. §7.1.

---

## 11. Relationship to other work

- **task-060 / task-074 (dispatch, auto-dispatch).** Playbooks instantiate through
  dispatch unchanged and add no authorisation surface. Auto-dispatch remains off
  everywhere; nothing here arms it.
- **task-081 / tasks 204–209 (queue program).** The queue verbs are the rails reorder
  rides; task-209 is the first, manual run of what `reorder` generalises, and its
  decisions (graded effort, ordering-is-not-grooming) are encoded here as the
  reference brief's rules. The anchor mechanism consumes `queue_move` provenance the
  program created.
- **task-207 (React queue list).** The objection surface (§5.4) lands on the list 207
  builds. 207 may ship first; the pointer note on 207 (that *why a task sits where it
  sits* needs surfacing) is resolved by this design's inversion — the surface shows
  objections when they exist rather than reasons always.
- **task-078 (agent loops).** Recurrence, when it reopens, lands on its authorization
  machinery; `run_task.acceptance` is the prepared hook for evaluable playbook runs.
- **task-080 / task-156 / task-177 (model policy).** Playbooks are named consumer #1
  of the difficulty → profile table and strengthen its reopen trigger 3. task-156 is
  the prerequisite for a playbook's difficulty being durable data; carried as
  `related` on the instantiation child.
- **task-188 (one-click dispatch authorisation).** The `playbook run` surfaces reuse
  its pattern verbatim; the design leans on its "what is checked vs what is claimed"
  distinction rather than restating it.

---

## 12. Derived implementation tasks

Six, in dependency order, created as drafts under task-211 and held until this design
is approved. Each leaves the system in a working state. The natural stopping point if
appetite runs out is after child 3: playbooks exist, are runnable, and the proving
case has run live — everything after is more playbooks and the reactive lane.

1. **task-214 — playbook storage, format, and read surfaces.** `playbooks_directory` in project
   config; the frontmatter model and validation (name/filename match, enums, run_task
   defaults); `agentjobs playbook list|show`; `GET /api/projects/{id}/playbooks`;
   MCP `playbooks_list`; the shipped reference playbooks and `playbook init` copy-in.
   Nothing executes anything.

2. **task-215 — instantiation: `playbook run`.** CLI, REST and GUI; run-task creation from
   frontmatter for `target: project`; playbook-dispatch of the target for
   `target: task`; the stub's one-line playbook pointer; `playbook` +
   `playbook_hash` in the dispatch entry; task-188 authorisation reused; every
   dispatch gate demonstrated to bind (a test dispatching a playbook with dispatch
   disabled, and one with an agent as the would-be authoriser, both refused).
   *Related: task-156.*

3. **task-216 — groom, authored and proven live.** `playbooks/groom.md` per §5.1; one real run
   against this backlog through the full propose → approve → execute cycle, evidence
   on the run task. The playbook analogue of what task-209 is to the queue program.

4. **task-217 — reorder, authored, with anchors.** `playbooks/reorder.md` per §5.2; anchor
   derivation from `queue_move` provenance exposed where the run can read it (the
   queue listing gains last-move actor); a live run against this backlog after
   task-209's manual pass, demonstrating anchor respect and recorded reasons.

5. **task-218 — flesh-out, authored and proven.** `playbooks/flesh-out.md` per §5.3; one live
   run against a genuinely thin task; the parked-at-review gate demonstrated.

6. **task-219 — the reactive lane: `queue-check`.** The debounced trigger on human queue-move
   bursts; the batch-mode check call with its spend ceiling; objection entries on
   objected tasks; the React notice with one-click undo/keep; the kept-over-objection
   record and its strong-anchor consumption by child 4's brief; fail-open
   demonstrated with dispatch disabled. Carries the same explicit constraint as the
   loops children: **do not start without Jeff's go-ahead** — it is the most new
   machinery and the only part that spends money on an everyday human gesture.

---

## Appendix: acceptance criteria coverage (task-211)

| Criterion | Where |
|---|---|
| sc-1 — what a playbook is, where it lives, what one contains, versioned in git | §1–§3 |
| sc-2 — instantiation durable, run-is-a-task defended (and its scoped exception argued) | §4, §5.4 |
| sc-3 — groom fully specified: propose-then-approve, close outcomes, never delete/rewrite, relationship to reorder | §5.1, §5 preamble |
| sc-4 — surfaces, runner/model interaction, recurrence decided | §7, §8 |
| sc-5 — implementation children derived, held as drafts | §12 |
