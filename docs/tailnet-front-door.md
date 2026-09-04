# The tailnet front door

> **Shipped (task-244).** The optional tsnet proxy in
> [`scripts/tailscale-service-host/`](https://github.com/jeffposey/agentjobs/tree/main/scripts/tailscale-service-host)
> establishes identity for every remote request and denies three routes. What an
> identified caller may then do is [authorization](authorization.md)'s question, and how
> the application decides whether to believe the proxy is
> [principals](principals-design.md)'s.

AgentJobs binds to `127.0.0.1`. The proxy publishes it on a Tailscale tailnet as
`svc:agentjobs`, which is how the dashboard reaches a phone. Before task-244 it was a
bare `httputil.NewSingleHostReverseProxy`: every request from the tailnet was forwarded
unchanged and arrived at the application on loopback, indistinguishable from the person
sitting at the machine. Ninety-nine operations, sixty-one of them mutating, with no
authentication in front of them.

## What the proxy does, and the two things it deliberately does not

It does exactly two things beyond moving bytes.

**1. It establishes identity.** Only this process can. It holds the tsnet node, so it is
the only thing on the machine that can ask tailscaled who is at the other end of a
tailnet connection:

```go
who, err := localClient.WhoIs(request.Context(), request.RemoteAddr)
```

The login that comes back is set as `X-Tailscale-User` on the forwarded request, and the
proxy's own copy of `X-AgentJobs-Front-Door` proves the header was written here rather
than merely passed through. **A connection `WhoIs` cannot identify is refused, never
forwarded as anonymous** — there is no unauthenticated remote caller in the finished
design. Four cases refuse, all with `403` and a sentence saying which:

| Case | Why it is a refusal rather than a fallback |
| --- | --- |
| the lookup fails | forwarding would resolve the caller as the machine owner, who holds everything |
| no node comes back | as above; "we could not tell" must never mean "believe what it claims" |
| the node is **tagged** | a tagged node is a machine, and AgentJobs maps a login to a configured *person*. There is no human to map a tag to, and inventing one is the failure `agentjobs.actors` exists to prevent. This tailnet has two tagged nodes — both service hosts |
| no user profile, or an empty login | nobody was named |

It also **strips** the caller's own `X-Tailscale-User`, `X-AgentJobs-Front-Door` and
`X-AgentJobs-Run` before setting its own. This is load-bearing rather than tidy-up: every
one of those is evidence the application acts on, and all of them arrive from a remote
caller who can type anything. Forwarding a client's identity header would make the
`WhoIs` call decorative; forwarding a client's run credential would let a phone act as a
dispatched agent on somebody else's task.

**2. It denies three routes.**

| Route | |
| --- | --- |
| `POST /api/projects` | register a directory as a project |
| `POST /api/projects/init` | initialise and register any existing directory |
| `POST /api/projects/inspect` | ask whether a path exists |

These are the only rung that turns the API into arbitrary code execution on this
machine: initialise any directory as a project, enable dispatch on it, file a task,
dispatch a session with that working directory. `/inspect` is the filesystem existence
oracle that makes the first step aimable. Nobody performs any of them from a phone, and
**all three stay fully reachable on loopback**. `GET /api/projects` — the project list
the dashboard draws itself from — is not denied; only the `POST` that creates one is.

Matching is on the cleaned path, so `/api/projects/x/../init` and `/api/proj%65cts/init`
are the same route here as they are to the application.

**It decides nothing else.** No authorization logic, no project knowledge, no policy.
The capability table lives in the application, and a proxy holding a second copy of it
would be a policy drifting from the policy.

**A fourth denied route is a decision to record, not a judgement call made while
editing.** `frontdoor_test.go` fails if the list stops being three entries, and a second
test walks every route the audit originally wanted denied and asserts it is *not*.

## Why the deny-list is three routes and not ten

The [Big Dawg audit of 2026-08-21](../audits/2026-08-21/12-security.md) proposed a much
longer list: `/api/all/tasks`, `dispatch/enable|disable`, `/queue/repair`, `/webhooks*`,
`/docs`, `/redoc`, and the run output, tail and transcript routes.

That list was written for a world with no identity model — "either layer alone is a
large improvement" — and with one in place most of it protects nothing identity does not
protect better. A grep on 2026-09-03 found the React app calls every one of those routes
**zero times except the run output routes**, which draw the structured dispatch output
panel. So the audit's list would have broken the one surface it touched and defended
nothing else. Two of its entries are worth naming individually:

- **`dispatch/enable` and `disable`.** `enable` acts only against an already-defined
  runner, so its danger is entirely parasitic on being able to point a project at an
  arbitrary directory — which `init` does and is denied. `disable` is the brake, and it
  is wanted *most* from a phone, away from the desk.
- **`/api/all/tasks`.** Filtered by task-333 rather than denied, which stays correct when
  there is a second front door.

Dispatching a task was never on the audit's list and must not acquire a ban here: it is
ordinary workflow, and doing it from a phone is the point of publishing the dashboard at
all.

## The shared secret

Loopback says a request came from this machine. It does not say it came from the proxy.
Until task-244 those were the same claim, so any local process could set
`X-Tailscale-User` and be believed as a remote human — and a remote human holds every
capability there is, against a dispatched run's five of eleven.

The proxy now presents a secret on every request it forwards.

| | |
| --- | --- |
| **Where** | `~/.agentjobs/front-door-secret`, or `$AGENTJOBS_FRONT_DOOR_SECRET`, or `-front-door-secret-file` |
| **Who writes it** | the proxy, on first run — 32 bytes from the OS CSPRNG, hex, mode `0600`. The application only ever reads |
| **When it is absent** | there is no front door, so no request can resolve `tailnet` at all. That is the ordinary state of the many installs that run no proxy |
| **Compared with** | `hmac.compare_digest`, so a wrong secret cannot be found a character at a time |

The path is a constant on both sides and neither is told it by the other, so each end
pins the spelling in a test: a disagreement between them is silent, and its symptom is
every remote caller quietly becoming the machine owner.

[What it is worth, and what it is not](principals-design.md#what-the-shared-secret-is-worth)
is stated at length on the principals page rather than here. In one line: the proxy and
every dispatched agent run as the same user, so this is a barrier an agent must cross
deliberately, not one it cannot cross.

## What the tailnet ACL currently allows

Read on 2026-09-04 from this machine's netmap (`tailscale debug netmap`), because the
audit filed the question and nobody had answered it. It does not change the design —
identity is per-connection either way — but it sizes what is exposed.

The policy is the **default allow-all**: the packet filter distributed to this node
accepts `100.64.0.0/10` and `fd7a:115c:a1e0::/48` — the whole tailnet — to
`0.0.0.0/0`/`::/0` on every port. The `services/agentjobs` capability, which is what lets
a node route to the service's VIP `100.126.67.115`, is present in this node's netmap
under that same policy. **So `svc:agentjobs` is reachable by every node on the tailnet,
not a subset.**

Five nodes today, on one user account (`hellfiregames.com`, single-user):

| Node | Owner |
| --- | --- |
| `coruscant` (this machine) | `jposey@` |
| `jeffs-tab-s7` | `jposey@` |
| `jeffs-z-flip6` | `jposey@` |
| `agentjobs-service-host` | `tag:agentjobs-host` |
| `jobsearch-service-host` | `tag:jobsearch-host` |

So "every node" is today three of Jeff's own devices plus two tagged service hosts —
which is exactly why the tagged-node refusal above is not theoretical. Adding a person
to this tailnet, or a grant that widens the service's reach, changes who can knock; it
does not change whether they are identified, and after task-332 it does not change what
they may do.

## Running it

```powershell
cd scripts/tailscale-service-host
go build -o tailscale-service-host.exe .
$env:TS_AUTHKEY = '<single-use tagged key>'    # first run only
./tailscale-service-host.exe -backend http://127.0.0.1:8765
```

Go is **not** a dependency of this repository and the proxy is not built by
`scripts/bootstrap.py` or checked by `scripts/check.py`; it is optional infrastructure
that most installs never run. Its own tests are Go tests:

```powershell
go test ./scripts/tailscale-service-host/
```

The application half — the trust rule, the secret's supply, and what each principal may
do — is in the Python suite the gate does run: `tests/test_principals.py`,
`tests/test_front_door.py`, `tests/test_authorization.py`.

**Restart both processes after changing the secret**, and start the proxy before or
after the server as you like: the application re-reads the secret file on a few seconds'
cache precisely so the start order cannot matter.
