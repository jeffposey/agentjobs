# Authorization: what each kind of principal may do

> **Shipped** (task-332). Every mutating route in the API is checked against a
> capability table keyed by principal kind, and a body `user`/`actor` that disagrees
> with the principal the request resolved to is refused. What it does **not** do is
> authenticate a remote caller — that is still tailnet membership, and
> [the API reference](api-reference.md) is still right about it. Read
> [the limits](#what-this-does-not-do) before concluding otherwise.

Extended by task-506 with one capability and one log entry type, so an agent at the
owner's keyboard can record an authorisation given in a chat without signing anyone's name to
it: [Relaying a human's authorisation](#relaying-a-humans-authorisation-task-506).

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
[the 2026-08-21 security audit](https://github.com/jeffposey/agentjobs/blob/main/audits/2026-08-21/12-security.md).

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
| `task.edit` | `PATCH`/`DELETE /tasks/{id}`, deliverables | ✓ | ✓ | ✓ |
| `task.verb` | promote, claim, handoff, release, close, log, progress | ✓ | ✓ | ✓ |
| `task.queue` | queue-move, queue-keep, reprioritize | ✓ | ✓ | ✓ |
| `history.record` | `PUT /history/finishes/{id}`, `PUT /history/gates/{id}` -- a finish or a gate indexing itself (task-472) | ✓ | ✓ | ✓ |
| `task.review` | approve, request-changes, answer, redirect, hold, resume, reject | ✓ | ✓ | — |
| `model.draft` | `POST .../model/draft` -- one drafting call to a model provider (task-175) | ✓ | ✓ | — |
| `dispatch.start` | task dispatch, playbook run, run cancel, queued-dispatch cancel | ✓ | ✓ | — |
| `dispatch.relay_authorization` | `POST .../tasks/{id}/authorization` -- record that a person authorised a dispatch, as the agent they told (task-506) | ✓ | ✓ | — |
| `dispatch.over_ceiling` | the `over_ceiling` field on a task dispatch | ✓ | ✓ | — |
| `dispatch.admin` | dispatch enable / disable, pull-mode arm / disarm, idle-session settings | ✓ | ✓ | — |
| `project.admin` | project register / init / inspect | ✓ | ✓ | — |
| `queue.admin` | queue repair / compact | ✓ | ✓ | — |
| `webhook.admin` | webhook create / delete / test | ✓ | ✓ | — |
| `run.output` | run and finish output, tail, transcript | ✓ | ✓ | own run |

**`model.draft` is a purchase, which is why it sits with `dispatch.start` and not with
`task.create`** (task-175). Filing the drafted task afterwards is an ordinary create and
needs only `task.create`, which a run does hold; what a run may not do is make the machine
pay for the prose. That is the whole of what forecloses the loop -- a drafting call
deliberately consumes no dispatch run slot, so nothing dispatch counts bounds it, and an
agent that cannot reach the route cannot spend the budget however much it does.

Its sibling `GET /model` is **not** on the table, which is the one read in this API left
out on purpose rather than by the policy that leaves reads out. It answers a boolean about
this machine's own configuration and there is no field on its response a credential could
occupy; gating it would leave a run unable to discover that it may not draft, and a run
that reads it still cannot call the route it describes.

**A queued dispatch is cancelled under `dispatch.start`, like the run it has not become**
(task-459). It is the same route -- `POST /projects/{id}/dispatch/runs/{run_id}/cancel`,
with the queue entry's id -- and deliberately not a capability of its own: a waiting entry
and the run it turns into are one card to whoever is looking at it, and a click that lands
a moment late must stop the agent rather than be refused for naming the wrong kind of
thing. So an agent cannot cancel a queued dispatch, for the reason it cannot cancel a run:
a run is a purchase a person signed for, and unqueueing one is a decision about that
purchase.

**`dispatch.over_ceiling` is the one row keyed to a body field rather than to a route**
(task-461). `POST .../tasks/{id}/dispatch` needs `dispatch.start` from everybody, and a
body carrying `over_ceiling: true` needs this as well -- the request asks to start a run
although `limits.max_concurrent_runs` says the machine is full, which is a different
question from whether the caller may spend money at all. The ceiling exists to stop a
click starting an agent the machine cannot afford; a person choosing to exceed it with
the slot holders named in front of them is a judgement about *this* machine at *this*
moment, and it is the one place where "a human clicking repeatedly is a decision rather
than a malfunction" survives, because since task-332 the server can tell a person from a
run.

Two locks, not one, and the second is currently unreachable on purpose. No run holds
`dispatch.start`, so the route rule already refuses a run before the body is read --
which is the answer a client gets, and what `tests/test_run_authorization.py` asserts.
The field check is what stops the overage riding along if `dispatch.start` is ever
widened; `tests/test_capabilities.py` asserts it on its own terms, where such a widening
is what would break it. What an overage does *not* widen is everything else: it is a
dispatch, so it counts against `dispatches_per_hour` like any other, and every other
gate binds unchanged.

### Relaying a human's authorisation (task-506)

`dispatch.relay_authorization` is the newest row and the one whose **absence** from a run's
set is the entire feature, so it is worth stating on its own.

**The problem.** [The dispatch rule](agent-dispatch-design.md#2-the-governing-rule-the-loop-is-human-clocked)
requires the log entry a run is attributed to to be a human's. An interactive agent session
told, in a chat, to file a task and start it satisfies that in no way: the newest entry on
the task is its own. The instruction was a real human act and the server cannot see it.

Two paths existed and neither was the answer. **The owner clicks Dispatch** — correct, and
a round trip for something already said. **The agent writes the note as the owner** — it
works, because a human principal may claim itself, and it puts a signature in an
append-only log under a name its owner did not type. No reader can tell that entry from a
click, and the MCP tool contract tells agents not to do it in as many words.

**The shape.** A log entry type, `authorization`, whose author and subject are different
parties:

| Field | Holds |
| --- | --- |
| `actor` | the agent that typed it. Never the person. |
| `data.authorized_by` | the person whose act it records. Must be `kind: human`. |
| `body` | what they asked for, in the agent's words. |

`assert_human_clocked` resolves `authorized_by` on this type and the `actor` on every
other. What is *not* relaxed is which ids count: an agent named there is refused in the
same words an agent author is, and an unconfigured id is refused rather than assumed.

**The ask lives in `body` for a reason that is not stylistic.** `agentjobs quotations` and
the gate's corpus checks address a log entry's prose as `log[<id>].body` and do not reach
`data`, so an ask stored in the payload would sit outside the paraphrase rule this
repository enforces on every other sentence in a record. In `body` it is covered like
anything else.

**Two locks, and they shut different doors.** A run holds `task.verb`, so `POST /log` is a
request it may make — which is why this is a *type* and not a marker on a `note`.
`authorization` is in `MANAGER_WRITTEN_LOG_TYPES`, so **no** caller reaches it through the
generic log route, human or run; and the dedicated verb that can write one needs this
capability, which is what distinguishes a run from a person. Both are asserted in
`tests/test_run_authorization.py`.

**What it does not confer.** It starts nothing. It is not an approval, it releases no merge
gate, it arms nothing, and it consumes no run slot. A dispatch afterwards is an ordinary
`manual` one, so it counts against `dispatches_per_hour` and every other cap in
[§7](agent-dispatch-design.md#7-runaway-protection) — asserted, because those caps are what
actually bound a dispatch loop and a trigger they did not count would be a way round the
only real control. The entry is **not consumed** either: it clocks as many runs as a
human's own note does today, which is to say as many as the caps allow. No new looseness,
and no new single-use mechanism to get subtly wrong.

**What it is worth, stated honestly.** The server cannot verify that the person said
anything, exactly as it cannot verify the `user` field on an HTTP dispatch — that is audit
P1-2 and it is unchanged. What this buys is two things and not a third: a record that is
**true about who typed it**, and a write a **dispatched run cannot make**. So the
accidental loop stays unrepresentable through every supported path, and a deliberate one is
attributable to whoever's credential made it.

**It does not reach the CLI, and does not claim to.** `agentjobs dispatch authorize` is
served as the person at this machine, like every CLI verb, and a run with a shell has the
whole CLI — the standing boundary the last section of this page states, not a new one. The
gate added here is a *principal* gate: HTTP and MCP refuse a run.

**Arming the pull mode is `dispatch.admin`, and the reason is what it grants** (task-462).
Every other row on this table is permission to do one thing once. An arming is a standing
authority for the server to keep starting runs, unattended, until its bound runs out --
which is why it sits with enable/disable rather than with `dispatch.start`: it is a change
to what this machine will do on its own, not a purchase. A run holds neither capability,
so the property that matters holds under either reading and is the one worth stating
plainly: **an agent cannot arm the machine to keep starting agents.** It is refused 403
with `capability_denied` before the handler is entered, by the same dependency that
refuses it `approve`.

Disarm shares the capability with arm, on the rule every kill switch follows here: whoever
may turn a thing on may turn it off, and a switch reachable by fewer people than the thing
it stops is worse than none.

**The two human kinds are identical, deliberately.** `owner` and `tailnet` differ in how
identity was established, not in what they may do. Narrowing `tailnet` would be an
*access policy*, and an access policy belongs at the proxy (task-244) where it can be
stated once for every route rather than restated per capability.

**A run may write any task, and none of the acts a human signs for** (task-411,
2026-09-13). task-332 shipped the three task capabilities scoped to the run's own task.
The owner lifted that scope because it stopped the work instead of protecting it: the
`groom` and `reorder` playbooks exist to close and move *other* tasks and refused
`wrong_task` at their first write (task-406), and a run that found a bad record turning
its own gate red could not correct it.

What still bounds a run is the rest of the table. It cannot approve, and approval is the
only route that starts a scripted finish or an auto-dispatch, so no write a run makes to
another task reaches a merge or spends money. It writes only as the agent it was
dispatched as, so every such write is attributed to it in an append-only log.

The cost, stated: a run can close, rewrite or reorder a task another run is working or a
person is reviewing. Those writes are logged verbs and recoverable, and nothing pushes.
The scope is not narrowed to the run's own *project* either, because the run credential
does not carry one; that is the change to make if a project ever has a different owner.
`OWN_TASK_ONLY` in `capabilities.py` is kept, empty, so re-scoping is one line.

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

Since task-506 there is one write where naming somebody else is the *point*, and it is not
an exception to this table. `POST .../authorization` still checks `actor` exactly as above —
a run may not name a person there either — and carries the human in a separate field the
entry is explicit about. The rule is unchanged: you may not claim to be somebody else; you
may record that somebody else asked for something.

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
| `wrong_task` | 403 | a run addressing a task other than its own; unused since task-411, kept for re-scoping |
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
2.  **Reads are almost entirely untouched, and the reader's question is answered
    elsewhere.** The only reads in this table are the run-output routes. What the API
    exposes to a *reader* was task-333's question and is now
    [Exposure](exposure.md): a capability is a property of the caller, while whether a
    project may be read at all is a property of the data, and the two needed different
    machinery. Answering it here would have smuggled a second change into this one.
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

Nothing here constrains the CLI. Its task verbs, `agentjobs queue` included, call the
service over HTTP but send no run credential, so they are served as the person at the
machine; `agentjobs dispatch walk`, the session poller and the scripted finish open the
store directly. A run with a shell therefore has the whole CLI (Big Dawg Audit II, R4). The HTTP callers are the React app (a human)
and the MCP server, whose client presents the run credential when it is inside a run —
which is what makes a dispatched agent's MCP tools capability-checked.
