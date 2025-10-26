# AgentJobs

Lightweight task management system designed for AI agent workflows.

## Features

- **Structured Task Data**: YAML-based task definitions with rich metadata
- **Browser-Based GUI**: View and manage tasks via web interface
- **REST API**: Programmatic access for agents
- **Python Client Library**: Simple API for agent integration
- **Git-Friendly**: All tasks stored as version-controlled YAML files
- **Migration Tools**: Convert existing Markdown tasks to structured format

## Installation

```bash
pip install agentjobs
```

## Quick Start

```bash
# Initialize in your project
cd /path/to/your-project
agentjobs init

# Start the web server
agentjobs serve

# Open browser to http://localhost:8765
```

## Quick Start for AI Agents

AgentJobs provides a Python client for programmatic task management.

### Basic Workflow

```python
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
```

### Interactive CLI

```bash
# Option 1: Provide agent name
agentjobs work --agent=my-agent

# Option 2: Will prompt for agent name
agentjobs work
```

### Full Documentation

- [Agent Workflow Guide](docs/agent-workflow.md)
- [Examples](examples/)

## Agent Integration

```python
from agentjobs import TaskClient

client = TaskClient()

# Get highest priority task
task = client.get_next_task()

# Mark in progress
client.mark_in_progress(task.id, agent="my-agent")

# Get starter prompt
prompt = client.get_starter_prompt(task.id)

# Add progress update
client.add_progress_update(
    task.id,
    summary="Completed Phase 1",
    details="All tests passing"
)
```

## Documentation

- [Installation Guide](docs/installation.md)
- [Quick Start](docs/quickstart.md)
- [API Reference](docs/api-reference.md)
- [Agent Workflow Guide](docs/agent-workflow.md)
- [Task Schema](docs/task-schema.md)
- [Migration Guide](docs/migration-guide.md)

## Development

AgentJobs uses itself to manage its own development tasks.

### View Development Tasks

```bash
agentjobs serve
# Open http://localhost:8765 to browse tasks
```

### Contributing

1. Check existing tasks: `agentjobs list`
2. Inspect a candidate task: `agentjobs show task-XXX`
3. Claim work: `agentjobs work --agent your-name`
4. Follow linked prompts and update status via CLI or API
5. Submit a PR referencing the task when ready

### Task Structure

- `tasks/` - AgentJobs development tasks in YAML
- `prompts/` - Phase-specific execution instructions
- `examples/` - Agent integration samples
- `docs/` - Product and API documentation

### Local Setup

```bash
# Clone repository
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs

# Install dependencies
poetry install

# Run tests
poetry run pytest

# Start development server
poetry run agentjobs serve
```


## License

MIT License - see [LICENSE](LICENSE) file for details.
