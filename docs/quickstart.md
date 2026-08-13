# AgentJobs quick start

This guide initializes a project, opens the packaged React application, and runs the
canonical schema-v2 handoff loop.

## 1. Install and initialize

Until AgentJobs is published, run it from a clone:

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install

cd /path/to/your-project
poetry -P /path/to/agentjobs run agentjobs init
```

Initialization creates `.agentjobs/config.yaml`, the configured task directory, and a
project registration for the local server.

## 2. Open the React application

```bash
poetry -P /path/to/agentjobs run agentjobs open
```

The primary UI opens at `http://localhost:8765/app/`. The **Project** selector in the
shared header switches among registered projects. From there you can create a draft or
ready task, inspect hierarchy and dependencies, and record review approval or requested
changes. FastAPI's interactive API reference is available separately at
`http://localhost:8765/docs`.

## 3. Create and find work

Create work in the React UI, or use the CLI:

```bash
poetry -P /path/to/agentjobs run agentjobs create \
  --title "Ship REST layer" --category engineering --priority high
poetry -P /path/to/agentjobs run agentjobs list --lifecycle ready
poetry -P /path/to/agentjobs run agentjobs work --agent codex
```

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
