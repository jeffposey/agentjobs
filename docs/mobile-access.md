# Mobile and installed-app access

AgentJobs has no authentication. Keep its Python server on loopback and put a private,
HTTPS reverse proxy in front of it when another device needs access. The intended setup
uses Tailscale Serve: the server remains reachable only on the host at
`127.0.0.1:8765`, while Tailscale terminates HTTPS at the host's private MagicDNS name.
Do not use Tailscale Funnel; Funnel is public internet exposure.

## Addresses in this setup

| Address | Used by | Purpose |
| --- | --- | --- |
| `http://127.0.0.1:8765` | Host computer only | Loopback AgentJobs server |
| `https://<host>.<tailnet>.ts.net/app/` | Tailnet devices | HTTPS React application and PWA |
| `http://<specific-ip>:8765/app/` | Fallback only | Direct, non-installable access without an HTTPS proxy |

HTTPS is required for service workers outside `localhost`. Plain HTTP on another
device can display the page, but the browser will not provide the supported offline
shell or reliable app installation. AgentJobs never caches task API responses: if the
host cannot be reached, the installed shell hides task data and says that it is
offline instead of presenting old assignments as current.

## Recommended private HTTPS setup

Prerequisites:

1. Install Tailscale on the computer running AgentJobs and sign it into your tailnet.
2. Install Tailscale on the phone or tablet and sign in to the same tailnet. If device
   approval is enabled, approve the new device in the admin console.
3. Confirm tailnet policy permits the reviewing device to reach the host. Tailscale's
   [add-a-device guide](https://tailscale.com/docs/features/access-control/device-management/how-to/set-up)
   covers installation, sign-in, and approval.

On the host computer, start AgentJobs on loopback:

```powershell
poetry run agentjobs serve --host 127.0.0.1 --port 8765
```

In a second terminal on that host, create the persistent HTTPS proxy:

```powershell
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

The command prints the private `https://<host>.<tailnet>.ts.net` address. Open its
`/app/` path on the phone. Tailscale documents the current command and TLS behavior in
the [Serve CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve).
The first run may provide a URL for enabling HTTPS in the tailnet.

**This path identifies nobody.** `tailscale serve` forwards from loopback and presents no
front-door secret, so every request a tailnet device makes through it is served as the
person at the host's keyboard, with every capability that person has. The `tsnet` proxy
in the next section, run with the front-door secret, is the setup that tells a remote
caller from the owner — see [the tailnet front door](tailnet-front-door.md).

This machine-level setup intentionally shares one web origin with every other app
served from that Tailscale hostname. Ports and paths separate HTTP routing, but some
Android browsers still group installed apps by hostname. Use the dedicated Service
setup below when AgentJobs must coexist with another installed app from the same
physical computer.

## Dedicated hostname for a separately installed app

A Tailscale Service gives AgentJobs its own MagicDNS name and virtual IP while the
Python server remains on the same computer. The Service host must have a tag-based
server identity, so do not convert a person's existing workstation node merely to add
an app hostname. Run the small `tsnet` proxy in
`scripts/tailscale-service-host` instead; it appears as a separate virtual node and
leaves the workstation's identity unchanged.

One-time tailnet administration:

1. Create the tag `tag:agentjobs-host` and allow the intended administrator to own it.
2. Define `svc:agentjobs` with endpoint `tcp:443`.
3. Generate a single-use auth key restricted to `tag:agentjobs-host`.
4. Approve the virtual host's advertisement for `svc:agentjobs`.
5. Ensure the reviewing users and devices can access `svc:agentjobs` in the tailnet
   policy.

Build and authenticate the proxy on its first run:

```powershell
cd scripts/tailscale-service-host
go build -o tailscale-service-host.exe .
$env:TS_AUTHKEY = '<single-use tagged key>'
./tailscale-service-host.exe -backend http://127.0.0.1:8765
```

The virtual node stores its identity in a hostname-specific directory under the
current user's configuration directory. Run one proxy process per named Service; the
same compiled helper can therefore give several apps on one computer independent
origins and identities.
After the first successful connection, clear `TS_AUTHKEY` and revoke or discard the
single-use key. Later starts need only the executable and the backend argument. The
private install URL is `https://agentjobs.<tailnet>.ts.net/app/`; it has a different
origin from every machine-level Serve URL.

Tailscale documents the virtual IP and stable MagicDNS behavior in
[Tailscale Services](https://tailscale.com/docs/features/tailscale-services) and the
in-process host pattern in
[Register a tsnet application as a Tailscale Service](https://tailscale.com/docs/features/tsnet/how-to/register-service).

To install AgentJobs, use the browser's **Install app** or **Add to Home Screen** action,
then launch the new AgentJobs icon. It should open without browser chrome because the
manifest requests standalone display. Installation wording varies by browser and OS.

### Keep a project off the phone

A project whose files must never leave this machine is marked in its own config, and
then no remote device is served it -- not its tasks, not its runs, and not the session
transcripts those runs left behind:

```yaml
# <project>/.agentjobs/config.yaml
visibility: local
```

`agentjobs project list` marks the local-only ones. The default is `shared`, so nothing
you already reach from a phone changes, and the setting is per project rather than per
route -- see [Exposure](exposure.md) for what a hidden project answers and why it
answers that rather than a refusal.

### Add another device

Install Tailscale on the new device, sign it into the same tailnet, complete any
required administrator approval, and open the same private HTTPS `/app/` URL. The
Serve configuration stays on the host; it is not repeated for every phone.

### Stop and remove the setup

Stop private sharing and verify it is gone:

```powershell
tailscale serve off
tailscale serve status
poetry run agentjobs stop --port 8765
```

Use `tailscale serve reset` instead when every Serve endpoint configured on that host
should be removed. Removing or signing out the phone from the tailnet separately
revokes that device's network access.

### Sleeping and offline hosts

Tailscale Serve can keep its configuration across a reboot when `--bg` is used, but it
cannot wake or run AgentJobs. If the host is asleep, shut down, disconnected, or the
AgentJobs process is stopped, the phone receives the explicit unavailable screen and
no task data. Wake the host and restart AgentJobs; the installed app will reconnect.

## Direct-bind fallback (no HTTPS)

Use this only on a trusted network when Tailscale Serve or another certificate-backed
proxy is unavailable. Bind one specific interface address, never every interface:

```powershell
poetry run agentjobs serve --host 100.x.y.z --port 8765
```

Replace `100.x.y.z` with the host's specific Tailscale address, or use one specific LAN
address and an appropriate host firewall rule. `0.0.0.0`, `::`, `[::]`, `*`, and `+`
are refused by `serve`, `restart`, and `open` because they would expose the
unauthenticated API on every interface.

This fallback is HTTP, so treat it as browser access only: do not claim PWA
installation, service-worker offline behavior, or transport privacy. **Parts of the UI
also stop working**, not just the installability: `crypto.randomUUID` is only available
in a secure context, and the React app calls it to mint the `operation_id` on several
writes, a queue move, a reprioritize and an issue report among them. Over plain HTTP
those actions throw. Read the
task list, do not drive the queue from it. Anyone allowed by
the network and firewall can act as the configured AgentJobs user until multi-user
authentication is implemented.

## Updates and cache behavior

The service worker precaches only the application shell: HTML, hashed JavaScript/CSS,
the manifest, and icons. `/api/` requests are always network-only. Every production
build gives the shell cache a new revision; the worker activates immediately, removes
older shell caches, takes control, and the app reloads once when that replacement
controller arrives. That is the design; it is not yet the behaviour everywhere. Big Dawg
Audit II (2026-09-11) showed an installed app launched after a rebuild getting the old
shell with no server request, and the new worker precaching that old shell as its
offline page. Until task-260 lands, a reload after the first launch — or a manual cache
clear — may be needed to see a new build.

For a physical-device release check:

1. Install build A from the private HTTPS URL and leave it installed.
2. Build and serve build B with an obvious shell-only text change.
3. **Fully close the installed app and launch it again** while the host is reachable.
   Bringing a backgrounded app to the foreground is not a navigation, so it does not
   check for a new worker — the phone can sit on build A indefinitely while looking
   perfectly healthy. Pull-to-refresh inside the app is the other way to force it.
4. Confirm build B appears without clearing site data.
5. Stop AgentJobs, relaunch the app, and confirm it shows the unavailable screen with
   no task rows or counts.

## Dictating into a field

Typing a task into a phone is the reason tasks do not get filed, so dictation is a
mobile-access question rather than a feature request. Task-171 measured what the devices
on this tailnet actually do, over this page's HTTPS origin, in September 2026. The
instrument is `scripts/voice_input_probe.py`; re-run it before trusting any of this on a
new browser version.

### What was measured

| Device / browser | `SpeechRecognition` | On-device API | `available({processLocally:true})` | `MediaRecorder` | Keyboard mic in a plain field |
| --- | --- | --- | --- | --- | --- |
| Galaxy Z Flip 6, Android, Chrome 153 | yes, both spellings | yes | **`unavailable`** | yes | **yes** — `insertCompositionText` |
| Windows 11, Chrome 153 | yes, both spellings | yes | **`downloadable`** → `available` after `install()` | yes | not measured |
| Windows 11, Edge 153 | yes, both spellings | yes | **`unavailable`** | yes | not measured |
| Firefox 153 | **no, neither spelling** | no | n/a | yes | not measured |

The tailnet HTTPS origin is a secure context on the phone (`isSecureContext` true), so
both the microphone and the speech API are reachable the way this page's recommended
setup already serves the app. A plain-http LAN address is not, and the
[direct-bind fallback](#direct-bind-fallback-no-https) therefore has no dictation at all
on top of everything else it loses.

Two of those cells are the reason this section exists.

**The on-device language pack is not there by default, and on two of the three
Chromium browsers it cannot be fetched at all.** A mic button that opens with
`processLocally = true` fails on first press with `language-not-supported` — on the
phone, permanently. Only desktop Chrome reported `downloadable`, where `install()`
returned `true` and flipped the state to `available`.

**Firefox has neither spelling of the constructor.** It is not a matter of a permission
or a language: `window.SpeechRecognition` and `window.webkitSpeechRecognition` are both
absent, so a mic button rendered there is a button that does nothing.

### What ships

**The operating system keyboard's own microphone, always.** It needs no code, no
permission prompt and no network, and it is already working: Gboard dictated into the
probe's plain `<textarea>` over the tailnet origin, arriving as `insertCompositionText`.
The issue reporter's details field is an ordinary `<textarea>` whose only
`preventDefault` is on paste and drop, so the path is live in the app today.

**This is a constraint on anything built later, not a feature to maintain.** A control
that replaces the textarea, or that cancels `beforeinput` or `keydown`, removes the one
dictation path that works everywhere — including the browsers where nothing else does.
`tests/test_voice_input_probe.py` guards the probe's copy of that property; the shipped
control needs its own equivalent.

**An in-page mic button where the constructor exists, feature-detected, never
assumed** — and it must not force `processLocally`, because on the phone that is a
permanent failure rather than a first-run download. So on Android the audio goes to the
browser's speech service, and **the control has to say which mode it is in** rather than
imply a privacy property it has not got. Where `available({processLocally:true})`
reports `available`, say the audio stays on the device; where it reports `downloadable`,
offer the one-time download and say what it costs; otherwise say plainly where the audio
goes.

**Where the constructor is absent the button is absent** — not present and dead, not
disabled with a tooltip. The field still takes the keyboard microphone, and that is the
answer for Firefox.

### Two behaviours to build around

**`continuous = true` is not honoured on Android Chrome.** Recognition ended 7.4 seconds
in, mid-paragraph, while the speaker was still talking. Continuous dictation there means
restarting the recogniser on `end` and stitching the results — the behaviour the Web
Speech literature attributes to iOS, observed here on Android.

**Android marks each progressively longer result final and re-sends it.** The usual
`finals += results[i][0].transcript` over the slice from `event.resultIndex` produces
`"the menuthe menu salethe menu sale in task..."`. Rebuild the transcript from the whole
`event.results` list on every event instead. The probe shipped with this bug and scored
its first real phone run on the corrupted string.

### Transcript quality, and what was not measured

On the phone, against a deliberately task-shaped paragraph, the Web Speech path returned
`the menu sale in task 167 needs a decision for ships to work tree is already` before
cutting out. It normalised "task one six seven" to `task 167`, which is the useful
behaviour; it lost `shell` to "sale" and split `worktree` into "work tree". The keyboard
microphone, reading the same page aloud, was comparable — "touches his box" for "touches
this box". **Treat both as good enough to draft with and not good enough to submit
unread**, which is an argument for dictating into a visible field the person edits
rather than into anything that acts on what it heard.

Three cells above are blank and should stay honestly blank until someone fills them. The
tablet was not exercised — it is the same operating system and browser family as the
phone, so it is expected to match, and that expectation is not evidence. Desktop
transcript quality was not measured because Chrome's speech recogniser does not read
`--use-file-for-fake-audio-capture` and this machine's default input device captures
digital silence.

### What was built (task-172)

One control, `frontend/src/components/DictationControl.tsx`, beside every free-text
field the capture form has — title, what happened, summary, intent, constraints, out of
scope and acceptance criteria — and beside the dispatch brief, which is the only place a
person writes a prompt for a run today. The recogniser's lifecycle is
`frontend/src/voice/useDictation.ts`; the browser's API is wrapped once in
`frontend/src/voice/speech.ts`.

Four things about it are consequences of the measurements above rather than taste.

**Text reaches the field one recogniser session at a time, not one result at a time.**
`event.results` is read whole and written once when the session ends, which is what
makes the re-sent-and-lengthened Android result harmless — there is no place left for an
incremental reader to duplicate from. The cost is that words land in chunks; the interim
line pays for it by showing what is being heard before it arrives.

**The words go in at the caret, through the field itself.** `insertText` first, so the
browser's own undo stack takes the insertion and Ctrl+Z takes back a sentence you did
not mean to say; the native value setter and a dispatched `input` event where that is
missing. Nothing is intercepted, nothing is cancelled, and the field keeps its own type,
name, handlers and value — which is the constraint the section above leaves behind.

**The mode is one answer shared by every control on the page**, not one per field. It
has to be: taking up the one-time download in one place while six other fields go on
saying the audio leaves would be six wrong sentences, and the wrong ones are the
sentences about privacy. For the same reason the download is offered once per form
rather than once per field, and only where `available({processLocally:true})` reported
`downloadable`.

**`scripts/dictation_sandbox.py` is how this gets looked at**, separately from
`capture_control_sandbox.py` because that one's `--tailnet` mode is plain HTTP and
dictation needs a secure context. It serves both halves in one browser: `?dictation=off` deletes both constructors before the bundle runs, so the
Firefox path can be seen without Firefox, and `?dictation=fake` drives a scripted
recogniser that ends its session the way Android does, so the restart-and-stitch
behaviour can be seen on a machine with no usable microphone. Both shims are in the
sandbox and neither is in the application — the application only ever feature-detects
what the browser really has.

### Sending audio to a server instead

Rejected for now; see task-173. It is the only path that covers a browser with no speech
API and the only one where AgentJobs decides where the audio goes, and it costs a local
transcription model, an audio upload path and the latency of both. What it buys over the
two paths above is an in-page button for Firefox users — who already have their
operating system's microphone key in the same field. Reopen it if a browser appears here
with no speech API *and* no usable keyboard dictation, or if audio leaving the device
becomes a reason someone will not use the feature.
