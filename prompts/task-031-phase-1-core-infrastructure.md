# Task 031 - Phase 1: Core Infrastructure Implementation

**Context**: You are implementing Phase 1 of Tasks 2.0 (AgentJobs), a standalone task management system for AI agent workflows. See `ops/tasks/task-031-tasks-2.0-system.md` for full architectural details.

**Objective**: Create the foundational repository, data models, storage layer, and CLI framework for AgentJobs.

---

## Repository Setup

### 1. Create New GitHub Repository

**IMPORTANT**: Work with human to create GitHub repository under the `jeffposey` account.

**Repository Details**:
- **Name**: `agentjobs`
- **Owner**: `jeffposey` (same account as privateproject)
- **URL**: `https://github.com/jeffposey/agentjobs`
- **Visibility**: Public (eventually)
- **Note**: Can be transferred to `hellfiregamesinc` organization later if desired

**Steps**:
1. Human will create the GitHub repository at `https://github.com/jeffposey/agentjobs`
2. Codex will initialize local repository and push

**Initial Setup**:
```bash
# Create directory structure
mkdir -p ~/projects/agentjobs
cd ~/projects/agentjobs

git init
git branch -M main

# Create initial structure
mkdir -p src/agentjobs/api/{routes,templates,static/{css,js}}
mkdir -p src/agentjobs/utils
mkdir -p tests/fixtures/sample-tasks
mkdir -p examples
mkdir -p docs

# Add GitHub remote (after human creates repo)
git remote add origin https://github.com/jeffposey/agentjobs.git
```

### 1.5. Reserve PyPI Package Name (Optional but Recommended)

**Package Name**: `agentjobs`

To prevent someone else from taking the name, consider reserving it early on PyPI:

```bash
# Option 1: Create minimal placeholder package
# - Create basic setup.py with version 0.0.1
# - Publish to PyPI with "coming soon" description
# - This reserves the name immediately

# Option 2: Wait until Phase 1 complete
# - Publish functional package after testing
# - Riskier (someone could take the name)
```

**Recommendation**: Human should decide whether to reserve now or wait for Phase 1 completion.

### 2. Create Core Configuration Files

**pyproject.toml** (Poetry):
```toml
[tool.poetry]
name = "agentjobs"
version = "0.1.0"
description = "Lightweight task management system for AI agent workflows"
authors = ["Jeff Posey <jposey@hellfiregames.com>"]
readme = "README.md"
license = "MIT"
homepage = "https://github.com/jeffposey/agentjobs"
repository = "https://github.com/jeffposey/agentjobs"
keywords = ["ai", "agents", "task-management", "workflow", "automation"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

[tool.poetry.dependencies]
python = "^3.11"
pydantic = "^2.5"
pyyaml = "^6.0"
fastapi = "^0.109"
uvicorn = {extras = ["standard"], version = "^0.27"}
jinja2 = "^3.1"
typer = "^0.9"
watchdog = "^3.0"
httpx = "^0.26"  # For TaskClient
python-multipart = "^0.0.6"  # For FastAPI file uploads

[tool.poetry.group.dev.dependencies]
pytest = "^7.4"
pytest-cov = "^4.1"
pytest-asyncio = "^0.23"
black = "^23.12"
ruff = "^0.1"
mypy = "^1.8"

[tool.poetry.scripts]
agentjobs = "agentjobs.cli:app"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
python_classes = "Test*"
python_functions = "test_*"
addopts = "--cov=src/agentjobs --cov-report=term-missing --cov-report=html"

[tool.black]
line-length = 100
target-version = ["py311"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

**README.md**:
```markdown
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
- [Task Schema](docs/task-schema.md)
- [Migration Guide](docs/migration-guide.md)

## Development

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
```

**LICENSE** (MIT):
```
MIT License

Copyright (c) 2025 [Your Name]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**.gitignore**:
```
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
venv/
ENV/
env/
.venv/

# Testing
.pytest_cache/
.coverage
htmlcov/
.tox/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# AgentJobs runtime
.agentjobs/agentjobs.db
```

---

## Implementation: Data Models

**File**: `src/agentjobs/models.py`

Implement all Pydantic models from the architecture doc:
- `TaskStatus` (enum)
- `Priority` (enum)
- `Phase`
- `SuccessCriterion`
- `Prompt`
- `StatusUpdate`
- `Deliverable`
- `Dependency`
- `ExternalLink`
- `Issue`
- `Branch`
- `Task` (main model)

**Requirements**:
- Use Pydantic v2 syntax
- Include docstrings for all models
- Add `model_config` for JSON serialization
- Use `datetime` for timestamps
- Add helper methods (e.g., `is_completed()`, `is_blocked()`)

**Example Structure**:
```python
from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    """Task workflow status."""
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"
    ARCHIVED = "archived"

class Priority(str, Enum):
    """Task priority levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class Task(BaseModel):
    """Main task model representing a unit of work."""

    # Core metadata
    id: str = Field(..., description="Unique task identifier (e.g., task-001)")
    title: str = Field(..., description="Human-readable task title")
    created: datetime = Field(..., description="Task creation timestamp")
    updated: datetime = Field(..., description="Last update timestamp")

    # ... (rest of fields from architecture doc)

    def is_completed(self) -> bool:
        """Check if task is completed."""
        return self.status == TaskStatus.COMPLETED

    def is_blocked(self) -> bool:
        """Check if task is blocked."""
        return self.status == TaskStatus.BLOCKED
```

**Testing**: Create `tests/test_models.py` with comprehensive tests for all models.

---

## Implementation: Storage Layer

**File**: `src/agentjobs/storage.py`

Implement YAML-based storage with these functions:

```python
from pathlib import Path
from typing import List, Optional
import yaml
from .models import Task

class TaskStorage:
    """YAML-based task storage."""

    def __init__(self, tasks_dir: Path):
        """Initialize storage with tasks directory."""
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def load_task(self, task_id: str) -> Optional[Task]:
        """Load task from YAML file."""
        # Implementation
        pass

    def save_task(self, task: Task) -> None:
        """Save task to YAML file."""
        # Implementation
        pass

    def list_tasks(self) -> List[Task]:
        """List all tasks."""
        # Implementation
        pass

    def delete_task(self, task_id: str) -> bool:
        """Delete task (archive)."""
        # Implementation
        pass

    def search_tasks(self, query: str) -> List[Task]:
        """Full-text search across tasks."""
        # Implementation (simple: search title, description, tags)
        pass
```

**Requirements**:
- Use PyYAML for YAML parsing
- Handle malformed YAML gracefully (log error, skip file)
- Auto-update `updated` timestamp on save
- Create task file with pattern: `{task_id}.yaml`
- Validate task data with Pydantic on load

**Testing**: Create `tests/test_storage.py` with tests for all storage operations.

---

## Implementation: Task Manager

**File**: `src/agentjobs/manager.py`

Implement core business logic:

```python
from typing import List, Optional
from .models import Task, TaskStatus, Priority, StatusUpdate
from .storage import TaskStorage
from datetime import datetime

class TaskManager:
    """Core task management logic."""

    def __init__(self, storage: TaskStorage):
        self.storage = storage

    def create_task(
        self,
        id: str,
        title: str,
        description: str,
        priority: Priority = Priority.MEDIUM,
        category: str = "general",
        **kwargs
    ) -> Task:
        """Create new task."""
        # Implementation
        pass

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        return self.storage.load_task(task_id)

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        author: str,
        summary: str,
        details: Optional[str] = None
    ) -> Task:
        """Update task status with status update entry."""
        # Implementation
        pass

    def get_next_task(self, priority: Optional[Priority] = None) -> Optional[Task]:
        """Get highest priority available task."""
        # Filter: status == PLANNED
        # Sort by priority (CRITICAL > HIGH > MEDIUM > LOW)
        # Return first match
        pass

    def add_progress_update(
        self,
        task_id: str,
        author: str,
        summary: str,
        details: Optional[str] = None
    ) -> Task:
        """Add progress update to task."""
        # Implementation
        pass

    def mark_deliverable_complete(
        self,
        task_id: str,
        deliverable_path: str
    ) -> Task:
        """Mark deliverable as completed."""
        # Implementation
        pass
```

**Requirements**:
- Update `task.updated` timestamp on all modifications
- Validate task exists before updates (raise exception if not found)
- Add status update entry when status changes
- Save task to storage after every modification

**Testing**: Create `tests/test_manager.py` with tests for all manager operations.

---

## Implementation: CLI Application

**File**: `src/agentjobs/cli.py`

Implement Typer-based CLI:

```python
import typer
from pathlib import Path
from typing import Optional
from .manager import TaskManager
from .storage import TaskStorage
from .models import Priority

app = typer.Typer(
    name="agentjobs",
    help="Lightweight task management for AI agent workflows"
)

@app.command()
def init(
    project_name: str = typer.Option(..., prompt=True),
    tasks_dir: str = typer.Option("tasks", prompt=True),
    prompts_dir: str = typer.Option("prompts", prompt=True),
    port: int = typer.Option(8765, prompt=True),
):
    """Initialize AgentJobs in current directory."""
    # Create directories
    # Create .agentjobs/config.yaml
    # Update .gitignore
    typer.echo("✅ AgentJobs initialized successfully!")

@app.command()
def serve(
    host: str = typer.Option("localhost"),
    port: int = typer.Option(8765),
    reload: bool = typer.Option(False),
):
    """Start web server."""
    # Import and run FastAPI app
    import uvicorn
    from .api.main import app as fastapi_app

    typer.echo(f"🚀 Starting AgentJobs server at http://{host}:{port}")
    uvicorn.run(
        "agentjobs.api.main:app",
        host=host,
        port=port,
        reload=reload
    )

@app.command()
def create(
    title: str = typer.Option(..., prompt=True),
    id: Optional[str] = None,
    description: str = typer.Option("", prompt=True),
    priority: Priority = typer.Option(Priority.MEDIUM),
    category: str = typer.Option("general"),
):
    """Create new task."""
    # Generate ID if not provided (e.g., task-001, task-002, ...)
    # Create task via TaskManager
    # Save to YAML
    typer.echo(f"✅ Created {id}.yaml")

@app.command()
def list(
    status: Optional[str] = None,
    priority: Optional[Priority] = None,
):
    """List tasks."""
    # Load tasks, filter, display table
    pass

@app.command()
def show(task_id: str):
    """Show task details."""
    # Load task, display formatted output
    pass

@app.command()
def migrate(
    from_dir: Path = typer.Option(..., help="Source Markdown directory"),
    to_dir: Path = typer.Option("tasks", help="Destination YAML directory"),
    prompts_dir: Path = typer.Option("prompts", help="Prompts directory"),
    dry_run: bool = typer.Option(False, help="Preview without writing files"),
):
    """Migrate Markdown tasks to YAML."""
    # Phase 4 - implement in Phase 4
    typer.echo("Migration tool coming in Phase 4!")

if __name__ == "__main__":
    app()
```

**Requirements**:
- Use Typer for CLI framework
- Colorful output (typer.echo with colors)
- Helpful error messages
- Interactive prompts for `init` and `create`
- `serve` command runs FastAPI app (Phase 2)
- `migrate` command placeholder (implement in Phase 4)

**Testing**: Manual testing of CLI commands.

---

## Implementation: Package Setup

**File**: `src/agentjobs/__init__.py`

```python
"""AgentJobs - Lightweight task management for AI agent workflows."""

from .models import (
    Task,
    TaskStatus,
    Priority,
    Phase,
    SuccessCriterion,
    Prompt,
    StatusUpdate,
    Deliverable,
    Dependency,
    ExternalLink,
    Issue,
    Branch,
)
from .manager import TaskManager
from .storage import TaskStorage
from .client import TaskClient  # Phase 2

__version__ = "0.1.0"

__all__ = [
    "Task",
    "TaskStatus",
    "Priority",
    "Phase",
    "SuccessCriterion",
    "Prompt",
    "StatusUpdate",
    "Deliverable",
    "Dependency",
    "ExternalLink",
    "Issue",
    "Branch",
    "TaskManager",
    "TaskStorage",
    "TaskClient",
]
```

**File**: `src/agentjobs/__version__.py`

```python
"""Version information."""
__version__ = "0.1.0"
```

---

## Example Task File

**File**: `examples/sample-task.yaml`

Create a complete example task demonstrating all features:

```yaml
id: task-001-example-feature
title: Example Feature Implementation
created: 2025-01-15T10:00:00Z
updated: 2025-01-15T14:30:00Z

status: in_progress
priority: high
category: feature_development
assigned_to: codex
estimated_effort: 3-5 days

description: |
  Implement a new feature that does XYZ.

  This is a multi-line description with details about
  the feature requirements and context.

phases:
  - id: phase-1
    title: Design and Planning
    status: completed
    completed_at: 2025-01-15T12:00:00Z

  - id: phase-2
    title: Implementation
    status: in_progress
    notes: "Working on core logic"

success_criteria:
  - id: sc-1
    description: "All unit tests passing"
    status: completed

  - id: sc-2
    description: "Integration tests passing"
    status: pending

prompts:
  starter: |
    Implement feature XYZ with the following requirements:
    - Requirement 1
    - Requirement 2
    - Requirement 3

  followups:
    - timestamp: 2025-01-15T13:00:00Z
      author: claude
      content: "Please add error handling for edge case ABC"
      context: "Discovered during code review"

status_updates:
  - timestamp: 2025-01-15T10:00:00Z
    author: claude
    status: planned
    summary: "Task created and planned"

  - timestamp: 2025-01-15T11:00:00Z
    author: codex
    status: in_progress
    summary: "Started implementation"
    details: "Completed design phase, moving to coding"

deliverables:
  - path: src/features/new_feature.py
    status: completed
    description: "Core feature implementation"

  - path: tests/test_new_feature.py
    status: in_progress
    description: "Unit tests"

dependencies:
  - task_id: task-000-prerequisite
    type: depends_on
    status: completed

external_links:
  - url: https://example.com/spec
    title: "Feature Specification"

issues:
  - id: issue-1
    title: "Edge case not handled"
    status: resolved
    resolution: "Added error handling in commit abc123"

tags:
  - feature
  - high-priority
  - api

branches:
  - name: feature/task-001-example-feature
    status: active
```

---

## Testing Requirements

### Unit Tests Coverage

**File**: `tests/test_models.py`
- Test all model validation
- Test enum values
- Test helper methods (is_completed, is_blocked)
- Test datetime handling
- Test JSON serialization

**File**: `tests/test_storage.py`
- Test load_task (valid YAML)
- Test load_task (invalid YAML)
- Test save_task
- Test list_tasks
- Test delete_task
- Test search_tasks

**File**: `tests/test_manager.py`
- Test create_task
- Test get_task
- Test update_status
- Test get_next_task (priority ordering)
- Test add_progress_update
- Test mark_deliverable_complete

**Coverage Target**: >80%

Run tests:
```bash
poetry run pytest --cov
```

---

## Documentation Files

Create these placeholder docs (will be expanded in later phases):

**docs/installation.md**:
```markdown
# Installation Guide

## From PyPI (Once Published)

```bash
pip install agentjobs
```

## Development Installation

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
```
```

**docs/quickstart.md**:
```markdown
# Quick Start Guide

Coming soon...
```

**docs/api-reference.md**:
```markdown
# API Reference

Will be auto-generated from FastAPI in Phase 2.
```

**docs/task-schema.md**:
```markdown
# Task Schema Reference

Full YAML schema documentation coming soon...
```

---

## Acceptance Criteria

- [ ] GitHub repository created with initial structure
- [ ] All Pydantic models implemented and tested
- [ ] TaskStorage implements load/save/list/delete/search
- [ ] TaskManager implements all core operations
- [ ] CLI has `init`, `serve`, `create`, `list`, `show` commands
- [ ] Example task YAML file demonstrates all features
- [ ] pytest suite with >80% coverage
- [ ] All tests passing
- [ ] Package can be installed via `pip install -e .`
- [ ] CLI executable via `agentjobs --help`

---

## Validation Steps

After implementation:

```bash
# 1. Install package
cd ~/projects/agentjobs
poetry install

# 2. Run tests
poetry run pytest --cov
# Verify >80% coverage

# 3. Test CLI
poetry run agentjobs --help
poetry run agentjobs init
poetry run agentjobs create --title "Test Task"
poetry run agentjobs list

# 4. Manual verification
# - Check that tasks/ directory created
# - Check that YAML file created
# - Check that task can be loaded
```

---

## Next Steps

After Phase 1 completion:
- **Phase 2**: Implement REST API and Python client library
- **Phase 3**: Build web GUI (HTML templates, Tailwind CSS)
- **Phase 4**: Create migration tool for Markdown → YAML
- **Phase 5**: Integrate into PrivateProject

---

## Notes

- Focus on clean, well-tested code in Phase 1
- Keep implementation simple - don't over-engineer
- Prioritize test coverage and documentation
- Use type hints everywhere for mypy compatibility
- Follow PEP 8 and Black formatting standards

Good luck! 🚀
