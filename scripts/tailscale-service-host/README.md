# AgentJobs Tailscale Service host

This optional proxy publishes a loopback AgentJobs server through `svc:agentjobs`.
It uses `tsnet`, so the app gets a virtual Tailscale node and a dedicated Service
hostname without changing the identity of the physical host.

The tailnet administrator must first define `svc:agentjobs` on `tcp:443`, create
`tag:agentjobs-host`, and issue a single-use auth key restricted to that tag. Build and
run the proxy with Go 1.26.5 or later:

```powershell
go build -o tailscale-service-host.exe .
$env:TS_AUTHKEY = '<single-use tagged key>'
./tailscale-service-host.exe -backend http://127.0.0.1:8765
```

Approve `agentjobs-service-host` on the Service page when the tailnet does not use an
auto-approver. Once the first authentication succeeds, the node identity is persisted
under the current user's configuration directory and later starts do not need
`TS_AUTHKEY`.

The proxy terminates private Tailscale HTTPS and forwards requests to the loopback
origin. Keep AgentJobs bound to `127.0.0.1`; do not use Funnel for this setup.
