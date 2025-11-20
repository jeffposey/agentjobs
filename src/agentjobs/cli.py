"""Typer-powered CLI entry point for AgentJobs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

import typer
import yaml

from .manager import TaskManager
from .migration import migrate_tasks
from .migration.reporter import MigrationReporter
from .models import Priority, TaskStatus
from .storage import TaskStorage

app = typer.Typer(
    name="agentjobs",
    help="Lightweight task management for AI agent workflows.",
)

CONFIG_DIR = Path(".agentjobs")
CONFIG_FILE = CONFIG_DIR / "config.yaml"
DEFAULT_CONFIG = {
    "project_name": "AgentJobs Project",
    "tasks_directory": "tasks",
    "prompts_directory": "prompts",
    "gui": {"host": "localhost", "port": 8765, "theme": "dark"},
    "agents": [
        {"name": "claude", "display_name": "Claude (Lead Engineer)"},
        {"name": "codex", "display_name": "Codex (Workhorse)"},
    ],
    "categories": [
        "infrastructure",
        "strategy_development",
        "validation",
        "documentation",
    ],
    "defaults": {"priority": "medium", "status": "planned"},
}


def _load_config(base_dir: Path) -> dict:
    """Load AgentJobs configuration or return defaults."""
    config_path = base_dir / CONFIG_FILE
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    content = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(content) or copy.deepcopy(DEFAULT_CONFIG)


def _save_config(base_dir: Path, config: dict) -> None:
    """Persist AgentJobs configuration to disk."""
    config_path = base_dir / CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_yaml = yaml.safe_dump(config, sort_keys=False, allow_unicode=False)
    config_path.write_text(config_yaml, encoding="utf-8")


def _ensure_gitignore(base_dir: Path) -> None:
    """Guarantee AgentJobs runtime artifacts are ignored."""
    gitignore_path = base_dir / ".gitignore"
    if not gitignore_path.exists():
        return
    entry = ".agentjobs/agentjobs.db"
    lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    if entry not in lines:
        lines.append(entry)
        gitignore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_tasks_dir(base_dir: Path, config: dict) -> Path:
    """Resolve tasks directory relative to the project root."""
    tasks_dir = Path(config.get("tasks_directory", "tasks"))
    if not tasks_dir.is_absolute():
        tasks_dir = base_dir / tasks_dir
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


def _build_manager(base_dir: Path) -> TaskManager:
    """Instantiate a TaskManager for the current project."""
    config = _load_config(base_dir)
    tasks_dir = _resolve_tasks_dir(base_dir, config)
    storage = TaskStorage(tasks_dir)
    return TaskManager(storage)



@app.command()
def init(
    project_name: Optional[str] = typer.Option(
        None, help="Project display name."
    ),
    tasks_dir: Optional[str] = typer.Option(
        None, help="Relative path for task YAML files."
    ),
    prompts_dir: Optional[str] = typer.Option(
        None, help="Relative path for prompt files."
    ),
    port: Optional[int] = typer.Option(
        None, help="Default port for the web UI."
    ),
) -> None:
    """Initialize AgentJobs in current directory."""
    base_dir = Path.cwd()
    project_name = project_name or typer.prompt("Project name")
    tasks_dir = tasks_dir or typer.prompt("Tasks dir", default="tasks")
    prompts_dir = prompts_dir or typer.prompt("Prompts dir", default="prompts")
    port = port or int(typer.prompt("Port", default="8765"))

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project_name"] = project_name
    config["tasks_directory"] = tasks_dir
    config["prompts_directory"] = prompts_dir
    config["gui"]["port"] = port

    _save_config(base_dir, config)
    _ensure_gitignore(base_dir)
    _resolve_tasks_dir(base_dir, config)
    typer.echo("✅ AgentJobs initialized successfully!")


@app.command()
def serve(
    host: str = typer.Option("localhost"),
    port: int = typer.Option(8765),
    reload: bool = typer.Option(
        False,
        "--reload",
        "-r",
        help="Reload server on changes (development only).",
    ),
) -> None:
    """Start web server."""
    typer.echo(f"🚀 Starting AgentJobs server at http://{host}:{port}")
    import uvicorn

    uvicorn.run(
        "agentjobs.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command()
def create(
    title: Optional[str] = typer.Option(
        None, help="Task title to use when creating the record."
    ),
    id: Optional[str] = typer.Option(None, help="Optional explicit task identifier."),
    description: Optional[str] = typer.Option(
        None, help="Task description body."
    ),
    priority: Priority = typer.Option(
        Priority.MEDIUM.value,
        help="Task priority label.",
    ),
    category: str = typer.Option(
        "general", help="Categorisation label for filtering."
    ),
) -> None:
    """Create new task."""
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    tasks_dir = _resolve_tasks_dir(base_dir, config)
    manager = TaskManager(TaskStorage(tasks_dir))

    title = title or typer.prompt("Title")
    description = description if description is not None else typer.prompt(
        "Description", default=""
    )

    task_id = id or manager.storage.generate_task_id()
    task = manager.create_task(
        id=task_id,
        title=title,
        description=description,
        priority=priority,
        category=category,
    )
    typer.echo(f"✅ Created {task.id}.yaml")


@app.command("list")
def list_tasks(
    status: Optional[TaskStatus] = typer.Option(None),
    priority: Optional[Priority] = typer.Option(None),
) -> None:
    """List tasks."""
    base_dir = Path.cwd()
    manager = _build_manager(base_dir)
    tasks = manager.storage.list_tasks()

    if status is not None:
        tasks = [task for task in tasks if task.status == status]
    if priority is not None:
        tasks = [task for task in tasks if task.priority == priority]

    if not tasks:
        typer.echo("No tasks found.")
        return

    for task in tasks:
        status_value = (
            task.status.value if hasattr(task.status, "value") else task.status
        )
        priority_value = (
            task.priority.value
            if hasattr(task.priority, "value")
            else task.priority
        )
        typer.echo(
            f"- {task.id} | {task.title} "
            f"[status={status_value}, priority={priority_value}]"
        )


@app.command()
def load_test_data(
    storage_dir: str = typer.Option(
        "./tasks",
        help="Directory for task storage.",
    ),
) -> None:
    """Load sample test data for demos and manual testing."""
    from agentjobs.test_data import create_sample_tasks

    base_dir = Path.cwd()
    target_dir = Path(storage_dir)
    if not target_dir.is_absolute():
        target_dir = base_dir / target_dir

    storage = TaskStorage(target_dir)
    manager = TaskManager(storage)

    tasks = create_sample_tasks()
    created_count = 0
    updated_count = 0

    for task in tasks:
        payload = task.model_dump(mode="python")
        try:
            manager.create_task(**payload)
            typer.echo(f"✓ Created {task.id}: {task.title}")
            created_count += 1
        except ValueError:
            manager.replace_task(task.id, **payload)
            typer.echo(f"↻ Updated {task.id}: {task.title}")
            updated_count += 1

    typer.echo(f"\n✅ Loaded {len(tasks)} test tasks")
    typer.echo(
        f"   - {sum(1 for t in tasks if t.status == TaskStatus.WAITING_FOR_HUMAN)} waiting for human"
    )
    typer.echo(
        f"   - {sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)} in progress"
    )
    typer.echo(
        f"   - {sum(1 for t in tasks if t.status == TaskStatus.BLOCKED)} blocked"
    )
    typer.echo(
        f"   - {sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)} completed"
    )
    typer.echo(
        f"   - {sum(1 for t in tasks if t.status == TaskStatus.PLANNED)} planned"
    )

    if created_count and updated_count:
        typer.echo(f"\n📦 {created_count} created, {updated_count} refreshed.")
    elif created_count:
        typer.echo(f"\n📦 {created_count} created.")
    elif updated_count:
        typer.echo(f"\n📦 {updated_count} refreshed.")


@app.command()
def work(
    agent: str = typer.Option(
        ...,
        prompt="Your agent name",
        help="Agent identifier used in status updates.",
    ),
    priority: Optional[str] = typer.Option(
        None, help="Filter by priority (high, medium, low, critical)"
    ),
    storage_dir: str = typer.Option(
        "./tasks",
        help="Directory for task storage.",
    ),
) -> None:
    """Interactive agent workflow: get task, display prompt, mark complete."""
    base_dir = Path.cwd()
    target_dir = Path(storage_dir)
    if not target_dir.is_absolute():
        target_dir = base_dir / target_dir

    target_dir.mkdir(parents=True, exist_ok=True)

    storage = TaskStorage(target_dir)
    manager = TaskManager(storage)

    priority_enum = None
    if priority:
        try:
            priority_enum = Priority(priority.lower())
        except ValueError:
            typer.echo(f"Invalid priority: {priority}", err=True)
            raise typer.Exit(1)

    task = manager.get_next_task(priority=priority_enum)

    if not task:
        typer.echo("No tasks available")
        raise typer.Exit(0)

    divider = "=" * 60
    typer.echo(f"\n{divider}")
    typer.echo(f"TASK: {task.title}")
    typer.echo(f"ID: {task.id}")
    typer.echo(f"Priority: {getattr(task.priority, 'value', task.priority)}")
    typer.echo(f"Category: {task.category}")
    typer.echo(f"{divider}\n")

    if getattr(task.prompts, "starter", None):
        typer.echo(task.prompts.starter)
        typer.echo(f"\n{divider}\n")

    if not typer.confirm(f"Start working on this task as '{agent}'?"):
        typer.echo("Cancelled")
        raise typer.Exit(0)

    manager.update_status(
        task.id,
        status=TaskStatus.IN_PROGRESS,
        author=agent,
        summary=f"Started by {agent}",
    )
    typer.echo("✓ Task marked IN_PROGRESS")

    typer.echo("\n💼 Work on the task, then return here when done...\n")

    if not typer.confirm("Mark task as COMPLETED?", default=True):
        typer.echo("Task still IN_PROGRESS. Use CLI to update later.")
        raise typer.Exit(0)

    summary = typer.prompt("Summary of work done", default="Task completed")
    manager.update_status(
        task.id,
        status=TaskStatus.COMPLETED,
        author=agent,
        summary=summary,
    )
    typer.echo(f"\n✅ Task {task.id} marked COMPLETED!")


@app.command()
def show(task_id: str) -> None:
    """Show task details."""
    base_dir = Path.cwd()
    manager = _build_manager(base_dir)
    task = manager.get_task(task_id)
    if task is None:
        typer.secho(f"Task '{task_id}' not found.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(task.model_dump(mode="json"), indent=2))


@app.command()
def migrate(
    source: str = typer.Argument(...),
    target_dir: str = typer.Argument(...),
    prompts_dir: Optional[str] = typer.Option(None, "--prompts-dir", help="Optional prompts directory"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview migration"),
    report_file: str = typer.Option("migration-report.md", "--report", help="Report path"),
) -> None:
    """Migrate Markdown task files to YAML."""
    target_path = Path(target_dir)

    if not dry_run:
        target_path.mkdir(parents=True, exist_ok=True)

    typer.echo(f"{'[DRY RUN] ' if dry_run else ''}Migrating tasks...")

    results = migrate_tasks(
        source_patterns=[source],
        target_dir=target_path,
        prompts_dir=Path(prompts_dir) if prompts_dir else None,
        dry_run=dry_run,
    )

    reporter = MigrationReporter()
    reporter.generate_report(results, Path(report_file), dry_run)

    successful = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)

    typer.echo(f"\n✓ Migration complete!")
    typer.echo(f"  Successful: {successful}")
    typer.echo(f"  Failed: {failed}")
    typer.echo(f"  Report: {report_file}")

    if dry_run:
        typer.echo("\n⚠️  This was a dry run - no files were written.")


if __name__ == "__main__":
    app()
