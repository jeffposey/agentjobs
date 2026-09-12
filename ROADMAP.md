# Roadmap

AgentJobs is a task manager for work that AI agents do. A task is a durable record that
outlives the session that wrote it, so a second agent — or the same one tomorrow, with no
memory of today — can pick the work up from the record alone. The CLI, the browser and an
agent's own MCP tools all read one store, and a task can become a running agent, be
watched to a conclusion, and be merged without a human retyping what it was for.

This page is the plan, grouped into the work it is actually made of. The exhaustive
listing — every open task with its summary, in the order the queue hands them out — is
[docs/backlog.md](docs/backlog.md), which is generated from the store.

Two things are worth knowing before reading further. **The queue decides order, not this
page**: `agentjobs next` is the only authority on what gets worked, and the phases below
say what a group of tasks is *for*, never when it ships. And **this page is written by
hand, so it runs behind the listing**. A check keeps it from advertising finished work,
but a task filed this morning may not be placed in a workstream yet.

---

## Phase 1 — Trust the loop that already runs

Dispatch works today. A task becomes a real agent process, the process takes its own
worktree, and a scripted finish rebases, gates and merges what it produced. Nothing in
this phase is new capability; all of it is what months of running that unattended turned
up. It is the difference between a loop that works and a loop you would walk away from.

### Starting a run, and knowing what it may do

The panel that starts a run still cannot say what the run is allowed to do with the
repository, which is the one thing a person deciding to click it needs.

- `task-160-dispatch-phase-two` — the umbrella: who can cause a run, what it can be told, what it does with an epic
- `task-309` — the panel names a posture but never says it will merge without review
- `task-161-project-level-dispatch` — dispatch a project, not a task: start an agent on whatever is next
- `task-163-role-dispatch` — one agent's handoff starting a different agent, when a human configured it to
- `task-178-runner-authoring-in-the-gui` — author runners and groups from the browser
- `task-181-dispatch-phase-one-hardening` — the umbrella for the holes found by using it for real
- `task-179-dispatch-onto-a-worked-task` — dispatch is offered on a task an agent already holds
- `task-162-author-the-dispatch-prompt` — say something to the agent you are starting
- `task-170-dispatch-in-the-menu` — reach dispatch, and its settings, from the menu
- `task-176-dispatch-on-create` — start an agent on the task you just created, off by default
- `task-156-task-difficulty-field` — a difficulty field, so a profile can pick a model from it
- `task-344` — change how many runs this machine may make at once, from the browser
- `task-350` — the slot board wastes its width at a ceiling of one
- `task-201` — make supervision posture an explicit choice, not the supervisor's guess
- `task-232` — let an epic whose children stand or fall together share one branch
- `task-379` — decide whether the dispatch subsystem moves inside the server

### The run that goes wrong

A run that stalls, is cancelled, or is polled with the wrong executable currently ends in
a state nothing resolves, and several of these are how an epic quietly stops.

- `task-389` — a run whose meta lacks a dispatch entry id is invisible to the poller for ever
- `task-197` — the poller resolves the project's default runner, not the group's
- `task-310` — a machine-wide default group makes `dispatch enable --runner` a silent no-op
- `task-370` — a cancelled dispatch is reported as failed under load, and turns the gate red
- `task-375` — a resumed run never tells its session the posture, so an autonomous child stops anyway
- `task-377` — a supervisor that finds its children need sequencing can only wake a human
- `task-406` — a dispatched run cannot execute `groom` or `reorder`: both act on tasks other than its own
- `task-349` — the task page lights Dispatch on a task that already has a live run
- `task-195` — a refusal outlives the state that produced it, and reads as an answer to a click nobody made

### Approval to merged and delivered

Clicking Approve runs the merge itself, with no agent in it. Every task here is a way it
reports the wrong thing, or declines for a reason that has nothing to do with the branch.

- `task-312` — approving a task whose session is alive declines the finish, and the documented remedy posts a false alarm
- `task-355` — Approve on a task with no recorded branch silently merges nothing
- `task-322` — the panel reports only the newest attempt, so a merged branch can read "nothing was merged"
- `task-343` — the approval note is dropped the moment the scripted finish takes over
- `task-348` — a red gate should re-rebase and re-run that stage once before escalating
- `task-407` — a task written during the gate turns the roadmap stage red, and the merge is declined for it
- `task-326` — a review waiting on a human offers the wrong verb, and the sandbox URL it names is dead text

### The gate, and the checks under it

The repository's own verification has drifted. Some of it asserts nothing, some is slow
enough to shape how people work, and some fails for reasons unrelated to the branch.

- `task-411` — the corpus checks have asserted nothing since task-311: a fixture hides the store from them
- `task-410` — Big Dawg Audit II: hold the first audit to account, then audit what came after it
- `task-369` — Playwright is now the gate's critical path: 107 end-to-end tests on one worker
- `task-404` — a Playwright browser that dies at launch is indistinguishable from a real failure
- `task-409` — `--since-gate` can skip pytest on evidence that no longer covers it
- `task-243` — the grandchild-kill test asserts on a moment and loses the race under load
- `task-325` — the same test flakes often enough to stop a scripted finish
- `task-351` — main is red: task-350 points at files that exist only on an unmerged branch
- `task-396` — `corpus_stats.py` measures the frozen YAML copy the migration retired
- `task-408` — `bench.py` seeds a directory of YAML nothing serves, and so measures an empty backlog
- `task-323` — the context budget measures only the repository half of the always-loaded bundle
- `task-196` — `agentjobs restart` can add a server generation instead of replacing one
- `task-198` — enabling dispatch from the UI destroys every comment in the machine's dispatch config

---

## Phase 2 — Make a record fast to write and worth reading

The resumption contract only pays off if the record is good, and a good record is
expensive to type. This phase attacks both ends of that: capture a finding in seconds,
dictate instead of typing, let a model draft the spec before the task exists, and make
the browser somewhere you work rather than somewhere you look.

### Capture: getting a finding into the backlog before you lose it

- `task-120-issue-reporter-workflow` — the umbrella: rapid screenshot-backed and batch capture
- `task-121-batch-issue-capture` — collect a review's worth of findings and file them in one action
- `task-342` — there is no way to specify priority when reporting an issue
- `task-175-flesh-out-with-ai` — flesh out the task with a model before it is created, on by default
- `task-382` — edit a task's spec prose and its list fields from the browser
- `task-359` — a question can be asked without ever reaching the human
- `task-360` — the draft question panel pastes a wall of text at the human

### The menu, dictation, and model access

One reusable control holding every AgentJobs action, and text fields you can fill by
speaking into a phone.

- `task-167-agentjobs-menu` — the umbrella
- `task-168-menu-shell` — a top-right popup holding About, API Docs and the pages the nav row evicts
- `task-346` — one capture control: Create and Report issue become the same act
- `task-171-voice-input-decision` — decide how dictation works on the devices it has to work on
- `task-172-dictation-control` — talk into any AgentJobs text field
- `task-173-transcription-fallback` — a path for browsers with no speech API, or close it
- `task-174-model-access-decision` — decide how AgentJobs is allowed to call a model at all
- `task-169-embeddable-menu` — the same menu, in somebody else's React app, in one import

### The tasks shell, and the rest of the browser

- `task-367` — the umbrella: the polish pass after living in it
- `task-364` — a fold is thrown away on reload whenever a descendant of it is selected
- `task-365` — a drag carries the grip glyph rather than the row, and nothing shows where it will land
- `task-366` — on a phone there is no way to open the task list without leaving the record
- `task-368` — drag the divider between the sidebar and the record
- `task-385` — space savings in the tasks sidebar
- `task-353` — the same waste in button and field widths
- `task-345` — the top bar carries navigation only: Dashboard, Tasks, Runs
- `task-383` — the browser offers the workflow verbs on any task, not only one parked at review
- `task-015` — one long ball prompt swallows a task list row
- `task-012` — contain a render crash so one bad field cannot blank the whole app
- `task-013` — ship sourcemaps so a production stack trace names a source line
- `task-295` — the pinned top bar may sit under the iOS status bar in the installed PWA
- `task-104-react-readme-screenshots` — stage and capture polished screenshots for the README
- `task-106-physical-device-pwa-verification` — verify the PWA on a physical phone and tablet

### Analytics: the counts, grown into trends

A five-tile snapshot occupies the middle of the dashboard and cannot answer the question
anybody actually has, which is whether the backlog is growing and how fast work moves.

- `task-212` — the umbrella: move the count tiles off the dashboard and grow them into trends
- `task-372` — the analytics API: one endpoint that answers the four questions
- `task-373` — the analytics page and its charts
- `task-374` — the dashboard's entry point to it

### The record model itself

- `task-063-schema-v2` — finish the dedicated CLI state verbs
- `task-053-schema-v2-cli` — the CLI mirrors: inbox, next, claim, handoff, log, close
- `task-018` — a parent can close while its children are open, and nothing can reopen it
- `task-105-next-task-id-all-slugged-records` — generate the next task number from all slugged ids
- `task-335` — map a proven login to an actor without hand-editing YAML
- `task-247` — atomic writes for the machine-level YAML, above all the storage config
- `task-387` — `visibility: local` hides a project from its owner, whose surfaces all arrive over a tailnet
- `task-381` — the CLI says it created a YAML file on a project that has no files
- `task-299` — run a playbook from where the work is, and stop the guard refusing its own targets
- `task-300` — fire `groom` and `reorder` on events, not on a human remembering
- `task-405` — groom the backlog
- `task-413` — this page: the published roadmap was a flat printout of the queue
- `task-412` — the roadmap playbook never says how its branch reaches main or comes down

---

## Phase 3 — Take it off this machine

Everything above makes AgentJobs better for the one person running it. This phase is what
makes it useful to anybody else: install it without cloning it, drop its interface into a
project that is not AgentJobs, and let a task carry acceptance checks a loop can evaluate
without waking a human between iterations.

### Install it without a clone

- `task-125-plugin-install-without-a-clone` — the umbrella
- `task-127-publish-and-invoke-without-a-venv` — publish the package, invoke it without a clone or a virtualenv
- `task-128-onboarding-from-inside-a-session` — create and register a first project from the agent session
- `task-129-guard-silent-noop-on-empty-registry` — stop the write guard silently protecting nothing
- `task-319` — retire the root MCP config and let the plugin be the only AgentJobs server on a machine
- `task-287` — the docs site renders no Mermaid diagrams at all

### Embed it in somebody else's app

- `task-137-embedded-helper-ui` — the umbrella: drop the helper UI into an app you are building
- `task-138-embed-distribution-decision` — decide how the embedded UI reaches another React app
- `task-140-embed-server-surface` — make the server callable from another origin, in one bootstrap request
- `task-144-embed-security-posture` — decide what an embedded surface may do, and from where
- `task-139-reporter-host-agnostic-core` — lift the issue reporter out of the AgentJobs app
- `task-141-embed-launcher-and-project-link` — open this project's backlog, or report what you just saw
- `task-143-report-and-dispatch-from-host` — start an agent on it without leaving the app you are building
- `task-142-embed-settings-and-identity` — settings, and who the host app says is reporting
- `task-145-embed-zero-config-wiring` — one dependency and one component: the widget wires itself up
- `task-146-embed-reference-integration` — prove the loop from a real project that is not AgentJobs, then write the guide

### Loops: a task that can tell whether it is done

- `task-147-acceptance-check-schema` — executable acceptance checks, and the check-result log entry
- `task-148-check-evaluator` — run a task's checks and report the vector
- `task-149-check-trigger` — evaluate a task's checks on demand, from the CLI and the API
- `task-150-chain-authorization` — what a human approves before a loop may run
- `task-151-loop-driver` — iterate, evaluate, and stop loudly
- `task-152-loops-gui` — check results and chains in the web UI

---

## What this page leaves out

Drafts are not listed. A draft is an idea nobody has specified, no agent can claim one,
and publishing one advertises work that may never exist. Closed tasks are not listed
either — the store has them, and this page is about what happens next. Everything a
record carries beyond its id, title and summary — the working spec, the decision log,
branches, verification evidence — stays in the store, which is both an editorial choice
and the reason these two files are safe to publish.
