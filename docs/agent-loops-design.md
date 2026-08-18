# Agent loops: making acceptance criteria executable

**Status:** design pass, task-078. Nothing here is implemented. Derived implementation
tasks are listed in §12.

**Depends on:** [agent-dispatch-design.md](agent-dispatch-design.md) §2a, which states
decision D5 (bounded autonomy) and names this document as the place its mechanism is
designed.

---

## 1. The claim, and whether it survives contact

The claim this task was created to test:

> Anyone can write `while true: claude -p`. What nobody has is a loop that knows when it
> is done, knows whether iteration 12 improved on iteration 11, and leaves a record you
> can audit next week.

It survives, narrowly, and the narrowing is the useful part of this document. AgentJobs'
contribution to agent loops is **not** running them. It is that a task already carries
the two things a loop needs and a chat window does not: a stated definition of done
(`acceptance[]`) and a history that outlives the process that wrote it (`log[]`).

What is missing is that `acceptance[]` is prose. A loop cannot read prose and decide it
has finished. Everything in this document follows from closing that one gap.

### The honest counter-argument, addressed first

Most of what gets credited to "agent loops" is the **inner** loop — write, run the tests,
read the failure, fix, repeat — and every agent CLI already does that inside a single
session, for free, and *better* than an outer loop can, because it keeps its context
while doing it. An outer loop that re-dispatches a fresh agent after every iteration
throws away everything the previous iteration learned and pays full price to rebuild it.

So the outer loop buys exactly three things, and it is worth being precise because
everything else attributed to it is the inner loop wearing a costume:

1. **Survival past context exhaustion.** The inner loop stops when the session's context
   fills up. That is the most common way a long piece of work actually ends, and it ends
   with the work unfinished and the agent unable to say much about why. An outer
   iteration starts a fresh session whose memory is the task record — which is the
   resumption contract this project already enforces for human handoffs, applied to a
   machine. **This is the big one, and it is specific to AgentJobs having a durable
   record.**
2. **A termination condition the agent does not control.** Inside a session, the agent
   decides it is done. That is the same agent that decided its work was correct. A check
   deciding is the difference between "the agent believes it has finished" and "the
   criteria pass".
3. **An audit trail that outlives the process**, which is the difference between "it
   worked when I watched it" and evidence.

**The scoping consequence, stated as a rule:** an outer iteration is only worth starting
when the inner loop has genuinely stopped — the session ended with criteria still
failing. Re-dispatching while an inner loop could still make progress is paying to
forget. §8's design follows this: an iteration begins only after the previous run has
reached a terminal state.

---

## 2. What `verify` actually is today, measured

This task's own constraints say:

> `AcceptanceCriterion.verify` already exists in the schema and is unused. Prefer filling
> it over inventing a parallel mechanism.

**The premise is wrong, and it changes the design.** `verify` is not unused. In the
AgentJobs corpus as of 2026-08-18:

| | count |
|---|---|
| `acceptance[].verify` values present | 132 |
| Of those, shaped like a runnable command | ~36 |
| Of those, prose describing how a human checks | ~96 |

A representative sample of the prose form:

```
verify: Browser against a freshly started server, one draft and one non-draft, asserting…
verify: Show the test failing against the unfixed code, then passing.
verify: Mutate the task from another surface with the detail page open, then promote.
verify: Jeff runs the install; the manifest validation errors, if any, are recorded…
```

Those are *good* entries. They are instructions to a person about what evidence would
settle the criterion, and several of them describe things no command can decide.

This matters because it rules out the obvious design. Treating `verify` as executable
would reinterpret ~96 existing prose strings as commands. Any rescue — "run it only if it
looks like a command" — makes a **heuristic the security boundary**, and a heuristic that
decides whether to execute a string from a versioned file is not a boundary at all.

**Decision L1. `check` is a new field. `verify` keeps its meaning.**

```yaml
acceptance:
  - id: sc-1
    text: The double-claim race is closed
    verify: Two concurrent claims against one ready task; one wins, one is refused.
    check: ["poetry", "run", "pytest", "tests/test_concurrency.py", "-q"]
    status: pending
```

`verify` is what a person should do. `check` is what a machine may run. They coexist on
the same criterion, and a criterion with both is the best-documented kind: the command
says whether it passes, the prose says what passing is supposed to mean.

*Rejected: repurposing `verify`.* Ninety-six prose values would silently become
commands, and the field would then mean two different things depending on a guess.

*Rejected: restructuring `verify` into `{text, check}`.* Cleaner on a blank page, and a
breaking schema change to 132 existing values across the corpus for no behaviour a new
optional field does not give.

---

## 3. `check` is argv, never a shell string

**Decision L2. `check` is a list of strings, handed to `subprocess` as a list.**

The same discipline as dispatch, for the same reason and with the same wording: argv is a
list because there is no shell anywhere in this subsystem. A criterion whose check
contains a semicolon, a backtick, a `&&` or a newline is one argument containing those
characters, not two commands.

The cost is real and worth stating: `["poetry", "run", "pytest", "tests/x.py", "-q"]` is
uglier than `poetry run pytest tests/x.py -q`, and every author will notice. The
alternative is worse.

*Rejected: a string split with `shlex`.* The parse then becomes the security boundary,
and `shlex` is POSIX-shaped: it does not describe how Windows actually splits a command
line, which is the platform this runs on. A quoting rule that differs between the machine
that wrote the task and the machine that runs it is a vulnerability with a plausible
cover story.

### Where it runs, and with what

| | |
|---|---|
| **cwd** | The project root, from the registry. Not a worktree: a check reports on the tree as it stands, and the loop driver evaluates *after* an iteration's run has ended and its work is committed. |
| **env** | Inherited, plus `AGENTJOBS_TASK_ID`. No secrets are injected — a check that needs a credential is a check that should not be in a versioned file. |
| **timeout** | 300s per check, 900s for a whole evaluation pass. A check is meant to be a cheap oracle; one that takes longer than five minutes is not cheap and the loop it gates will not converge in a useful time either. |
| **exit code** | `0` → `met`. Non-zero → `failed`. Timeout → `failed`, with the timeout named. A check that cannot be started at all → `failed`, not skipped (see below). |
| **output** | stdout and stderr captured, tail of 40 lines retained on the result. Same limit as a dispatch run's failure body, for the same reason. |

**A check that cannot run counts as failed, never as skipped.** A missing interpreter, a
deleted test file, a typo in argv — all of them mean the criterion is not demonstrably
met, and "not demonstrably met" is what `failed` means here. Treating it as skipped would
let a loop converge by breaking its own oracle, which is the single most valuable thing
to make impossible.

---

## 4. Security: a check is arbitrary code from a versioned file

sc-3, and the question that most shapes the answer. `tasks/*.yaml` is committed. Cloning
a repository would therefore hand the cloner a file full of commands, and any mechanism
that runs them automatically turns `git clone` into remote code execution.

**Decision L3. Checks run only for projects already enabled for dispatch, under the same
four gates task-068 defines, and never as a side effect of reading anything.**

That is not a new gate vocabulary, and deliberately so. The argument is short: enabling a
project for dispatch already authorizes AgentJobs to start an autonomous coding agent
with write access in that repository. A check is *strictly less* dangerous than the thing
you already said yes to. Inventing a second, weaker permission for the smaller capability
would be ceremony that reads as security.

Concretely, three properties:

- **Never on read.** Loading a task, listing tasks, rendering the GUI, the API's normal
  request path — none of these evaluate a check. Ever. A check runs only when something
  explicitly asks it to.
- **Never implicitly.** The two things that ask are an explicit human command
  (`agentjobs check <task-id>`, or the equivalent button) and an authorized loop
  iteration. Both trace to a human act, exactly as a dispatch does.
- **Gated per project, machine-locally.** `assert_dispatch_permitted(project_id)` must
  pass. A cloned repository on a machine that has not enabled it for dispatch cannot run
  a single check, whatever its task files say.

*Rejected: an allow-list of permitted check executables* (`check_allow: [poetry, npm,
pytest]`). It reads like defence in depth and is not. `poetry run <anything>` and `npm
run <any script defined in the repo's own package.json>` are both on any plausible
allow-list and both execute arbitrary repository-supplied code. A control that stops
nothing an attacker would do, while creating the impression of a boundary, is worse than
its absence — people relax the real gate because the fake one is there.

*Rejected: keeping checks in machine-local config instead of the task file.* It would be
airtight, and it destroys the feature. The value of an executable criterion is that it
travels with the task — a different agent, on a different day, evaluates the same
definition of done. Machine-local checks are a private config file that happens to
mention task ids, and nobody would maintain it.

**Stated plainly so nobody is surprised:** after this ships, enabling dispatch for a
project means task files in that project can run commands on this machine. That was
already true — an agent it dispatches can run anything — but it is now true through a
second, quieter path, and someone reviewing a pull request that adds a `check:` line
should know they are reviewing a command, not a comment.

---

## 5. Who may set a check

An agent that writes its own passing check has graded its own homework, and that is the
failure mode this whole subsystem exists to prevent — an autonomous loop with a
termination condition it controls is not bounded by anything.

The obvious rule is "only humans may set `check`". It does not survive contact: the agent
writing a task usually *is* the thing that knows the right test command, and a human
retyping `poetry run pytest tests/test_concurrency.py -q` adds no judgement, only
friction. A rule that is pure friction gets worked around.

**Decision L4. Anyone may write a check. Authorizing a chain freezes the checks as they
stood at that moment, and any later change stops the chain.**

The authorization record stores a digest over the criteria's `(id, check)` pairs. Every
iteration recomputes it. A mismatch means the definition of done moved after a human
agreed to it, and the chain stops with `ball: human` and the diff named.

This is better than a write restriction on three counts. It permits the useful case (an
agent proposing the obvious command) while making the dangerous case (an agent
*changing* the command mid-loop to one that passes) not merely prohibited but
structurally ineffective. It also catches the honest version of the same accident — a
task edited from another surface mid-chain — which a write restriction would not.

Two supporting rules:

- **Changing a criterion's `check` resets its `status` to `pending`.** A status
  established by a command that no longer exists is not evidence.
- **A chain whose checks all pass at authorization time is refused.** There is nothing to
  converge on, and the only thing such a loop can do is change something that was
  already correct.

---

## 6. Logging: one entry per iteration, carrying the whole vector

The question as posed had two bad answers. Per-criterion-per-evaluation is honest and
unreadable — a 20-iteration chain over 7 criteria writes 140 log entries into a file a
human is supposed to read. On-change-only is readable and hides flapping, which is
precisely the signal that tells you a loop is not converging.

**Decision L5. One `check_result` log entry per evaluation pass, carrying every
criterion's result as a vector.**

```yaml
- id: 24
  ts: '2026-08-18T14:02:11Z'
  actor: dispatcher
  type: check_result
  re: 19                       # the chain authorization entry
  body: 'Iteration 3 of 5: 2 of 3 checks pass.'
  data:
    chain_id: chain_9f3a2b1c
    iteration: 3
    results:
      - id: sc-1
        status: met
        exit_code: 0
        duration_seconds: 12.4
      - id: sc-2
        status: failed
        exit_code: 1
        duration_seconds: 8.1
        output_tail: "FAILED tests/test_loop.py::test_regression_guard"
      - id: sc-3
        status: failed
        exit_code: 1
        duration_seconds: 0.3
    unchecked: [sc-4, sc-5]
```

That is 20 entries for a 20-iteration chain: readable, and complete enough that flapping
is visible by reading consecutive vectors. Neither horn of the dilemma, because the
dilemma assumed one entry per criterion.

`check_result` joins `dispatch` and `dispatch_result` in the set of entry types
`add_log_entry` **refuses** (dispatch design §2a, "the rule is only as good as the
evidence it reads"). It is written by the evaluator as a side effect of a real
evaluation, never by a caller, for exactly the reason a `transition` cannot be posted by
hand: a result that did not accompany a real run is a lie, and this one is a lie a loop
would act on.

---

## 7. Mixed evaluable and prose criteria

sc-5, and the common case — the corpus measurement in §2 says most criteria will never
have a check.

**Decision L6. The loop converges on the checked criteria and hands the rest to a
person. It never marks an unchecked criterion met.**

| Situation | Behaviour |
|---|---|
| No criterion has a check | The chain is **refused**, not capped. §2a already requires this: a loop with no evaluable termination condition is an unbounded spend. |
| Some have checks | The chain's termination condition is *all checked criteria pass*. Unchecked ones are carried, reported, and untouched. |
| All checked criteria pass | The loop stops and hands off `ball: human`, `ball_reason: review` — **not** `outcome: completed`. |
| A checked criterion regresses | Stop immediately (§8). |

The third row is the important one. The loop finishing is not the task finishing. What
the loop has established is that the machine-checkable half of the definition of done now
holds; the prose half is exactly the part that required taste, which is why it was prose.
Handing it to a human at that point is not a limitation of the design — it is the design.
The `ball_prompt` on that handoff should say which criteria the loop settled and which it
never touched, so the person knows precisely what they are being asked to judge.

---

## 8. The loop: bounds, guardrails, and the numbers

D5 requires three bounds. Each is stated with a value and the reasoning for that value,
because a number chosen without a reason is a number someone raises at 2am.

### The three bounds

| Bound | Default | Ceiling | Why this number |
|---|---|---|---|
| **Iterations** | 5 | 20 | The realistic shape of a converging chain is *fail → fix → fail differently → fix → pass*. Five allows that plus one wasted turn. A chain that needs more than five is not converging, it is wandering, and the difference matters more than the extra attempts. |
| **Chain wall-clock** | 4 hours | 12 hours | Long enough for five iterations of a real run (§9's per-run timeout is 30 minutes) with room to spare; short enough that a chain started after dinner has stopped before morning. |
| **Per-iteration wall-clock** | Inherited from `limits.run_timeout_seconds` (1800s) | — | An iteration is a dispatch. It gets a dispatch's ceiling; there is no reason for a second number. |

### The guardrails

**Thrash detection: three consecutive iterations with an identical result vector stop the
chain.**

Three, not two. Two identical vectors is one flaky rerun plus one no-op fix, which
happens routinely and is not yet evidence of anything. Three is a pattern. Note that the
comparison is on the *result vector*, not on the diff: an agent that edits files busily
while every check keeps returning the same answer is thrashing, and looking at the diff
would call that progress.

**Regression guard: any criterion that was `met` and becomes `failed` stops the chain
immediately, with no retry.**

No grace period and no "try once more", deliberately. Going backwards is worse than
standing still, because it means the loop's model of what it is doing is wrong — and the
specific failure this catches is an agent breaking a passing check in order to make a
failing one pass, which looks like progress in the aggregate count and is the worst
possible outcome.

**Every stop is loud.** Each of these hands the ball to `human` with a `ball_reason` and
a `ball_prompt` naming which guardrail tripped, at which iteration, and what the vector
looked like. A chain that stops silently is indistinguishable from one still running,
which is the failure the whole dispatch subsystem is built against.

### The conflict with §7's budget caps, and how to resolve it

**This is a correction to the dispatch design, not an elaboration of it.** §2a currently
says:

> §7's caps now bind here. They were scoped to auto-dispatch (D3) because a human
> clicking is a decision; a loop is not clicking, so the per-task counts and cooldown
> apply to every iteration after the first.

Read literally, that makes the whole feature inert. The per-task-per-day cap is **3**. A
chain a human authorized for 5 iterations would be refused at iteration 4 by a limit
designed for a different mechanism, and the 60-second cooldown would insert a minute of
dead time between iterations for no reason — the previous iteration has already finished,
which is the condition the cooldown exists to guarantee.

**Decision L7. The per-day cap counts chains, not iterations. The iteration cap is the
loop's own bound. The lifetime cap keeps counting dispatches. The cooldown does not apply
within a chain.**

- **Per-task-per-day (3):** now counts *authorized chains*. Three separate chains against
  one task in a day is still the signal it was meant to be — something is wrong with the
  task — and it no longer truncates the mechanism it was supposed to bound.
- **Per-task-lifetime (10):** unchanged, still counting dispatches. It stays a true
  backstop: it is the number that catches a bug in the loop driver itself, and a backstop
  that is redefined to accommodate the thing it guards is not one. A chain that would
  cross it is refused mid-chain and stops loudly.
- **Cooldown (60s):** does not apply between iterations of one chain. It exists to stop
  two runs being started in the same breath; iteration *n+1* by construction begins only
  after iteration *n* has reached a terminal state.

§2a should be amended to say this. Left as written, the first person to implement D5
would discover the contradiction by watching a chain die at iteration 4.

---

## 9. Authorization: what a human actually approves

Dispatch §2's forgeability requirement applies unchanged and is the reason this section
is short: nothing about a chain may be read from a request.

A human authorizes a chain by naming, at that moment:

- the task,
- the maximum iterations (≤ the ceiling),
- the wall-clock ceiling,
- **implicitly, the checks as they stand** — recorded as the digest from §5.

That produces one `chain_authorized` log entry whose `actor` is the human. Every
iteration resolves it **from the stored task**, never from the request body, exactly as
`caused_by` is resolved at spawn time. A request that supplies its own authorization is
not evidence of anything.

Revocation is the kill switch and must be as blunt as `agentjobs dispatch stop`: one
command, no arguments beyond the chain id, effective before the next iteration, and
`~/.agentjobs/DISPATCH_DISABLED` stops every chain on the machine at once because a chain
iteration is a dispatch and the sentinel is checked immediately before every spawn.

---

## 10. Which loops are worth running

The discriminator, because without it this is indiscriminate automation:

**A loop pays off exactly where a cheap objective oracle exists.** Failing tests, a lint
error, a typecheck error, a benchmark threshold, a schema validation. In those cases the
check is not an approximation of done — it *is* done, and an agent iterating against it
is doing something a person would otherwise do by hand with worse patience.

**A loop does not pay off where "good" requires taste**, which is most feature work: API
shape, error message wording, whether an abstraction earns its place, whether a UI reads
clearly. There is no cheap oracle, the expensive oracle is a person, and the human review
step is not overhead in those cases — it is the only evaluation that exists.

Two concrete refusals fall out of this and belong in the implementation:

- A chain with no evaluable criterion is refused (§7).
- A chain whose checks already all pass is refused (§5) — nothing to converge on.

**Not designed here, noted so it is not re-derived:** generator/critic chains, where a
second agent supplies the oracle. That is a genuinely different mechanism — the oracle is
no longer cheap or objective — and it should not be smuggled in as "a check that happens
to invoke an agent". Scheduled or recurring loops are likewise a different and smaller
problem: cron with judgement, not convergence.

---

## 11. Rejected alternatives

Collected, including ones argued above, because the rejected list is the part of a design
document that stops the same conversation happening twice.

- **Repurposing `verify` as executable.** §2. Ninety-six prose values become commands.
- **A `verify` string parsed with `shlex`.** §3. The parse becomes the security boundary,
  and it is the wrong shell for the platform.
- **An allow-list of check executables.** §4. Stops nothing real; creates the impression
  of a boundary, which makes the real gate feel less load-bearing.
- **Checks stored machine-locally rather than in the task.** §4. Airtight and useless:
  the point of an executable criterion is that it travels with the task.
- **"Only humans may write a check."** §5. Pure friction on the useful case, and it does
  not stop the dangerous one as well as freezing the checks at authorization does.
- **One log entry per criterion per evaluation.** §6. 140 entries per chain.
- **On-change-only logging.** §6. Hides flapping, which is the signal.
- **Letting the loop close a task.** §7. The loop settles the checkable half. Closing is a
  judgement, and the prose criteria are the judgement.
- **A standing queue that drains itself.** Already refused by §2a and worth repeating:
  the authorization is per chain, per task. A loop that picks its own next task has no
  human-set termination condition, only a human-set appetite.
- **Retrying once after a regression.** §8. It converts the clearest possible signal that
  the loop is confused into one more chance to do damage.

---

## 12. Derived implementation tasks

Each independently reviewable, in dependency order. None of them is this document.

1. **Schema: `acceptance[].check` and the `check_result` log entry.** The argv field with
   validation (non-empty list of strings, no shell), the new entry type with a validated
   payload, `add_log_entry` refusing it, and a `status` reset when a check changes.
   Nothing executes anything.
2. **The check evaluator.** Run one task's checks: argv, cwd, timeout, capture, exit code
   → status, output tail. Gated behind `assert_dispatch_permitted`. Deliberately usable
   with no loop anywhere — evaluating a task's own definition of done on demand is worth
   having by itself.
3. **`agentjobs check <task-id>` and `POST .../tasks/{id}/check`.** The explicit human
   trigger for (2), writing one `check_result` entry. This is the point at which a person
   can get value from the feature, before any autonomy exists.
4. **Chain authorization.** The `chain_authorized` entry, the check digest, the bounds,
   resolution from the stored task, and revocation. No driver yet.
5. **The loop driver.** Iterate: dispatch, wait for terminal, evaluate, compare, decide.
   Thrash detection, regression guard, ceilings, and the loud handoff on every stop.
   Depends on 1–4.
6. **GUI.** Check results per criterion on the task page, a chain's iteration history,
   authorize and revoke. Revoke reachable in one click, per §9.
Numbered 1–6 above, they are **task-147** through **task-152**, each carrying its own
acceptance criteria and its `needs` edge to the one before it. Tasks 150 and 151 — chain
authorization and the loop driver — additionally carry an explicit constraint: *do not
start without the owner's go-ahead on building the outer loop at all*. The design being
finished is not the same as the decision being made, and an agent working the backlog
overnight would otherwise make it by default.

A seventh item, amending dispatch design §2a with L7, was done in this pass rather than
deferred: it corrects a rule that is already published, and leaving a known-inert clause
in a design document for someone to discover by watching a chain die at iteration 4 is
not a saving.

The natural stopping point if appetite runs out is after task 3: executable acceptance
criteria, evaluated when a human asks, with results on the record. That is useful on its
own, it is the whole schema and security surface, and it carries none of the autonomy
risk. Tasks 4–6 are the loop.
