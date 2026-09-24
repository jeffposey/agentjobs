# The end-to-end suite

Playwright, driving a real browser against a real AgentJobs server. What is here is what
only a browser can answer: layout at a width, a badge that refreshes without a reload, a
click that starts a process on this machine. Anything a jsdom render can prove belongs in
`src/` with the rest of the Vitest suite, which costs a tenth as much.

## One server per worker

`playwright.config.ts` starts **one `run_server.py` per worker**, each on its own port,
and `e2e/fixtures.ts` hands each worker the URL of its own. A spec reaches its server by
importing `test` from `./fixtures` rather than from `@playwright/test`; nothing else in a
spec knows there is more than one.

Each server builds its own throwaway project, its own throwaway `AGENTJOBS_HOME` and its
own copy of `frontend_dist`, so a worker owns its tasks, its queue order, its database,
its dispatch configuration, its run ledger and the bundle it serves outright. That is
what makes the table below a description rather than a constraint: **no spec in this
directory has to be quarantined**, because no spec can see another worker's writes.

The bundle copy was the one that had to be found by running it. Isolating the project and
the home was the design; isolating the bundle was a defect report from the first parallel
measurement, and it is why the four-way categorisation below has a `bundle` row that a
reading of the specs alone did not produce.

Within a worker, files still run one at a time (`fullyParallel: false`), so a spec that
puts machine-level state back the way it found it is still doing something necessary --
the next file on that worker inherits it.

## What each spec touches

The point of the audit. A spec's row says the strongest thing it does, and the three
rows mean:

- **read-only** -- navigates and asserts. Files nothing, changes no setting. Safe beside
  anything, on any server.
- **project** -- creates, edits, closes or reorders tasks, or reads a project-wide list
  or count to make its assertion. Needs a project of its own; several of these would be
  wrong, not merely flaky, beside a neighbour filing into the same one.
- **machine** -- writes the machine-level dispatch configuration or starts a real run
  process. Needs a home of its own as well as a project.
- **bundle** -- writes the built frontend the server is serving. The category the first
  audit missed, and the one that cost a measurement: `capture-draft.spec.ts` rewrites
  `sw.js` and `build-info.json` to make a tab believe the app was rebuilt under it, and
  until `run_server.py` gave each server a private copy of the bundle, that write reached
  every other worker's service worker and reloaded three browsers mid-interaction.

`frontend/src/__tests__/e2e-inventory.test.ts` fails when this table and the directory
disagree, so a new spec cannot be added without classifying it.

| Spec | Touches | What makes it that |
| --- | --- | --- |
| `actions-menu.spec.ts` | project | Files fixture tasks over the API and acts on them from the menu. |
| `analytics.spec.ts` | project | Files and closes tasks to draw the charts it measures. |
| `answer-questions.spec.ts` | project | Files a task carrying a question and answers it. |
| `attach-screenshot.spec.ts` | project | Files a task through the capture form, with an image. |
| `attention-badge.spec.ts` | project | Reads the project's whole blocking count and asserts it rose by one. |
| `capture-draft.spec.ts` | bundle | Rewrites `sw.js` and `build-info.json` to provoke a real service-worker reload; also files from the tray. |
| `capture-tray.spec.ts` | project | Files from the tray, then lists every task in the project to find it. |
| `create-task.spec.ts` | project | Files a task through the create form and reads the record back. |
| `dashboard-one-screen.spec.ts` | project | Files the corpus the Dashboard summarises. Its live-run panel is a `page.route` stub, which is browser-local and not machine state. |
| `dictation.spec.ts` | read-only | Opens the capture dialog and asserts on its controls; files nothing. |
| `dispatch.spec.ts` | machine | Turns dispatch on for the project, starts a real process, cancels it, turns it off. |
| `dispatch-one-click.spec.ts` | machine | Enables dispatch over the API and starts a real process. |
| `draft-spec.spec.ts` | project | Drafts against the stub provider, then files the result. |
| `edit-fields.spec.ts` | project | Files a task and edits its fields in place. |
| `filter-popover.spec.ts` | project | Files a tagged corpus and filters it. |
| `list-divider.spec.ts` | read-only | Drags the list/record divider; its only write is the browser's own `localStorage`. |
| `live-runs.spec.ts` | machine | Asserts the machine's live-run count and slot board, against a real run. |
| `nav-current.spec.ts` | read-only | Walks the nav at several widths; files nothing. |
| `perf-budget.spec.ts` | project | Files a corpus large enough to measure the list against a budget. |
| `pinned-header.spec.ts` | project | Files a task and scrolls its page at several widths. |
| `promote-draft.spec.ts` | project | Files a draft and promotes it. |
| `queue-move-check.spec.ts` | project | Reorders the project's queue. |
| `queue-order.spec.ts` | project | Reorders the project's queue. |
| `report-issue.spec.ts` | project | Files an issue from the header control and reads the record back. |
| `review-findability.spec.ts` | project | Files tasks waiting on review and finds them. |
| `task-detail-fit.spec.ts` | project | Files a task and measures its detail page. |
| `task-list-density.spec.ts` | project | Files a corpus and measures the list's density. |
| `tasks-shell.spec.ts` | project | Files a crowd of its own so the list overflows, drives the shell, then closes them. |
| `task-tree.spec.ts` | project | Files a parent and children and folds the tree. |
| `version-skew.spec.ts` | read-only | Stubs `/api/version` in the browser; touches no server state. |

## Running it

```bash
cd frontend
npx playwright test                       # all of it, on four workers
npx playwright test e2e/dispatch.spec.ts  # one file
AGENTJOBS_E2E_WORKERS=1 npx playwright test   # serial, for readable output
```

`AGENTJOBS_E2E_PORT` names the **first** of the run's ports; the rest are 10000 apart.
`AGENTJOBS_E2E_WORKERS` is capped at 4, and `e2e/ports.ts` says why.
