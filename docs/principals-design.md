# Principals: who is asking

> **Partly shipped.** Resolution is real and covered by tests (task-329): every request
> resolves to exactly one principal, or to a reported absence, before any handler runs.
> **Nothing is enforced.** No route reads the principal, no request is refused, and the
> API remains as open as [the API reference](api-reference.md) says it is. The three
> children that change that are named at the foot of this page.

Implemented in [`src/agentjobs/principals.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/principals.py),
wired in `api/dependencies.py` and `api/main.py`, tested in `tests/test_principals.py`.

## The model

Every request resolves to exactly one **principal**. There are three kinds and no
fourth — in particular there is no anonymous kind, because a caller nobody can name is
an absence, and giving the absence a name is how it stops being visible.

| Kind | Who | Established by |
| --- | --- | --- |
| `tailnet` | a remote caller the front door authenticated | an identity header, believed only from the front door |
| `owner` | the person at the machine | arriving on loopback with no run credential |
| `run` | a dispatched agent | a run-scoped credential, minted at dispatch |

A principal carries the `kind`, the `source` (which of the three routes above
established it — the audit trail's evidence, distinct from the kind), an `actor_id`
once task-330 maps logins to configured actors, and `run_id`/`task_id` for a run.

## Why loopback is split in two

This is the load-bearing part, and no earlier design had it.

The [Big Dawg audit of 2026-08-21](../audits/2026-08-21/12-security.md) proposed
identity at the proxy plus a path deny-list. Both are proxy-side, and **a dispatched
agent never goes through the proxy** — it is already on this machine, on loopback.
So a design that trusts loopback wholesale in order to keep the desktop dashboard
working leaves the agent-impersonation chain fully intact while looking like a fix:
an agent can still `POST /tasks/{id}/dispatch` with `{"user": "Jeff Posey"}` and be
believed.

Splitting loopback into `owner` (no credential) and `run` (a credential) is what makes
the boundary real rather than decorative. It is also why the credential is checked
*first*: consult the address or the header before the credential and an agent resolves
to a person, at which point every capability the epic gives a person is the agent's.

## Resolution order

1. **Run credential**, presented in `X-AgentJobs-Run`.
   A credential that is presented and **does not verify resolves nothing** — it does
   not fall back to `owner`. Falling back would mean any local process could trade a
   rejected credential for the person's identity, which is a downgrade attack on the
   only boundary this adds.
2. **Proven identity header**, `X-Tailscale-User`, believed only from the front door.
3. **Loopback with no credential** → `owner`.
4. **Anything else** → nothing resolves, and the absence is reported with a problem
   code and a sentence.

## The trust rule

**The identity header is believed only when the request arrives by the path the front
door controls.** The proxy terminates the tailnet's HTTPS and forwards to
`127.0.0.1`, so a proven identity reaches the application on loopback; the check is on
the socket's peer address, which cannot be forwarded, rewritten or spoofed. A header
arriving any other way is ignored — not refused, not logged as an identity, just
absent — so the request resolves to exactly what it would have resolved to without it.

A header trusted unconditionally is **worse than no header at all**: it converts a body
field anyone could set into a header field anyone can set while looking authoritative.
Bind the server to `0.0.0.0`, which is a thing people do, and any host on the LAN could
name itself as any user.

Two consequences worth stating plainly rather than discovering later:

- `X-Forwarded-For` and friends are not consulted. They are set by whatever sent the
  request, so reading them would undo the rule entirely.
- **Loopback does not separate the proxy from any other local process.** A local
  process could present an identity header and be believed as `tailnet`. That residual
  is bounded — both kinds are human, so nothing is escalated by it, and the local
  caller that actually matters is a dispatched agent, which rule 1 catches by
  credential rather than by path. Narrowing it further needs the proxy to prove it is
  the proxy, which is a proxy-side change (task-244).

## The two seams left open

Both are stubs on purpose, so that the risky work arrives on a foundation that is
already tested rather than alongside it.

- `no_run_credentials` is the installed verifier, and it verifies nothing. **With it
  in place no request can resolve `run`**, which is what makes this change inert.
  Task-331 replaces it through `set_run_credential_verifier`.
- `actor_id` is `None` on every resolved principal. Guessing a mapping here would
  write an attribution nobody configured — the failure
  [`agentjobs.actors`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/actors.py)
  exists to prevent. Task-330 owns it.

## What comes next

| Task | Adds |
| --- | --- |
| task-330 | maps a proven login to a configured actor, replacing `human_identity`'s `MULTIPLE` refusal with a per-request answer |
| task-331 | mints the run credential at dispatch and installs the verifier |
| task-332 | capabilities per principal kind, and what an absent principal means |
| task-244 | sets the identity header at the proxy |

The parent is task-066, whose decision entry of 2026-09-03 is binding on all of them.
