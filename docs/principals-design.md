# Principals: who is asking

> **Partly shipped.** Resolution is real and covered by tests (task-329), and the run
> credential it resolves is real and minted at dispatch (task-331): every request
> resolves to exactly one principal, or to a reported absence, before any handler runs,
> and a dispatched agent now resolves as its **run** rather than as the person at the
> machine. **Nothing is enforced.** No route reads the principal to decide anything, no
> request is refused, and the API remains as open as
> [the API reference](api-reference.md) says it is. The children that change that are
> named at the foot of this page.

Implemented in [`src/agentjobs/principals.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/principals.py)
and [`src/agentjobs/dispatch/credentials.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/dispatch/credentials.py),
wired in `api/dependencies.py` and `api/main.py`, reported by `GET /api/whoami`, tested
in `tests/test_principals.py` and `tests/test_run_credentials.py`.

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
established it — the audit trail's evidence, distinct from the kind), the raw `login` the
front door proved for a `tailnet` caller, and `run_id`/`task_id` for a run. Turning that
login into one of a project's configured actor ids is
[the identity registry](identity-registry.md)'s job, not this module's.

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

## The run credential

Minted by `dispatch/credentials.py` when the ledger starts a run, and installed over
`no_run_credentials` by the API application at import. It is what turns rule 1 from a
slot into a boundary.

| | |
| --- | --- |
| **Shape** | `<run_id>.<nonce>` — the run id in the clear, 256 bits from the OS CSPRNG behind it |
| **Stored** | its SHA-256 digest only, in the run's own directory, `0600`. The ledger holds nothing replayable |
| **Delivered** | in the child process's environment. For a Claude `--bg` session, whose worker is spawned by a daemon that discards the launcher's environment, through the `0600` session-settings document `session_env` already writes for secrets |
| **Never** | in argv — which is recorded verbatim into `meta.yaml` *and* into the task's dispatch log entry — nor in any task record, log entry, transcript or API response |
| **Expires** | with its run. Verification reads the run's status, and the write that ends a run destroys the digest as well |

### What it buys, and what it does not

**A run can read its own credential out of its own environment, and nothing stops it
passing that credential to something else.** The property this buys is **"an agent cannot
claim to be a person"**, not "an agent cannot misbehave as itself". A run principal is
strictly less capable than an owner once task-332 gives either of them capabilities, so
there is no escalation path through it — but it is **not a secret from the agent holding
it**, and anything built on the assumption that it is will be unsound.

Stated here because the natural next assumption is the wrong one, and because
`docs/agent-dispatch-design.md` already overclaimed once in the same direction: it calls
the self-dispatch loop structurally impossible, and it is bounded rather than impossible.
Task-334 corrects that prose.

### Refusal, never fallback

Two ways a credential can fail, and neither resolves the owner:

- **`unverified_run_credential`** — it proves nothing. Malformed, an unknown run, or a
  digest that does not match.
- **`expired_run_credential`** — it is genuine and its run has ended.

They are separate problems because "somebody presented a forgery" and "a real run
outlived its credential" are different events for whoever is reading the log. Falling
back to a *more capable* principal on expiry would invert the entire control, which is
why the second one exists rather than being folded into silence.

### A run that has no credential

A run dispatched before this shipped, or one whose settings document could not be
written, carries none — and therefore resolves as **`owner`**, exactly as every run did
before task-331. That is the pre-existing state rather than a new hole, and it is the
narrowest answer available today: nothing is enforced yet, so there is no less-capable
principal to assign, and refusing such a run outright would break dispatch for the case
the constraint says must keep working. A session whose credential could not be delivered
records `session_env: uncredentialed` on its run, so this is readable off the ledger
rather than inferred. Task-332, which decides what each kind may do, is where an
uncredentialed run stops being indistinguishable from a person.

## Why `actor_id` is empty

`actor_id` is `None` on every principal this module resolves, and that is the finished
state rather than a stub — the last version of this page said task-330 would fill it in,
and task-330 deliberately did not.

Which configured person a login belongs to depends on a **project's** actor vocabulary,
while principal resolution is project-agnostic by design: one request, one principal,
before any handler knows what it is addressing.
`human_identity(config, principal)` is the function that answers it — see
[the identity registry](identity-registry.md). A resolved principal carries the raw
`login` the front door proved, which is that function's input.

It stays `None` forever for a `run` under any caller: a run is not a human and must
never be attributed as one. Guessing a mapping here would write an attribution nobody
configured, which is the failure
[`agentjobs.actors`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/actors.py)
exists to prevent.

## What comes next

| Task | Adds |
| --- | --- |
| task-330 **(shipped)** | maps a proven login to a configured actor, replacing `human_identity`'s `MULTIPLE` refusal with a per-request answer — [the identity registry](identity-registry.md) |
| task-332 | capabilities per principal kind, and what an absent principal means |
| task-244 | sets the identity header at the proxy |

The parent is task-066, whose decision entry of 2026-09-03 is binding on all of them.
