# Identity: which person a proven login is

> **Shipped** (task-330). Several people can be configured on one project, each request
> is attributed to whoever made it, and a person can be retired without their history
> changing. This is *attribution*, not authorization: nothing here decides what a caller
> may do — that is [task-332](principals-design.md#what-comes-next).

Implemented in [`src/agentjobs/identities.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/identities.py)
and [`src/agentjobs/actors.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/actors.py),
tested in `tests/test_identities.py`, `tests/test_actors.py` and
`tests/test_identity_registry_api.py`.

## The problem it closes

[Principals](principals-design.md) resolve *who is asking* and stop there: a remote
caller arrives carrying the raw login the front door proved, and nothing turns that into
one of the ids a task record names. Until it did, a project configuring two people was
refused outright — a yellow box telling the second person to edit YAML and delete
somebody. That refusal was deliberate (task-064): attributing one person's approval to
another is worse than attributing it to nobody, and with no way to tell who was at the
keyboard there was no third option. There is one now.

## Where the mapping lives, and why

**Two files, deliberately.**

| | File | Holds |
| --- | --- | --- |
| Machine | `~/.agentjobs/identities.yaml` | which login is which actor id, and who the machine's owner is |
| Project | `.agentjobs/config.yaml` | the actor vocabulary — who exists, their kind, their display name, whether they are retired |

```yaml
# ~/.agentjobs/identities.yaml
owner: jeffposey
identities:
  - login: jeff@example.com
    actor: jeffposey
  - login: sam@example.com
    actor: sam
```

The mapping is machine-level because a tailnet login is a property of this machine's
tailnet and not of any repository: one person has one login across every project this
server serves. It sits beside `projects.yaml` for the same reason that file is there —
both record what this particular machine has, and both are disposable.

The rejected alternative was putting the mapping in each project's `.agentjobs/config.yaml`
alongside the actor vocabulary, on the reasonable grounds that splitting identity across
two files makes both harder to reason about. It loses on two counts. Project config is
committed and travels with a clone, so a login map there publishes the machine's account
list to everyone who clones the repository. And the same person would have to be
re-declared in every project on the machine, so adding a second person is *n* edits
rather than one — which is exactly the friction this task exists to remove.

The **actor vocabulary stays per project**, unchanged, because who may act on a project
is a property of that project and travels with it correctly.

**One residual, stated rather than designed around:** the mapping names one actor id, so
a person whom two projects call by different ids can only be mapped to one of them. The
other project refuses with `unknown_actor`, naming the id and the config file. Using the
same id in both is the fix, and a per-project alias can be added if a real install ever
needs one — speculative generality here would be worse than the refusal.

## Resolution, per request

`human_identity(config, principal)` answers *which person is acting on this project right
now*. The order is fixed:

1. **A run is not a person.** Refused first, before anything can map it. A dispatched
   agent that resolved to a human would be the impersonation the epic exists to close.
2. **A proven login** is looked up in the machine registry. Mapped to a person who can
   act, that is the answer, whatever else config contains.
3. **Anything else** — bare loopback, or no request at all, such as the CLI — is the
   machine's owner. `owner:` names them; failing that, a project with at most one person
   who can act resolves them from `default_user` or from being the only candidate, and a
   project with several is refused.

### Refusal, never fallback

An identity that maps to nothing is **refused**. It does not become `default_user`.

That fallback is precisely the defect task-064 removed, and re-introducing it under a
registry would undo that task while appearing to extend it. It is asserted by test in
both `tests/test_actors.py` and — over HTTP, where the mistake would actually be made —
`tests/test_identity_registry_api.py`.

The refusal is the whole onboarding experience for a second person, so it names the
login that was not recognised, the file to add it to, and the shape of the entry. One
test feeds the printed entry straight back through the parser, so the instruction cannot
drift from what parses.

| Problem | Means |
| --- | --- |
| `unconfigured` | nobody at all is configured; add yourself to `actors:` |
| `unmapped` | a proven login that maps to no actor; add it to `identities.yaml` |
| `unknown_actor` | mapped to an id this project's `actors:` does not define |
| `retired` | mapped to somebody who has stopped acting |
| `ambiguous` | several people can act and the request proved no identity |
| `not_a_person` | the caller is a dispatched run |

`ambiguous` is the successor to the old `multiple`, and is narrower by exactly one case:
a remote caller whose login is mapped is not ambiguous however many people are
configured. It is also where `default_user` is deliberately *not* consulted — project
config is committed and shared, so it cannot say who is at *this* keyboard, which is why
the machine's `owner:` is the thing that answers.

## Retirement

```yaml
actors:
  - name: sam
    kind: human
    retired: true
```

Retirement changes exactly one thing: `validate_actor` refuses new writes as that
person. It changes **nothing** about reading history. The id stays in the vocabulary, so
`load_actors`, `actor_kinds` and every renderer of an old log entry keep resolving it to
a name and a kind — which is what "the log is append-only" has to mean in practice.
Deleting the entry instead would leave every entry they ever wrote pointing at nothing.

The refusal is a `RetiredActorError`, a subclass of `UnknownActorError`. Every caller
that already turns an unwritable id into a 400 does so for this one too, with its own
message and no plumbing, while a caller that wants the distinction still has it.

A project with two people, one of them retired, has exactly one who can act — so
retiring somebody resolves the ambiguity rather than creating one, and needs no further
configuration.

## What this is not

- **Not authentication.** The front door proves the identity; this only says which
  configured person it is.
- **Not authorization.** Which kind of principal may do what is task-332, now shipped —
  see [authorization](authorization.md).
- **Not per-person permissions.** Out of scope for the whole epic, per task-066.
- **Not a UI.** Config is the interface. The dashboard renders the refusals and nothing
  else about people.
