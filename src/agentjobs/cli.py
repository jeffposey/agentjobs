"""Typer-powered CLI entry point for AgentJobs."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import typer
import yaml

from .manager import TaskManager
from .migration import migrate_tasks
from .migration.reporter import MigrationReporter
from .models_v2 import Ball, Lifecycle, Outcome, Priority
from .projects import ProjectError, ProjectRegistry
from .storage import TaskStorage


def _make_output_encoding_safe() -> None:
    """Stop non-ASCII CLI output from crashing on legacy-codepage streams.

    When stdout is a console, Python writes through a Unicode-aware path and the
    emoji in our output render fine. When it is redirected to a pipe or file, it
    falls back to the locale encoding instead (cp1252 on a default Windows
    install), and the first emoji raises UnicodeEncodeError -- so the CLI works
    interactively but dies under CI, background shells, and log redirection.

    Reconfiguring here rather than stripping the emoji keeps a later added glyph
    from reintroducing the crash.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - non-standard stream
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - defensive
            # Stream refuses re-encoding; degrade to replacing bad glyphs.
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


_make_output_encoding_safe()

app = typer.Typer(
    name="agentjobs",
    help="Lightweight task management for AI agent workflows.",
)

CONFIG_DIR = Path(".agentjobs")
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Annotated because the values are heterogeneous -- strings, ints, a nested dict and
# a list of dicts. Without it mypy infers Collection[Collection[str]] from the
# literal and rejects `config["gui"]["port"] = port` as an unsupported assignment.
DEFAULT_CONFIG: Dict[str, Any] = {
    "project_name": "AgentJobs Project",
    "tasks_directory": "tasks",
    "prompts_directory": "prompts",
    "gui": {"host": "localhost", "port": 8765, "theme": "dark"},
    # One vocabulary of actors, each carrying its kind -- which is exactly what D4 says
    # config resolves. A legacy `agents:` list is still read (see actors.load_actors),
    # so an existing project keeps working without editing its config.
    "actors": [
        {"name": "claude", "kind": "agent", "display_name": "Claude (Lead Engineer)"},
        {"name": "codex", "kind": "agent", "display_name": "Codex (Workhorse)"},
    ],
    "default_user": None,
    "categories": [
        "infrastructure",
        "strategy_development",
        "validation",
        "documentation",
    ],
    "defaults": {"priority": "medium", "lifecycle": "draft"},
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
    project_name: Optional[str] = typer.Option(None, help="Project display name."),
    tasks_dir: Optional[str] = typer.Option(None, help="Relative path for task YAML files."),
    prompts_dir: Optional[str] = typer.Option(None, help="Relative path for prompt files."),
    port: Optional[int] = typer.Option(None, help="Default port for the web UI."),
    user: Optional[str] = typer.Option(None, help="Your actor id, recorded on your actions."),
) -> None:
    """Initialize AgentJobs in current directory."""
    import getpass

    base_dir = Path.cwd()
    project_name = project_name or typer.prompt("Project name")
    tasks_dir = tasks_dir or typer.prompt("Tasks dir", default="tasks")
    prompts_dir = prompts_dir or typer.prompt("Prompts dir", default="prompts")
    port = port or int(typer.prompt("Port", default="8765"))
    # Asked at init because a project with no human configured records every review
    # action anonymously, and nobody goes looking for that setting afterwards.
    user = user or typer.prompt("Your user id", default=getpass.getuser().lower())

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project_name"] = project_name
    config["tasks_directory"] = tasks_dir
    config["prompts_directory"] = prompts_dir
    config["gui"]["port"] = port
    config["actors"] = list(config["actors"]) + [
        {"name": user, "kind": "human", "display_name": user}
    ]
    config["default_user"] = user

    _save_config(base_dir, config)
    _ensure_gitignore(base_dir)
    _resolve_tasks_dir(base_dir, config)
    typer.echo("✅ AgentJobs initialized successfully!")

    # Register on the machine so one server can serve this project alongside others.
    # A registration failure must not fail init -- the project is initialized either
    # way, and it stays usable from its own directory.
    try:
        project = ProjectRegistry().add(base_dir)
    except ProjectError as exc:
        typer.echo(f"⚠️  Not registered for multi-project use: {exc}")
    else:
        typer.echo(f"   Registered as '{project.id}' — visible in 'agentjobs project list'.")


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


def _find_process_by_port(port: int) -> Optional[int]:
    """Find PID of process listening on given port."""
    import platform
    import subprocess

    system = platform.system()

    try:
        if system == "Windows":
            # Use netstat on Windows
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=True)
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    return int(parts[-1])
        else:
            # Use lsof on Unix-like systems
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
    except (subprocess.CalledProcessError, ValueError, IndexError):
        pass

    return None


@app.command()
def stop(
    port: int = typer.Option(8765, help="Port number of server to stop."),
) -> None:
    """Stop the running web server."""
    import platform
    import subprocess

    pid = _find_process_by_port(port)

    if pid is None:
        typer.echo(f"No server found running on port {port}.")
        return

    typer.echo(f"Stopping server (PID {pid}) on port {port}...")

    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
        else:
            subprocess.run(["kill", str(pid)], check=True)
        typer.echo("✓ Server stopped successfully.")
    except subprocess.CalledProcessError as e:
        typer.echo(f"Failed to stop server: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def status(
    port: int = typer.Option(8765, help="Port number to check."),
) -> None:
    """Check if the web server is running."""
    pid = _find_process_by_port(port)

    if pid is None:
        typer.echo(f"❌ No server running on port {port}.")
        raise typer.Exit(1)
    else:
        typer.echo(f"✓ Server is running (PID {pid}) on http://localhost:{port}")


@app.command()
def restart(
    host: str = typer.Option("localhost"),
    port: int = typer.Option(8765),
    reload: bool = typer.Option(
        False,
        "--reload",
        "-r",
        help="Reload server on changes (development only).",
    ),
) -> None:
    """Restart the web server."""
    # Stop existing server if running
    pid = _find_process_by_port(port)
    if pid is not None:
        typer.echo(f"Stopping existing server (PID {pid})...")
        import platform
        import subprocess

        try:
            if platform.system() == "Windows":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
            else:
                subprocess.run(["kill", str(pid)], check=True)
            typer.echo("✓ Server stopped.")
        except subprocess.CalledProcessError:
            typer.echo("Warning: Failed to stop existing server.", err=True)

    # Start new server
    typer.echo(f"🚀 Starting AgentJobs server at http://{host}:{port}")
    import uvicorn

    uvicorn.run(
        "agentjobs.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command()
def open(
    port: int = typer.Option(8765, help="Port number to check/use."),
    host: str = typer.Option("localhost", help="Host to use if starting server."),
) -> None:
    """Open the web UI in browser (starts server if needed)."""
    import platform
    import subprocess
    import time
    import webbrowser

    url = f"http://{host}:{port}"
    pid = _find_process_by_port(port)

    if pid is None:
        # Server not running, start it in background
        typer.echo(f"Starting AgentJobs server at {url}...")

        if platform.system() == "Windows":
            # Start server in a new window (minimized)
            subprocess.Popen(
                ["poetry", "run", "agentjobs", "serve", "--port", str(port), "--host", host],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
            )
        else:
            # Start server in background
            subprocess.Popen(
                ["poetry", "run", "agentjobs", "serve", "--port", str(port), "--host", host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # Wait for server to start
        typer.echo("Waiting for server to initialize...")
        max_retries = 10
        for _ in range(max_retries):
            time.sleep(1)
            if _find_process_by_port(port) is not None:
                break
        else:
            typer.echo("Warning: Server may not have started successfully.", err=True)
    else:
        typer.echo(f"Server already running (PID {pid})")

    # Open browser
    typer.echo(f"Opening {url}...")
    webbrowser.open(url)


@app.command()
def create(
    title: Optional[str] = typer.Option(None, help="Task title to use when creating the record."),
    id: Optional[str] = typer.Option(None, help="Optional explicit task identifier."),
    description: Optional[str] = typer.Option(None, help="Task description body."),
    priority: Priority = typer.Option(
        Priority.MEDIUM.value,
        help="Task priority label.",
    ),
    category: str = typer.Option("general", help="Categorisation label for filtering."),
) -> None:
    """Create new task."""
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    tasks_dir = _resolve_tasks_dir(base_dir, config)
    manager = TaskManager(TaskStorage(tasks_dir))

    title = title or typer.prompt("Title")
    description = (
        description if description is not None else typer.prompt("Description", default="")
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
    lifecycle: Optional[Lifecycle] = typer.Option(None),
    ball: Optional[Ball] = typer.Option(None),
    priority: Optional[Priority] = typer.Option(None),
) -> None:
    """List tasks."""
    base_dir = Path.cwd()
    manager = _build_manager(base_dir)
    loaded = manager.storage.load_all()
    tasks = loaded.tasks

    # Reported before the list, not after: a broken file is the thing most worth
    # noticing, and it is not in the list below precisely because it is broken.
    for broken in loaded.errors:
        typer.secho(f"⚠️  {broken.path.name}: {broken.reason}", fg=typer.colors.RED, err=True)
    if loaded.errors:
        typer.secho(
            f"{len(loaded.errors)} file(s) could not be loaded and are missing from this list.",
            fg=typer.colors.RED,
            err=True,
        )

    if lifecycle is not None:
        tasks = [task for task in tasks if task.lifecycle == lifecycle]
    if ball is not None:
        tasks = [task for task in tasks if task.ball == ball]
    if priority is not None:
        tasks = [task for task in tasks if task.priority == priority]

    if not tasks:
        typer.echo("No tasks found.")
        return

    for task in tasks:
        typer.echo(
            f"- {task.id} | {task.title} "
            f"[{task.display_status}, priority={task.priority.value}]"
        )


@app.command()
def load_test_data(
    storage_dir: str = typer.Option(
        "./tasks/test-data",
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

    tasks = create_sample_tasks()
    created_count = 0
    updated_count = 0

    from .storage import TaskLoadError

    for task in tasks:
        try:
            existed = storage.load_task(task.id) is not None
        except TaskLoadError:
            # A broken or unmigrated file at this id is replaced, not preserved --
            # this command exists to (re)seed demo data.
            existed = True
        storage.save_task(task)
        if existed:
            typer.echo(f"↻ Updated {task.id}: {task.title}")
            updated_count += 1
        else:
            typer.echo(f"✓ Created {task.id}: {task.title}")
            created_count += 1

    typer.echo(f"\n✅ Loaded {len(tasks)} test tasks")
    from collections import Counter

    status_counts = Counter(t.display_status for t in tasks)
    for label, count in sorted(status_counts.items()):
        typer.echo(f"   - {count} {label.lower()}")

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
    typer.echo(f"Priority: {task.priority.value}")
    typer.echo(f"Category: {task.category}")
    typer.echo(f"{divider}\n")

    typer.echo(task.spec.description)
    typer.echo(f"\n{divider}\n")

    if not typer.confirm(f"Start working on this task as '{agent}'?"):
        typer.echo("Cancelled")
        raise typer.Exit(0)

    manager.claim_task(task.id, agent=agent)
    typer.echo("✓ Task claimed (active, ball: agent/work)")

    typer.echo("\n💼 Work on the task, then return here when done...\n")

    if not typer.confirm("Close the task as completed?", default=True):
        typer.echo("Task still claimed. Use the API or hand it off later.")
        raise typer.Exit(0)

    summary = typer.prompt("Summary of work done", default="Task completed")
    manager.close_task(
        task.id,
        actor=agent,
        outcome=Outcome.COMPLETED,
        body=summary,
    )
    typer.echo(f"\n✅ Task {task.id} closed: completed.")


project_app = typer.Typer(
    name="project",
    help="Manage which projects this machine's AgentJobs server can serve.",
)
app.add_typer(project_app)


@project_app.command("add")
def project_add(
    path: str = typer.Argument(".", help="Project directory to register."),
    project_id: Optional[str] = typer.Option(
        None, "--id", help="Short id used in URLs. Derived from the project name if omitted."
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Display name."),
) -> None:
    """Register a project directory."""
    try:
        project = ProjectRegistry().add(Path(path), project_id=project_id, name=name)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"✅ Registered '{project.id}' ({project.name}) at {project.root}")


@project_app.command("list")
def project_list() -> None:
    """List registered projects."""
    projects = ProjectRegistry().list_projects()
    if not projects:
        typer.echo("No projects registered. Run 'agentjobs project add <path>'.")
        return
    for project in projects:
        missing = "" if project.root.is_dir() else "  [missing]"
        typer.echo(f"{project.id:20} {project.name:30} {project.root}{missing}")


@project_app.command("remove")
def project_remove(
    project_id: str = typer.Argument(..., help="Id of the project to unregister."),
) -> None:
    """Unregister a project. Its files are never touched."""
    try:
        ProjectRegistry().remove(project_id)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"✅ Unregistered '{project_id}'. No files were deleted.")


@app.command("migrate-schema")
def migrate_schema_command(
    tasks_dir: Optional[str] = typer.Option(
        None, "--tasks-dir", help="Directory of task YAML files. Defaults to the project's."
    ),
    output_dir: Optional[str] = typer.Option(
        None,
        "--output-dir",
        help="Write converted files here instead of in place. Strongly recommended first.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually write files. Without it this is a dry run."
    ),
    report: Optional[str] = typer.Option(
        None, "--report", help="Also write the summary to this path."
    ),
) -> None:
    """Convert task files from schema v1 to v2.

    Dry run by default: it converts everything in memory, verifies that no information
    was lost, and prints what it would do. Nothing is written without --apply, and
    --apply refuses to write anything at all if a single file fails, because a corpus
    half-converted is worse than one not converted.
    """
    from .migrate_schema import migrate_corpus

    base_dir = Path.cwd()
    config = _load_config(base_dir)
    source = Path(tasks_dir) if tasks_dir else _resolve_tasks_dir(base_dir, config)
    paths = sorted(source.glob("*.yaml"))
    if not paths:
        typer.secho(f"No task files found in {source}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    result = migrate_corpus(
        paths,
        output_dir=Path(output_dir) if output_dir else None,
        write=apply,
    )
    rendered = result.render()
    typer.echo(rendered)
    if report:
        Path(report).write_text(rendered + "\n", encoding="utf-8")
        typer.echo(f"\nReport written to {report}")

    if result.failures:
        typer.secho(
            "\nNothing was written: fix the failures above and re-run.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if not apply:
        typer.secho(
            "\nDry run. Re-run with --apply to write, ideally with --output-dir first.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def show(task_id: str) -> None:
    """Show task details."""
    base_dir = Path.cwd()
    manager = _build_manager(base_dir)
    task = manager.get_task(task_id)
    if task is None:
        typer.secho(f"Task '{task_id}' not found.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(task.model_dump(mode="json", by_alias=True), indent=2))


@app.command()
def migrate(
    source: str = typer.Argument(...),
    target_dir: str = typer.Argument(...),
    prompts_dir: Optional[str] = typer.Option(
        None, "--prompts-dir", help="Optional prompts directory"
    ),
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

    typer.echo("\n✓ Migration complete!")
    typer.echo(f"  Successful: {successful}")
    typer.echo(f"  Failed: {failed}")
    typer.echo(f"  Report: {report_file}")

    if dry_run:
        typer.echo("\n⚠️  This was a dry run - no files were written.")


if __name__ == "__main__":
    app()
