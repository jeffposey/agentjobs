# 09 — React frontend

Auditor 9, Big Dawg Audit (task-242), night of 2026-08-21 → 2026-08-22. Read-only.
Scope per PLAN.md: `frontend/src/` (App, components, generated client, queryClient,
pwa, service worker, report/, styles), the vitest suite, the Playwright specs, and
the server-side serving of the bundle where the brief's questions required it
(`src/agentjobs/api/spa.py`). Live checks were GETs against the running server on
8876 (`curl -s -D -`), and a read of the live task list for enum spellings.

Headline: the frontend is in better shape than most of what the audit plan expected
to find. Enum values reach the DOM as plain strings (verified live), the generated
client is pinned against the schema at build time, the revision-poll invalidation
allowlist has a drift test, and the Playwright specs assert rendered text against a
real server. The real findings cluster in one place — **the machinery that is
supposed to get a new bundle onto an open tablet does not do that** — plus a
documented HTTP fallback path on which four features throw, and a handful of
vocabulary mismatches between what a filter says and what a badge says.

Severity counts: P1 0 · P2 3 · P3 9 · P4 6.

---

## Findings

### P2-1 — `/app/` shell is served with no `Cache-Control`, and the service worker precaches it through the HTTP cache

**Evidence.**
- `src/agentjobs/api/spa.py:77-90` — `shell()` returns `FileResponse(index)` with no
  headers. Compare `:50` (manifest) and `:67` (sw.js), which both set
  `Cache-Control: no-cache`.
- Live, 2026-08-22 05:29 UTC:
  ```
  GET /app/          → last-modified, etag, NO cache-control
  GET /app/sw.js     → cache-control: no-cache
  GET /app/manifest  → cache-control: no-cache
  ```
- `tests/test_spa.py:70` asserts `no-cache` on `sw.js` only. Nothing asserts anything
  about the shell's headers, so this cannot regress loudly because it was never
  pinned.
- `frontend/src/service-worker.js:7` — `cache.addAll(SHELL_URLS)`, where `SHELL_URLS[0]`
  is `"/app/"` (`scripts/build-service-worker.mjs:13`). `addAll` fetches with the
  default cache mode, i.e. through the HTTP cache.

**Why it matters.** A response with `Last-Modified` and no explicit freshness is
eligible for heuristic freshness (RFC 9111 §4.2.2; Chromium and Firefox both use
~10% of the document's age). A bundle built a week ago is therefore heuristically
fresh for ~17 hours. Two consequences:

1. **The offline shell can be the wrong shell.** When a new `sw.js` installs, its
   `addAll` can receive the *previous* `index.html` from the HTTP cache while the
   asset URLs it precaches are the new hashed ones. The new cache then holds an
   `index.html` whose `<script src>` points at assets the activate step just deleted
   (`service-worker.js:15`). Online this is invisible — navigations are network-first
   (`:36`). Offline, `caches.match("/app/")` serves an HTML page whose assets 404:
   a blank page, which is exactly what `docs/mobile-access.md:163-164` says the
   offline screen must not be.
2. **On the documented plain-HTTP fallback** (`docs/mobile-access.md:130-147`), where
   there is no service worker at all, a navigation can be served the old `index.html`
   from the HTTP cache for that heuristic window with no self-heal except a manual
   reload. This is the stale-bundle incident class (ENGINEERING.md, 2026-08-17)
   reproduced client-side.

**Fix.** In `spa.py`, give `shell()` `headers={"Cache-Control": "no-cache"}` (same as
its siblings) and add an assertion for it to `test_spa.py` on both `/app` and a deep
link. In `service-worker.js`, precache with `new Request(url, { cache: "reload" })`
for `/app/` so the offline shell is always the one the build wrote. Hashed assets can
stay as they are.

**Open verification.** I did not reproduce the heuristic-cache behaviour on a device;
the claim rests on the headers above and the caching RFC. The physical-device check
in `docs/mobile-access.md:157-165` would *not* catch it as written, because step 1
installs build A seconds after building it (age ≈ 0 → heuristic window ≈ 0).

### P2-2 — An open tab or foregrounded PWA never learns that a new bundle exists

**Evidence.**
- `frontend/src/pwa.ts:23-27` — `register("/app/sw.js")` is called once, on `load`.
  That is the only thing in the app that can trigger a service-worker update check.
- `frontend/src/components/LiveUpdates.tsx:135-150` — the poller reads only
  `/api/projects/{id}/revision` (task-file revision). Nothing polls a bundle or server
  identity. `grep -rn "registration\|\.update()" frontend/src` finds no call to
  `ServiceWorkerRegistration.update()`.
- `GET /api/version` (live) carries `source_commit` and `started_at`; nothing in
  `frontend/src` reads either.
- `docs/mobile-access.md:153-155` claims the upgrade path is "the worker activates
  immediately … and the app reloads once", and the release check at `:161` says
  "Relaunch **or foreground** the installed app". Foregrounding is not a navigation,
  and browsers run the service-worker update check on navigation, `register()`,
  `update()` and functional events — not on visibility change. The
  `visibilitychange` handler at `LiveUpdates.tsx:177-179` re-polls the revision
  endpoint and nothing else.

**Consequence.** A tablet that resumes the installed app from the background (the
normal phone gesture) keeps the bundle it was launched with until the OS evicts the
window. There is no bound. The scripted finish (task-241) rebuilds and restarts the
server with no agent in the loop, so this is now the ordinary way a bundle changes
underneath an open PWA, and the first symptom is a generated client disagreeing with
the API it is talking to.

**Fix.** Add the bundle revision (the `agentjobs-shell-<hash>` string is already
computed at build time, `build-service-worker.mjs:25`) or `started_at` to the
`/revision` response, compare it in the existing poller, and on change call
`navigator.serviceWorker.getRegistration().then(r => r?.update())`. The existing
`controllerchange` → reload path then does the rest. **Do not let that reload fire
while a form is dirty** — today the reload can only happen within seconds of page
load; once updates are polled it could land mid-sentence in a feedback box, and
`TaskDetail.tsx:334-346` and `DispatchPanel.tsx:311-323` both exist because losing
typed prose was judged worse than a stale screen. A "new version — reload" banner
beside `LiveUpdateStatus` is the safe shape.

### P2-3 — `crypto.randomUUID()` throws on the documented plain-HTTP fallback origin

**Evidence.**
- `frontend/src/App.tsx:180,193` (queue move, reprioritize),
  `frontend/src/components/IssueReporter.tsx:101` (file issue),
  `frontend/src/report/attachments.ts:85` (attach a screenshot) all call
  `crypto.randomUUID()`.
- `randomUUID` is `[SecureContext]`: defined on `https:` and on `localhost`, undefined
  on `http://100.x.y.z:8765/app/` — which `docs/mobile-access.md:130-147` documents as
  a supported "Direct-bind fallback (no HTTPS)" and describes as "browser access only".
- `TaskList.tsx:274-285` catches the resulting `TypeError` and renders
  `"task-x could not be moved, so the list has been put back the way the server has
  it. Reload and try again."` — advice that cannot work.

**Consequence.** On the fallback origin: every reorder fails with a misleading
message, filing an issue fails, and pasting a screenshot fails. The doc warns about
install and offline, not about these. Severity is P2 only if that path is used; if
Jeff has never bound to a LAN/tailnet IP, read it as P3 and fix the doc.

**Fix.** One `newOperationId()` helper in `frontend/src/api/` that falls back to a v4
built from `crypto.getRandomValues` (available on insecure origins), used at all four
sites; and a sentence in `mobile-access.md` stating what the fallback cannot do.

### P3-4 — The "Blocked" status filter does not match rows the list labels "Blocked"

**Evidence.**
- `frontend/src/components/TaskList.tsx:357` — the option labelled **Blocked** has
  `value="external"`; `:122-125` matches it against `task.ball === "external"` only.
- `frontend/src/components/DependencyState.tsx:48-53` — a task with `unmet_needs`
  (ball = agent) is rendered with the badge **Blocked**.
- Live corpus today: `ball` ∈ {agent: 89, human: 7, null: 144}; zero `external`. So
  selecting the "Blocked" filter on the real backlog shows nothing while several rows
  carry the word.

**Fix.** Either label the option "Blocked on external" or make the filter use
`dependencyState(task).kind === "blocked"`, which is the vocabulary the row shows.
Add a test that filters by "Blocked" and expects a row whose badge reads Blocked.

### P3-5 — Reject is the one mutation that navigates without invalidating

**Evidence.** `frontend/src/App.tsx:425` — `onReject` awaits the mutation and then
`navigate(..., { replace: true })`; every other handler in the file calls
`refresh()` / `queryClient.invalidateQueries()` first. With `staleTime` = 30 s
(`queryClient.ts:17`), the list the user lands on is served from cache and still shows
the rejected task as open until the revision poller (≤ 15 s, `LiveUpdates.tsx:6`)
notices. Not "until reload" — the poller is a real backstop — but it is the answer to
the brief's question "which mutation's result will a user not see immediately".

**Fix.** `await refresh()` before `navigate`, or `invalidateProjectTaskQueries`.

### P3-6 — `ConnectionUnavailable`'s `offline` state is unreachable, and a 404 project is reported as a dead server

**Evidence.**
- `frontend/src/App.tsx:116` and `:157` are the only renders, both `offline={false}`.
  `navigator.onLine` is never read anywhere in `frontend/src`. The "You're offline"
  heading and `ConnectionUnavailable.test.tsx:7-14` test a state the application
  cannot produce — decoration by ENGINEERING.md's definition.
- `GET /api/projects/nope/dashboard` (live) → **404**
  `{"detail":"Unknown project 'nope'. Registered projects: agentjobs, fantasy-football, …"}`.
  `DashboardPage` turns any error into "AgentJobs cannot be reached … wake the computer
  running AgentJobs, then reload." The server answered, by name, and the page says it
  did not.

**Fix.** Branch on `readRefusal(error)` / response status before falling back to the
unreachable card; pass `offline={!navigator.onLine}`.

### P3-7 — Reordering has no touch path, on the device the backlog is read from

**Evidence.** `TaskList.tsx` offers two gestures: `Alt+Arrow/Home/End` on a focused
grip (`:313-320`) and HTML5 drag (`:446-466`). `e2e/queue-order.spec.ts:6-9` states
drag "cannot be performed on the phone and tablet this backlog is actually read from,
so the keyboard path is the one that has to keep working" — and a tablet has no Alt
key. HTML5 `draggable` does start from touch on iPadOS Safari (platform behaviour,
untested here) and does not on Android Chrome. GLOBAL-AGENTS.md names the phone and
tablet as where Jeff reads the tracker.

**Fix.** Explicit "move up / move down / to top / to bottom" controls reachable from
the grip (a small menu on tap), which also closes P3-8 for keyboard users who cannot
hold Alt. The server verbs already exist; this is UI only.

### P3-8 — Keyboard users cannot change a task's band, and the band-change dialog receives no focus

**Evidence.** A band change is reachable only by dropping across bands
(`TaskList.tsx:328-332`); the keyboard steps are computed within the band
(`queueOrder.ts:54-77`). When the `role="alertdialog"` at `:380-411` opens, nothing
moves focus into it; a screen-reader user who did manage a cross-band drop is left on
the grip. The e2e and jsdom tests click the button directly, so neither asks.

**Fix.** Keyboard band change (e.g. `Alt+Shift+Arrow`, or the menu from P3-7), and a
`useEffect` that focuses the dialog's first button on open and returns focus to the
grip on close.

### P3-9 — Report-issue dialog: `aria-modal` without a focus trap, and focus is not returned on close

**Evidence.** `IssueReporter.tsx:128-134` sets `role="dialog" aria-modal="true"`;
`:69-71` moves focus to the title on open; `:73-79` closes on Escape. Nothing prevents
Tab from leaving the dialog into the page behind it, and on close the trigger button
(`:35-41`, still mounted) does not get focus back — it lands on `<body>`. Same shape
as task-207's lesson: a focused node is removed and nobody puts focus anywhere.
Smaller instance of the same thing: `TaskDetail.tsx:245` unmounts the whole review
panel the moment a send-back lands while its Submit button holds focus.

**Fix.** A focus trap (cheap to hand-roll: Tab/Shift+Tab wrap on the dialog's
focusable set) and `triggerRef.current?.focus()` in `onClose`.

### P3-10 — `REFUSAL_ACTIONS` is a second copy of the server's reason vocabulary with no drift check

**Evidence.**
- `DispatchPanel.tsx:43-69` keys on 17 reason strings. The server types `reason` as a
  free `str` — `grep -n "Literal\[\|class .*Reason" src/agentjobs/dispatch/guards.py
  src/agentjobs/api/routes/dispatch.py` is empty — so `openapi.json` emits `string`
  and the generated types cannot catch a mismatch.
- Strings `"no_eligible_runner"` and `"undefined_runner"` appear as reason values in
  `src/agentjobs/dispatch/` and have no browser entry. Whether the dispatch endpoint
  can return them to a browser I did not trace; what is certain is that nothing would
  tell anyone if it did. Contrast `LiveUpdates.drift.test.tsx`, which exists precisely
  so an allowlist like this cannot drift silently.
- Failure mode is soft: `:147-149` falls back to `suggestedAction || REFUSAL_ACTIONS[r]`,
  so an unknown reason shows the server's message with no remedy line.

**Fix.** Make `reason` a `Literal[...]` on the server so the schema carries the enum;
then `generated-contract.ts`-style `@ts-expect-error` pins plus a test that every
literal has a `REFUSAL_ACTIONS` entry or an explicit exemption.

### P3-11 — Approve / reject / send-back errors discard the server's reason

**Evidence.** `App.tsx:396-407` — `actionError` is collapsed to `"The action could not
be recorded. Reload and try again."` for all five review mutations. The promote and
note handlers on the same page (`:440-446`, `:457-467`) read `readRefusal()` and show
the server's `message`, and promote distinguishes `revision_conflict`. An approve that
races another surface (the case `promote-draft.spec.ts:92-118` proves matters for
promote) gets the generic sentence.

**Fix.** Route the five through `readRefusal` the way promote does.

### P3-12 — `npm run dev` proxies to 8765, the port GLOBAL-AGENTS.md says never to trust

**Evidence.** `frontend/vite.config.ts:20-23` → `http://127.0.0.1:8765`;
`frontend/README.md:40-41` documents the same. The real dashboard is on 8876 and the
standing instructions describe a stale server on 8765 as a known hazard. A developer
following the README gets either nothing or the stale process. Not a production
defect; it is the kind of doc/config drift the docs auditor is hunting.

**Fix.** Read the target from an env var with a loud default, and mention 8876 in the
README.

### P4-13 — Raw enum tokens rendered as human-facing text

Not the `Ball.HUMAN` class — live API values are plain lowercase strings (see
§1 below) — but places where the token, not a label, is what a person reads:
`Dashboard.tsx:154` (`ball_reason` as the backlog "Reason" column: "spec",
"decision"), `DispatchPanel.tsx:374` (`posture`), `:415` (`run.mode`),
`TaskDetail.tsx:610` (`({ball}/{ball_reason})`). `DispatchPanel.test.tsx:427` pins
that *outcomes* are never shown raw; these were left out of that rule.

### P4-14 — `PRIORITY_CLASSES[...]` unguarded in two of three places

`TaskList.tsx:497` and `TaskDetail.tsx:571` index the map without a fallback, so an
unexpected priority yields `className="… undefined"`. `Dashboard.tsx:58` guards with
`?? priorityClasses.medium`. `Priority` is a closed union, so only server drift reaches
this; worth one line each for consistency.

### P4-15 — Every browser mutation costs two full refetch cycles

`LiveUpdates.tsx:143-150` — `currentRevision` is updated only by the poll, never by the
app's own writes. A mutation invalidates and refetches (`App.tsx:183`, `:299`, `:393`
…), then the next poll sees a revision it did not know about and invalidates again.
Harmless at 240 tasks; noted because `queryClient.ts` exists specifically to stop
refetching data the app already has.

### P4-16 — Dead component, stale directory, drifted counts

- `frontend/src/components/TaskCount.tsx` is imported by nothing outside its own test
  (`grep -rn TaskCount frontend/src --include=*.tsx | grep -v test` → the definition
  only). `README.md:89-90` still cites it as "the first example". Decoration.
- `frontend/dist/` exists with `index-cQWpbDfX.js` and its own `sw.js`; the real
  output is `src/agentjobs/frontend_dist/` (`vite.config.ts:9`, `index-CekVzuli.js`).
  Both gitignored (`.gitignore:9-10`); the stale one will mislead the first person to
  `ls` it.
- ENGINEERING.md says "26 Playwright tests"; `grep -c "^\s*test(" frontend/e2e/*.spec.ts`
  sums to **22** across 8 files. `frontend/README.md:93-95` says Playwright "is
  deliberately limited to one high-value path". Both stale (for auditors 1 and 2).

### P4-17 — The Playwright suite shares one server and one project across specs, by design and with cleanup in the specs

`playwright.config.ts:67-68` — `workers: 1`, `fullyParallel: false`. Specs clean up
after themselves by rejecting or archiving what they created (`dispatch.spec.ts:76-86`,
`dispatch-one-click.spec.ts:71-84`, `:118-123`) and filter their assertions to the ids
they seeded (`queue-order.spec.ts:35-48`). A spec that fails before its cleanup leaves
state for the next one; `queue-order.spec.ts:119-125` records exactly one such
ordering dependency that was found and removed. Working as intended; flagged so the
next "flaky e2e" report starts here rather than in the application.

### P4-18 — `ResponsiveTable.test.tsx` asserts markup, not the rendered label

The mobile label is painted by CSS (`styles.css:110-111`,
`content: attr(data-label)`), which jsdom cannot render, so the test checks the
attribute exists (`:19-20`). What it would catch: someone dropping the prop. What it
cannot catch: the `@media (max-width: 819px)` block being wrong, which is the only
way the feature actually breaks. The e2e `queue-order.spec.ts:116` does read
`[data-label="Queue"]` text against a real browser, but at desktop width.

---

## The six brief items, in order

### 1. Rendered-value correctness (the `data-ball="Ball.HUMAN"` class)

**Examined, nothing found of that class.** Evidence:

- Live `GET /api/projects/agentjobs/tasks` (240 records): `ball` ∈ {`agent`, `human`,
  `null`}; `lifecycle` ∈ {`closed`, `ready`, `draft`, `active`}; `display_status` is
  prose ("On hold (claude)", "Superseded (archived)"). No `Enum.NAME` spellings.
- `frontend/src/api/generated-contract.ts` turns a widened `Lifecycle`/`Ball`/
  `BallReason`/`Outcome` union into a `tsc` failure (`@ts-expect-error` on an impossible
  comparison), which the `build` gate stage runs.
- Every place an enum reaches the DOM: `TaskList.tsx:433-434` (`data-task`,
  `data-queue-position`), `:122-125` (filter compare), `:497` (priority class),
  `DependencyState.tsx` (badge from `lifecycle`/`ball`/`ball_reason`/`outcome`),
  `DispatchPanel.tsx:113-133` (`runStateLabel` switch), `:259-260`, `:400-401`
  (`data-*` yes/no), `Dashboard.tsx:82` (`next_action` switch). All compare against
  the closed generated unions; none stringify a model object.
- Tests assert rendered values where it counts: `review-findability.spec.ts:62-68`
  ("Superseded"/"Completed" visible, "Done" count 0, against the real server);
  `promote-draft.spec.ts:61-62` ("Actionable now"); `DispatchPanel.test.tsx:427-435`
  (no raw `finished_without_handoff`); `TaskList.test.tsx:302-306` (the position as a
  value, and "—" for a closed task).

What I did find is the weaker cousin: vocabulary that is *correct* but not *the same*
in two places (P3-4), and tokens shown raw by design (P4-13). Assertion mix in the
vitest suite: 66 `toBeInTheDocument` vs 54 `toHaveTextContent` + 16 `toHaveAttribute`
+ 9 `toHaveValue` — roughly half the presence checks are for negative assertions
(`queryBy… not.toBeInTheDocument`), which is the right use of them.

### 2. Query invalidation

Mechanism: `queryClient.ts` sets `staleTime` = 30 s; `LiveUpdates.tsx` polls
`/revision` every 15 s and invalidates a **named allowlist** of project-scoped queries
(`:20-36`), with an explicit exemption list (`:43-79`) and
`LiveUpdates.drift.test.tsx` requiring every generated project-scoped query to be in
one or the other. That test is the best thing in the suite: it is the only check I
found that makes "a new endpoint was added and nobody thought about freshness" fail.

Per-mutation invalidation, from `App.tsx`:

| Mutation | After | Verdict |
|---|---|---|
| queue move / reprioritize (`:183`, `:200`) | `invalidateProjectTaskQueries` | scoped, correct |
| dispatch start / cancel (`:299`, `:316`) | `invalidateQueries()` (all) | broad, correct |
| dispatch enable / disable (`:330`) | all | correct (state endpoint is exempt from the poller, refetched here) |
| approve / resume / send-backs / note / promote (`:411-468`) | `refresh()` = all | correct |
| create task (`:496`) | all, then navigate | correct |
| file issue (`IssueReporter.tsx:105`) | all | correct |
| **reject** (`:425`) | **none** — navigates | **P3-5** |

The allowlist is complete for today's endpoints (the drift test passes in the last
recorded run, `test-results/.last-run.json: passed`). `explainNext` and `getQueue` are
on it with reasons; `getDispatchState` is off it with a reason and is refetched
explicitly. Nothing stays stale until reload.

### 3. Staleness machinery

What is built (`service-worker.js`, `pwa.ts`, `build-service-worker.mjs`, `spa.py`):
- Shell precache keyed by a content hash of `index.html` + every shell file
  (`build-service-worker.mjs:20-25`), so a rebuild always yields a byte-different
  `sw.js`. `sw.js` and the manifest are `no-cache`. `/api/` is network-only (`:30-33`).
  Navigations are network-first with the cached shell as offline fallback (`:35-38`).
  `skipWaiting` + `clients.claim` + reload-once on `controllerchange` (`pwa.ts:3-18`,
  pinned by `pwa.test.ts`).
- The `api` gate stage and `scripts/build_release.py` keep the packaged bundle honest
  at release time (README `:53-62`); not re-verified here.

How an open tablet finds out — the brief's question — and how long it can serve the old
bundle:

| Situation | What happens | Bound |
|---|---|---|
| Fresh launch / hard reload, HTTPS | navigation fetches `/app/` (subject to P2-1), `register()` finds new `sw.js`, installs, activates, reloads once | one page load of old code |
| Foreground a backgrounded PWA | `visibilitychange` → revision poll only; **no SW update check** | **unbounded** (P2-2) |
| Tab left open on a desk | same | **unbounded** (P2-2) |
| Offline after an upgrade | shell from the new cache — possibly the old HTML (P2-1) | blank page until online |
| Plain-HTTP fallback, no SW | heuristic HTTP cache on `/app/` | ~10% of the bundle's age (P2-1) |

So the statement in `mobile-access.md:153-155` is true for the first row and silently
false for the second and third, and the second is how a phone is used.

### 4. Mobile / tailnet path

**Examined, nothing origin- or path-dependent found beyond P2-3.** Checked:
- Every URL the app builds is root-relative (`grep -rn "http://\|https://\|localhost\|window.location" frontend/src` outside `generated/` and tests hits only the
  reload in `pwa.ts:5` and a comment). Router `basename="/app"` (`main.tsx:17`);
  manifest `id`/`scope`/`start_url` all `/app/…`; SW scope `/app/` with
  `Service-Worker-Allowed: /app/`; `output_url` is server-built and root-relative
  (`routes/dispatch.py:239`); attachment `href`s are root-relative
  (`TaskDetail.tsx:461`).
- The tsnet proxy (`scripts/tailscale-service-host/main.go:56-64`) is a
  `NewSingleHostReverseProxy` of the whole origin, so the two escapes from the SPA —
  `<a href="/projects/new">` (`App.tsx:80`, legacy Jinja route at
  `routes/web.py:359`) and `<a href="/docs">` (`:519`) — are reachable behind it.
  `InAppLinks.test.tsx` pins that those are the only raw anchors.
- Generated client base URL: `client.gen.ts:16` uses `createConfig()` with no
  `baseUrl`, so requests go to the page's own origin. Correct for both
  `localhost:8876` and `agentjobs.tailfed1df.ts.net`.
- Responsive layout: one breakpoint at 820 px (`styles.css:64`), table rows collapse
  to labelled cards, action rows go vertical, 44 px touch targets. Reorder on touch is
  P3-7.

### 5. Accessibility and focus

Good: `aria-label`/`aria-keyshortcuts`/`aria-describedby` on the grip with a resolving
target (`TaskList.test.tsx:250-275`); `aria-live` announcements for reorders (`:422`)
and live updates (`LiveUpdates.tsx:212`); `role="alert"` vs `role="status"`
distinguished on purpose (`DispatchPanel.tsx:136-159`); the task-207 focus restore
(`TaskList.tsx:245-250`) and an e2e test that presses twice **without** refocusing
(`queue-order.spec.ts:70-76`).

Same-shape-as-task-207 interactions (focused node removed or moved, nobody restores):
- Review panel unmounts under its own Submit button when a send-back lands
  (`TaskDetail.tsx:245`, `:343-346`) → focus to `<body>` (P3-9, minor half).
- Issue dialog close → trigger not refocused; no trap (P3-9).
- Band-change `alertdialog` opened without focus (P3-8).
- `Log`'s expand-all remounts every `<details>` via `key={String(expandAll)}`
  (`TaskDetail.tsx:509`); the pressed button itself survives, so focus is fine — but a
  reader who had focused a `<summary>` and then pressed the button loses it. Edge.
- `DispatchRunList` is keyed by `run_id` and the server orders runs; no client re-sort,
  so the 2 s poll does not move nodes. Examined, nothing found.

Keyboard operability gaps: band change (P3-8) and, on touch devices, any reorder at all
(P3-7).

### 6. Test quality pass

What each would catch. "Sets up the state it verifies" is called out where it applies.

**vitest**

| Test | Would catch | Verdict |
|---|---|---|
| `LiveUpdates.drift.test.tsx` "classifies every project-scoped generated query" | a new read endpoint nobody added to the poller's allowlist — the silent-stale failure | **strong; the model for P3-10** |
| `queryClient.test.tsx:67` "does not refetch … when the view remounts" | removal of `staleTime` (the measured 138 → 403 ms regression) | strong; asserts request count, not markup |
| `queryClient.test.tsx:100` "still refetches when the revision poll says …" | an invalidation that the cache policy swallowed — cannot happen in TanStack as configured | decoration-adjacent; documents intent |
| `TaskList.test.tsx:352` "keeps focus on the task that moved" | the task-207 defect, *if* jsdom drops focus on `insertBefore` of a live node | focuses once, then uses `document.activeElement` — **not** the anti-pattern. Open question: remove the `useLayoutEffect` and see if it goes red; if jsdom preserves focus it is green either way, and the e2e test is the real evidence |
| `TaskList.test.tsx:320` "writes nothing for a gesture that would not move" | a no-op `queue_move` entry (a decision nobody made) | strong |
| `TaskDetail.test.tsx:269` "never sends a draft through approve" | a draft left `draft/agent-work`, claimable by nobody forever | strong |
| `Dashboard.test.tsx:126` "renders exactly the … call to action" (×6) | two panels at once, or the wrong rung of the ladder | strong — asserts heading text and exclusivity |
| `DispatchPanel.test.tsx:427` "never shows a raw enum spelling" | exactly the `Ball.HUMAN` class, for outcomes | strong; should be extended (P4-13) |
| `InAppLinks.test.tsx` | a raw `<a href>` dropping the basename (task-006/008) | strong; renders **with** a basename, which the other suites cannot |
| `ConnectionUnavailable.test.tsx:7` "refuses to present cached task data … while offline" | nothing — the app never renders `offline={true}` (P3-6) | **decoration** |
| `TaskCount.test.tsx` | nothing — component is unreferenced (P4-16) | **decoration** |
| `pwa.test.ts` | a double reload on controller change | narrow; says nothing about registration, scope, or what the worker caches |

**Playwright**

| Spec | Would catch | Verdict |
|---|---|---|
| `queue-order.spec.ts:54` keyboard reorder + reload | a move that only ever happened in the browser; the second-press focus loss | **strongest test in the repo**: the reload is the assertion, and the second press deliberately does not refocus |
| `promote-draft.spec.ts:92` "changed underneath the open page" | a promote written twice, or a refusal swallowed | strong; counts log entries by content, not by number |
| `dispatch.spec.ts` | the whole enable → dispatch → watch → cancel → disable loop; asserts `href` of the output link and "Cancelled" not "Failed" | strong; 15 s timeouts are the only waits |
| `review-findability.spec.ts:52` | "Done" reappearing for a superseded task | strong; `toHaveCount(0)` on the old label |
| `attach-screenshot.spec.ts:40` | the base64 → sidecar → `<img>` chain | **synthesises the paste** (`dispatchEvent(new ClipboardEvent…)`, `:31-33`). It proves handler → server → render; it cannot prove a real Ctrl+V reaches the handler. The file says so at `:16-22`; this is the honest form of "set up the state", and it is the only one I found |
| `queue-order.spec.ts:231` autoscroll | `dragover` not reaching the document, `scrollBy` not moving the page | honest caveat at `:217-230` about `Input.setInterceptDrags`; evidence for the mechanism, not the gesture |

**Anti-pattern search result.** `grep -rn "\.focus()" frontend/src frontend/e2e` finds
four calls: two in `TaskList.test.tsx` (`:273` checks focusability; `:362` is the
single initial focus before a two-press sequence) and two in `queue-order.spec.ts`
(`:68`, `:98`, each the single initial focus). **No test focuses before every press.**
Examined, nothing found.

---

## What I did not get to

- **Device reproduction** of P2-1 and P2-2. Both are argued from headers, code paths
  and the service-worker update rules, not from a tablet. The check is: build, install,
  background the PWA, rebuild + restart, foreground — does the new bundle appear?
  `mobile-access.md:157-165` is close to this but not it.
- `frontend/src/api/generated/` (≈10 k lines) beyond the client config and the
  drift test's use of it. Auditor 7 owns freshness of the generated client.
- `DispatchOutput.tsx` tail polling under a long transcript (`max-h-80 overflow-auto`,
  scroll-to-bottom on every poll while live) — I did not look at what happens to a
  reader who has scrolled up while the run is live. Likely fights them; unverified.
- `scripts/build_release.py`'s wheel verification of the PWA assets (auditor 11).
- `e2e-bench/open-task.bench.ts` and `playwright.bench.config.ts` (auditor 11, §6).
- Whether jsdom drops focus on node reinsertion, which decides whether
  `TaskList.test.tsx:352` is evidence or decoration (see table). One experiment,
  requires an edit, not done in a read-only session.
- Vitest wall-clock and the 228 count in ENGINEERING.md; my `it()` grep gives ~203
  before `it.each` expansion, so the figure is plausible and not checked.
- Tailwind/CSS beyond the breakpoint and touch-target rules.

## Questions for other auditors

- **Auditor 7 (API/webhooks):** the dispatch refusal `reason` is a free `str` on the
  server; `openapi.json` therefore says `string` where the real shape is a closed set
  (P3-10 here, item 2 in your brief). Also: `mutation-error.ts:4-8` says the schema
  declares only 422 for mutation routes, so every 409 arrives typed as a validation
  error — is that still true after task-231?
- **Auditor 10 (dispatch):** do `no_eligible_runner` and `undefined_runner` ever reach
  the `/dispatch` POST response, or are they internal? The browser has no text for
  either.
- **Auditor 11 (gate):** `tests/test_spa.py` pins `no-cache` on `sw.js` only; nothing
  pins the shell's headers (P2-1). Is there a place in the gate where "production
  serving headers" could be one table rather than scattered assertions? And: `vitest`
  + `build` + `e2e` are serial in the gate; `e2e` rebuilds nothing and `vitest` needs
  no bundle, so those two could overlap if the port-per-checkout rule holds for both.
- **Auditor 12 (security):** `/projects/new` (legacy Jinja, `routes/web.py:359`) and
  `/docs` are linked from the SPA and are reachable through the tsnet proxy unchanged.
  I did not look at what else the legacy router exposes behind the tailnet.
- **Auditor 1 / 2 (context, docs):** ENGINEERING.md "26 Playwright tests" → 22 counted;
  `frontend/README.md:93-95` "limited to one high-value path" → 8 spec files;
  `README.md:40-41` and `vite.config.ts` name 8765 as the dev proxy target while
  GLOBAL-AGENTS.md says 8876 is the server that exists.
- **Auditor 4 (storage):** the frontend's freshness model is "a task write bumps
  `/revision`". Does every write path bump it — including `queue repair`, migrations,
  a `git checkout` under the server, and attachment sidecar writes? If any does not,
  the allowlist in `LiveUpdates.tsx` is correct and the screen still goes stale.
