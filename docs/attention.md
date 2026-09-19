# Attention: telling you that work has stopped on you

AgentJobs has always been able to *show* you that a task is waiting: the red badge in
the header, and the alarm on the Dashboard. Both require you to be looking at the page.
This is the part that works when you are not — a persistent red mark on the Windows
taskbar and one desktop notification, driven by durable state rather than by whichever
agent session happens to still be alive.

Built on task-422, under the notifications epic (task-421). The mobile push child reuses
the rule below rather than inventing a second one.

## What is being tracked

**The waiting set** is exactly what the badge counts: open tasks whose ball is on a
human and whose lifecycle is not `draft`
(`agentjobs.dashboard.human_waiting_tasks`). A parked draft is backlog, not a blockage —
`tests/test_attention_tiers.py` records what counting them cost. One predicate, so a
notification can never disagree with the page it links to.

**An attention episode** is one run of that set being non-empty. It is not an event per
task, and that is the whole design: a notification per handoff is what makes people turn
notifications off.

## The rule

| | |
|---|---|
| **Opens** | the waiting set goes from empty to non-empty. One interruptive notification is owed. |
| **Grows** | a task joins while the episode is open. The count and the indicator move; **nothing interrupts**. |
| **Acknowledged** | a person performs one of the three deliberate acts below. |
| **Re-arms** | a task the previous reconcile did not see joins while the episode is already acknowledged. A *new* episode opens, owing one notification. |
| **Resets** | the waiting set empties. No episode, no indicator. |

The three acts that acknowledge an episode, and nothing else does:

1. activating the Windows notification;
2. clicking the red badge in the header;
3. opening the detail of a task the episode names.

### What acknowledgment does and does not do

**It does not clear the indicator.** The taskbar badge, the tab icon and the header
badge track the *waiting set*: work is still stopped on you after you have looked at it,
and they go quiet when it is genuinely finished. What acknowledgment buys is that the
next task to stop on you is allowed to interrupt again.

Note which way this errs. An **unacknowledged** episode is the quiet one — it owes no
further alerts. So a strict rule about what counts as acknowledgment can only reduce
interruptions, never multiply them, which is why the bar is set where it is.

### Rejected, and why

* **Window or browser focus.** The epic forbids it, and it would mean every alt-tab past
  the AgentJobs window silenced the alarm.
* **Landing on the Dashboard.** The Dashboard is the app's index route, so "opened the
  waiting-on-human view" would be indistinguishable from "opened the app" — focus, with
  extra steps.
* **A dedicated Acknowledge button.** More chrome for a gesture the three acts already
  cover, and a button nobody presses leaves the episode unacknowledged forever, which
  silently costs later alerts.

## Where the state lives

`~/.agentjobs/attention/<project>.yaml` — beside the dispatch state and the finish
directory, **not** in the task store.

It is machine state about a person rather than project history: it must not be exported
with a task, must not travel in a clone, and would be noise in a backup of the corpus.
It *is* shared by every client this server has, which is what will let a phone reuse the
same episode instead of opening a second one.

Every transition is computed inside that file's merge lock
(`dispatch.atomic_yaml.merge_yaml_atomically`), so two polls landing in the same
millisecond cannot each decide the episode is new. Five handoffs at once are one episode
and one notification.

## Delivery is not the source of truth

Nothing above depends on a pixel having reached a screen, and that is a constraint
rather than an accident:

* **A service restart replays nothing.** The episode is on disk and already open.
* **A browser restart replays nothing.** Each client records the last episode id it drew
  a notification for, in `localStorage`. A reload is not an alert.
* **A first load facing five existing waits raises one notification**, summarising five
  — never five notifications.
* **Windows Focus Assist and Do Not Disturb** suppress the banner without telling the
  page, so a suppressed notification looks exactly like a delivered one to
  `showNotification`. The interruption is lost, the red indicator is not, and the badge
  is what greets you when quiet hours end.
* **A denied permission** costs the notification and nothing else. The Dashboard says
  so, in a notice that names the Chrome setting, and the badge keeps working.

The consequence worth stating plainly: a notification is a wake-up signal. The task
record is the complete account of what happened and what you have to do, and nothing in
a toast is the only copy of anything.

## What drives the Windows shell

| Surface | Mechanism | Notes |
|---|---|---|
| Taskbar icon | `navigator.setAppBadge()` / `clearAppBadge()` | The supported Windows mechanism for an installed PWA. Chrome draws the overlay and Windows keeps drawing it while the app is closed. In an ordinary tab the call resolves and does nothing. |
| Tab icon | a red SVG data URL swapped into `<link rel="icon">` | The colour AgentJobs actually controls. The taskbar badge is painted by Chrome, so its colour is the browser's; this one is `#ef4444`, the same red as the header badge and the Dashboard alarm. |
| Bottom-right notification | `ServiceWorkerRegistration.showNotification()` | Through the worker, not the page: the case this exists for is a window that is minimised, behind something, or closed, and a page's notification dies with the page. One `tag` per project, so the Action Center holds one entry that updates rather than a stack of stale numbers. Falls back to a page `Notification` where there is no worker. |
| The click | `notificationclick` in `service-worker.js` | Focuses an existing AgentJobs window and navigates it, or opens one. One waiting task goes to the task; several go to the `status=human` list. The episode rides in an `attention_ack` query parameter, which the app acts on and then strips — a bookmarked URL must not acknowledge an episode every time it is opened. |

**For the badge to appear on the taskbar, AgentJobs has to be installed as an app**
(Chrome's install button, or ⋮ → Cast, save and share → Install page as app). In a
browser tab, the red tab icon and the header badge are what you get; the notification
works either way.

## Where the code is

| | |
|---|---|
| The rule, as a pure function | `src/agentjobs/attention.py` — `advance()` |
| Persistence and reconciliation | `src/agentjobs/attention.py` — `reconcile()`, `acknowledge()` |
| The endpoints | `GET /api/projects/{id}/attention`, `POST /api/projects/{id}/attention/ack` |
| What a client does with an episode | `frontend/src/components/attention/episode.ts` |
| The three shell affordances | `frontend/src/components/attention/shell.ts` |
| The wiring | `frontend/src/components/attention/WindowsAttention.tsx` |
| Tests | `tests/test_attention_episodes.py`, and the three suites beside the modules above |

`GET /attention` **reconciles** — the episode is a fact about the waiting set, so it is
brought up to date on the read the header was making anyway rather than on a clock of
its own. The transition is idempotent, so polling cannot manufacture attention.

Acknowledging is its own capability (`attention.ack`) and **no run holds it**. An agent
able to make the claim could silence the alarm raised by its own handoff, and the next
task to stop on the owner would then arrive with no notification at all.

## Seeing it for yourself

```bash
poetry run python scripts/attention_sandbox.py
```

A throwaway project on its own port with buttons that stop work on you and clear it
again, so the taskbar, the tab and the bottom-right corner can be watched doing it. It
never touches a real project. `--help` for the options.
