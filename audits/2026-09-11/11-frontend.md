# 11 — React frontend and the PWA

Auditor 11, Big Dawg Audit II, night of 2026-09-11 (finished 2026-09-12 after a
usage-limit pause). Read-only in the main clone at `096f33ae`. Servers used: my own on
8911 (`scripts/review_panel_sandbox.py`), 8912 (a private copy of `frontend_dist/` for
the staleness experiment) and 8913 (`scripts/slot_board_sandbox.py`); all three stopped
before this file was written. Nothing touched 8876 or the live store.

Backlog searched first (`agentjobs list` over draft/ready/active, 149 rows, grepped for
frontend terms). Records that bear on this system: task-260 (PWA staleness, **still
draft**), task-275 (legacy Jinja XSS, **still draft**), task-326, task-359, task-360,
task-366, task-295, task-369, task-404, task-106.

Summary of verdicts:

| # | Finding | Sev | Status |
|---|---|---|---|
| 1 | Legacy Jinja page executes script stored in a task record | P1 | Confirms task-275, by running |
| 2 | Every review sandbox serves an empty project | P2 | New, by running (2 of 21) |
| 3 | Stale shell served on launch, precached by the new worker, and offline | P2 | Confirms task-260, by running; partly refutes prior P2-2 |
| 4 | Worker update reloads the page under a dirty form | P3 | Confirms a task-260 bullet, by running |
| 5 | Six `crypto.randomUUID()` sites, no fallback | P3 | Confirms task-260, by reading |
| 6 | Reject navigates without invalidating | P3 | New record (prior audit P3-5, never filed), by reading |
| 7 | Action errors discard the server's reason | P3 | New record (prior P3-11, never filed), by reading |
| 8 | Nothing automated registers the service worker | P3 | New, by reading |
| 9 | Raw enum tokens shown to the human | P4 | Prior P4-13, unchanged, by reading |

---

## 1. The legacy Jinja page executes script stored in a task record — P1

**Confirms task-275.** Verified by running.

The task-275 draft says the Alpine `x-markdown` directive pipes task content through
`marked.parse` into `innerHTML`. Tonight I proved it executes, on today's code:

1. Started `scripts/review_panel_sandbox.py 8911` (its own `AGENTJOBS_HOME`, throwaway
   database).
2. `POST /api/projects/sandbox-panel/tasks` with a description of
   `hello <img src=x onerror="document.title='XSS-FIRED-'+location.pathname"> world`.
   Created as `task-001`.
3. `GET /p/sandbox-panel/tasks/task-001` returned 200; the payload is in the page at
   line 331 inside `x-data='{ content: "…<img src=x onerror=…" }'` — correctly
   escaped for the attribute, which is the half that does not matter.
4. Playwright (Chromium) loaded that URL and the React URL for the same task:

   ```
   legacy: { title: "XSS-FIRED-/p/sandbox-panel/tasks/task-001", imgsWithOnerror: 1 }
   react:  { title: "AgentJobs",                                  imgsWithOnerror: 0 }
   ```

   The script ran on the legacy page. The React page is clean, as task-275 says.

Still mounted: `src/agentjobs/api/main.py:365-366` includes `web_router` under
`/p/{project_id}` and `web_legacy_router` at the root. Root `/` and `/tasks` 307 to the
legacy project pages; `/projects`, `/projects/new`, `/p/<id>/`, `/p/<id>/tasks` all 200.
The SPA still links into that router with raw anchors: `frontend/src/App.tsx:105`
(`/projects/new`), `frontend/src/components/Dashboard.tsx:288` and
`PrimaryNav.tsx:264` (`/docs`).

Three CDN scripts with no `integrity`, not two: `api/templates/base.html:9` loads
`cdn.tailwindcss.com` as well as Alpine (`:30`) and marked (`:32`). Task-275 names only
the jsdelivr pair.

**Why P1 rather than the P2 the last audit gave it.** The exploit is one API call by
anyone who can write a task — every agent, every MCP client, anything on the tailnet —
and it runs as whoever opens the page, which on 8876 is the owner with `default_user`
identity and full API access. It has been a draft for 20 days. A demonstrated,
tailnet-reachable, one-request stored XSS is a blocking defect for a tool whose selling
point is that agents write to it.

**Fix.** As task-275 says: delete `web_router`, `web_legacy_router` and
`api/templates/`; move project onboarding into the SPA; repoint the three anchors.
Until then, the cheapest mitigation is one line in `main.py` not including the two
routers, which removes the surface without touching the templates. Promote task-275 out
of draft tonight.

## 2. Every `*_sandbox.py` review harness serves an empty project — P2

**New.** Verified by running on two sandboxes; inferred for the other nineteen from
identical structure.

The review-server rule in GLOBAL-AGENTS.md rests on these scripts, and since
2026-09-10 they have been standing up servers with nothing in them.

- `scripts/review_panel_sandbox.py 8911` prints seven task URLs. `GET
  /api/projects/sandbox-panel/tasks` → `[]`; `/tasks/task-101/detail` → 404;
  `/dashboard` → `next_action: "empty_project"`; the React page shows "Getting Started
  with AgentJobs — No tasks yet" and every task URL sits on "Opening task..." forever.
  The temp home holds **two** databases: `local-tasks-bdfd767ef5.db` (where the seed
  went) and `sandbox-panel.db` (what the server reads, empty). My XSS probe above was
  created as `task-001`, which is itself proof the served store had never seen the
  seven seeded tasks.
- `scripts/slot_board_sandbox.py 8913`: `/api/projects` reports `task_count: 0` for
  both `sandbox-here` and `sandbox-elsewhere`.

**Cause** (verified in `scripts/sandbox_store.py:41-56`): `sandbox_store(tasks_dir)`
is called with no `project_id`, tries `ProjectRegistry().resolve_default(directory)`,
which fails because every sandbox **seeds before it registers** — `build()` runs the
seed, then `main()` calls `ProjectRegistry(home).add(...)`. The fallback is
`LOCAL_PROJECT_ID` + `local_database(directory)`, a file named from the directory
hash. The server then resolves the now-registered id to `<project_id>.db`. Seed order
per script (`grep -n "sandbox_store(" / ".add("`): all 19 scripts that register do so
after seeding; `finish_sandbox.py` and `finish_lock_refusal_sandbox.py` do not
register at all and were not run.

`sandbox_store.py` landed in `7e19d69d` on 2026-09-10 ("rework the suite off the
deleted file backend"). Its own docstring names this exact hazard — "a seed written
anywhere else is a page with nothing on it" — and then does it.

**What would have caught it.** Nothing does: `grep -rl sandbox tests/` matches only
dispatch tests using the word for something else. No test calls a sandbox's `build()`
and reads the result back through the store the server would open.

**Fix.** Either register before seeding, or pass `project_id=` explicitly from every
sandbox (the function already supports it). Add one test that builds a sandbox project
into a temp home and asserts `task_count > 0` through `store_factory.task_manager_for`.
This also means task-326's acceptance ("sandbox URL it names") and every review done
"in the sandbox" since 2026-09-10 need a second look — they cannot have shown anything.

## 3. A launched PWA is served the stale shell, the new worker precaches it, and offline keeps it — P2

**Confirms task-260** (draft since 2026-08-22), with new running evidence. **Partly
refutes the prior audit's P2-2** ("an open tab never learns a new bundle exists"): it
now does, see below.

Headers on the sandbox tonight (`curl -D -`):

```
GET /app/                         200  etag, last-modified, NO cache-control
GET /app/p/<id>/tasks/task-101    200  etag, last-modified, NO cache-control
GET /app/sw.js                    200  cache-control: no-cache
GET /app/manifest.webmanifest     200  cache-control: no-cache
GET /app/assets/index-Dj-7_p8q.js 200  etag only (no cache-control, not immutable either)
```

`src/agentjobs/api/spa.py:138` is still a bare `FileResponse(index)`. Task-260 ac-1
is open.

**The experiment** (Playwright Chromium against a private copy of `frontend_dist/` on
8912, files backdated ten days so heuristic freshness is ~24 h, a hit counter on the
server; script kept in my scratch as `stale_probe.mjs`):

| Step | What happened | Server hits `/app/` |
|---|---|---|
| 1. First visit | worker installs, precaches shell `agentjobs-shell-769e350159a5` | 1 (the precache came from the HTTP cache, not the server) |
| 2. "Rebuild": new `index.html` with a marker, new `sw.js` cache name, new `build-info.json` | server now serves the marker | +1 (my check) |
| 3. Fresh navigation to `/app/` (a launch, not a reload) | **page has no marker**; `fromServiceWorker: true`; **sw.js was not re-fetched** | **0** |
| 4. `registration.update()` | new worker installs; its precached `/app/` **has no marker** — it read the stale shell from the HTTP cache | 0 |
| 4b. `controllerchange` → `pwa.ts` reloads by itself | page now has the marker | +1 |
| 5. `location.reload()` (the VersionSkew button) | marker | +1 |
| 6. Offline navigation | **page has no marker** — the offline shell is the old build, in the new worker's cache | — |

So on today's code: a phone that launches the installed app after a rebuild gets the
old shell without the server being asked (step 3); the worker, when it does update,
bakes that old shell in as the offline page (steps 4 and 6). In a real rebuild the
hashed asset names change too, so that offline shell references assets the activate
step just deleted — task-260's "blank offline page", now demonstrated rather than
argued.

**What has improved since the last audit.** `VersionSkew.tsx` (commit `3cf301b2`,
2026-09-09) polls `/api/version` every 60 s and on window focus, compares
`bundle_id`, and offers a Reload that works (step 5). The prior "never learns" is no
longer true; it learns within a minute. But nothing calls
`ServiceWorkerRegistration.update()` (`grep -rn "\.update()" frontend/src` is empty),
and Chrome did not check `sw.js` on the navigation in step 3, so the worker and its
offline shell lag until the browser's own daily check.

**Fix**, unchanged from task-260 and now with a test to hang it on: `Cache-Control:
no-cache` on the shell route and deep links, `immutable` on `/app/assets/*`, precache
`/app/` with `{cache: "reload"}`, call `update()` when `bundle_id` moves, and turn my
`stale_probe.mjs` into a Playwright spec — it is the test that would have caught this
and there is currently none (finding 8).

## 4. A worker update reloads the page regardless of a dirty form — P3

**Confirms** the task-260 bullet "do not reload while a form is dirty". Verified by
running: step 4b above — the page reloaded on its own (`load` count went 2 → 3) the
moment the new worker claimed it.

`frontend/src/pwa.ts:9-17` reloads on every `controllerchange` after the first.
`VersionSkew.tsx:22-24` says the opposite in its own words — it "stops short of
reloading by itself" because "the tab may hold a half-written note". Both are in the
same bundle. Whichever browser-scheduled update check lands while a note is being
typed loses the note, and `TaskDetail.tsx` / `DispatchPanel.tsx` have unsaved-form
guards precisely because that is worse than a stale page.

**Fix.** On `controllerchange`, defer the reload behind the same dirty check the
forms use, or route it through the VersionSkew banner instead of reloading.

## 5. `crypto.randomUUID()` with no fallback, six sites — P3

**Confirms task-260** bullet 4. Inferred from reading; not demonstrated, because
`127.0.0.1` is a secure context and I did not bind a LAN address.

`App.tsx:331, 346, 359, 790`, `components/IssueReporter.tsx:101`,
`report/attachments.ts:85`. `randomUUID` is `[SecureContext]`, so on the
`http://<ip>` origin `docs/mobile-access.md` documents as the fallback, reorder,
field edit, issue filing and screenshot attach all throw before the request is built.
The prior audit counted four sites; there are six now.

**Fix.** One `newOperationId()` helper with a `getRandomValues` v4 fallback.

## 6. Reject navigates without invalidating — P3

**New as a record** (the prior audit's P3-5; never filed). Inferred from reading.

`App.tsx:769`: `onReject` awaits the mutation and then `navigate(...)` to the list.
Every other handler on that page awaits `refresh()` first (`:753-767`, `:808`, `:830`).
`queryClient.ts:17` sets `staleTime` to 30 s, so the list mounts from cache and shows
the rejected task in its pre-reject state until the 15 s revision poll
(`LiveUpdates.tsx:6`) notices — up to 15 s of a list that says the opposite of what
the person just did, on the surface they were just sent to.

**Fix.** `await refresh()` before the navigate, as approve does.

## 7. Approve / reject / send-back errors discard the server's reason — P3

**New as a record** (the prior audit's P3-11). Inferred from reading.

`App.tsx:748`: `error={actionError ? "The action could not be recorded. Reload and
try again." : null}` for all seven mutations. The same page already knows how to do
this properly: `onSaveFields` (`:793-806`) calls `readRefusal(error)` and shows the
server's message. A 403 `wrong_task`, a 409 revision conflict and a dead server all
render the same sentence, and "reload" fixes none of the first two.

**Fix.** Run `actionError` through `readRefusal` and fall back to the generic sentence.

## 8. Nothing automated registers the service worker — P3

**New.** Inferred from reading.

`grep -rl "serviceWorker\|sw.js" frontend/e2e frontend/src` → only `pwa.ts` and
`pwa.test.ts`. The unit test checks the reload-once logic on a fake container. No
Playwright spec loads `/app/` with the worker, rebuilds, and navigates again. The
whole class in finding 3 has zero coverage, and it is the class the owner hits from
a phone. My `stale_probe.mjs` (six steps, ~20 s) is the spec; it needs a
`register_spa(app, tmp_copy)` fixture, which is how I ran it.

## 9. Raw enum tokens shown to the human — P4

Prior P4-13, unchanged. Inferred from reading. `Dashboard.tsx:226` renders
`{task.ball_reason}` (`review`, `spec`, `hold`) as the Reason cell;
`TaskDetail.tsx:950` renders the heading "Current ask (agent/work)".

The rendered-value class itself is sound: the API serialises `ball: "human"` (checked
on the sandbox task JSON), and every `data-*` assertion in the suite asserts a value
(`toHaveAttribute("data-health", "parked")` and the like, 13 sites), not the presence
of the attribute. `ResponsiveTable.test.tsx:19-20` still asserts `data-label`, which
the prior audit noted; on the phone layout `data-label` *is* the rendered label, so
I am not raising it.

---

## Things checked and found sound

- **Test honesty, task-207 pattern.** `TaskList.test.tsx:526` and `:1190` focus the
  handle once, then press on `document.activeElement`; `e2e/queue-order.spec.ts:77-86`
  does the same in a browser and says why. The three `.focus()` calls in the suite are
  all the one-time setup, not the per-press refocus that hid task-207.
- **Query invalidation.** `LiveUpdates.tsx:20-45` is an allowlist with a drift test
  (`LiveUpdates.drift.test.tsx`) that fails when a generated project query is neither
  listed nor excused. Every mutation in `App.tsx` and `IssueReporter.tsx` invalidates
  the whole client afterwards (nine sites), including refusals. Blunt, complete, and
  the one omission is finding 6. Finish and runs poll on their own clocks
  (`App.tsx:531, 630`, `LiveRuns.tsx:60`, `DispatchOutput.tsx:207, 220`).

## What I did not get to

- **The live surfaces on a browser** — review panel verbs (task-326), the questions UI
  (task-359/360), the slot board, live runs, the dashboard's next action. Finding 2
  emptied every sandbox I could have used; seeding through the API needs claim and
  handoff verbs the HTTP surface does not expose. The unit tests for these components
  are extensive, but I did not verify them against a page.
- **Insecure-origin behaviour** (finding 5) on a real LAN/tailnet origin.
- **iOS specifics** — status-bar overlap (task-295), standalone-mode navigation
  (task-366). No device.
- **`frontend/src/api/generated`** drift from `openapi.json` beyond trusting the
  gate's `api` stage.
- **The Playwright suite's cost and flake** (task-369, task-404). Did not run it.
- **The gate's `build` stage emptying the live bundle** (task-260 bullet 3):
  `frontend_dist/index.html` has an mtime of 21:21 tonight, which is consistent with
  someone's gate rebuilding the dashboard's bundle mid-audit, but I did not trace what
  ran.
- **Accessibility items** from the prior audit (P3-8, P3-9): not re-checked.

## Questions for other auditors

- **Security / auditor 12.** The tsnet proxy: does it forward `/p/`, `/projects` and
  `/docs`, or only `/app/` and `/api/`? If it forwards everything, finding 1 is
  reachable from every device on the tailnet, not only this machine.
- **SQLite storage / auditor 02.** `sandbox_store` falls back to `LOCAL_PROJECT_ID`
  and a directory-hashed file when the directory is not yet registered. Is the same
  fallback reachable in production — a server started on a directory, written to, and
  then registered by id, after which its rows appear to vanish? Finding 2 is that
  sequence in miniature.
- **Gate / auditor 05.** Was the 21:21 rebuild of `src/agentjobs/frontend_dist/` a
  gate run in the main clone? If so, the 8876 dashboard served a half-empty
  `frontend_dist/` for the duration of that build, which is the outage task-260
  bullet 3 describes.
- **Dispatch / auditor 04.** `VersionSkew` reads `/api/version` every 60 s from every
  open tab. Does anything rate-limit or cache that endpoint's `api_digest`
  computation, or is it recomputed per request?
