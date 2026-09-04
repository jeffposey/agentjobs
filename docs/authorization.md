# Authorization: what each kind of principal may do

> **Shipped** (task-332). Every mutating route in the API is checked against a
> capability table keyed by principal kind, and a body `user`/`actor` that disagrees
> with the principal the request resolved to is refused. What it does **not** do is
> authenticate a remote caller — that is still tailnet membership, and
> [the API reference](api-reference.md) is still right about it. Read
> [the limits](#what-this-does-not-do) before concluding otherwise.

Implemented in
[`src/agentjobs/capabilities.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/capabilities.py)
(the rule) and
[`src/agentjobs/api/authorization.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/api/authorization.py)
(the route table and the gate). Tested in `tests/test_capabilities.py`,
`tests/test_authorization.py` and `tests/test_run_authorization.py`.

## What it replaces

The review endpoints compared the submitted `user` against the project's
`default_user` — and `GET /api/projects` publishes `default_user` for every project.
That is not a weak check, it is a **circular** one: the API told you the password and
then asked for it. The dispatch endpoint had the same shape, accepting any configured
human's id from the same published list. Both are finding S-1 of
[the 2026-08-21 security audit](../audits/2026-08-21/12-security.md).

The comparison is now against
[the principal](principals-design.md) the transport proved, which no response hands out.

## The table

Keyed by principal **kind**, never by person. There are three kinds and no fourth, so
the table is complete by construction rather than by maintenance. Per-person permissions
are out of scope for the whole of task-066: the moment a capability depends on *which*
human is asking, this stops being a table and becomes a role system.

| Capability | Routes | `owner` | `tailnet` | `run` |
| --- | --- | :-: | :-: | :-: |
| `task.create` | `POST /tasks` | ✓ | ✓ | ✓ |
| `task.edit` | `PATCH`/`DELETE /tasks/{id}`, deliverables | ✓ | ✓ | own task |
| `task.verb` | promote, claim, handoff, release, close, log, progress | ✓ | ✓ | own task |
| `task.queue` | queue-move, queue-keep, reprioritize | ✓ | ✓ | own task |
| `task.review` | approve, request-changes, answer, redirect, hold, resume, reject | ✓ | ✓ | — |
| `dispatch.start` | task dispatch, playbook run, run cancel | ✓ | ✓ | — |
| `dispatch.admin` | dispatch enable / disable | ✓ | ✓ | — |
| `project.admin` | project register / init / inspect | ✓ | ✓ | — |
| `queue.admin` | queue repair / compact | ✓ | ✓ | — |
| `webhook.admin` | webhook create / delete / test | ✓ | ✓ | — |
| `run.output` | run and finish output | ✓ | ✓ | own run |

**The two human kinds are identical, deliberately.** `owner` and `tailnet` differ in how
identity was established, not in what they may do. Narrowing `tailnet` would be an
*access policy*, and an access policy belongs at the proxy (task-244) where it can be
stated once for every route rather than restated per capability.

**A run's set is scoped, not merely small.** Membership is only half the answer: a run
that may close *any* task can still close somebody else's. The task-scoped capabilities
additionally require the run to name the task it was dispatched to work.

Two judgement calls inside that, since the spec settles neither:

- **Queue moves are scoped.** [ALLAGENTS.md](../ALLAGENTS.md) tells an agent that
  disagrees with the backlog's order to move the task it thinks should be first, which
  argued for leaving moves unscoped. That instruction addresses a session *choosing*
  what to work on next; a dispatched run is given its task rather than choosing it, and
  a run reordering other people's work is the same class of act as closing it.
- **Creating a task is not scoped.** It is on neither of the spec's lists, agents file
  follow-ups as a matter of course, and a new task is not "a task other than its own".

## The body field must agree

The explicit `actor`/`user` field survives and is not going away: agents and scripts post
directly and hold no session, and the field is how a write says whose name goes in an
append-only log. What changed is that it is a **claim checked against the transport**
rather than a claim taken on trust. The check is made on the axis each principal could
lie about, and only there:

| Principal | May claim | May not claim |
| --- | --- | --- |
| `run` | the agent id it was dispatched as | any `kind: human` id; any other agent |
| `owner` / `tailnet` | themselves, or any `kind: agent` id | a *different* person |

A human claiming an agent id is not a hole. The CLI and the MCP server run as the person
at this machine and legitimately attribute their writes to the tool, and an
agent-attributed entry is strictly the weaker one — it cannot clock a dispatch, which is
what `assert_human_clocked` exists for. Refusing it would have broken every local agent
write and protected nothing.

The review loop is stricter: `approve`, `reject` and the send-backs require the claim to
be the acting human exactly, because "approved by" is the one field whose entire content
is which person said yes.

## Refusals

Every refusal carries a **code** and a **sentence**. The code is in the body as well as
the status because 403 alone cannot tell "your credential is for another run" from "no
principal holds this at all", and the two need different responses. The sentence names
what was claimed and what was resolved, because the likeliest cause of seeing one of
these is a misconfigured identity mapping rather than an attack — and a message that
says only "forbidden" turns a five-second config fix into a debugging session.

| Code | Status | Means |
| --- | --- | --- |
| `capability_denied` | 403 | no row of the table grants this to your kind |
| `wrong_task` | 403 | a run addressing a task other than its own |
| `wrong_run` | 403 | a run reading another run's output |
| `actor_mismatch` | 403 | the body named somebody you are not |
| `identity_unresolved` | 400 | who you are cannot be worked out; the fix is a config file |
| `no_proven_identity`, `unverified_run_credential`, `expired_run_credential` | 403 | nothing resolved — see [principals](principals-design.md) |

`identity_unresolved` is the one 400, and the distinction is not cosmetic: it is not a
refusal of the caller at all. It says the machine cannot work out who a legitimate caller
is, and no different request fixes that.

## Where the check happens

One dependency, installed application-wide, keyed on the **endpoint function**.

Keying on the function rather than the path is not cosmetic: every task-facing router is
mounted twice — unscoped at `/api` and again at `/api/projects/{project_id}` — so a
path-keyed table would need two entries per route and could silently cover only one of
them. A dependency rather than middleware, because middleware runs *before* routing and
would know neither the matched endpoint nor the path parameters.

**Coverage is enforced rather than promised.** `tests/test_authorization.py` walks the
application's own router, filters the mutating methods, and fails when an endpoint is not
named in the table — in both directions, so a *renamed* endpoint that silently unchecks
its route fails too. A route added without a decision about who may call it turns the
suite red; it does not ship unchecked.

## What this does not do

Four limits, stated because the natural next assumption is wrong in each case.

1.  **It is not authentication.** A tailnet peer still reaches this API without proving
    anything, and until [task-244](principals-design.md) sets the identity header at the
    proxy, every such caller resolves as `owner` or as nothing at all. This closes the
    *agent* half of audit S-1 and the impersonation half of the human one; the network
    half is still tailnet membership.
2.  **Reads are almost entirely untouched.** The only reads in the table are the run and
    finish output routes. What the API exposes to a reader is task-333's question, and
    answering it here would have smuggled a second change into this one.
3.  **An uncredentialed run is still the owner.** A run dispatched before task-331, or
    one whose settings document could not be written, presents no credential and
    therefore resolves as `owner` — and now holds everything an owner does. That is the
    pre-existing state rather than a new hole, and the ledger records
    `session_env: uncredentialed` so it is readable rather than inferred.
4.  **A run holds its own credential and can pass it on.** The property is "an agent
    cannot claim to be a person", not "an agent cannot misbehave as itself". A run
    principal is strictly less capable than an owner, so there is no escalation path
    through it — see
    [what the credential buys](principals-design.md#what-it-buys-and-what-it-does-not).

Nothing here reaches the CLI, which drives the manager directly and speaks no HTTP. So
`agentjobs dispatch walk`, the session poller, the scripted finish and `agentjobs queue`
are all outside this gate by construction. The HTTP callers are the React app (a human)
and the MCP server, whose client presents the run credential when it is inside a run —
which is what makes a dispatched agent's MCP tools capability-checked.
