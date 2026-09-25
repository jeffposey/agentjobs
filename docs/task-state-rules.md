# Task state rules

This is the design pass for task-603, written on 2026-09-25. Nothing in it is built yet.
It covers two things: what each task state means, and which rules keep a task from being
written into a state that contradicts facts the server already holds. The follow-up tasks
at the end were proposed here to be filed after the owner's review; no step did that, and
they were filed by hand as epic task-609 on 2026-09-25 (task-617 is why).

## The finding that shapes everything else

Schema v2 already refuses a class of broken states. An open task must have a ball, and a
handoff must carry its ask. **Every wrong state observed since then has a different
shape: the record says something is happening, or is waiting on something, and a fact
the server holds says otherwise.** Four examples:

- A ball of `agent/work` with no run, no walk and no finish reads "Working".
- `external/dependency` naming a task that nothing is actually waiting on reads
  "Blocked".
- An epic parked on a child while a sibling is claimable.
- A Dispatch button for a task that dispatch will refuse.

The inventory explains why this keeps happening. `task_status()`
(`models_v2.py:2327`) already derives most labels from live facts: Landing from a live
finish, Queued and Starting from the dispatch queue, Walking from an open walk, Blocked
from unmet `needs`, Error from a cycle, Quota from the newest handoff. **Working is the
exception.** It derives from `ball = agent` alone. Separately, three pieces of dispatch
code *write* a dependency wait, each choosing the blocker by its own heuristic:

- the walk's `record_walk_outcome` (`dispatch/epic.py:2641`);
- the handback resolver (`dispatch/handback.py:897`);
- the attention retraction sweep (`retraction.py:225`).

Nothing un-writes any of these when the fact changes.

So the rethink is small, and it is one sentence:

> **The record stores what someone decided. Whatever is happening, or whatever a task is
> waiting on, is derived on read from the facts, and nobody can write it.**

The four state axes stay. What changes is which values mean what, who may write them, and
the one thing that has to be true before "Working" is ever said.

## 1. What was found

### Every writer of the state axes

Every live write of `lifecycle`, `ball`, `ball_reason`, `ball_prompt` and `outcome` goes
through a `TaskManager` verb. The importer and the v1 migration are the only exceptions.
`SqlTaskStore.mutate_task` re-validates the six model rules on every write. **No verb
checks whether a run, walk or finish is live.** `claim` is the only verb that reads any
facts at all, and it ignores the current ball.

| Writer | Writes | Reachable contradiction today |
|---|---|---|
| `claim_task` (`manager.py:1646`) | active, `agent/work` | A claim over MCP or REST with no run is "Working" with nothing running. It silently overwrites a `human/*` or `agent/hold` ball on a ready task (task-259). |
| `handoff` (`manager.py:1716`) | any ball | Checks only `is_open`, so all of these are writable: `agent/work` on draft or ready; `agent/available` on active with an owner; `human/*` on ready; `external/dependency` with nothing unmet. REST accepts `agent/available`; MCP refuses it. |
| approve / resume / answer / request-changes / redirect (`api/routes/tasks.py:975–1437`) | `agent/*` | None of them checks lifecycle or the current ball. Approving a draft gives draft `agent/work`, which can never be claimed. Resume works on a task that is not on hold. |
| `release_task` | ready, `agent/available` | Allowed while a live run still holds the task. |
| `close_task` | closed | Closes an epic that still has open children, and closes a task under a live walk. The survey below found one orphan. |
| `create_task`, `update_task(parent=…)` | — | An open child can be created under a closed parent. Adding a `needs` edge to an active task leaves it "Working". |
| Walk `record_walk_outcome` (`epic.py:2578`) | epic → `external/dependency`, `human/decision`, `human/review`, `agent/work` | Parks the epic on a child while a sibling is claimable. `_resume_parent` flips it back to `agent/work` with no run. `already_waiting_on` matches by substring, so `task-1` matches a prompt that names `task-12`. |
| Handback `record_handback` (`handback.py:825`) | `external/dependency` | Parks the task on its *own* unmet `needs` even when it is an epic whose children are claimable. Its prompt promises the task "becomes workable again" automatically, but nothing ever moves the ball back. |
| Retraction `_correction` (`retraction.py:211`) | `external/dependency` or `agent/available` | Names a successor child that is "live" only because it is claimed, which is task-596's fix. On a claimed epic it writes active with `agent/available`. |
| Runner `_finish_session` (`runner.py:4460`) | `human/decision` only for FINISHED_WITHOUT_HANDOFF | A run that is interrupted, cancelled or crashed on the poll path leaves `agent/work` standing. A stall or a controller escalation may catch it later. |
| Finish `escalate_on_record` (`finish.py:2928`) | `agent/work` before dispatching | If the park that follows fails, `agent/work` persists with no run. |
| Importer (`sqlstore/importer.py:405`) | any valid state, verbatim | Anything that passes the six model rules. |

### Every surface that works out a state for itself

The chip is clean. Since task-562 every chip draws the server's `display_status` and
`status_category`, so **task-563's three answers are down to one for the label.**
These places still compute from the raw axes:

| Surface | What it derives | How it can disagree with the chip |
|---|---|---|
| `DependencyState.tsx:36` `reasonsFor` | The reason line under the chip | It re-implements `task_status`'s order and has no draft branch. A Draft or Queued epic gets told to "claim it to supervise". |
| `TaskList.tsx:245` `matchesStatus` | The status filters | "Blocked" means `ball = external`, so it misses every task Blocked by an unmet `needs`. "Working" means lifecycle active, which includes Needs review, On hold and Landing. |
| `TaskDetail.tsx:303` `verbsFor` | The review panel's heading and verbs | Any human reason it does not know falls through to "Approve — agent may merge". |
| `TaskDetail.tsx:1135`, `DispatchPanel.tsx:391` | Whether Dispatch is offered | Offered on a task that is Blocked or Error. The server refuses at claim, *after* it has written the admission and the authorising entry (task-563). |
| `QueueDispatch.tsx:108` (slot board) | Whether Dispatch is offered | Offered for anything claimable, and claimable ignores the ball. So it is offered on `ready` + `agent/hold`, which the server refuses as `task_on_hold`. |
| `LiveRuns.tsx:250` `runCounts` (NavStatus) | The blue "working" dot | Counts parked, silent and finished runs as working. |
| `analyticsSecondSet.ts:266` `stuckBand` | Analytics buckets | Unmet `needs` are counted as "The queue", and a hold as "With an agent". |
| Jinja `status_badge.html` | Colour from raw fields, word from a bare `Task` | It never says Blocked (unmet), Error, Queued, Landing or Walking, and its colours are not `status_category`'s. |

### The live store, surveyed 2026-09-25 about 20:25 UTC

There were 605 tasks in `agentjobs` and 169 of them were open. The rules proposed below
would flag these:

- **task-555: active, `agent/work`, reading "Working", with no run, walk or finish.** It
  has been in that state since the owner approved it at 19:27 UTC. The approve wrote
  `agent/work` onto an epic that has no branch, and nothing started.
- task-414, task-524 and task-167: each is `external/dependency` with no unmet `needs`.
  - task-414 is waiting on evidence.
  - task-524 is parked until a date.
  - task-167's ask was rewritten by the retraction sweep to "waiting on task-169", with
    no edge behind it.
- task-156 is open under task-080, which is closed.

Nothing else is flagged: no `agent/available` on an active task, no agent-work reason on
a draft or ready task, no human ball on a ready task, no closed task with an owner. The
rules below are mostly already true of the data. What is missing is the refusal, not a
clean-up.

## 2. The states

A task's state has two layers.

- **Stored intent** is what a person or an agent decided. It is written only by the
  manager verbs.
- **Derived activity** is what is happening right now. It is computed on every read from
  facts the server holds, and there is no field or verb that writes it.

### Stored

| Axis | Values | Means |
|---|---|---|
| `lifecycle` | `draft` · `ready` · `active` · `closed` | Whether the task is specified (draft vs ready), claimed (active), or over (closed). |
| `ball = human` + reason | `spec` · `review` · `plan` · `decision` · `approval` · `input` | A person has an open ask. The one stored state that demands attention. |
| `ball = agent` + reason | `available` | Unclaimed; any eligible agent may take it. |
| | `work` · `revise` · `answer` · `redirect` | The claimant is expected to act, and the reason says what the person meant when they sent it back (task-231). **This is an expectation, not a claim that anything is running.** |
| | `hold` | A person stopped it. The release condition is in `ball_prompt`. |
| `ball = external` + reason | `service` | Waiting on a third party the server cannot see: a vendor, credentials, quota. |
| | ~~`dependency`~~ | **Retired from writes (rule S4).** Waiting on another task is a `needs` edge. |
| `outcome` | `completed` · `cancelled` · `superseded` · `duplicate` | How it ended. |
| `needs` edges | `dependencies[].type = needs` | What it cannot start without. This, and not the ball, is how a task waits on another task. |

### Derived: the one label a reader sees

These are the labels `task_status()` already returns. The two changes this design
proposes are in bold. **A live holder** means any one of these:

- a non-terminal run attempt, admitted or live (`ExecutionStore.live_attempts`);
- an open walk (`supervision.state = walking`);
- a live finish (`finish_status.live_finishes`).

An interactive session counts once it has registered its run, which ALLAGENTS.md already
requires.

| Label | Category | Derived from, in precedence order |
|---|---|---|
| Completed / Cancelled / Superseded / Duplicate | closed / closed_unfinished | `lifecycle = closed` and its `outcome`. |
| Landing | finishing | A live finish. |
| Error | needs_you | A `needs` cycle. |
| Needs spec / review / plan approval / decision / approval / input | needs_you | `ball = human` and its reason. |
| **Needs review** or **Needs decision** on an epic | needs_you | **§4: every child closed, or the last walk stopped for cause. Derived for an epic and not written.** |
| On hold | not_now | `agent/hold`. |
| Blocked | not_now | Unmet `needs`. Nothing else produces it (S4). |
| Quota / Blocked | not_now | `external/service`, split on whether the wait clears by itself. |
| Draft | draft | `lifecycle = draft`. |
| Walking | working | An open walk that is not grounded. |
| **Waiting** | not_now | **An open walk grounded on a child a person holds. This is the walk vocabulary's existing word, now also the epic's chip, and the reason line names the child.** |
| Working | working | **`ball = agent`, lifecycle active, and a live holder.** |
| **Stalled** | needs_you | **`ball = agent` with an agent-work reason, lifecycle active, and no live holder.** Nothing happens until someone dispatches it or releases it, so it is red, and Dispatch is offered. |
| Queued / Starting | queued | A dispatch waiting for a slot. |
| Ready | ready | Otherwise. |

**Stalled is the whole fix for "Working with nothing running".** Such a state cannot be
refused at the moment it is written. Approve, answer and request-changes all write
`agent/*` a moment before the handback dispatch admits a run, and a run can die at any
time without writing anything. So the label must not claim more than the facts support.
Once Working needs a live holder, "Working with nothing running" is no longer
something any surface can say. The same rule makes a crashed run show immediately,
instead of waiting for the stall detector.

## 3. The rules

Every rule says where it is enforced:

- **model**: refused on every write by `Task._check_consistency`, through
  `mutate_task`.
- **verb**: refused by the manager verb before it writes.
- **read**: derived on read, so the contradiction has no representation.
- **check**: a cross-record fact that time or another record can falsify. It is reported
  by `agentjobs state check` (§6).

### Single-record rules (model)

The six rules that exist today are S1 to S6 and are unchanged.

| # | Rule | Enforced | New? |
|---|---|---|---|
| S1–S6 | ball iff open; reason in the holder's vocabulary; outcome iff closed; prompt unless `agent/available`; owner tracks lifecycle; queue position iff open | model | existing |
| S7 | `agent/available` ⇔ lifecycle `ready`. | model | task-259 |
| S8 | `agent/work`, `revise`, `answer` and `redirect` ⇒ lifecycle `active`. | model | task-259 |
| S9 | `draft` ⇒ `ball = human`. A draft is by definition waiting on a person for its spec or a decision. | model | new |
| S10 | `agent/hold` is allowed on `ready` and `active`. A held ready task is not claimable (V1). | model + verb | task-259, decided |
| S11 | `closed` ⇒ `assignment.owner` empty, and `archived` ⇒ `closed`. | model | task-254's rule 5 extension |
| S4′ | `external/dependency` is not a writable value. It stays readable so that old log entries still load. | model (new writes) | new |

### Verb preconditions (verb)

Each precondition is a named function. The verb calls it, and so does the `offers` field
(§5). **The button and the refusal are then the same code.**

| # | Verb | Refused when |
|---|---|---|
| V1 | claim | The ball is not `agent/available`. That covers a hold, a person's ask, or a third-party wait. Also refused on unmet `needs`, which is checked today. |
| V2 | handoff | The target breaks S7 to S9, which falls out of the model. The target is `external/dependency` (S4′). The target is `agent/*` on a task with open children (§4). |
| V3 | approve, request-changes, answer, redirect | The ball is not `human`, or it is not at the gate the page showed. The last part is checked today for plan versus final only. |
| V4 | resume | The ball is not `agent/hold`. |
| V5 | release | A live holder holds the task. Stop the run first; releasing under it hands a task that is being edited to the pool. |
| V6 | close | The task has an open child. Close or re-parent the children first. |
| V7 | create, and `update_task(parent=…)` | The parent is closed. |
| V8 | dispatch | Any of: closed; draft; `ball` not `agent`; hold; unmet `needs` or a cycle. **All are checked before the admission and the authorising entry are written.** Today unmet `needs` is found only at claim, after both (task-563). |
| V9 | walk retry of a child | The child is closed. A child closed `completed` counts as landed, and any other outcome is a stop for cause. The walk reads this from the child's record, never from how its run ended. task-596 shipped this; it is listed here so it becomes a rule rather than a patch. |
| V10 | `update_task` | The patch names anything outside the content allowlist. This is task-254's half, and a prerequisite: without it every rule above can be bypassed from Python. |

### Cross-record rules (read, then check)

| # | Rule | Enforced |
|---|---|---|
| X1 | Nothing reads Working without a live holder. | read (Stalled) |
| X2 | Nothing reads Blocked without an unmet `needs` edge or a third-party wait. | read, once S4′ has retired the other writers |
| X3 | No epic reads Blocked or Waiting while one of its children can be claimed. | read (§4) |
| X4 | No open task sits under a closed parent. | verb (V6, V7). **check** covers records from before the rule; task-156 is one today. |
| X5 | No live holder sits on a closed task, and no open walk sits under a closed parent. | check. A run can outlive its task's close. |
| X6 | No `needs` cycle. | read (Error). Existing. |

## 4. Epics

**No dispatch code writes an epic's ball.** An epic here means a task with open
children. These are the only things that write its ball:

- claim, which the walk uses to take the supervisor's seat;
- release;
- a person's own handoff to a `human/*` ask.

An epic's label is derived from its walk and its children, in this order:

1. An open walk that is not grounded reads **Walking**.
2. An open walk grounded on a child that a person holds reads **Waiting**, and names the
   child.
3. With no open walk, if any child can be claimed, the epic reads **Ready**, and Dispatch
   starts a walk.
4. With no open walk, if the newest walk stopped for cause, the epic reads **Needs
   decision**. The reason line takes its detail from the walk row.
5. If no open child remains, the epic reads **Needs review**. What remains is judging the
   parent's own acceptance criteria, and Approve closes the epic.
6. Otherwise every open child is blocked or held by someone else, and the epic reads
   **Blocked**, with the reason line naming the children.

A person's stored `human/*` ask outranks all of these, and so do Landing and closed.

What this removes:

- the walk's four writes to the parent's ball (`epic.py:2641`, `:2651`, `:2667`,
  `:2710`);
- `_resume_parent`;
- `waiting_child` as something that writes;
- the epic half of `retraction.py`.

That last module exists to sweep away stale asks that the walk wrote, which is "hoped
for" enforcement: a sweep repairing a write that should not have happened.
`attention` then needs to count derived epic asks (4 and 5) in its waiting set. That is
the one real cost.

**Rejected: let the walk write exactly two asks, `human/review` when every child has
landed and `human/decision` on a stop.** It is smaller, but the asks go stale the moment
a child is filed or reopened. Keeping them current is exactly what `retraction.py` does,
and task-555's wrong state came out of that module (entry 14).

## 5. Surfaces read the answer; they do not work it out

**Rule: a surface never computes a label, a filter or a button's availability from the
raw axes.** Two server fields make that possible:

- `status_reason`: the line under the chip. It comes from the same `task_status()` call,
  so it can never disagree with the chip. This retires `reasonsFor`.
- `offers`: for each verb (claim, dispatch, approve, request-changes, answer, resume,
  release, promote, close), either `ok` or the refusal the verb would give. It is
  computed by V1 to V8. **A button is drawn from `offers`, and nothing else**: the task
  page, the slot board, and the Jinja pages while they last.

The filters follow the same rule. The TaskList filters select on `status_category`,
`runCounts` counts run health's own `working` key, and `stuckBand` uses the category.
The Jinja badge takes the read model's label and category, or is deleted with the rest
of the legacy UI.

## 6. Enforcement: why the live-store check is not a gate stage

The spec proposed checking the invariants "as a gate stage over the live store". **I
recommend against that one piece, and for the same thing aimed at the code instead.**

- **The gate verifies a branch.** A stage that reads the owner's live store turns red
  because of a record the branch never touched. ENGINEERING.md already ruled that out
  for the roadmap files ("neither roadmap file is a branch's business"). A red gate that
  a branch cannot fix gets skipped within a week.
- **What the gate should prove is that no code path can write a broken state.** That is
  a property test in the pytest stage. It runs random sequences of verbs, walk ticks,
  run deaths and finishes over a seeded store, and asserts S1 to S11 and X1 to X6 after
  every step. It fails on the branch that introduces the path, which is where the fix
  belongs.
- **The live store gets `agentjobs state check`.** It reports every X-rule violation,
  names the record, and repairs nothing. The poller runs it each tick and counts
  violations into the dashboard's attention set, the way `queue check` does for order.
  `--strict` exits non-zero, for a person or a scheduled job to use.

## 7. The observed failures, mapped (ac-3)

| Failure | What happened | Rule that stops it |
|---|---|---|
| task-555 #1 | The walk treated a child closed `completed` as a failed start, and ended. | **V9.** A closed child is read as landed or ended, never retried. Fixed in task-596; it becomes a rule. |
| task-555 #2 | The resolver parked the epic `external/dependency` "waiting on task-001" while two children were ready. It read Blocked, and Dispatch vanished. | **S4′** (the value can no longer be written), **§4** (no dispatch code writes an epic's ball), **X3** (with a claimable child the epic reads Ready and Dispatch is offered). |
| task-555 #3 | An agent set the epic to `agent/work` with nothing running. It read Working. | **X1 / Stalled.** It would read Stalled, red, with Dispatch offered. **V2** refuses `agent/*` onto an epic by hand; claim or release is the verb. |
| task-555, now | The approve wrote `agent/work` on an epic with no branch, and it has read Working since 19:27 UTC. | **§4.5.** Approve on an epic with every child closed closes it. **X1** would have shown it as Stalled meanwhile. |
| task-563 | `ready` + `agent/available` with an unmet `needs`. Three surfaces gave three answers, and Dispatch was offered and then refused after admission. | The label was fixed by task-562. The rest: **`offers`** (§5) means Dispatch is not drawn; **V8** refuses before admission; **`status_reason`** retires the sidebar's own derivation. |

## 8. Related tasks (ac-4)

- **task-259 is absorbed.** Its rules become S7, S8 and S10, plus V1 (claim refuses a
  hold), V3 and V4 (the human routes check their gate), and V8 (dispatch refuses a draft
  and a human ball). One point it left open is decided here: a hold on a `ready` task is
  allowed, and it makes the task unclaimable. task-527 is held that way today, at the
  owner's direction.
  Close task-259 as superseded by the follow-up that implements S7 to S11.
- **task-254 stays separate but goes first.** Its content allowlist is V10. Without it,
  `update_task` writes any axis from Python and every rule here is a convention. Its
  reopen verb is independent.
- **task-263 stays separate.** It is about who may write, the actor's kind, not what may
  be written. The one overlap is an agent writing `human/approval`, which is 263's to
  refuse.
- **task-564 builds on this.** Its "waiting on a dependency" is the derived Blocked. When
  the edge clears, an active task becomes Stalled and a ready one becomes Ready.
  Dispatching it automatically at that moment is 564's feature.

## 9. Migration

Five records need a decision when the rules are switched on. `agentjobs state check`
reports them first, and the migration names each one in a `decision` entry.

- task-414 and task-524 move `external/dependency` → `agent/hold`, with their release
  conditions (a date, some evidence) kept in `ball_prompt`. A person releases them. That
  is what "parked by the owner's choice" already means, and it is the precedent: task-527
  was corrected from `external/dependency` to `agent/hold` on 2026-09-22 for exactly this
  reason, because the owner asked for On Hold and got Blocked.
- task-167 is an epic. Under §4 its ball moves to what its claim left, `agent/work`, and
  its label derives from its children.
- task-156 is open under closed task-080. Re-parent it or close it; the owner decides.
- task-555 is closed or released by the owner. That can happen today, independent of
  any of this.

## 10. Proposed follow-ups, in order

None of these was filed with the design. They were filed by hand as epic task-609 after the approval, because nothing else would have filed them; task-617 makes a design file its own before review.

1. **task-254**, the existing task: the content allowlist (V10). This comes first.
2. **Model rules S7 to S11 and S4′**, with the migration in §9 and the property test in
   §6. task-259 closes as superseded by this one.
3. **The live-holder fact and Stalled** (X1). This is one function, used by
   `task_status` and by `offers`.
4. **Retire the `external/dependency` writers**: handback, the walk, and retraction's
   non-epic path.
5. **Derived epic status (§4).** The walk stops writing the parent's ball, `attention`
   counts the derived asks, and retraction's epic path is deleted. Depends on 3 and 4.
6. **Verb preconditions V1 to V9 and `offers`**, with every button drawn from `offers`.
7. **`status_reason`, and the remaining client derivations**: the filters, NavStatus,
   analytics, the Jinja badge.
8. **`agentjobs state check`** and its poller count. This can run alongside 2.

Steps 3 and 4 can run in parallel. The others are in dependency order, so the natural
home for all eight is one epic.
