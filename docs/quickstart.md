# AgentJobs quick start

This guide initializes a project, opens the packaged React application, and runs the
canonical schema-v2 handoff loop.

## 1. Install and initialize

Until AgentJobs is published, run it from a clone:

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
npm --prefix frontend ci && npm --prefix frontend run build

cd /path/to/your-project
poetry -P /path/to/agentjobs run agentjobs init
```

The `npm` line builds the React bundle, which is gitignored and which no `poetry
install` produces. Skip it and step 2 has nothing to open. See
[the installation guide](installation.md) for why, and for the release-wheel case that
needs no Node at all.

Initialization creates `.agentjobs/config.yaml`, the configured task directory, a
project registration for the local server, and a `.mcp.json` declaring the AgentJobs
MCP server so agents working here have the tools rather than falling back to the CLI.
See [the MCP server](mcp.md#every-registered-project-declares-the-server) for what that
file contains and how to add it to a project registered earlier.

## 2. Open the React application

```bash
poetry -P /path/to/agentjobs run agentjobs open
```

The primary UI opens at `http://localhost:8765/app/`. If the bundle was never built,
`open` prints the build command and exits without opening a browser, so this step
either works or tells you what is missing. The **Project** selector in the
shared header switches among registered projects. From there you can create a draft or
ready task, inspect hierarchy and dependencies, and record review approval or requested
changes. FastAPI's interactive API reference is available separately at
`http://localhost:8765/docs`.

## 3. Create and find work

Create work in the React UI, or use the CLI:

```bash
poetry -P /path/to/agentjobs run agentjobs create --ready \
  --title "Ship REST layer" --category engineering --priority high
poetry -P /path/to/agentjobs run agentjobs list --lifecycle ready
poetry -P /path/to/agentjobs run agentjobs work --agent codex
```

`--ready` matters. Without it a task is born `draft`, and a draft is deliberately
not claimable -- so `list --lifecycle ready` prints nothing and `work` reports "No
tasks available". Drafting is the right default for a task whose spec is still being
written; `agentjobs promote <id>` is the same step taken later. `work` reads the
project's configured `tasks_directory`, so it sees the same records the UI does.

## 4. Use the schema-v2 Python client

```python
from agentjobs import Ball, BallReason, TaskClient

with TaskClient() as client:
    task = client.get_next_task(agent="codex")
    if task is not None:
        task = client.claim_task(task.id, agent="codex")
        client.add_progress_update(
            task.id,
            agent="codex",
            summary="Initial implementation is ready",
            details="The complete project check passed.",
        )
        client.handoff_task(
            task.id,
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Review the change; approve it or request specific revisions.",
        )
```

The task record—not chat—is the durable working memory. See the
[agent workflow guide](agent-workflow.md) for claiming, releasing, closing, logging,
and resuming tasks.

## 5. Stop the server

```bash
poetry -P /path/to/agentjobs run agentjobs stop
```

If you ran `agentjobs serve` in the foreground, press `Ctrl+C` instead.
