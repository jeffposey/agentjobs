# Push: being woken when you are not at the desk

[Attention](attention.md) is the state — which tasks have stopped on you, whether you
have been told, and whether you have acted. This page is the delivery channel that
reaches a phone: what it takes to turn on, what a push actually says, which platforms
it works on, and what happens when it fails.

Built on task-423, under the notifications epic (task-421). **It adds no policy.** One
interruption per episode, three deliberate acts acknowledge it, the waiting set
emptying resets it — all of that is task-422's rule, and a phone is simply another
client of the same episode. The only thing that differs is where each client remembers
what it has already drawn: `localStorage` in a browser, the subscription row on the
server for a device.

## Turning it on

On the Dashboard, under **Phone notifications**, press *Turn on for this device*. The
browser asks for permission, AgentJobs registers the subscription, and that is the
whole of the setup — there is no key to copy and no account to make.

**On iPhone and iPad, add AgentJobs to the Home Screen first.** iOS exposes push only
to an installed web app: in a Safari tab there is no `PushManager`, no prompt, and
nothing to turn on. The panel says so explicitly rather than reporting "not supported",
because the advice that works on every other platform — allow notifications, check site
settings — is wrong there, and a person told "not supported" would conclude AgentJobs
cannot do this when it can, after one gesture nobody asked them to make.

**The page must be served over HTTPS or from localhost.** On this machine that means
`http://127.0.0.1:8876` for the desktop, and the tailnet address for a phone.

*Send a test* pushes one message on purpose. It is the answer to "did that actually
work", which nothing else gives: a device that opted in while nothing was waiting would
otherwise find out whether push reaches it at the moment it matters least. A test does
**not** count as the episode's interruption.

## What a push says, and what it does not

> **3 tasks are waiting on you**
> Needs review — task-421: Notify the human when AgentJobs is waiting on them — and 2 others.

**By default it names the lead task and what is wanted of you**, the same as the
desktop toast. A notification that says only a number does not tell a person whether to
get up, which is the one thing it is for.

**Privacy is a per-device toggle, under *Phone notifications*, and it is off.** Turned
on, that device's pushes withhold the task's id and title and keep the ask:

> **3 tasks are waiting on you**
> Needs review — and 2 others.

The ask carries in both modes because it is not task content: "Needs review" says what
is wanted, never what the work is, and a device in the quiet mode chose it deliberately
— the ask is the minimum that makes a quiet push worth receiving.

It is per device because that is the shape of the question. A tablet on a desk at home
and a phone held up on a train are the same person with different bystanders. The
setting is `detail` on the subscription row (`task` or `count`); the toggle re-posts
the subscription, which keeps the row's id and the episode it has already been told
about, so changing it never costs you a repeat notification.

**This reverses the default task-423 shipped**, on the owner's decision of 2026-09-20
(task-421). That default withheld the name because a push lands on a lock screen, in a
hallway, on a watch — reasoning that is sound for a product whose users are not the
person who installed it. This one is a single consumer on his own phone over his own
tailnet, and the cautious default cost him the usefulness of every notification he
received.

The complete ask is on the task record, as it is for every other channel. A person who
has read a handoff in a bubble has read it in the one place they cannot act on it.

**Tapping it opens the work**: the waiting task when there is exactly one, the
`status=human` list when there are several. The link carries the episode id, because
activating a notification is one of the three acts that acknowledge an episode and the
tap may arrive at a window that did not exist a moment ago.

**A stale push tells the truth.** A push service holds a message for an offline device
for up to its TTL, which here is twelve hours, and "3 tasks are waiting on you" can
stop being true in that time. So the service worker re-reads the live attention state
when a push arrives and renders *that*, falling back to the payload only when the fetch
fails — which is the offline case, where the payload is the best that exists. A push
that outlived its work says so rather than repeating a number that is no longer true.

## How it works

| | |
|---|---|
| Transport | Web Push: [RFC 8030](https://www.rfc-editor.org/rfc/rfc8030) delivery, [RFC 8291](https://www.rfc-editor.org/rfc/rfc8291) `aes128gcm` payload encryption, [RFC 8292](https://www.rfc-editor.org/rfc/rfc8292) VAPID identity |
| Application-server key | One P-256 keypair per machine, `~/.agentjobs/push/vapid.json`. Generated on first read of the panel |
| Devices | `~/.agentjobs/push/<project>.yaml`, one row per subscription |
| Delivery | `agentjobs.push.watcher`, a loop in the server's lifespan, every 15 seconds |
| Endpoints | `GET/POST /api/projects/{id}/push[...]`, all four needing `push.manage` |

**The body is encrypted end to end.** The push service routes it and cannot read it;
what it does see is the endpoint, the size and the timing.

**Nothing secret reaches the client.** The browser is handed the *public* half of the
VAPID key, which is what a subscription must be created against. The private half never
leaves the server process.

**A subscription endpoint is treated as a secret**, because it is one: it is a URL
anybody holding it can send a notification to. The API never returns it. What a device
row publishes is an opaque id, the push service's host, and a label you chose.

### Why there is no push library in `pyproject.toml`

The usual Python route is `pywebpush`, which brings `http-ece`, `py-vapid` and
`cryptography`. AgentJobs already has `cryptography` and `PyJWT` in its closure, and
what is left once those are present is about a hundred lines: one ECDH, two HKDF
extracts, three expands and an AES-GCM. The dependency would buy packaging, not
cryptography.

That trade is only defensible because the result is checkable rather than plausible.
RFC 8291 section 5 publishes a complete worked example with every key and the salt
fixed, and `tests/test_push_encryption.py` drives the encryption with those exact
inputs and asserts that exact body. A hand-rolled encryption with no known-answer test
would be the wrong call whatever it saved.

### Why the server watches, rather than a page

The desktop notifier is driven by a page: the header polls `/attention`, the episode
advances, and the browser draws a toast. That is enough for a person at their desk and
useless for the case push exists for — a laptop closed, a phone in a pocket, and work
that has just stopped on its owner. So the server runs its own loop, beside the
dispatch poller and for the same reason that one exists: a piece of the system that
only worked while something else was watching it was not working.

It watches projects that have devices and nothing else, so its cost on a machine with
no subscriptions is one directory listing per tick. Reconciling is idempotent, so the
loop and any number of open pages cannot manufacture attention between them.

## Supported platforms

| Platform | Works | Notes |
|---|---|---|
| Android — Chrome, Edge, Firefox | Yes | In a tab or as an installed app |
| Windows / macOS / Linux desktop — Chrome, Edge, Firefox | Yes | The desktop already has its own toast; push adds nothing unless the browser is closed |
| iOS / iPadOS 16.4+ — Safari | Yes, **installed only** | Must be added to the Home Screen. No push from a Safari tab, at all |
| iOS below 16.4 | No | The platform has no Web Push |
| macOS Safari 16+ | Yes | Permission must be granted from a user gesture |

Delivery through a push service also requires the AgentJobs server to have outbound
internet access. Nothing else about AgentJobs does, so this is the one feature that
stops working on an isolated machine — and it stops visibly, as failures on the device
rows, rather than silently.

## When it fails

Every attempt lands on the device row, which is what the panel shows:

| What happened | What AgentJobs does |
|---|---|
| `404` / `410` — the push service no longer knows this endpoint | Forgets the device. The only thing that deletes a row |
| `401` / `403` / `400` — refused | Records it and does not retry this episode. A service that has made up its mind will say the same thing again, and a retry loop against one is how an application server gets its key blocked |
| `429` / `5xx`, or unreachable | Retries, backing off from two seconds to an hour, for as long as the person is still waited on |
| Ten failures in a row | Reported as unreachable. **Not removed** — a laptop closed for a week recovers; a row deleted on a count does not |
| A browser rotating the subscription | The service worker's `pushsubscriptionchange` handler re-registers, using a project id the page left in a cache entry |
| Permission revoked | The next push is refused by the service, and the panel offers to register again |

**None of this is on the path of a handoff.** Delivery happens afterwards, in the
watcher, so a push service being slow, broken or unreachable cannot affect a task
write — the worst it can do is leave a status on a device row.

The bound on retrying is deliberately not a counter. It is that the episode stops
being owed a notification the moment it is acknowledged or the waiting set empties, so
a phone in a tunnel keeps being tried for exactly as long as the work is still stopped
on its owner.

## Where the code is

| | |
|---|---|
| Encryption, with the RFC's own vector as its test | `src/agentjobs/push/webpush.py` |
| The VAPID keypair | `src/agentjobs/push/keys.py` |
| The devices, and what happened to each | `src/agentjobs/push/subscriptions.py` |
| What a push says and who is owed one | `src/agentjobs/push/delivery.py` |
| The loop | `src/agentjobs/push/watcher.py` |
| The endpoints | `src/agentjobs/api/routes/push.py` |
| The panel | `frontend/src/components/attention/MobilePush.tsx`, `push.ts` |
| Receiving a push | `frontend/src/service-worker.js` |
| Tests | `tests/test_push_encryption.py`, `tests/test_push_delivery.py`, `tests/test_push_api.py`, and the two suites beside the frontend modules |

The one policy function both channels share is
`agentjobs.attention.owes_notification`, mirrored for a browser as `shouldNotify` in
`components/attention/episode.ts`.
