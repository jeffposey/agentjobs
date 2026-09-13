# Roadmap

AgentJobs is a task manager for work that AI agents do. A task is a durable record that
outlives the session that wrote it, so a second agent — or the same one tomorrow, with no
memory of today — can pick the work up from the record alone. The CLI, the browser and an
agent's own MCP tools all read one store, and a task can become a running agent, be
watched to a conclusion, and be merged without a human retyping what it was for.

This page is the plan. It ends where the project is going — **somebody who is not its
author is using it** — and everything before that is what has to be true first, in the
order it has to become true. The phases are the owner's, written by hand; the tasks
under each phase are the backlog read against it. The exhaustive listing — every open
task with its summary, in the order the queue hands them out — is
[docs/backlog.md](docs/backlog.md), generated from the store.

Three things to know before reading further:

- **The phases are the plan, and the queue is expected to follow them.** A task in a
  later phase sitting above one in an earlier phase is a question for the `reorder`
  pass, not a fact about the plan. Until the queue has caught up, `agentjobs next` still
  says what is worked: an agent never reads this page to pick a task.
- **This page runs behind the listing.** A check keeps it from advertising finished
  work, but a task filed this morning may not be placed in a phase yet.
- **A phase can run ahead of its backlog.** A phase holding only an umbrella, or nothing
  at all, is not an omission. It is the plan saying the backlog is short of what the
  plan needs, and it is written here so the gap has an owner.

---

## Phase 1 — Stable enough to walk away from

Dispatch works today. A task becomes a real agent process, the process takes its own
worktree, and a scripted finish rebases, gates and merges what it produced. What it does
not yet do is survive being left alone: one expired login cost three human touches in a
single afternoon and silently swapped the runner underneath the run. Nothing in this
phase is new capability. All of it is the difference between a loop that works and a
loop you would walk away from.

### Durable execution: a dispatch is owed a completion

The model the rest of this phase hangs off. An authorised dispatch becomes an intent
the system owes a completion to — journaled, replayed after a crash, retried where a
retry is safe, and resumed rather than restarted. Most of the run-goes-wrong bugs below
are symptoms of not having it.

- `task-414` — the umbrella: a dispatch runs to completion and wakes a human only for what only a human can do
- `task-264` — the transactional journal: admission, ownership, budgets and results become recoverable
- `task-415` — report every runner candidate's real eligibility, not only the winner's
- `task-375` — the authorised envelope, posture included, survives a retry or a resume
- `task-416` — replay accepted dispatches after an interruption, with bounded recovery
- `task-417` — a run parked on an expired login is probed and woken, and the human is told once
- `task-418` — an epic's waits persist, so a dead supervisor costs no child a restart
- `task-419` — prove it with crash injection and a count of human touches per incident
- `task-389` — a run whose meta lacks a dispatch entry id is invisible to the poller for ever
- `task-197` — the poller resolves the project's default runner, not the group's
- `task-310` — a machine-wide default group makes `dispatch enable --runner` a silent no-op
- `task-370` — a cancelled dispatch is reported as failed under load, and turns the gate red
- `task-377` — a supervisor that finds its children need sequencing can only wake a human
- `task-201` — supervision posture is an explicit choice, not the supervisor's guess
- `task-379` — decide whether the dispatch subsystem moves inside the server
- `task-181-dispatch-phase-one-hardening` — the umbrella for the holes found by using it for real
- `task-179-dispatch-onto-a-worked-task` — dispatch is offered on a task an agent already holds

### Approve means what it says

Clicking Approve runs the merge itself, with no agent in it. Every task here is a way
the finish reports the wrong thing, declines for a reason that has nothing to do with
the branch, or never told the person clicking what the click would do.

- `task-312` — approval is delivered durably: no cancellation, no lost clearance
- `task-348` — a red gate re-rebases and re-runs that stage once before escalating
- `task-322` — the finish resumes from durable merge evidence and reports delivery truthfully
- `task-355` — Approve on a task with no recorded branch silently merges nothing
- `task-343` — the approval note is dropped the moment the scripted finish takes over
- `task-326` — a review waiting on a human offers the wrong verb, and its sandbox URL is dead text
- `task-309` — the panel names a posture but never says it will merge without review
- `task-349` — the task page lights Dispatch on a task that already has a live run
- `task-195` — a refusal outlives the state that produced it, and reads as an answer to a click nobody made
- `task-232` — an epic whose children stand or fall together can share one branch

### It finds you when it needs you

Walking away is only safe if the system can get you back. Today a task waiting on a
human is a row that changed colour in a browser tab that may not be open, and a
question an agent asks can fail to reach anybody at all.

- `task-421` — the umbrella: a durable, chat-independent way of telling the human they are needed
- `task-422` — a persistent indicator and a notification on the desktop, without nagging
- `task-423` — mobile push that opens the right task
- `task-359` — a question can be asked without ever reaching the human
- `task-360` — the draft question panel pastes a wall of text at the human

### The gate tells the truth about the branch

The repository's own verification has drifted. Some of it asserts nothing, some is slow
enough to shape how people work, and some fails for reasons unrelated to the branch —
which, once the finish runs the gate unattended, is a merge declined for nothing.

- `task-411` — the corpus checks have asserted nothing since task-311: a fixture hides the store from them
- `task-369` — Playwright is now the gate's critical path: 107 end-to-end tests on one worker
- `task-404` — a browser that dies at launch is indistinguishable from a real failure
- `task-409` — `--since-gate` can skip pytest on evidence that no longer covers it
- `task-243` — the grandchild-kill test asserts on a moment and loses the race under load
- `task-325` — the same test flakes often enough to stop a scripted finish
- `task-351` — main is red: task-350 points at files that exist only on an unmerged branch
- `task-407` — a task written during the gate turns the roadmap stage red, and the merge is declined for it
- `task-396` — `corpus_stats.py` measures the frozen YAML copy the migration retired
- `task-408` — `bench.py` seeds a directory of YAML nothing serves, and so measures an empty backlog
- `task-323` — the context budget measures only the repository half of the always-loaded bundle
- `task-427` — every review sandbox seeds a database the server never serves, so a UI review opens an empty project

### The record, the store and the CLI

The record is the product, and a few things still let it say something false.

- `task-018` — a parent can close while its children are open, and nothing can reopen it
- `task-247` — atomic writes for the machine-level YAML, above all the storage config
- `task-387` — `visibility: local` hides a project from its owner, whose surfaces all arrive over a tailnet
- `task-381` — the CLI says it created a YAML file on a project that has no files
- `task-105-next-task-id-all-slugged-records` — generate the next task number from all slugged ids
- `task-063-schema-v2` — finish the dedicated CLI state verbs
- `task-053-schema-v2-cli` — the CLI mirrors: inbox, next, claim, handoff, log, close
- `task-196` — `agentjobs restart` can add a server generation instead of replacing one
- `task-198` — enabling dispatch from the UI destroys every comment in the machine's dispatch config

---

## Phase 2 — Polish the browser after living in it

The React app is where the owner spends the day, and a few months of that turned up the
things a first build cannot know: which fold you wanted kept, where a phone needs a
control the desktop does not, which tiles nobody looks at. This phase is the pass a
product gets once somebody has used it in anger.

### The tasks shell

- `task-367` — the umbrella: the polish pass after living in it
- `task-364` — a fold is thrown away on reload whenever a descendant of it is selected
- `task-365` — a drag carries the grip glyph rather than the row, and nothing shows where it will land
- `task-366` — on a phone there is no way to open the task list without leaving the record
- `task-368` — drag the divider between the sidebar and the record
- `task-385` — space savings in the tasks sidebar
- `task-353` — the same waste in button and field widths
- `task-015` — one long ball prompt swallows a task list row
- `task-383` — the browser offers the workflow verbs on any task, not only one parked at review
- `task-382` — edit a task's spec prose and its list fields from the browser
- `task-012` — contain a render crash so one bad field cannot blank the whole app
- `task-013` — ship sourcemaps so a production stack trace names a source line
- `task-295` — the pinned top bar may sit under the iOS status bar in the installed PWA
- `task-106-physical-device-pwa-verification` — verify the PWA on a physical phone and tablet

### The menu, and talking to it

One reusable control holding every AgentJobs action, and text fields you can fill by
speaking into a phone. The model-access decision lives here because dictation and the
AI-assisted forms in Phase 6 both wait on it.

- `task-167-agentjobs-menu` — the umbrella
- `task-168-menu-shell` — a top-right popup holding About, API Docs and the pages the nav row evicts
- `task-345` — the top bar carries navigation only: Dashboard, Tasks, Runs
- `task-171-voice-input-decision` — decide how dictation works on the devices it has to work on
- `task-172-dictation-control` — talk into any AgentJobs text field
- `task-173-transcription-fallback` — a path for browsers with no speech API, or close it
- `task-174-model-access-decision` — decide how AgentJobs is allowed to call a model at all

### Runs in the browser

- `task-344` — change how many runs this machine may make at once, from the browser
- `task-350` — the slot board wastes its width at a ceiling of one

### Analytics: the counts, grown into trends

A five-tile snapshot occupies the middle of the dashboard and cannot answer the question
anybody actually has, which is whether the backlog is growing and how fast work moves.

- `task-212` — the umbrella: move the count tiles off the dashboard and grow them into trends
- `task-372` — the analytics API: one endpoint that answers the four questions
- `task-373` — the analytics page and its charts
- `task-374` — the dashboard's entry point to it

---

## Phase 3 — Tested and working on Linux

Everything so far has been built and run on one Windows machine, and the code knows it:
process groups, the launcher scripts, the tailnet proxy, the interpreter the bootstrap
prints, and the desktop half of the notification work in Phase 1 are all written against
that machine. Before AgentJobs can work from anybody else's computer it has to work from
a computer that is not this one, and Linux is the first such computer because it is
where the owner's own serious projects move to and where any server would live.

The code already branches on platform in the places that matter most — killing a
process tree, stopping a run, asking whether a pid is alive, finding an executable — and
only the Windows side of each branch has ever run. So this phase starts as a single
umbrella whose first act is to try: bootstrap and gate a Linux checkout, dispatch a run
there and cancel another, and file each breakage as a child. The children are unknown
until somebody has tried, which is the point of planning the attempt.

- `task-428` — the umbrella: the CLI, the server, dispatch and the gate on a machine that is not Windows

---

## Phase 4 — Works from someone else's machine

Today AgentJobs is a clone, a virtualenv, a Node toolchain and a machine-level YAML
file that one person knows the shape of. This phase is what a second person needs:
install it without cloning it, register a first project from inside an agent session,
and have the system know who they are without anybody hand-editing a file.

### Install it without a clone

- `task-125-plugin-install-without-a-clone` — the umbrella
- `task-127-publish-and-invoke-without-a-venv` — publish the package, invoke it without a clone or a virtualenv
- `task-128-onboarding-from-inside-a-session` — create and register a first project from the agent session
- `task-129-guard-silent-noop-on-empty-registry` — stop the write guard silently protecting nothing
- `task-319` — retire the root MCP config and let the plugin be the only AgentJobs server on a machine

### Who you are, and what runs the agent

- `task-335` — map a proven login to an actor without hand-editing YAML
- `task-420` — evaluate a hosted agents API as an optional execution backend, for a machine without the current runner

---

## Phase 5 — Embedded in an app that is not AgentJobs

The browser app is one client of the server. This phase makes the same controls a
dependency somebody else's React app can import: a launcher that opens this project's
backlog, a reporter that files what you just saw, and the server surface and security
posture that let another origin talk to it.

- `task-137-embedded-helper-ui` — the umbrella: drop the helper UI into an app you are building
- `task-138-embed-distribution-decision` — decide how the embedded UI reaches another React app
- `task-140-embed-server-surface` — make the server callable from another origin, in one bootstrap request
- `task-144-embed-security-posture` — decide what an embedded surface may do, and from where
- `task-139-reporter-host-agnostic-core` — lift the issue reporter out of the AgentJobs app
- `task-141-embed-launcher-and-project-link` — open this project's backlog, or report what you just saw
- `task-142-embed-settings-and-identity` — settings, and who the host app says is reporting
- `task-145-embed-zero-config-wiring` — one dependency and one component: the widget wires itself up
- `task-169-embeddable-menu` — the same menu, in somebody else's React app, in one import

---

## Phase 6 — Report it from the frontend, and have it fixed and deployed

This is the phase the previous five are for. You are using an app — AgentJobs itself,
or one that embeds it — you see something wrong, and you report it from where you are.
A model fleshes the report out into a task, an agent starts on it without you leaving
the page, the task carries checks that say whether the agent is done, and the finish
merges and deploys it. You never open a terminal. Phase 1 is what makes leaving the
loop alone safe; Phase 5 is what puts the report button in your own app.

### Capture: a finding becomes a task in seconds

- `task-120-issue-reporter-workflow` — the umbrella: rapid screenshot-backed and batch capture
- `task-121-batch-issue-capture` — collect a review's worth of findings and file them in one action
- `task-342` — there is no way to specify priority when reporting an issue
- `task-346` — one capture control: Create and Report issue become the same act
- `task-175-flesh-out-with-ai` — flesh out the task with a model before it is created, on by default

### Start the agent from where you are

- `task-160-dispatch-phase-two` — the umbrella: who can cause a run, what it can be told, what it does with an epic
- `task-176-dispatch-on-create` — start an agent on the task you just created, off by default
- `task-170-dispatch-in-the-menu` — reach dispatch, and its settings, from the menu
- `task-162-author-the-dispatch-prompt` — say something to the agent you are starting
- `task-161-project-level-dispatch` — dispatch a project, not a task: start an agent on whatever is next
- `task-163-role-dispatch` — one agent's handoff starting a different agent, when a human configured it to
- `task-143-report-and-dispatch-from-host` — start an agent on it without leaving the app you are building
- `task-156-task-difficulty-field` — a difficulty field, so a profile can pick a model from it
- `task-178-runner-authoring-in-the-gui` — author runners and groups from the browser

### The loop closes itself

A task that can tell whether it is done is a task an agent can iterate on without
waking a human between attempts.

- `task-147-acceptance-check-schema` — executable acceptance checks, and the check-result log entry
- `task-148-check-evaluator` — run a task's checks and report the vector
- `task-149-check-trigger` — evaluate a task's checks on demand, from the CLI and the API
- `task-150-chain-authorization` — what a human approves before a loop may run
- `task-151-loop-driver` — iterate, evaluate, and stop loudly
- `task-152-loops-gui` — check results and chains in the web UI

### The backlog tends itself

Reports filed from a frontend arrive faster than a person grooms them. The playbooks
that groom, reorder and explain the backlog have to run from where the work is and
fire on events, or the queue Phase 6 feeds stops being one anybody trusts.

- `task-300` — fire `groom` and `reorder` on events, not on a human remembering
- `task-406` — a dispatched run cannot execute `groom` or `reorder`: both act on tasks other than its own
- `task-299` — run a playbook from where the work is, and stop the guard refusing its own targets
- `task-405` — groom the backlog
- `task-412` — the roadmap playbook never says how its branch reaches main or comes down
- `task-413` — this page: the published roadmap was a flat printout of the queue

---

## Phase 7 — Someone else is using AgentJobs

The end of the current roadmap. Not a release and not a launch: one person who did not
build it has installed it on their machine, pointed it at a project of their own, and
kept using it. Every phase above is a precondition, and the tasks here are the ones
that only make sense once the preconditions hold — the proof from outside, and the
front door a stranger arrives through.

### The proof from outside

- `task-146-embed-reference-integration` — prove the loop from a real project that is not AgentJobs, then write the guide
- `task-429` — a first-run walkthrough that somebody other than the author has followed from a clean machine

### The front door

A stranger needs three things the author never has: a page that shows them what they
are installing, somewhere to say what broke, and a straight account of where their data
goes. The store is a database beside the server, and nobody but the owner has yet had to
trust that.

- `task-430` — somewhere for a user who is not the author to report what broke
- `task-431` — what AgentJobs promises a user about their data: where it lives, what leaves the machine, how to back it up
- `task-104-react-readme-screenshots` — stage and capture polished screenshots for the README
- `task-287` — the docs site renders no Mermaid diagrams at all

---

## What this page leaves out

Drafts are not listed. A draft is an idea nobody has specified, no agent can claim one,
and publishing one advertises work that may never exist. Closed tasks are not listed
either — the store has them, and this page is about what happens next. Everything a
record carries beyond its id, title and summary — the working spec, the decision log,
branches, verification evidence — stays in the store, which is both an editorial choice
and the reason these two files are safe to publish.
