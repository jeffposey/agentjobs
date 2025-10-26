# AgentJobs Quick Start

This guide covers the minimum steps to spin up AgentJobs, populate tasks, and interact via
the REST API and Python client library.

## 1. Install & Initialise

```bash
pip install agentjobs
cd /path/to/project
agentjobs init
```

Follow the prompts to create `.agentjobs/config.yaml` and the initial `tasks/` directory.

## 2. Launch the REST API

```bash
agentjobs serve --reload
```

Open `http://localhost:8765/docs` to explore the interactive Swagger UI. The API is served
from `http://localhost:8765`.

## 3. Create Tasks via CLI or API

```bash
agentjobs create --title "Ship REST layer" --category engineering --priority high
```

Equivalent `curl` request:

```bash
curl -X POST http://localhost:8765/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
        "title": "Ship REST layer",
        "description": "Implement FastAPI routes",
        "priority": "high",
        "category": "engineering"
      }'
```

## 4. Use the Python Client

```python
from agentjobs import TaskClient

client = TaskClient()

# Fetch next available work item
next_task = client.get_next_task()
if next_task:
    client.mark_in_progress(next_task.id, agent="codex")
    prompt = client.get_starter_prompt(next_task.id)
    print("Starter prompt:\n", prompt)

    client.add_progress_update(
        next_task.id,
        summary="Initial endpoints ready",
        details="Tasks list + detail routes implemented",
        agent="codex",
    )
    client.mark_deliverable_complete(
        next_task.id,
        deliverable_path="docs/api-reference.md",
    )
    client.mark_completed(next_task.id, summary="Phase 2 delivered")
```

Set a custom base URL if the agent runs remotely:

```python
client = TaskClient(base_url="http://agentjobs.internal:8765", timeout=10)
```

## 5. Search & Prompts

```bash
curl "http://localhost:8765/api/search?q=docs"
```

```python
client.add_followup_prompt(
    next_task.id,
    content="Need regression tests for client library",
    author="lead-engineer",
    context="Planning review",
)
```

## 6. Shut Down

Press `Ctrl+C` in the terminal running `agentjobs serve`. The server exits cleanly.
