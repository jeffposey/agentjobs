# Loop prompt: React phase 1 (task-095)

For Codex, or any agent that can run unattended for a while. Drives
`task-095-react-phase-1-foundation` and its three children to completion.

Paste the whole thing. **Set the authorization line in section 0 first** — the loop
behaves very differently depending on it, and it is the one thing this prompt cannot
decide for you.

---

## 0. Authorization — SET THIS BEFORE PASTING

```
MERGE_AUTHORIZATION = per-task-review
```

Choose one:

- **`per-task-review`** — the standing rule in ENGINEERING.md. You stop at the merge
  gate on every task and wait for a human. **Consequence: the loop completes exactly
  one task (083) and then stops.** It cannot reach 084 or 085, because
  `claim_task` refuses a task with unmet dependencies and those two need 083 *closed*,
  which needs a merge, which needs a human. This is the safe default and it is not
  really a loop.

- **`phase-gate-review`** — you may rebase, merge `--no-ff`, mark the branch merged,
  and `close` each phase-1 child **without** waiting for per-task approval, then stop
  at the phase gate and hand `task-095` to the human for one review of the whole
  phase. This is a deliberate relaxation of the merge gate, scoped to this phase only.
  It is defensible here specifically: phase 1 ships no user-facing surface, the Jinja
  UI is untouched, and the phase is the reviewable unit. It is **not** a precedent for
  phase 2, 3, or 4.

If the line above says `per-task-review`, do task 083, hand off, and stop. Do not
interpret silence, urgency, or your own confidence as approval. Do not edit this line
yourself.

---

## 1. What you are doing

`C:/projects/agentjobs` — AgentJobs, a task manager whose task records are YAML in
`tasks/agentjobs/`. Read `AGENTS.md`, `ENGINEERING.md`, and `ALLAGENTS.md` first;
they are binding and they contain rules that will otherwise cost you real work.

Your scope is `task-095-react-phase-1-foundation` and its children, in this order:

1. `task-083-react-scaffold` — Vite + React + TS + Tailwind at `/app`
2. `task-084-typed-api-client` — types generated from `/openapi.json`
3. `task-085-frontend-test-harness` — Vitest + React Testing Library

083 is first and blocks the other two. After 083, 084 and 085 are independent.

**The task records are the specification.** Read each one in full before starting it,
including its `log[]` newest-first. Several binding `decision` entries are already
recorded — the project id goes in the URL path, there is no CI in this repository,
Tailwind must be bundled rather than loaded from a CDN. Do not relitigate them. If you
think one is wrong, say so in a `question` log entry and keep going.

Nothing outside phase 1 is yours. If you find a defect elsewhere, file it as a new
task and move on.

---

## 2. The loop

Repeat until the exit condition in section 5:

1. **Pick.** `poetry run agentjobs list` and choose the lowest-numbered open child of
   095 that has no unmet dependencies. Never work two at once.
2. **Worktree first, before anything is written to disk.**
   `git worktree add ../aj-083 -b feat/task-083-react-scaffold`. This is not optional
   and not a preference — other agents share this clone, a checkout would replace the
   files under one of them mid-task, and neither of you would get an error.
3. **Claim.** `claim` the task, and record the branch in `branches[]`.
4. **Work** in the worktree, in small commits, tests green before each one. Stage
   explicit paths. **Never `git add -A`** — it has committed a peer's in-flight work
   here before.
5. **Task records go to `main`, not to your branch.** Write them through the API or
   the manager (both resolve the project root from the registry, so they land in the
   main clone's tree even while you work in a worktree), then commit them there:
   `git -C C:/projects/agentjobs add tasks/agentjobs/task-083-*.yaml`. A handoff
   committed to a branch is invisible in the dashboard to the person it is addressed
   to.
6. **Verify.** `poetry run pytest`, plus the frontend checks once they exist. Then
   exercise the change the way a user would, against a freshly started server — a
   passing suite is not evidence a feature works. Restart the server rather than
   trusting a running one: `poetry run agentjobs restart`.
7. **Log a `progress` entry** saying what was done, what was verified and how, and
   what remains. Write for a reader with no access to this session.
8. **Close out** according to `MERGE_AUTHORIZATION`:
   - `per-task-review` → `handoff` to `human` / `review` with a self-contained
     `ball_prompt`, commit that to `main`, **stop the loop entirely**, and report.
   - `phase-gate-review` → rebase onto `main`, merge `--no-ff`, mark the branch
     `merged`, `close` with `outcome: completed`, `git worktree remove ../aj-083`,
     then continue to the next task.

---

## 3. Budget and getting cut off

You have usage limits and you may be interrupted mid-task without warning. Design for
that rather than trying to predict it.

- **Assume every tool call could be your last.** The task record — not this
  conversation — is the only thing the next session will see.
- **Log before you dig in, not after.** Before starting anything that will take a
  while, write a `progress` entry saying what you are about to do and where you are.
  A session that dies with its findings in the transcript has lost them.
- **At every natural boundary** — a commit, a passing suite, a decision made — flush
  to the task log. Boundaries are cheap; reconstruction is not.
- **If you can see your remaining budget, say so** in your progress entries, in
  absolute terms ("~20% of the weekly window left"). If you cannot see it, say that
  instead. Do not estimate it from vibes, and do not let an assumed shortage become a
  reason to cut scope silently.
- **When you judge you are close to the end of a window**, stop at the next commit
  boundary rather than starting a new task. Write a handoff describing exactly where
  you are, then stop. A clean stop mid-phase is a good outcome; a task left `active`
  with no log entry is not.
- **Never leave a worktree behind for a task you are not actively working.**
  `git worktree list` is the inventory and a stale one is litter that confuses the
  next agent.

---

## 4. Things that will bite you here specifically

- **Restart the server after changing models, storage, or task files.** A running
  `agentjobs serve` holds old code in memory and will report dozens of validation
  errors naming fields that no longer exist. The application is fine; the process is
  stale.
- **Assert rendered values, not the presence of markup.** A test for `data-ball=`
  passed happily in this repo while the app emitted `Ball.HUMAN` and every filter
  silently matched nothing.
- **There is no CI.** No `.github/workflows`, no pre-commit config. Do not write an
  acceptance criterion against a pipeline that does not exist, and do not stand CI up
  as a side effect — that is its own decision. Add checks to a single documented
  command.
- **Do not touch `src/agentjobs/api/templates/`.** The Jinja UI must keep working,
  unchanged in behaviour, at every commit.
- **Do not extend the root `package.json`.** It is `agentjobs-schema-tools`, mermaid
  rendering for the LinkML schemas, dev tooling only. `frontend/` is separate.
- **Never `git push`.** Merging is not authorization to push. Ask.

---

## 5. Exit condition

Stop and report when any of these is true:

- `MERGE_AUTHORIZATION = per-task-review` and task 083 has been handed off for review.
- All three children of 095 are `closed` with `outcome: completed`. Then hand
  `task-095` itself to `human` / `review` with a `ball_prompt` summarising the phase,
  and stop. **Do not start phase 2** — 096 is a separate gate and its first child
  ports a real screen.
- You are blocked on something you cannot decide. Move the ball to `human` /
  `decision` with a `ball_prompt` stating the question and the options, and stop.
- You judge you are near the end of your usage window. Stop at a commit boundary with
  a handoff, as in section 3.

In your final message: what you finished, what you verified and how, what you decided,
what you left, and where it is. Assume the reader has not seen any of this session.
