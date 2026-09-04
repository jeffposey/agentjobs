# AgentJobs Tailscale Service host

This optional proxy publishes AgentJobs' loopback server and packaged React application
through a named Tailscale Service, and it is the **front door**: it establishes the
identity of every remote caller and refuses one it cannot identify. The design, the
refusal cases and the deny-list are documented in
[docs/tailnet-front-door.md](../../docs/tailnet-front-door.md); this file is how to run
it.
It uses `tsnet`, so each app gets a virtual Tailscale node and a dedicated Service
hostname without changing the identity of the physical host. One compiled binary can
host several apps by running one process and state directory per Service.

The tailnet administrator must first define `svc:agentjobs` on `tcp:443`, create
`tag:agentjobs-host`, and issue a single-use auth key restricted to that tag. Build and
run the proxy with Go 1.26.5 or later:

```powershell
go build -o tailscale-service-host.exe .
$env:TS_AUTHKEY = '<single-use tagged key>'
./tailscale-service-host.exe -backend http://127.0.0.1:8765
```

For another app, provide its Service and virtual-host names explicitly:

```powershell
$env:TS_AUTHKEY = '<single-use key tagged for this host>'
./tailscale-service-host.exe `
  -service svc:jobsearch `
  -hostname jobsearch-service-host `
  -backend http://127.0.0.1:8766
```

Approve `agentjobs-service-host` on the Service page when the tailnet does not use an
auto-approver. Once the first authentication succeeds, the node identity is persisted
under a hostname-specific directory in the current user's configuration directory;
later starts do not need `TS_AUTHKEY`.

The proxy terminates private Tailscale HTTPS and forwards requests to the loopback
origin. Keep AgentJobs bound to `127.0.0.1`; do not use Funnel for this setup.

## The front door

Every request is looked up with `LocalClient().WhoIs` before anything is forwarded. The
proven login is set as `X-Tailscale-User`, and a shared secret as
`X-AgentJobs-Front-Door`, so the application can tell this process from any other one on
the machine. A connection that cannot be identified -- a failed lookup, a tagged node,
no user profile -- is refused with `403` and a sentence saying why; it is never forwarded
as anonymous. Any copy of those headers the caller sent, and any `X-AgentJobs-Run`, is
stripped first.

Three routes are denied through the tailnet and stay reachable on loopback:
`POST /api/projects`, `POST /api/projects/init` and `POST /api/projects/inspect`. They
are the only rung that turns the API into arbitrary code execution on this machine.
Adding a fourth is a decision to record on a task, not a judgement call made while
editing -- `frontdoor_test.go` fails if the list stops being three entries.

### The shared secret

Written by this proxy on first run to `~/.agentjobs/front-door-secret` (`0600`), which is
where AgentJobs reads it from. Override with `-front-door-secret-file` or the
`AGENTJOBS_FRONT_DOOR_SECRET` environment variable, and set the same value on both
processes if you do. With no secret present the application believes no identity header
at all, so a remote caller resolves as the machine owner rather than as themselves --
functional, but attributed to the wrong person. `AGENTJOBS_HOME` moves both ends
together.

## Tests

```powershell
go test ./...
```

Go is not a dependency of the AgentJobs repository, so these are not part of
`scripts/check.py`; run them by hand when you change this directory. The application's
half of the contract -- when the identity header is believed, and what a caller may do
once believed -- is covered by the Python suite the gate does run.
