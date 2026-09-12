<!-- Generated from the AgentJobs task store by scripts/export_roadmap.py. Do not edit. -->

# Roadmap

The open AgentJobs backlog: what is planned, in the order it will be worked. Bands run
critical, high, medium then low, and within a band the order is the queue's own — the
same order `agentjobs next` hands work out in.

This file is generated from the task store, which lives in a database beside the server
rather than in this repository. It is written one way and read by people: nothing here
reads it back, and it is never a source of truth. Regenerate it with

```bash
poetry run python scripts/export_roadmap.py ROADMAP.md
```

A task shows its id, its title, its one-sentence summary, what it is still waiting on,
and the umbrella it belongs to. Everything else a record carries — the working spec, the
decision log, branches, verification evidence — stays in the store.

## Critical (2)

- **task-410** — Big Dawg Audit II — hold the first audit to account, then audit everything built since

  Follow-up to task-242, the Big Dawg Audit of 2026-08-21. Fourteen auditors and a synthesis session, overnight on 2026-09-11, on claude-fable-5-1; the runbook is audits/2026-09-11/PLAN.md.

- **task-411** — The corpus checks have asserted nothing since task-311: an autouse fixture hides the store from them

  Every check that reads this repository's own backlog skips under pytest, because conftest re-points AGENTJOBS_HOME at a temp directory for every test. Turning them on finds eighteen dangling context pointers immediately.

## High (64)

- **task-364** — A fold is thrown away on reload whenever a descendant of the folded parent is selected

  Fold an epic while you are reading one of its descendants, reload, and the fold is gone — and the stored preference is erased, not merely overridden. Found by hand during task-240's review; task-238's own e2e test misses it because it reloads with nothing selected.

  *part of task-367 (Tasks shell: the polish pass after living in it)*

- **task-365** — A drag carries the grip glyph rather than the row, and nothing shows where it will land

  Dragging a sidebar row works — it lands where you drop it — but you cannot see that while you are doing it. The thing under the pointer is the six-dot handle, the row you are moving looks untouched, and no marker says where the drop will go.

  *part of task-367 (Tasks shell: the polish pass after living in it)*

- **task-366** — On a phone there is no way to open the task list without leaving the record

  Below the device-class threshold the shell shows the list or the record, never both, and the only way back to the list is a link that drops the record. The owner asked for the list to open from a burger control instead, over whatever is on screen.

  *part of task-367 (Tasks shell: the polish pass after living in it)*

- **task-309** — The Dispatch panel names a posture but never says what it will do to your branch

  Since task-021, a posture carries a merge policy and a project carries a push switch — so choosing `autonomous` means the run merges its own work with no review. The browser still renders posture as a bare word next to the runner name and says nothing about merging or pushing. The one surface where a human decides to start a run is the one that cannot tell them what the run is allowed to do with their repository.

  *part of task-160-dispatch-phase-two (Dispatch phase two: who can cause one, what it can be told, and what it does with an epic)*

- **task-212** — Analytics page: move the count tiles off the dashboard and grow them into trends

  The dashboard carries a five-tile count strip (Needs you / In Progress / Blocked / Completed / Total) in the middle of the page. It is a point-in-time snapshot occupying the most valuable space on the screen, and a snapshot cannot answer the question a person actually has. This umbrella moves the counts to their own page, reached from a small link or icon where the strip is today, and grows that page into real analytics — how the numbers are trending, whether the backlog is growing or shrinking, how fast work is moving.

- **task-385** — Space savings in tasks sidebar

  Space savings in tasks sidebar

- **task-167-agentjobs-menu** — The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone

  Report Issue is a lone floating button in the bottom-right corner of the AgentJobs app. Replace it with a single AgentJobs menu in the top-right that holds every AgentJobs action — report an issue, dispatch and its settings, About — build that menu as a component any project's React frontend can embed, and make the free-text fields behind it dictatable by voice and completable by a model.

- **task-018** — A parent can close while its children are open, and nothing can reopen it

  task-060 is `closed`/`completed` with ten open children. Nothing prevented that close, nothing flags it now, and there is no verb to undo it — `claim` refuses a closed task, so the umbrella for the largest active effort in the corpus cannot be reopened at all.

- **task-198** — Enabling dispatch from the UI destroys every comment in dispatch.yaml

  set_project_enabled round-trips the machine-local dispatch config through yaml.safe_dump, so one click on "Enable dispatch for this project" deletes every comment in the file and reflows the rest. The comments are operational documentation about what this machine may execute, and losing them has already caused a wrong diagnosis.

- **task-232** — An epic whose children stand or fall together can be worked on one branch, and the record can say so

  Today a parent with open children always buys three sessions, three worktrees, three gates, three review round trips and three merges, because dispatch picks the supervisor prompt from one property — does this task have open children — and nothing can override it. When the children are three views of one component and the human would never merge one while rejecting another, that is pure ceremony. Give the record a way to say "work these together on one branch", and state the criterion for when that is legitimate.

- **task-196** — `agentjobs restart` can add a server generation instead of replacing one, and nothing says so

  `agentjobs restart` produced a second server process that could not bind the port, left the previous generation serving, and reported nothing. Two generations were found alive hours apart on 2026-08-20, the older one still holding 8876 and serving stale code.

- **task-012** — Contain a render crash so one bad field cannot blank the whole app

  The React app has no error boundary anywhere, so a single exception during render unmounts the entire tree and leaves a white page with no message. A stale server that omitted one array field was enough to make the whole dashboard unusable on 2026-08-17.

- **task-120-issue-reporter-workflow** — Issue reporter: rapid screenshot-backed and batch task capture

  Turn observations made while using AgentJobs into normal, actionable tasks with near-zero friction, including pasted screenshot snippets and a tray for submitting many findings together.

- **task-121-batch-issue-capture** — Collect a review's worth of findings and file them in one action

  One pass through the React app turns up a dozen small problems at once. Today each one costs opening and submitting a form, so most of them get said out loud instead of filed. Collect findings as you go -- title, a note, a pasted screenshot -- and file the whole batch as normal tasks in one click.

  *part of task-120-issue-reporter-workflow (Issue reporter: rapid screenshot-backed and batch task capture)*

- **task-137-embedded-helper-ui** — Embedded AgentJobs: drop the helper UI into an app you are building

  A project that uses AgentJobs still has to leave its own app to use it. Ship a drop-in React component so any project's frontend carries an AgentJobs launcher of its own: open this project's backlog, adjust settings, and report an issue that becomes a real task -- optionally with an agent already working on it.

- **task-138-embed-distribution-decision** — Decide how the embedded UI reaches someone else's React app

  There are three defensible ways to get AgentJobs UI into a host project's React app -- a published package, a bundle served by the running server, or an iframe. Pick one with reasons and prove it consumable before anything is built on top of the answer.

  *part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-140-embed-server-surface** — Make the server callable from another app's origin, in one bootstrap request

  CORS is a hardcoded list of four localhost origins, and there is no single call that tells an embedded client what it needs to render. Make the allowlist configurable and add the one request a widget makes to bootstrap itself.

  *part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-139-reporter-host-agnostic-core** — Lift the issue reporter out of the AgentJobs app so a host app can render it

  The reporter is welded to react-router, the app's query client and its Tailwind classes, none of which a host app has. Extract a host-agnostic core that both the AgentJobs UI and the embedded widget render, so there is one reporter rather than two that drift.

  *needs task-138-embed-distribution-decision · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-144-embed-security-posture** — Decide what an embedded surface may do, and from where

  The API has no authentication. A widget that creates tasks and starts coding agents makes both reachable from any page an allowed origin serves -- including, if nobody decides otherwise, a production bundle or a tailnet address. Settle the posture before the widget exists.

  *needs task-140-embed-server-surface · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-141-embed-launcher-and-project-link** — The launcher: open this project's backlog, or report what you just saw

  The visible piece of the epic. One component a host app renders, putting a small AgentJobs launcher on every page of that app: a link into this project's backlog, and Report issue with the host page's context already captured.

  *needs task-138-embed-distribution-decision · needs task-139-reporter-host-agnostic-core · needs task-140-embed-server-surface · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-143-report-and-dispatch-from-host** — Start an agent on it without leaving the app you are building

  The report form grows an optional "start an agent on this now". Filing becomes dispatching, so the path from noticing a problem in your own app to an agent working on it never leaves the page you noticed it on.

  *needs task-141-embed-launcher-and-project-link · needs task-144-embed-security-posture · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-146-embed-reference-integration** — Prove the loop from a real project that is not AgentJobs, then write the guide

  Embed the widget in a registered project's own React app, run the whole loop from inside that app -- open the backlog, file an issue, start an agent on it -- and write docs/embedding.md from what actually happened rather than from what was intended.

  *needs task-141-embed-launcher-and-project-link · needs task-142-embed-settings-and-identity · needs task-143-report-and-dispatch-from-host · needs task-144-embed-security-posture · needs task-145-embed-zero-config-wiring · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-147-acceptance-check-schema** — Schema: executable acceptance checks and the check_result log entry

  Add `acceptance[].check` -- an argv list whose exit code decides whether a criterion is met -- and the `check_result` log entry that records an evaluation pass. Schema only: nothing in this task executes anything. Derived from docs/agent-loops-design.md, decisions L1, L2, L4 and L5.

  *part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-148-check-evaluator** — The check evaluator: run a task's acceptance checks and report the vector

  Execute one task's `acceptance[].check` commands and turn their exit codes into a result vector, behind the same four gates that guard dispatch. This is the piece that makes a definition of done machine-readable; it has no loop in it and is worth having on its own. Derived from docs/agent-loops-design.md, decisions L3 and the execution table in section 3.

  *needs task-147-acceptance-check-schema · part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-149-check-trigger** — Evaluate a task's checks on demand, from the CLI and the API

  `agentjobs check <task-id>` and `POST /api/projects/{id}/tasks/{task_id}/check`: ask a task whether its own definition of done holds, and put the answer on the record. This is the point at which executable acceptance criteria are useful to a person, with no autonomy anywhere.

  *needs task-148-check-evaluator · part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-160-dispatch-phase-two** — Dispatch phase two: who can cause one, what it can be told, and what it does with an epic

  Umbrella for four asks Jeff raised on 2026-08-19 while task-060 was being closed out. Phase one built dispatch as a human pressing a button on one task with a fixed prompt. Each of these widens exactly one of those assumptions: who causes a dispatch, what it is dispatched against, what it is told, and what it does when the task has children.

- **task-161-project-level-dispatch** — Dispatch a project, not a task: one button that starts an agent on whatever is next

  Dispatch today requires choosing a task first. AgentJobs already computes what an agent should work on next, so a project-level Dispatch button should start an agent against that answer without a human picking anything.

  *part of task-160-dispatch-phase-two (Dispatch phase two: who can cause one, what it can be told, and what it does with an epic)*

- **task-168-menu-shell** — The actions menu: a top-right popup holding About, API Docs and the pages the nav row evicts

  An actions menu in the top-right of the AgentJobs header: a transient popup holding About and the API docs to begin with, and the shell that later entries -- Playbooks and Dispatch settings, evicted from the nav row by task-345 -- are added to.

  *part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-171-voice-input-decision** — Decide how dictation works, on the devices it actually has to work on

  Three defensible ways to get speech into a text field in a browser, with different device coverage and different install costs. Settle which combination AgentJobs uses by testing them on Jeff's actual phone, tablet and desktop, and record the answer before anything is built on it.

  *part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-169-embeddable-menu** — The same menu, in somebody else's React app, in one import

  Ship the menu built in task-168 as a component another project's React frontend renders — AgentJobs mark instead of a burger, plus an "AgentJobs dashboard" entry linking into that project's backlog. Adding it must be a recipe an agent can follow without reading the source.

  *needs task-168-menu-shell · needs task-138-embed-distribution-decision · needs task-139-reporter-host-agnostic-core · needs task-140-embed-server-surface · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-172-dictation-control** — Talk into any AgentJobs text field

  Build the dictation control task-171 chose and wire it into every free-text field behind the menu: the issue title and details, the dispatch prompt, and the create-task fields. One control, used everywhere, degrading honestly on devices that cannot do it.

  *needs task-171-voice-input-decision · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-174-model-access-decision** — Decide how AgentJobs is allowed to call a model at all

  AgentJobs starts coding agents but has never called a model itself. "Flesh this out with AI" needs one, and how that call is made — through the existing dispatch runner, or through a direct API with a key — is an architectural fork worth settling once, before any feature depends on the answer.

  *part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-175-flesh-out-with-ai** — Flesh out the task with AI before it is created, on by default

  A checkbox on the create and report forms — checked by default — that has a model expand what you typed into a real task spec: summary, intent, description, acceptance criteria. The draft lands in the form for the human to edit, and the human is still the one who presses create.

  *needs task-174-model-access-decision · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-179-dispatch-onto-a-worked-task** — Dispatch is offered on a task an agent is already working, and the guards would allow it

  A task that is claimed and actively being worked still shows an enabled Dispatch button, and the guard layer would let the click through — because "one live run per task" is enforced only against runs that dispatch itself started, and an active task owned by the same actor is passed through rather than refused. Two agents on one task and one branch is the failure this prevents nowhere.

  *part of task-181-dispatch-phase-one-hardening (Dispatch phase one hardening: the holes found by using it for real)*

- **task-181-dispatch-phase-one-hardening** — Dispatch phase one hardening: the holes found by using it for real

  Umbrella for the defects that turned up once dispatch was exercised on a real machine rather than in tests. Each child is a small, independently mergeable fix to shipped phase-one behaviour. Distinct from task-160, which widens what dispatch can do; this one closes holes in what it already does.

- **task-063-schema-v2** — Schema v2: finish the dedicated CLI state verbs

  Schema v2 is live across the models, corpus, application, API, Python client, and React UI. The remaining umbrella work is task 053: dedicated CLI commands for the canonical state verbs.

- **task-299** — Run a playbook from where the work is: flesh-out from a task's own page, and stop the brief guard refusing exactly its targets

  The Playbooks page ships and groom and reorder are one click on it. Two things stop flesh-out being usable the same way: running it against a task means typing that task's id into a field on a different page, and the dispatch confirmation fires on precisely the tasks flesh-out exists to fix, because it is keyed on an empty description.

- **task-300** — Design playbook triggers: groom and reorder should fire on events like an epic closing, not on a human remembering

  The playbooks design deferred recurrence and wrote the condition for reopening it. That condition was met on the day the playbooks shipped: groom ran twice and reorder ran twice in one day, against a stated threshold of "more than weekly". Design how a run fires on an event -- an epic closing, a batch of tasks arriving -- with the authorisation and bounds the product already requires of every other autonomy.

- **task-312** — Approving a task whose session is still alive declines the finish, and the documented remedy posts a false alarm to the human

  A dispatched session that hands off for review stays alive, so its run keeps the task's lock. The scripted finish that approval triggers then declines `locked` and nothing merges. The remedy the tool itself prints is to cancel the run — and cancelling rewrites the task's `ball_prompt` with "Run X ended `cancelled` and nobody was told what this task needs", which is false, alarming, and lands in front of the human who just approved. Both approvals on 2026-08-23/24 hit this.

- **task-325** — test_the_timeout_kills_the_grandchild_too flakes often enough to stop a scripted finish, and its 30-second budget rests on a premise that is no longer true

  One test — TestProcessGroup::test_the_timeout_kills_the_grandchild_too — has now failed three unrelated gates by timing out rather than by finding a bug. Its own comment says its 30-second budget is "large enough that a loaded machine cannot exhaust it"; that premise was written for a machine running one gate, and this one routinely runs two or three. Establish whether the grandchild genuinely survives or the check is simply slow, then fix the one that is wrong.

- **task-326** — A review waiting on a human offers "Answer Questions" instead of Approve, and the sandbox URL it names is dead text

  A task handed off for review can have its ball rewritten to `human`/`input` by the stall watcher, which strips Approve and Request Changes from the panel and leaves only "Answer Questions". Any sandbox URL the review names is also unclickable.

- **task-335** — Identity CLI: map a proven login to an actor without hand-editing YAML

  Mapping a tailnet login to an actor id means hand-editing ~/.agentjobs/identities.yaml, which is what the refusal card tells a new person to do. Add `agentjobs identity` subcommands that write that file safely, list it, and say who the caller resolves to.

- **task-344** — Change how many runs this machine may make at once, from the browser

  `limits.max_concurrent_runs` is machine-level and lives in ~/.agentjobs/dispatch.yaml, so changing it means hand-editing a file on the host. Once the Dashboard becomes a slot board (task-092) that number decides how much of the page there is, so it needs a control in the GUI. This adds one -- human-only, comment-preserving, and honest about being a machine-wide setting reached from a project page.

  *needs task-198*

- **task-345** — The top bar carries navigation only: Dashboard, Tasks, Runs

  The primary nav currently holds seven destinations — Dashboard, Tasks, Create, Dispatch, Playbooks, Runs, API Docs — and Jeff's call on 2026-09-05 is that only the first two and Runs are navigation at all. API Docs, Playbooks and Dispatch settings move behind the actions menu task-168 builds; Create leaves the bar as a text link and becomes the always-present capture control of task-346. What is left is a short row that fits a phone.

  *needs task-168-menu-shell · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-346** — One capture control: Create and Report issue become the same act

  Create and Report issue both file a task through the same manager path, yet the app offers them as two unrelated controls — a nav link and a floating button in the opposite corner. Merge them into one small always-present control, and decide whether what sits behind it is one form or two.

  *part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-351** — main is red: task-350 points at files that only exist on an unmerged branch

  The repository's gate is red on main and therefore on every branch. task-350's context pointers name two files that live only on feat/task-092-dashboard-slot-board, which has not merged, so tests/test_task_corpus.py::test_agentjobs_context_paths_exist fails against a clean main.

- **task-355** — Approve on a task with no recorded branch silently merges nothing

  Jeff approved task-354 in the web UI and nothing merged. Its branches[] was empty, so the scripted finish was never a candidate, and the GUI gave no sign the click would not merge.

- **task-359** — A question can be asked without ever reaching the human

  A question can be raised while the ball stays on an agent, so no surface ever shows it. And a question is only closed by an entry typed `answer`, so a reply logged as a `decision` leaves it open and the human is asked twice.

- **task-360** — The draft question panel pastes a wall of text at the human

  A question entry is rendered into the draft panel whole, whatever its length. A 300-word question fills the screen as one unbroken bold block above the answer box, and the reader cannot find the ask inside it.

- **task-367** — Tasks shell: the polish pass after living in it

  The list-and-detail shell (task-235) shipped and works. Using it turned up four things that are improvements rather than blockers, and this epic holds them so task-235 can close on what it actually promised.

- **task-368** — Shell 8: drag the divider between the sidebar and the record

  The sidebar and the record panel sit at a fixed ratio. Make the boundary between them draggable wherever the two-region shell renders, remember where the user put it, and keep today's ratio as the default until asked otherwise.

  *part of task-367 (Tasks shell: the polish pass after living in it)*

- **task-369** — Playwright is now the gate's critical path: 107 e2e tests on one worker, 139s of a 246s gate

  The gate's shape inverted while nobody was re-measuring it. Task-268 measured `e2e` at 135.3s and 138.8s against `pytest` at 79.3s and 89.1s, so the single biggest remaining lever on gate time is Playwright's `workers: 1`. This is task-268's proposal 7, promoted from "defer" by that measurement, and it still needs the spec-by-spec audit that deferral was waiting for.

- **task-370** — A cancelled dispatch is still reported as `failed` under load, and it turns the gate red

  `test_cancelling_a_live_run_stops_it_and_marks_it_cancelled` failed inside task-268's scripted finish with `assert 'failed' == 'cancelled'` — the exact race `runner.py`'s `cancel_requested` guard was written to close, still open under 32 xdist workers. It cost that finish its whole gate and no merge, which is the expensive half.

- **task-372** — The analytics API: one endpoint that answers the four questions

  Build GET /api/projects/{project_id}/analytics, returning the backlog level and its flows, throughput and cycle time, the age distribution and oldest work, where work is stuck, and an explicit statement of how much history is real. One request for the whole page.

  *part of task-212 (Analytics page: move the count tiles off the dashboard and grow them into trends)*

- **task-373** — The analytics page and its charts

  Build /p/:projectId/analytics: a summary row with real deltas, then four panels answering whether the backlog is growing, whether work is finishing faster, whether anything is aging badly and where work is stuck. Inline SVG, no charting dependency, phone first.

  *needs task-372 · part of task-212 (Analytics page: move the count tiles off the dashboard and grow them into trends)*

- **task-374** — The Dashboard's entry point to analytics

  Put one text link, "Analytics →", on the Dashboard's existing "Active tasks" heading row, reaching the analytics page. One link, zero new vertical pixels, and the Dashboard still never scrolls.

  *needs task-373 · part of task-212 (Analytics page: move the count tiles off the dashboard and grow them into trends)*

- **task-375** — A resumed run records its posture and never tells the session, so an autonomous epic child stops at the merge gate anyway

  When a dispatch resumes an existing session instead of starting a cold one, the run record gets the new posture but the session receives only the ball prompt. It keeps operating under the posture clause from its original prompt. An epic dispatched `autonomous` therefore has its children hand off for review, and the walk stops with `child_needs_a_human` on a branch it was authorised to merge.

- **task-377** — A supervisor that discovers its children need sequencing has no move but to wake a human

  Three individually-correct rules compose into a dead end. When an epic supervisor learns mid-flight that one child must precede another, it cannot record the dependency, cannot start the one child, and cannot run the walk without starting both. The only exit is a human click, which is the thing the epic walk exists to avoid.

- **task-247** — Atomic writes for the machine-level YAML in ~/.agentjobs -- storage.yaml above all

  Every machine-level YAML in ~/.agentjobs/ is written by truncate-then-write, with no temp file and no os.replace. storage.yaml is the worst of them: it is the only record of which projects are on SQLite, so a torn write makes the server silently serve a stale tasks/ directory instead.

- **task-387** — `visibility: local` hides a project from the owner too, because every one of his surfaces arrives over the tailnet

  A project marked `visibility: local` is served only to `owner` and `run` principals, and `owner` is established by a loopback connection. The owner never connects over loopback -- his desktop, phone and tablet all reach AgentJobs through the Tailscale proxy -- so he arrives as `tailnet` and the two local-only projects are invisible on every surface he uses.

- **task-389** — A run whose meta lacks dispatch_entry_id is invisible to the poller for ever, and shows as Starting until somebody cancels it by hand

  A run whose meta lacks dispatch_entry_id is invisible to the poller for ever, and shows as Starting until somebody cancels it by hand

- **task-406** — A dispatched run cannot execute groom or reorder: both act on tasks other than their own, which task-332 forbids

  `agentjobs playbook run groom` dispatches a run at the playbook's own run task, and task-332 scopes every task verb a run holds to that one task. Groom's whole executable set is closing other tasks and reorder's is moving them, so both playbooks refuse `wrong_task` at their first real write.

- **task-407** — Any task written during a finish's gate turns the roadmap stage red, so the merge is declined for a reason that has nothing to do with the branch

  The roadmap stage compares a committed file against a live database, and `agentjobs finish` re-gates before merging. So anybody filing, closing or claiming a task during that 200-second window declines a merge on a branch that never touched the roadmap.

- **task-408** — scripts/bench.py measures an empty backlog: it seeds a directory of YAML nothing serves

  The benchmark builds its corpus by writing task YAML into a throwaway project root, which stopped being a backlog when task-402 deleted the file backend. Every run since has timed an empty store, and the detail endpoint 404s.

## Medium (40)

- **task-243** — The grandchild-kill test asserts on a moment, and loses the race under load

  `tests/test_dispatch_runner.py::TestProcessGroup::test_the_timeout_kills_the_grandchild_too` fails intermittently on a loaded machine — twice on 2026-08-22, once at each of its two assertions. The feature looks fine; the test has no settle window, and on Windows the group kill it is waiting for is asynchronous. It cost task-241's merge a full gate cycle, and under the scripted finish every occurrence costs an escalation.

- **task-105-next-task-id-all-slugged-records** — Generate the next task number from all slugged task IDs

  Fix automatic task creation so repositories using IDs such as task-104-react-readme-screenshots generate the next corpus number instead of restarting from task-001 or task-002.

- **task-015** — Stop one long ball_prompt from swallowing a task list row

  In the task list, the Status column renders a task's entire ball_prompt untruncated, so a task carrying a long prompt produces a several-hundred-word wall of text in a single table row. The dashboard already truncates the same field; the list does not.

- **task-053-schema-v2-cli** — Schema v2: CLI mirrors -- inbox, next, claim, handoff, log, close

  Mirror the v2 API surface in the Typer CLI: agentjobs inbox / next / claim / handoff / log / close, rendering display_status and ball_prompt so the handoff loop is fully drivable from a terminal.

  *part of task-063-schema-v2 (Schema v2: finish the dedicated CLI state verbs)*

- **task-013** — Ship sourcemaps so a production stack trace names a source line

  The React bundle is built without sourcemaps, so a runtime error in the served app reports a column offset into one minified line. Diagnosing a crash currently means grepping the source for the suspected pattern rather than reading the stack.

- **task-295** — The pinned top bar may sit under the iOS status bar in the installed PWA

  Now that the header is pinned to the top of the viewport (task-292), a home-screen-installed PWA has no browser chrome above it, so the bar sits at the physical top of the display — under the clock, the battery and the notch. Nothing in the app reads `env(safe-area-inset-top)` and the viewport meta has no `viewport-fit=cover`, so there is nothing in place to stop it.

- **task-197** — The session poller resolves the project's default runner, so a run dispatched from a group is polled with the wrong executable

  poll_live_sessions rebuilds a DispatchRunner from assert_dispatch_permitted(project_id, home) with no group, so it resolves whatever runner the project defaults to rather than the one the run was actually started with. When those differ, every poll of that run fails and the run is never concluded.

- **task-201** — Make supervision posture an explicit choice, not the supervisor's guess

  When Jeff asks a session to supervise a dispatched run, there is no way to say which kind of supervision he wants. Watching for completion and actively steering the working agent are different jobs with different costs, and the supervisor currently picks one by guessing.

- **task-104-react-readme-screenshots** — Stage and capture polished React screenshots for README

  Create at least one polished, current screenshot of the React application and restore a strong visual near the top of README.md; use a second phone or tablet image only when it adds distinct product value.

- **task-106-physical-device-pwa-verification** — Verify the React PWA on a physical phone and tablet

  Complete the physical-device checks intentionally left pending in the mobile/PWA implementation and React parity epic: install, private HTTPS access, standalone launch, upgrade, reconnect, and second-device setup.

- **task-125-plugin-install-without-a-clone** — Make the AgentJobs plugin work for someone who only installs it

  Installing the AgentJobs plugin from a marketplace gives a user files, not a working system: the MCP server is a REST client with no service to talk to, the Python package it invokes is not installed, and the write guard silently protects nothing because the machine has no project registry. This epic makes a plugin install sufficient on its own.

- **task-127-publish-and-invoke-without-a-venv** — Publish the package and invoke it without a clone or a virtualenv

  The plugin's .mcp.json runs the bare console script `agentjobs`, which only exists if the user pip-installed the package or cloned the repo. Publish AgentJobs to PyPI and change the invocation to something that resolves on a machine with neither.

  *part of task-125-plugin-install-without-a-clone (Make the AgentJobs plugin work for someone who only installs it)*

- **task-128-onboarding-from-inside-a-session** — Let a new user create and register their first project from the agent session

  A successful plugin install leaves a user with working tools and nothing to point them at: `agentjobs init` exists only as a CLI command, unreachable for someone whose entire surface is the plugin. Give the MCP server a way to create and register a project so onboarding happens in conversation.

  *part of task-125-plugin-install-without-a-clone (Make the AgentJobs plugin work for someone who only installs it)*

- **task-129-guard-silent-noop-on-empty-registry** — Stop the write guard from silently protecting nothing

  When the machine registry is missing or empty, `managed_directories()` returns an empty list and the guard allows every write while appearing installed and healthy. Make that state observable instead of silent.

  *part of task-125-plugin-install-without-a-clone (Make the AgentJobs plugin work for someone who only installs it)*

- **task-142-embed-settings-and-identity** — Settings for the embedded widget, and who the host app says is reporting

  The widget needs a settings panel, and the interesting part is deciding which settings belong to the widget and which belong to the project's committed config in AgentJobs. Also settle how a host app -- which has no AgentJobs session -- says who is reporting.

  *needs task-141-embed-launcher-and-project-link · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-145-embed-zero-config-wiring** — One dependency and one component: make the widget wire itself up

  A host project already has `.agentjobs/config.yaml` and is already in the machine registry, so it should not have to be told its own project id and server URL by hand. Discover what can be discovered at dev-server start, and count the steps that remain.

  *needs task-141-embed-launcher-and-project-link · part of task-137-embedded-helper-ui (Embedded AgentJobs: drop the helper UI into an app you are building)*

- **task-150-chain-authorization** — Chain authorization: what a human approves before a loop may run

  The record a human writes to authorize a bounded chain of dispatches: the task, the iteration cap, the wall-clock ceiling, and a digest freezing the checks as they stood. Plus revocation. No driver yet -- this is the thing the driver will not be allowed to run without.

  *needs task-149-check-trigger · part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-151-loop-driver** — The loop driver: iterate, evaluate, and stop loudly

  Run an authorized chain: dispatch, wait for the run to reach a terminal state, evaluate the checks, compare against the previous vector, and decide whether to go again. Thrash detection, regression guard and every ceiling, each stopping with the ball on a human and the reason named.

  *needs task-150-chain-authorization · part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-152-loops-gui** — Check results and chains in the web UI

  Show each acceptance criterion's latest check result on the task page, let a human run the checks, and -- if the loop is built -- show a chain's iteration history with revoke in one click.

  *needs task-149-check-trigger · part of task-078-agent-loops (Agent loops: make acceptance criteria executable so a loop can stop itself)*

- **task-156-task-difficulty-field** — Add a difficulty field to the task schema, with list filtering

  Add an optional task-level difficulty of routine, standard or hard, defaulting to standard when absent while recording that it was defaulted rather than declared, and expose it for filtering in the CLI and the React list surfaces.

  *part of task-080-dispatch-model-profiles (Design dispatch profiles for model, reasoning, and task difficulty)*

- **task-162-author-the-dispatch-prompt** — Say something to the agent you are dispatching

  Dispatch generates a fixed prompt stub pointing the agent at the task record, with no way for the human pressing the button to add anything. Let them edit or extend it at dispatch time, on both the per-task and the project-level button.

  *needs task-161-project-level-dispatch · part of task-160-dispatch-phase-two (Dispatch phase two: who can cause one, what it can be told, and what it does with an epic)*

- **task-163-role-dispatch** — One agent's handoff starting a different agent, when a human configured it to

  Research and design: an implementation agent finishes, hands the task to review, and a reviewing agent starts on it, with no human in between. The design already permits bounded autonomous chains, but only iterating on one task against its own checks -- a handoff to a different role and a different runner is not covered. Answer whether it should be, and how it stays bounded.

  *part of task-160-dispatch-phase-two (Dispatch phase two: who can cause one, what it can be told, and what it does with an epic)*

- **task-170-dispatch-in-the-menu** — Dispatch from the menu: start a run, and reach its settings

  Put dispatch in the menu — both halves. Starting a project-wide run, and the settings that govern whether runs happen at all, reachable from any page in the app and from a host app that has no AgentJobs routes to navigate to.

  *needs task-168-menu-shell · needs task-161-project-level-dispatch · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-176-dispatch-on-create** — Start an agent on the task you just created, off by default

  A checkbox on the create and report forms — unchecked by default — that dispatches the new task the moment it is filed. Notice, describe, and have an agent working on it without a second visit to the task.

  *needs task-168-menu-shell · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-310** — With a machine-wide default_group, `dispatch enable --runner` is a silent no-op for every project

  Found by the task-184 session while establishing sc-3 against a throwaway home. On a machine whose `dispatch.yaml` sets a top-level `default_group:`, enabling a project with `--runner <name>` appears to succeed and changes nothing — the project still resolves through the default group. It affects the CLI as well as the browser. See log entry 2: this is live on Jeff's machine, contrary to what the finding session reported.

- **task-322** — The finish panel reports only the newest attempt, so a merged branch can read "nothing was merged"

  The task page's finish panel shows the most recent finish for a task. When an earlier attempt merged and a later one stopped, its headline says "Stopped — nothing was merged" about a branch that is in `main`. Carry the earlier merge into the panel so it cannot contradict the record.

- **task-323** — The context budget measures only the repository half of the @-chain, but calls itself the always-loaded bundle

  The bundle budget caps four repository files, but a session loads more than that. Decide what the cap should claim to cover, and whether the rest can be measured at all.

- **task-342** — There is no way to specify priority when reporting issues

  There is no way to specify priority when reporting issues

- **task-343** — The approval note is dropped the moment the scripted finish takes over

  Approving with a note records it on the task, then the scripted finish merges without it -- the note reaches no merge commit, no prompt, and no agent.

- **task-348** — A finish whose gate went red should re-rebase and re-run that stage once before escalating

  When the gate goes red and main has moved since the rebase, the red may belong to the old base rather than to the branch. Re-rebase once, re-run only the stage that failed, and only when gate_scope classifies every moved path. Escalate if it is still red.

- **task-349** — The task page lights a Dispatch button on a task that already has a live run, so clicking it can only fail

  A task with a live run still shows Dispatch as a primary, enabled action. Clicking it is refused with LiveRunExistsError every time. The control should say a run is live and offer to open or stop it instead.

- **task-350** — The slot board at a ceiling of 1 wastes its width on a single task

  Jeff, reviewing task-092 on 2026-09-05: with `max_concurrent_runs: 1` the board draws one full-width cell for one task, and most of that width is empty. The "why this one first" expander barely fills it. Find a layout for the one-slot board that earns its space, without changing what the board says.

- **task-353** — GUI space also wasted in button and field widths

  GUI space also wasted in button and field widths

- **task-379** — Decide whether the dispatch subsystem moves inside the server

  The storage migration left dispatch as the one CLI family that still opens the database directly. Three security guards make it so, and the alternative is a real architectural change: the dispatch verbs become thin launchers and the epic walk runs server-side.

- **task-382** — Edit a task's spec prose and its list fields from the browser

  Task-230 made the scalar authoring fields editable in the browser - priority, effort, category, title and tags. The spec prose and the object lists are still read-only there, and the patch route already accepts every one of them.

- **task-383** — The browser offers a human the workflow verbs on any task, not only on one parked at review

  The task panel's verbs render only when the ball is with the human, or on a held task. So a human looking at a task stuck with an agent has no way to release it, and one looking at a ready task has no way to park it - both are entitlements a person holds, reachable today only from the CLI.

- **task-404** — A Playwright browser that dies at launch turns the whole gate red, and nothing distinguishes it from a real failure

  One e2e test failed with `browser.newContext: Target page, context or browser has been closed` -- the browser process died during launch, before the test ran. The stage went red, so the gate went red, and the only way to learn it was infrastructure was to re-run and watch it pass.

- **task-405** — Groom the agentjobs backlog

  A run of the `groom` playbook over the 136 open tasks: find duplicates and superseded work, propose a closure list, and close only what the owner approves.

- **task-409** — --since-gate can skip pytest on evidence that no longer covers it: its corpus checks read a store outside the tree

  The pytest stage loads this repository's own backlog from the database, which anybody filing or closing a task moves. A diff over the working tree is therefore no evidence about it, which is the exact argument that put the roadmap stage in UNBOUNDED_STAGES.

- **task-412** — The roadmap playbook's last act is a commit on a branch, and it never says how that branch reaches main or comes down

  The roadmap playbook ends at "commit the result" and never says where that commit goes. A run that follows it exactly can leave a branch and a worktree behind and still satisfy the playbook's own acceptance criteria.

## Low (7)

- **task-195** — A dispatch refusal outlives the state that produced it, and reads as an answer to a click nobody made

  The dispatch panel keeps the last refusal from pressing Dispatch in React state and renders it unconditionally. When the page state moves on — the machine gate closes, the project is disabled, the task closes — neither control renders any more, but the old refusal is still on screen, announced as an alert, reading as a response to a click that did not happen.

- **task-173-transcription-fallback** — A transcription path for browsers with no speech API — build it or close it

  Record audio in the browser and transcribe it on the AgentJobs server, covering Firefox and anything else without the Web Speech API. Exists to be decided as much as built: if task-171 finds the native paths cover the devices that matter, close this unbuilt with the reasoning.

  *needs task-171-voice-input-decision · needs task-172-dictation-control · part of task-167-agentjobs-menu (The AgentJobs menu: one reusable control for every frontend, and forms fast enough to fill on a phone)*

- **task-178-runner-authoring-in-the-gui** — Authoring runners and groups from the browser, without reopening gate 3

  Today the Dispatch page can point a project at a runner that already exists and can never create one, because creating one is writing an argv that this machine will execute. Decide whether a browser may author a runner or a runner group at all, and if so under what proof that the person clicking is at the machine — then build whatever that decision allows.

  *part of task-160-dispatch-phase-two (Dispatch phase two: who can cause one, what it can be told, and what it does with an epic)*

- **task-287** — The docs site cannot render Mermaid: mkdocs-material empties the diagram containers and draws nothing

  Any Mermaid diagram added under docs/ is invisible on the built site. The fence is configured and the library loads, but the rendered page has three diagram containers and zero SVGs — mkdocs-material replaces each pre.mermaid with an empty div. Found 2026-08-22 while previewing the README rewrite (task-286); the README itself is unaffected because GitHub renders Mermaid natively.

- **task-319** — Retire the root .mcp.json and let the plugin be the only agentjobs MCP server on this machine

  Since task-317 fixed the plugin's dead port, this machine runs two working `agentjobs` MCP servers where one will do. The plugin should be the survivor, but two things have to be checked before the root `.mcp.json` is deleted, and neither has been.

- **task-381** — The CLI says it created a .yaml file on a project that has no files

  The CLI says it created a .yaml file on a project that has no files

- **task-396** — scripts/corpus_stats.py reads the tasks directory, so on a migrated project it measures the frozen copy

  The record-quality baseline in docs/task-corpus-audit.md is regenerated by scripts/corpus_stats.py, which resolves this repository's records by composing a directory from .agentjobs/config.yaml. Since the 2026-09-07 cutover that directory is a frozen copy, so the script silently reports a stale baseline. Route it through the store.

---

29 further open tasks are drafts, not listed here. A draft is an idea that has not been specified yet: no agent can claim one and the queue does not hand one out.
