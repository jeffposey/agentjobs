# AgentJobs Tailscale Service host

This optional proxy publishes AgentJobs' loopback server and packaged React application
through a named Tailscale Service.
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
