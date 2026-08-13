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

The virtual node stores its identity in the current user's configuration directory.
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
installation, service-worker offline behavior, or transport privacy. Anyone allowed by
the network and firewall can act as the configured AgentJobs user until multi-user
authentication is implemented.

## Updates and cache behavior

The service worker precaches only the application shell: HTML, hashed JavaScript/CSS,
the manifest, and icons. `/api/` requests are always network-only. Every production
build gives the shell cache a new revision; the worker activates immediately, removes
older shell caches, takes control, and the app reloads once when that replacement
controller arrives. A manual cache clear is not part of the upgrade path.

For a physical-device release check:

1. Install build A from the private HTTPS URL and leave it installed.
2. Build and serve build B with an obvious shell-only text change.
3. Relaunch or foreground the installed app while the host is reachable.
4. Confirm build B appears without clearing site data.
5. Stop AgentJobs, relaunch the app, and confirm it shows the unavailable screen with
   no task rows or counts.
