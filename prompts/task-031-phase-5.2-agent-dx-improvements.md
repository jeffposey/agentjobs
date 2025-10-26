# Task 031 - Phase 5.2: Agent Developer Experience Improvements

---
## ⚠️ CRITICAL: WORKING DIRECTORY ⚠️

**YOU MUST WORK IN THE AGENTJOBS REPOSITORY:**
```bash
cd /mnt/c/projects/agentjobs
```

**DO NOT work in privateproject.**

---

## Context

Phase 5.1 complete: AgentJobs integrated into privateproject, 34 tasks migrated, all systems working.

**Problem:** Agent developers don't have clear examples or convenience tooling.

**Current state:**
- ✅ `TaskClient` Python API works great
- ✅ All CRUD operations available
- ❌ No examples in repo showing agent workflows
- ❌ No "quick start for agents" documentation
- ❌ No CLI convenience command for interactive workflows
- ❌ Documentation scattered across phase prompts

**Impact:**
- New agents must read code to understand workflow
- No copy-paste templates
- Friction for adoption

---

## Objectives

1. Add `examples/` directory with agent workflow scripts
2. Create `docs/agent-workflow.md` with comprehensive guide
3. Add CLI command: `agentjobs work` for interactive agent workflow
4. Update README with "Quick Start for Agents" section
5. Add "Getting Started" to web GUI homepage
6. Test all examples

---

## Implementation

### 1. Create Examples Directory

Create `examples/` directory with working agent scripts:

**examples/README.md:**
```markdown
# AgentJobs Examples

Code examples for integrating AI agents with AgentJobs.

## For AI Agents

- [basic_workflow.py](basic_workflow.py) - Simple agent that processes one task
- [continuous_agent.py](continuous_agent.py) - Agent that continuously processes tasks
- [collaborative_agent.py](collaborative_agent.py) - Multi-agent coordination
- [custom_filters.py](custom_filters.py) - Query tasks with filters

## For Humans

- [create_tasks.py](create_tasks.py) - Programmatically create tasks
- [bulk_import.py](bulk_import.py) - Import tasks from external systems
- [reporting.py](reporting.py) - Generate status reports

## Running Examples

```bash
# Start AgentJobs server first
agentjobs serve

# In another terminal, run examples
cd examples/
python basic_workflow.py
```
```

**examples/basic_workflow.py:**
```python
#!/usr/bin/env python
"""Basic agent workflow: Get task, work on it, complete it."""

from agentjobs import TaskClient

# Configuration
AGENT_NAME = "example-agent"


def main():
    # Initialize client
    client = TaskClient(base_url="http://localhost:8765")

    # Get next high-priority task
    print(f"[{AGENT_NAME}] Looking for next task...")
    task = client.get_next_task(priority="high")

    if not task:
        print(f"[{AGENT_NAME}] No tasks available")
        return

    print(f"[{AGENT_NAME}] Found task: {task.title}")
    print(f"[{AGENT_NAME}] Priority: {task.priority}, Status: {task.status}")

    # Take ownership
    print(f"[{AGENT_NAME}] Taking ownership...")
    client.mark_in_progress(task.id, agent=AGENT_NAME)

    # Get instructions
    prompt = client.get_starter_prompt(task.id)
    print(f"\n{'='*60}")
    print(f"TASK: {task.title}")
    print(f"{'='*60}")
    print(f"\n{prompt}\n")
    print(f"{'='*60}\n")

    # Work on task (placeholder - implement your logic here)
    print(f"[{AGENT_NAME}] Working on task...")
    # TODO: Your implementation here

    # Add progress update
    client.add_progress_update(
        task.id,
        summary="Task completed",
        details="All deliverables finished",
        agent=AGENT_NAME
    )

    # Mark complete
    print(f"[{AGENT_NAME}] Marking task complete...")
    client.mark_completed(task.id, agent=AGENT_NAME)

    print(f"[{AGENT_NAME}] ✅ Task {task.id} complete!")


if __name__ == "__main__":
    main()
```

**examples/continuous_agent.py:**
```python
#!/usr/bin/env python
"""Continuous agent: Keep processing tasks until none available."""

import time
from agentjobs import TaskClient, TaskStatus

AGENT_NAME = "continuous-agent"
POLL_INTERVAL = 30  # seconds


def process_task(client: TaskClient, task):
    """Process a single task."""
    print(f"\n[{AGENT_NAME}] Working on: {task.title}")

    # Take ownership
    client.mark_in_progress(task.id, agent=AGENT_NAME)

    # Get prompt
    prompt = client.get_starter_prompt(task.id)
    print(f"Instructions: {prompt[:100]}...")

    # TODO: Your implementation here
    time.sleep(2)  # Simulate work

    # Complete
    client.add_progress_update(
        task.id,
        summary="Completed successfully",
        agent=AGENT_NAME
    )
    client.mark_completed(task.id, agent=AGENT_NAME)
    print(f"[{AGENT_NAME}] ✅ Completed: {task.title}")


def main():
    client = TaskClient()
    tasks_completed = 0

    print(f"[{AGENT_NAME}] Starting continuous task processing...")
    print(f"[{AGENT_NAME}] Poll interval: {POLL_INTERVAL}s")

    try:
        while True:
            # Get next task
            task = client.get_next_task()

            if task:
                process_task(client, task)
                tasks_completed += 1
            else:
                print(f"[{AGENT_NAME}] No tasks available, waiting...")
                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n[{AGENT_NAME}] Shutting down...")
        print(f"[{AGENT_NAME}] Total tasks completed: {tasks_completed}")


if __name__ == "__main__":
    main()
```

**examples/custom_filters.py:**
```python
#!/usr/bin/env python
"""Query tasks with custom filters."""

from agentjobs import TaskClient, Priority, TaskStatus

client = TaskClient()

# Get all high-priority tasks
high_priority = client.list_tasks(priority=Priority.HIGH)
print(f"High priority tasks: {len(high_priority)}")

# Get all in-progress tasks
in_progress = client.list_tasks(status=TaskStatus.IN_PROGRESS)
print(f"In progress: {len(in_progress)}")

# Get blocked tasks
blocked = client.list_tasks(status=TaskStatus.BLOCKED)
print(f"Blocked: {len(blocked)}")
for task in blocked:
    print(f"  - {task.title}")

# Search by keyword
results = client.search_tasks("database")
print(f"\nSearch 'database': {len(results)} results")
for task in results:
    print(f"  - {task.title}")
```

---

### 2. Add CLI Command: agentjobs work

Add interactive workflow command to `src/agentjobs/cli.py`:

```python
@app.command()
def work(
    agent: str = typer.Option(..., prompt="Your agent name", help="Agent identifier"),
    priority: Optional[str] = typer.Option(None, help="Filter by priority (high, medium, low)"),
    storage_dir: str = typer.Option("./tasks", help="Directory for task storage"),
) -> None:
    """Interactive agent workflow: Get task, display prompt, mark complete."""
    from .manager import TaskManager
    from .storage import TaskStorage
    from .models import Priority as PriorityEnum

    base_dir = Path.cwd()
    target_dir = Path(storage_dir)
    if not target_dir.is_absolute():
        target_dir = base_dir / target_dir

    storage = TaskStorage(target_dir)
    manager = TaskManager(storage)

    # Get next task
    priority_enum = None
    if priority:
        try:
            priority_enum = PriorityEnum(priority.lower())
        except ValueError:
            typer.echo(f"Invalid priority: {priority}", err=True)
            raise typer.Exit(1)

    task = manager.get_next_task(priority=priority_enum)

    if not task:
        typer.echo("No tasks available")
        raise typer.Exit(0)

    # Display task
    typer.echo(f"\n{'='*60}")
    typer.echo(f"TASK: {task.title}")
    typer.echo(f"ID: {task.id}")
    typer.echo(f"Priority: {task.priority.value}")
    typer.echo(f"Category: {task.category}")
    typer.echo(f"{'='*60}\n")

    # Display prompt
    if task.prompts and task.prompts.starter:
        typer.echo(task.prompts.starter)
        typer.echo(f"\n{'='*60}\n")

    # Confirm start
    if not typer.confirm(f"Start working on this task as '{agent}'?"):
        typer.echo("Cancelled")
        raise typer.Exit(0)

    # Mark in progress
    manager.update_status(
        task.id,
        TaskStatus.IN_PROGRESS,
        author=agent,
        summary=f"Started by {agent}"
    )
    typer.echo(f"✓ Task marked IN_PROGRESS")

    # Wait for completion
    typer.echo(f"\n💼 Work on the task, then return here when done...\n")

    if not typer.confirm("Mark task as COMPLETED?", default=True):
        typer.echo("Task still IN_PROGRESS. Use CLI to update later.")
        raise typer.Exit(0)

    # Mark complete
    summary = typer.prompt("Summary of work done", default="Task completed")
    manager.update_status(
        task.id,
        TaskStatus.COMPLETED,
        author=agent,
        summary=summary
    )
    typer.echo(f"\n✅ Task {task.id} marked COMPLETED!")
```

---

### 3. Create Agent Workflow Documentation

Create `docs/agent-workflow.md`:

```markdown
# Agent Workflow Guide

Complete guide for integrating AI agents with AgentJobs.

## Quick Start

\`\`\`python
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
\`\`\`

## Installation

\`\`\`bash
pip install agentjobs
\`\`\`

## Task Client API

### Initialization

\`\`\`python
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
\`\`\`

### Querying Tasks

\`\`\`python
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
\`\`\`

### Task Ownership

\`\`\`python
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
\`\`\`

### Progress Updates

\`\`\`python
# Add update
client.add_progress_update(
    task.id,
    summary="Phase 1 complete",
    details="Created files: src/module.py, tests/test_module.py. All tests passing.",
    agent="my-agent"
)
\`\`\`

### Prompts

\`\`\`python
# Get starter prompt (your working instructions)
prompt = client.get_starter_prompt(task.id)

# Add followup prompt (for multi-phase work)
client.add_followup_prompt(
    task.id,
    content="Phase 2: Optimize for performance",
    author="human-reviewer",
    context="Phase 1 approved"
)
\`\`\`

### Deliverables

\`\`\`python
# Mark deliverable complete
client.mark_deliverable_complete(
    task.id,
    deliverable_path="src/feature.py"
)
\`\`\`

### Creating Tasks

\`\`\`python
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
\`\`\`

## Task Object

\`\`\`python
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
\`\`\`

## Common Patterns

### Continuous Processing

\`\`\`python
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
\`\`\`

### Error Handling

\`\`\`python
from agentjobs import TaskClient, TaskClientError

try:
    task = client.get_task("task-999")
except TaskClientError as e:
    print(f"Error: {e}")
    # Handle 404, connection errors, etc.
\`\`\`

### Multi-Phase Tasks

\`\`\`python
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
\`\`\`

## Examples

See [examples/](../examples/) directory for complete working code:
- [basic_workflow.py](../examples/basic_workflow.py) - Simple workflow
- [continuous_agent.py](../examples/continuous_agent.py) - Continuous processing
- [custom_filters.py](../examples/custom_filters.py) - Query with filters

## CLI Alternative

Interactive workflow:

\`\`\`bash
agentjobs work --agent my-agent --priority high
\`\`\`

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

\`\`\`bash
# Start server
agentjobs serve

# Server runs on http://localhost:8765
\`\`\`

Or use custom server URL in client:
\`\`\`python
client = TaskClient(base_url="http://agentjobs.company.com:8765")
\`\`\`
```

---

### 4. Update README.md

Add "Quick Start for Agents" section to `README.md` after "Quick Start":

```markdown
## Quick Start for AI Agents

AgentJobs provides a Python client for programmatic task management.

### Basic Workflow

\`\`\`python
from agentjobs import TaskClient

client = TaskClient()  # connects to http://localhost:8765

# Get next task
task = client.get_next_task()

# Take ownership
client.mark_in_progress(task.id, agent="my-agent")

# Get instructions
prompt = client.get_starter_prompt(task.id)

# Work on task...
# (your implementation here)

# Complete
client.mark_completed(task.id, agent="my-agent")
\`\`\`

### Interactive CLI

\`\`\`bash
agentjobs work --agent my-agent
\`\`\`

### Full Documentation

- [Agent Workflow Guide](docs/agent-workflow.md)
- [Examples](examples/)
```

---

### 5. Add Getting Started to Web GUI

Update `src/agentjobs/api/templates/dashboard.html` to add a "Getting Started" card after stats:

```html
<!-- After stats cards, before Active Tasks -->
{% if stats.total == 0 %}
<div class="bg-gradient-to-r from-blue-900/20 to-purple-900/20 border-2 border-blue-500/30 rounded-lg p-6">
    <div class="flex items-start gap-4">
        <div class="text-4xl">🚀</div>
        <div class="flex-1">
            <h2 class="text-xl font-bold text-blue-300 mb-2">Getting Started with AgentJobs</h2>
            <p class="text-dark-muted mb-4">No tasks yet. Here's how to get started:</p>

            <div class="space-y-3 text-sm">
                <div>
                    <strong class="text-dark-text">For Humans:</strong>
                    <pre class="bg-dark-bg rounded p-2 mt-1 text-xs">agentjobs create --title "My First Task" --priority high</pre>
                </div>

                <div>
                    <strong class="text-dark-text">For AI Agents:</strong>
                    <pre class="bg-dark-bg rounded p-2 mt-1 text-xs">from agentjobs import TaskClient
client = TaskClient()
task = client.get_next_task()
client.mark_in_progress(task.id, agent="agent-name")</pre>
                </div>

                <div class="mt-4">
                    <a href="/docs" class="text-blue-400 hover:text-blue-300 underline">View Full Documentation →</a>
                </div>
            </div>
        </div>
    </div>
</div>
{% endif %}
```

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Test examples
.venv/bin/agentjobs serve &
sleep 2

# Load test data
.venv/bin/agentjobs load-test-data

# Run basic workflow
.venv/bin/python examples/basic_workflow.py
# Should complete a task

# Run custom filters
.venv/bin/python examples/custom_filters.py
# Should show task counts

# Test CLI workflow
.venv/bin/agentjobs work --agent test-agent --priority high
# Should show interactive prompts

# Kill server
pkill -f "agentjobs serve"

# Run tests
PYTHONPATH=src .venv/bin/pytest tests/ -v
# Should pass
```

---

## Acceptance Criteria

- [ ] examples/ directory created with 3+ working scripts
- [ ] examples/README.md documents all examples
- [ ] docs/agent-workflow.md comprehensive guide written
- [ ] README.md has "Quick Start for Agents" section
- [ ] CLI command `agentjobs work` implemented and working
- [ ] Web GUI shows "Getting Started" when no tasks
- [ ] All examples run successfully
- [ ] Documentation is accurate and complete
- [ ] All tests pass

---

## Commit

```bash
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(dx): add agent workflow examples and documentation

Make AgentJobs easy for AI agents to integrate with.

Changes:
- Add examples/ directory with working agent scripts
  - basic_workflow.py: Simple get/complete workflow
  - continuous_agent.py: Continuous task processing
  - custom_filters.py: Query with filters
- Create docs/agent-workflow.md comprehensive guide
- Add README.md 'Quick Start for Agents' section
- Add CLI command: agentjobs work (interactive workflow)
- Add 'Getting Started' card to web GUI dashboard
- All examples tested and working

Before: Developers must read code to understand workflow
After: Copy-paste examples and clear documentation

Improves developer experience and agent adoption"

git push origin main
```

---

## Notes

**Why this matters:**
- Lowers barrier to entry for new agents
- Reduces integration time from hours to minutes
- Copy-paste examples are most effective documentation
- Interactive CLI helps humans test agent workflows

**Future enhancements:**
- Jupyter notebook tutorial
- Video walkthrough
- Agent templates for common patterns
- Integration examples with LangChain, AutoGPT, etc.

Ready to make AgentJobs agent-friendly! 🤖
