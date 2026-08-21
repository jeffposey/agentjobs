# AgentJobs

**A git-backed handoff protocol for work that outlives an agent session.**

AgentJobs is for developers coordinating coding agents across short-lived sessions,
especially when a human must review, decide, or approve between passes.

Agents are stateless: a new session starts with no memory of the last one. AgentJobs
makes the task record the durable working memory, including the handoff itself:

```yaml
lifecycle: active
ball: human
ball_reason: review
ball_prompt: Review the diff; approve or request changes.
```

Every open task names who has the ball, why they have it, and what they need to do next.
A handoff without an ask is invalid.

A fresh agent resumes from the record alone: the specification, the current ask, the
decision and question log, and the acceptance criteria. That
[resumption contract](docs/schema-design.md#the-resumption-contract) was tested on
2026-08-11: a zero-context headless agent reconstructed the work and found
[three defects in the dispatch design](tasks/agentjobs/task-060-agent-dispatch.yaml).

## Agents connect over MCP

```bash
pip install agentjobs && agentjobs serve
```

Then point any MCP client at `agentjobs mcp`. Thirteen tools cover discovery, the whole
claim/handoff/release/close loop, the append-only log, and zero-context resumption —
each one validated, locked and logged by the same code the UI writes through.

Task YAML stays readable and stops being writable: there is no `set_lifecycle`, no
generic patch, and no way to author a state change without recording it. Retries are
safe (send the same `operation_id` and it replays rather than writing twice) and stale
decisions are refused rather than silently overwriting someone.

Codex additionally gets a bundled plugin with a workflow skill and a hook that refuses
direct writes to task files. Every client gets `agentjobs validate`, the portable
backstop. [What each layer does and does not prevent](docs/mcp.md#what-protects-what)
is written down rather than implied.

## Why this is not another task tracker

- **Hierarchy has workflow meaning.** Parent tasks roll up their children and are not
  claimable while a child remains open. The UI shows child progress and each child's
  derived status.
- **Git is the database.** One YAML file per task keeps work diffable, reviewable, and
  portable between tools without adding a service to operate.

## The React application

The primary human interface is a responsive React application at `/app/`. It is
designed for desktop and laptop browsers, tablets, and phones, so a reviewer can
inspect task details, create tasks, approve work, or request changes from the device
that is convenient at the time.

The React UI adapts rather than merely shrinking: navigation and action groups stack
on smaller screens, wide task tables become labelled cards, and interactive controls
retain touch-friendly sizing. Over private HTTPS, the same application can be
installed from a phone or tablet browser as a Progressive Web App (PWA). See
[Mobile and installed-app access](docs/mobile-access.md) for the secure setup and its
network-only task-data behavior.

The production React bundle is included in the Python package. Running an installed
release therefore requires Python, but not Node, npm, a separate frontend server, or
a particular desktop operating system.

## What works today

- Schema-v2 task records with lifecycle, ball, outcome, typed logs, acceptance criteria,
  dependencies, parent relationships, and strict validation
- A FastAPI REST API and Python client for claiming, handing off, releasing, closing,
  querying, and logging work
- A packaged React web application for desktop browsers, tablets, and phones, with
  a project switcher for multiple registered projects, task creation and detail pages,
  hierarchy roll-ups, and human review actions
- Basic CLI workflows for creating, listing, showing, claiming/finishing interactively,
  serving, and migrating tasks
- Markdown-to-YAML and schema-v1-to-v2 migration tools

The Python client and REST API expose the full schema-v2 state verbs. Some dedicated
CLI mirrors remain backlog work; the React application is the primary human interface.

## The design is part of the product

The project records decisions and rejected alternatives before implementation, rather
than leaving the rationale in a chat transcript:

- [Task schema v2](docs/schema-design.md) decides how a task becomes sufficient working
  memory for a zero-context agent, including the ball model and canonical handoff loop.
- [Agent dispatch](docs/agent-dispatch-design.md) is the accepted, **not yet implemented**
  design for turning authorized task state into a supervised agent process, with bounded
  autonomy and explicit safety gates.

Agent loops are also **not implemented**. Their design pass is queued in
[task-078](tasks/agentjobs/task-078-agent-loops.yaml); the proposed contribution is an
evaluable stopping condition and durable iteration history, not another `while true`
wrapper. No agent-loops design document exists yet.

## Installation

AgentJobs requires Python 3.11 or newer and is not yet published to PyPI. Run it from a
clone; Node is needed only for contributors building the React bundle, never to install
or run a release wheel:

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
```

## Quick start

```bash
# From the AgentJobs clone, explore the project's own task data
poetry run agentjobs open

# Or initialize another project while using the cloned package
cd /path/to/your-project
poetry -P /path/to/agentjobs run agentjobs init

# Start the server and open the packaged React application in a browser
poetry -P /path/to/agentjobs run agentjobs open
```

From the AgentJobs clone, useful commands include:

```bash
poetry run agentjobs create --title "Describe the work" --priority high
poetry run agentjobs list --lifecycle ready
poetry run agentjobs show task-001
poetry run agentjobs work --agent my-agent

poetry run agentjobs status
poetry run agentjobs restart --reload
poetry run agentjobs stop
```

Register more than one project with the same local server:

```bash
poetry run agentjobs project add /path/to/another/project
poetry run agentjobs project list
```

## Python client

The Python client exposes the schema-v2 state verbs even though dedicated CLI commands
for each verb are still planned:

```python
from agentjobs import Ball, BallReason, TaskClient

with TaskClient() as client:
    task = client.get_next_task(agent="my-agent")
    if task:
        client.claim_task(task.id, agent="my-agent")

        # Work, verify, and record decisions here.

        client.handoff_task(
            task.id,
            actor="my-agent",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Review the diff and approve or request changes.",
        )
```

## Documentation

- [Task schema reference](docs/task-schema.md)
- [Agent workflow guide](docs/agent-workflow.md)
- [API reference](docs/api-reference.md)
- [Quick start](docs/quickstart.md)
- [Installation guide](docs/installation.md)
- [Mobile and installed-app access](docs/mobile-access.md)
- [Migration guide](docs/migration-guide.md)
- [Task corpus audit](docs/task-corpus-audit.md)

## Development

AgentJobs uses itself to manage its own development. The roadmap lives in
[`tasks/agentjobs/`](tasks/agentjobs/), and the task YAML is the source of truth.

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
npm --prefix frontend install
poetry run python scripts/check.py
poetry run agentjobs open
```

The React application's source and focused development commands live under
`frontend/`:

```bash
cd frontend
npm install
npm run check
```

The repository commit gate is `poetry run python scripts/check.py` from the root. It
runs ten stages, cheapest first: formatting, lint and types, the generated-contract
checks, the frontend linter, then the Python suite, the Vitest component suite, the
production build, and one real-server Playwright path. `scripts/check.py --list` prints
them; `--from <stage>` resumes after a late failure without paying for the stages that
already passed. The unqualified command runs all ten, and that is the one the commit
rule means. `npm run check` is the focused frontend half of the gate. Run
`npm run generate:api` when an intentional backend contract change needs to be recorded.

During development Vite serves it at `http://localhost:5173/app/` and proxies API
requests to AgentJobs on port 8765. After `npm run build`, FastAPI serves the same app
at `http://localhost:8765/app` with deep-link fallback. The production output lives
inside the Python package at `src/agentjobs/frontend_dist/`, which is also where an
installed wheel resolves it.

Build release artifacts with `poetry run python scripts/build_release.py`. That command
reinstalls the locked frontend toolchain, creates a fresh bundle, invokes Poetry, and
verifies the finished wheel contains the React shell, hashed assets, manifest, icons,
and service worker. Node is required to create a release, but never to install or run
the universal wheel; `pip install agentjobs` followed by `agentjobs serve` is a
Python-only runtime path. Do not publish artifacts made through an alternate command—
the release script is the freshness and package-content gate. It enforces a
`py3-none-any` wheel and boots the installed server with Node removed from `PATH`.

Read [ENGINEERING.md](ENGINEERING.md) and [ALLAGENTS.md](ALLAGENTS.md) before
contributing; they define the worktree, task-record, verification, and human-review
workflow.

## License

MIT License — see [LICENSE](LICENSE).
