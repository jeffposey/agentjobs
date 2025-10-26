# Agent Workflow Guide

Complete guide for integrating AI agents with AgentJobs.

## Quick Start

```python
from agentjobs import TaskClient

client = TaskClient()  # defaults to http://localhost:8765

# Get next task
task = client.get_next_task()

# Take ownership
client.mark_in_progress(task.id, agent="my-agent")

# Get instructions
prompt = client.get_starter_prompt(task.id)

# Work on task...
# (your implementation)

# Complete
client.mark_completed(task.id, agent="my-agent")
```

## Installation

```bash
pip install agentjobs
```

## Task Client API

### Initialization

```python
from agentjobs import TaskClient

# Default (localhost:8765)
client = TaskClient()

# Custom URL
client = TaskClient(base_url="http://agentjobs.example.com:8765")

# With timeout
client = TaskClient(timeout=60.0)

# Context manager (auto-closes)
with TaskClient() as client:
    tasks = client.list_tasks()
```

### Querying Tasks

```python
from agentjobs import Priority, TaskStatus

# Get next task (highest priority, planned status)
task = client.get_next_task()

# Filter by priority
task = client.get_next_task(priority=Priority.HIGH)
task = client.get_next_task(priority="critical")

# List all tasks
tasks = client.list_tasks()

# Filter by status
in_progress = client.list_tasks(status=TaskStatus.IN_PROGRESS)
blocked = client.list_tasks(status="blocked")

# Filter by priority
high_priority = client.list_tasks(priority=Priority.HIGH)

# Get specific task
task = client.get_task("task-001")

# Search
results = client.search_tasks("database optimization")
```

### Task Ownership

```python
# Take ownership (mark in progress)
client.mark_in_progress(
    task.id,
    agent="my-agent",
    summary="Starting Phase 1"  # optional
)

# Mark complete
client.mark_completed(
    task.id,
    agent="my-agent",
    summary="All deliverables finished"  # optional
)

# Mark blocked
client.mark_blocked(
    task.id,
    reason="Waiting for API access",
    agent="my-agent"
)
```

### Progress Updates

```python
# Add update
client.add_progress_update(
    task.id,
    summary="Phase 1 complete",
    details="Created files: src/module.py, tests/test_module.py. All tests passing.",
    agent="my-agent"
)
```

### Prompts

```python
# Get starter prompt (your working instructions)
prompt = client.get_starter_prompt(task.id)

# Add followup prompt (for multi-phase work)
client.add_followup_prompt(
    task.id,
    content="Phase 2: Optimize for performance",
    author="human-reviewer",
    context="Phase 1 approved"
)
```

### Deliverables

```python
# Mark deliverable complete
client.mark_deliverable_complete(
    task.id,
    deliverable_path="src/feature.py"
)
```

### Creating Tasks

```python
from agentjobs import Priority

task = client.create_task(
    title="Implement caching layer",
    description="Add Redis caching to reduce database load...",
    priority=Priority.HIGH,
    category="performance",
    assigned_to="my-agent",
    estimated_effort="2 days",
    human_summary="Add Redis caching to reduce database queries by 80%"
)
```

## Task Object

```python
task = client.get_task("task-001")

# Metadata
task.id              # "task-001"
task.title           # "Implement Feature X"
task.status          # TaskStatus.IN_PROGRESS
task.priority        # Priority.HIGH
task.category        # "infrastructure"
task.assigned_to     # "codex"
task.estimated_effort # "1 week"

# Content
task.human_summary   # Concise 1-2 sentence summary
task.description     # Full markdown implementation details

# Structure
task.phases          # List[Phase] - progress tracking
task.deliverables    # List[Deliverable] - files to create
task.dependencies    # List[Dependency] - task relationships
task.external_links  # List[ExternalLink] - docs, PRs

# History
task.status_updates  # List[StatusUpdate] - timeline
task.prompts         # Prompts object (starter + followups)

# Timestamps
task.created         # datetime
task.updated         # datetime
```

## Common Patterns

### Continuous Processing

```python
import time

while True:
    task = client.get_next_task()
    if not task:
        time.sleep(30)  # wait for new tasks
        continue

    # Process task
    client.mark_in_progress(task.id, agent="agent")
    # ... do work ...
    client.mark_completed(task.id, agent="agent")
```

### Error Handling

```python
from agentjobs import TaskClient, TaskClientError

try:
    task = client.get_task("task-999")
except TaskClientError as e:
    print(f"Error: {e}")
    # Handle 404, connection errors, etc.
```

### Multi-Phase Tasks

```python
task = client.get_task("task-001")

for phase in task.phases:
    if phase.status == "planned":
        print(f"Working on: {phase.title}")
        # ... do phase work ...

        client.add_progress_update(
            task.id,
            summary=f"Completed {phase.title}",
            agent="agent"
        )
        break
```

## Examples

See [examples/](../examples/) directory for complete working code:
- [basic_workflow.py](../examples/basic_workflow.py) - Simple workflow
- [continuous_agent.py](../examples/continuous_agent.py) - Continuous processing
- [custom_filters.py](../examples/custom_filters.py) - Query with filters

## CLI Alternative

Interactive workflow:

```bash
agentjobs work --agent my-agent --priority high
```

This will:
1. Find next high-priority task
2. Display prompt
3. Prompt you to start
4. Mark in progress
5. Wait for you to complete work
6. Mark completed

## Best Practices

1. **Always identify yourself**: Use meaningful `agent` parameter
2. **Add progress updates**: Keep humans informed
3. **Check task.phases**: Multi-phase tasks need incremental updates
4. **Handle no tasks gracefully**: Poll or wait when queue is empty
5. **Use context managers**: `with TaskClient() as client:` auto-closes
6. **Catch errors**: Network issues happen, handle `TaskClientError`

## Human vs Agent Views

Tasks have two content fields:

- **human_summary**: Concise 1-2 sentences for human reviewers
- **description**: Full markdown implementation details for agents

**As an agent**: Use `get_starter_prompt()` for your instructions, not `task.description` directly.

## Task States

- **planned**: Ready to work on
- **in_progress**: Agent actively working
- **waiting_for_human**: Needs human review/approval
- **blocked**: Cannot proceed (external dependency)
- **under_review**: Code review in progress
- **completed**: Work finished
- **archived**: No longer relevant

## Server Setup

Agents need AgentJobs server running:

```bash
# Start server
agentjobs serve

# Server runs on http://localhost:8765
```

Or use custom server URL in client:
```python
client = TaskClient(base_url="http://agentjobs.company.com:8765")
```
