# Exposure: which projects a caller is served

> **Shipped** (task-333). A project marked `visibility: local` in its own
> `.agentjobs/config.yaml` is not served to a remote principal — not its tasks, not its
> runs, and not the session transcripts those runs left behind. To that caller it does
> not exist.

Implemented in
[`src/agentjobs/exposure.py`](https://github.com/jeffposey/agentjobs/blob/main/src/agentjobs/exposure.py)
(the rule) and `src/agentjobs/api/dependencies.py` (the enforcement). Tested in
`tests/test_project_exposure.py`.

## The question it answers

[Authorization](authorization.md) answers *what may this caller do*. It deliberately left
reads alone, and said so. This is the other half: **what may this caller read.**

They need different machinery, because a capability is a property of the caller and
exposure is a property of the **data**. Two projects this machine serves carry a standing
"local only, never push" rule, and knowing who is asking does not keep them local:

- `GET /api/all/tasks` returned every project's tasks to whoever asked — 3.9 MB of them,
  measured against the tailnet hostname (audit 2026-08-21, finding S-6).
- `GET .../runs/{id}/transcript` serves a file AgentJobs does not own. It is read out of
  the runner's own store under `~/.claude/projects/`, and its contents are whatever that
  session happened to look at. **A run that read a local-only project has that project's
  material in its transcript**, reachable through a route that is about runs.

There is no route that mentions those projects by name, so there is nowhere to put a rule
about them except on the project itself. Every surface then has to ask.

## The setting

```yaml
# <project>/.agentjobs/config.yaml
visibility: local     # or: shared
```

| Value | Served to |
| --- | :-- |
| `shared` (default) | every principal |
| `local` | `owner` and `run` — the principals already on this machine |

`agentjobs project list` marks the local-only ones, which is how you check that an edit
took without opening five config files.

**Why `visibility` and not `expose_on_proxy`.** The audit proposed the latter and it names
the wrong thing. The control is about which *principals* may read the project; the proxy
is merely today's only way for a non-local one to arrive. A second front door — another
proxy, a LAN bind, an authenticated public endpoint — would make a proxy-shaped name read
as a lie while the code carried on working correctly, which is the worst kind of wrong
name. `visibility` is a property of the thing being read, which is what this is.

**Why the project's own config and not the machine registry.** Local-only is a property of
the project, not of this laptop: `vault` is local-only wherever it is checked out. The
config travels with the repository and says it once instead of once per machine. The
registry stays what it is — a disposable list of what this machine has cloned.

**Why `local` includes `run`.** A dispatched agent runs here, as a local process, with the
project's files on its filesystem. Refusing it over HTTP would stop nothing and would break
a run dispatched *into* a local-only project from reading its own task. `tailnet` is the
kind this excludes, because it is the one whose identity was established somewhere else.
This is a rule about where the caller is, not about how much it is trusted — the two human
kinds remain equally privileged, and a `tailnet` caller keeps every capability it had over
the projects it can still see.

## The defaults, in both directions

**An unset key means `shared`**, and that is deliberate. Defaulting closed would be the
safer-sounding choice and the wrong one: every existing install loses its phone at upgrade,
silently, with no error naming the cause — and a change with that shape gets reverted
rather than configured. This is a disclosure control in front of a door that is already
authenticated, so default-open decides which of the owner's own projects their phone shows,
not whether a stranger sees any. New projects do not rely on it: `agentjobs init` writes the
key explicitly, so a project created today records a decision.

**A key that is present and unreadable means `local`.** The only way to get an unrecognised
value is to have written one, so somebody was trying to say something about this project's
exposure and mistyped it. Hiding it is recoverable in one edit; publishing it is not.

## Absent, never forbidden

A caller who may not see a project is told it does not exist. That is the convention
`_owned_run` already followed for runs of another project, and the reason is the same: a
refusal that distinguishes "forbidden" from "not here" tells a remote caller that the
hidden thing exists, which is most of what hiding it was for.

The 404 is built once, in `dependencies._no_such_project`, so the hidden case and the
never-registered case cannot drift into being distinguishable. It carries the same
`Registered projects: …` sentence an unknown id has always carried — **drawn from the
caller's own visible set**, because the registry's version lists every id and would have
disclosed the hidden projects through the very response that is hiding them.

## Where it is enforced

| Surface | What happens |
| --- | --- |
| `GET /api/projects` | hidden projects are omitted from the list |
| `/api/projects/{id}/…` (every scoped route) | 404, as if never registered |
| unscoped `/api/…` routes | the default project is checked too; an unresolvable default is the existing 409 |
| `GET /api/all/tasks` | **filtered**, not denied |
| `GET /api/runs/live` | hidden projects' runs and lock holders are dropped; `occupied` still counts them |
| `.../dispatch/runs/{id}/output`, `/tail`, `/transcript` | 404 through `_owned_run` |
| the legacy Jinja pages | the project switcher and picker list only visible projects |

Two of those rows are the interesting ones.

**`/api/all/tasks` is filtered rather than denied.** The audit proposed blocking it at the
proxy. Filtering is strictly better: the route keeps working for the projects a phone is
meant to see, and it stays correct the day there is a second front door — a proxy rule
protects only the door it is written on.

**`occupied` still counts a hidden project's run.** The rows and the number answer
different questions and only one of them is about exposure. The rows are "what may I
read"; the capacity is "why can I not dispatch", and a machine that is full because of a
hidden project's run is still full. Subtracting the hidden rows would make that surface
the one place in the system able to disagree with `dispatch/guards.py` about whether there
is a slot.

**One choke point does most of the work.** Every project-scoped route resolves its project
through `dependencies.request_project`, and every router is mounted twice behind it — so
the check lands once and a hidden project's tasks, runs, transcripts, webhooks, searches
and queue become absent together, rather than one route at a time as somebody remembers
them.

## What this does not do

1.  **It is not per person.** Exposure is per project and per principal *kind*. The moment
    it depends on *which* human is asking it stops being a setting and becomes a role
    system, which task-066 ruled out for the whole epic.
2.  **It does not touch the CLI**, which drives the manager directly and speaks no HTTP.
    `agentjobs dispatch walk`, the poller, the scripted finish and `agentjobs queue` all
    run as the person at the machine and see everything, by construction.
3.  **It is a disclosure control, not authentication.** A `tailnet` principal exists only
    because the front door proved an identity, which since task-244 means the tsnet proxy
    ran `WhoIs` on the connection, set `X-Tailscale-User`, and proved it was the proxy
    with the shared secret. Nothing here authenticates anybody; it decides what an
    already-authenticated caller is shown. See [principals](principals-design.md).

    The order those two landed in is worth knowing, because it is the difference between
    a control that works and one that is merely written down: **until the proxy set that
    header, every remote caller resolved as `owner`**, so a `visibility: local` project
    would have gone on being served to a phone. Both are on `main` as of 2026-09-04.
4.  **It hides projects, not tasks.** There is no per-task exposure and none is planned:
    a task that must not be read from a phone belongs in a project that must not be.
