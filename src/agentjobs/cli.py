"""Typer-powered CLI entry point for AgentJobs."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from .dispatch.config import (
    DispatchConfig,
    DispatchError,
    assert_dispatch_permitted,
    dispatch_config_path,
    load_dispatch_config,
    sentinel_active,
    sentinel_path,
    set_project_enabled,
)
from .dispatch.guards import DispatchRequest, dispatch_task
from .dispatch.ledger import DispatchLedger, LedgerError, list_runs, live_runs
from .dispatch.runner import DispatchRunError
from .manager import TaskManager
from .mcp.config import BASE_URL_ENV as MCP_BASE_URL_ENV
from .mcp.config import TIMEOUT_ENV as MCP_TIMEOUT_ENV
from .migration import migrate_tasks
from .migration.reporter import MigrationReporter
from .models_v2 import Ball, Lifecycle, Outcome, Priority
from .project_setup import DEFAULT_CONFIG, build_project_config, initialize_project
from .projects import ProjectError, ProjectRegistry, default_home
from .storage import TaskStorage, corpus_snapshot


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


@app.callback()
def _scope_one_invocation(ctx: typer.Context) -> None:
    """Parse each task file at most once per CLI invocation.

    A command like ``list`` used to walk the corpus several times for one answer, for
    the same reason the API did: the dependency computations each went back to storage
    independently. One invocation is one logical read, so it gets one scope.

    Entered here and closed through Click's ``call_on_close`` rather than wrapping the
    console-script entry point, so it applies identically however the app is invoked --
    the installed ``agentjobs`` command, ``python -m agentjobs.cli``, and the test
    runner's CliRunner, which calls ``app()`` directly and would otherwise never
    exercise this path.

    Writes drop the snapshot, so a command that mutates and then reads sees its own
    write.
    """
    scope = corpus_snapshot()
    scope.__enter__()
    ctx.call_on_close(lambda: scope.__exit__(None, None, None))


def _load_config(base_dir: Path) -> dict:
    """Load AgentJobs configuration or return defaults."""
    config_path = base_dir / CONFIG_FILE
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    content = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(content) or copy.deepcopy(DEFAULT_CONFIG)


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
    if (base_dir / CONFIG_FILE).exists():
        typer.secho(
            f"Refusing to initialize {base_dir}: {CONFIG_FILE} already exists; "
            "no files were changed.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    project_name = project_name or typer.prompt("Project name")
    tasks_dir = tasks_dir or typer.prompt("Tasks dir", default="tasks")
    prompts_dir = prompts_dir or typer.prompt("Prompts dir", default="prompts")
    port = port or int(typer.prompt("Port", default="8765"))
    # Asked at init because a project with no human configured records every review
    # action anonymously, and nobody goes looking for that setting afterwards.
    user = user or typer.prompt("Your user id", default=getpass.getuser().lower())

    config = build_project_config(
        project_name=project_name,
        tasks_directory=tasks_dir,
        prompts_directory=prompts_dir,
        port=port,
        user=user,
    )
    try:
        initialize_project(base_dir, config)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _ensure_gitignore(base_dir)
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
    host = _validated_bind_host(host)
    typer.echo(f"🚀 Starting AgentJobs server at http://{host}:{port}")
    import uvicorn

    uvicorn.run(
        "agentjobs.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command()
def validate(
    staged: bool = typer.Option(
        False,
        "--staged",
        help="Also require each staged task file to match a managed-write receipt.",
    ),
    install_hook: bool = typer.Option(
        False,
        "--install-hook",
        help="Install a pre-commit hook that runs `agentjobs validate --staged`.",
    ),
) -> None:
    """Check every task file, and optionally the staged ones.

    Without `--staged` this needs nothing but the files, so it is the check that works
    in CI and in a clean clone. It proves the corpus is safe to load; it cannot prove
    which program wrote a file, because a careful hand edit produces a file that
    validates perfectly.

    `--staged` closes that gap locally by requiring each staged task to match a receipt
    from a managed write. Receipts are machine-local and never committed, so the check
    is only meaningful on the machine that made the change.
    """
    from .validation import check_staged_receipts, override_reason, validate_corpus

    base_dir = Path.cwd()
    config = _load_config(base_dir)
    tasks_dir = _resolve_tasks_dir(base_dir, config)

    if install_hook:
        typer.echo(_install_pre_commit_hook(base_dir))
        raise typer.Exit(0)

    report = validate_corpus(tasks_dir, project_config=config, project_root=base_dir)
    findings = list(report.findings)

    if staged:
        reason = override_reason()
        staged_findings = check_staged_receipts(base_dir, tasks_dir)
        if reason and staged_findings:
            # Noisy on purpose. A bypass nobody notices becomes the normal path, so
            # every skipped file is named alongside the stated reason.
            typer.echo("⚠️  Managed-write gate bypassed by explicit maintainer override.", err=True)
            typer.echo(f"    Reason: {reason}", err=True)
            for finding in staged_findings:
                typer.echo(f"    Bypassed: {finding.filename} ({finding.rule})", err=True)
            typer.echo("    The schema and relationship checks still had to pass.", err=True)
        else:
            findings.extend(staged_findings)

    if findings:
        for finding in sorted(findings, key=lambda item: item.filename):
            typer.echo(finding.render(), err=True)
        typer.echo(
            f"\n❌ {len(findings)} problem(s) across {report.checked} task file(s).", err=True
        )
        raise typer.Exit(1)

    typer.echo(f"✓ {report.checked} task file(s) validated; no problems found.")


def _install_pre_commit_hook(base_dir: Path) -> str:
    """Write a pre-commit hook that runs the staged gate."""
    hooks_dir = base_dir / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise typer.BadParameter(f"{base_dir} does not look like a git repository.")
    path = hooks_dir / "pre-commit"
    script = (
        "#!/bin/sh\n"
        "# Installed by `agentjobs validate --install-hook`.\n"
        "# Refuses a commit whose staged task files were not written by AgentJobs.\n"
        "# Bypass for an emergency repair by stating a reason:\n"
        "#   AGENTJOBS_ALLOW_DIRECT_WRITE_REASON='...' git commit\n"
        "exec agentjobs validate --staged\n"
    )
    if path.exists() and "agentjobs validate --staged" not in path.read_text(encoding="utf-8"):
        return (
            f"A pre-commit hook already exists at {path} and was left alone. Add\n"
            "  agentjobs validate --staged\n"
            "to it yourself so both checks run."
        )
    path.write_text(script, encoding="utf-8")
    try:
        path.chmod(0o755)
    except OSError:  # pragma: no cover - Windows ignores the mode
        pass
    return f"✓ Installed the AgentJobs pre-commit hook at {path}."


@app.command("mcp")
def mcp_server(
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        envvar=MCP_BASE_URL_ENV,
        help="URL of the running AgentJobs service.",
    ),
    timeout: Optional[float] = typer.Option(
        None,
        "--timeout",
        envvar=MCP_TIMEOUT_ENV,
        help="Request timeout in seconds.",
    ),
) -> None:
    """Serve the AgentJobs MCP tools over STDIO.

    Speaks MCP on stdout and nothing else -- diagnostics go to stderr -- so it is safe
    to launch directly from an MCP client configuration. It requires an AgentJobs
    service to already be running and will not start one.
    """
    # The server module is imported here rather than at module scope so `agentjobs
    # --help` and every other command stay free of the MCP SDK's import cost.
    from .mcp.config import ConfigError, McpConfig
    from .mcp.server import run as run_mcp

    try:
        config = McpConfig.resolve(base_url=base_url, timeout=timeout)
    except ConfigError as exc:
        typer.echo(f"agentjobs mcp: {exc}", err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(run_mcp(config))


def _validated_bind_host(host: str) -> str:
    """Refuse addresses that expose the unauthenticated server on every interface."""
    from ipaddress import ip_address

    candidate = host.strip()
    unwrapped = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        is_unspecified = ip_address(unwrapped).is_unspecified
    except ValueError:
        is_unspecified = False

    if not candidate or candidate in {"*", "+"} or is_unspecified:
        raise typer.BadParameter(
            "Wildcard binding is refused because AgentJobs has no authentication. "
            "Use localhost/127.0.0.1 behind an HTTPS private-network proxy, or bind "
            "a specific interface only as the documented fallback.",
            param_hint="--host",
        )
    return candidate


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
    host = _validated_bind_host(host)
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
    """Open the primary React UI in a browser, starting the server if needed."""
    import platform
    import subprocess
    import sys
    import time
    import webbrowser

    host = _validated_bind_host(host)
    server_url = f"http://{host}:{port}"
    app_url = f"{server_url}/app/"
    pid = _find_process_by_port(port)

    if pid is None:
        # Server not running, start it in background
        typer.echo(f"Starting AgentJobs server at {server_url}...")
        command = [
            sys.executable,
            "-m",
            "agentjobs.cli",
            "serve",
            "--port",
            str(port),
            "--host",
            host,
        ]

        if platform.system() == "Windows":
            # Start server in a new window (minimized)
            subprocess.Popen(
                command,
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
            )
        else:
            # Start server in background
            subprocess.Popen(
                command,
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
    typer.echo(f"Opening {app_url}...")
    webbrowser.open(app_url)


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


@app.command("attachments")
def attachments_command(
    orphans: bool = typer.Option(
        False, "--orphans", help="List stored images no task references any more."
    ),
) -> None:
    """Report on the sidecar images stored beside this project's tasks.

    Reporting only, deliberately. Git keeps every blob it has ever seen, so a file that
    looks unreferenced today may still be referenced by an older revision or by a branch
    that is not checked out -- deleting on this evidence would destroy the thing an
    entry points at. What to do about an orphan is a person's call.
    """
    manager = _build_manager(Path.cwd())
    store = manager.storage.attachments
    tasks = manager.storage.list_tasks()
    referenced = store.referenced_paths(tasks)

    if not orphans:
        typer.echo(f"{len(referenced)} attachment(s) referenced by {len(tasks)} task(s).")
        typer.echo("Run with --orphans to list stored files nothing references.")
        return

    unreferenced = store.orphans(tasks)
    if not unreferenced:
        typer.echo("No orphaned attachments: every stored image is referenced.")
        return
    typer.secho(f"{len(unreferenced)} orphaned attachment(s):", fg=typer.colors.YELLOW)
    for path in unreferenced:
        typer.echo(f"  {path}")
    typer.echo("Nothing was deleted. Remove them yourself if you are sure.")


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


dispatch_app = typer.Typer(
    name="dispatch",
    help="Control whether this machine may launch agents from AgentJobs.",
)
app.add_typer(dispatch_app)


@dispatch_app.command("enable")
def dispatch_enable(
    project_id: str = typer.Argument(..., help="Registered project id to enable."),
    runner: Optional[str] = typer.Option(
        None, "--runner", help="Runner name from ~/.agentjobs/dispatch.yaml."
    ),
) -> None:
    """Allow dispatch for one project, using a runner this machine already defines."""
    try:
        ProjectRegistry().get(project_id)
        settings = set_project_enabled(project_id, True, runner=runner)
    except (ProjectError, DispatchError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ Dispatch enabled for '{project_id}' using runner '{settings.runner}'.")
    if not (load_dispatch_config() or DispatchConfig()).enabled:
        typer.secho(
            "⚠️  The master switch is still off; nothing will dispatch until "
            "'enabled: true' is set in ~/.agentjobs/dispatch.yaml.",
            fg=typer.colors.YELLOW,
        )
    if sentinel_active():
        typer.secho(
            f"⚠️  {sentinel_path()} exists; all dispatch is refused until it is removed.",
            fg=typer.colors.YELLOW,
        )


@dispatch_app.command("disable")
def dispatch_disable(
    project_id: str = typer.Argument(..., help="Project id to stop dispatching for."),
) -> None:
    """Refuse dispatch for one project. Always available, and never asks anything."""
    try:
        set_project_enabled(project_id, False)
    except DispatchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"✅ Dispatch disabled for '{project_id}'.")


@dispatch_app.command("run")
def dispatch_run(
    task_id: str = typer.Argument(..., help="Task to start an agent on."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    caused_by: Optional[int] = typer.Option(
        None,
        "--caused-by",
        help="Log entry authorising this run. Defaults to the newest; must be a human's.",
    ),
) -> None:
    """Start an agent on a task, if every gate permits it."""
    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    manager = TaskManager(TaskStorage(project.tasks_dir()))
    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(task_id=task_id, caused_by=caused_by),
        )
    except (DispatchError, DispatchRunError) as exc:
        reason = getattr(exc, "reason", "dispatch_failed")
        typer.secho(f"Refused ({reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ Dispatched {task_id} as run {handle.run_id} ({handle.mode.value}).")
    if handle.session_id:
        typer.echo(f"   Session {handle.session_id} — the CLI assigned that id, not us.")
    typer.echo(f"   Run directory: {handle.directory.path}")


@dispatch_app.command("status")
def dispatch_status(
    limit: int = typer.Option(20, "--limit", help="How many recent runs to show."),
    live_only: bool = typer.Option(False, "--live", help="Only runs nothing has ended."),
) -> None:
    """List live and recent agent runs."""
    records = live_runs(default_home()) if live_only else list_runs(default_home())
    if not records:
        typer.echo("No runs recorded." if not live_only else "No live runs.")
        return

    typer.echo(f"{'RUN':14} {'TASK':34} {'MODE':8} {'STATE':10} {'ELAPSED':>9}  SESSION")
    for record in records[:limit]:
        elapsed = record.elapsed_seconds()
        shown = f"{elapsed:,.0f}s" if elapsed is not None else "-"
        state = record.outcome or record.status
        typer.echo(
            f"{record.run_id:14} {record.task_id[:34]:34} {record.mode:8} "
            f"{state[:10]:10} {shown:>9}  {record.session_id or '-'}"
        )


@dispatch_app.command("cancel")
def dispatch_cancel(
    run_id: str = typer.Argument(..., help="Run id from 'agentjobs dispatch status'."),
) -> None:
    """Stop one run and record the outcome on its task."""
    ledger = DispatchLedger(default_home())
    try:
        result = ledger.cancel(run_id)
    except LedgerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    marker = "✅" if result.stopped else "⚠️ "
    typer.echo(f"{marker} {result.run_id}: {result.detail}")


@dispatch_app.command("stop")
def dispatch_stop_all() -> None:
    """Panic button: refuse all new runs, then stop every live one.

    One command, no arguments, on purpose. A kill switch you have to look up the syntax
    for is not one.
    """
    ledger = DispatchLedger(default_home())
    results = ledger.stop_everything()
    typer.secho(f"⛔ {sentinel_path()} written; no new run will start.", fg=typer.colors.YELLOW)
    if not results:
        typer.echo("   No runs were live.")
    for result in results:
        typer.echo(f"   {result.run_id}: {result.detail}")
    typer.echo("   Delete the sentinel file to re-enable dispatch.")


@dispatch_app.command("reconcile")
def dispatch_reconcile() -> None:
    """Settle runs left behind by a previous process.

    Batch runs do not outlive their supervisor, so a live one here means a crash and is
    marked interrupted. Sessions do outlive it deliberately, so a live one is re-attached
    and left running; only a session the manager no longer knows about is concluded.
    """
    results = DispatchLedger(default_home()).reconcile()
    if not results:
        typer.echo("Nothing to reconcile.")
        return
    for result in results:
        typer.echo(f"{result.run_id}: {result.detail}")


@dispatch_app.command("reap")
def dispatch_reap() -> None:
    """Remove finished sessions and the task worktrees they still hold.

    A worktree for a run that ended is litter, and it holds a pid in the session
    manager's ledger besides. This is the command that clears both.

    A reap that is **refused** is the useful outcome, not the failure: the session
    manager will not delete a worktree with uncommitted changes in it, which means that
    run produced work nobody has looked at. Reported rather than forced -- passing `-f`
    here would delete exactly the thing worth keeping.
    """
    results = DispatchLedger(default_home()).reap_finished()
    if not results:
        typer.echo("Nothing to reap.")
        return
    kept = 0
    for result in results:
        if result.stopped:
            typer.echo(f"🧹 {result.run_id}: {result.detail}")
        else:
            kept += 1
            typer.secho(f"⚠️  {result.run_id}: {result.detail}", fg=typer.colors.YELLOW)
    if kept:
        typer.echo(f"\n{kept} worktree(s) kept. Look at what is in them before reaping again.")


@dispatch_app.command("config")
def dispatch_show_config(
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Also report whether this project may dispatch right now."
    ),
) -> None:
    """Show the resolved dispatch configuration and every gate's current state."""
    path = dispatch_config_path()
    try:
        config = load_dispatch_config()
    except DispatchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Config file:    {path}{'' if config else '  (absent - dispatch is off)'}")
    typer.echo(f"Master switch:  {'on' if config and config.enabled else 'off'}")
    typer.echo(
        f"Sentinel:       {sentinel_path()} "
        f"{'PRESENT - all dispatch refused' if sentinel_active() else '(absent)'}"
    )

    if config is None:
        return

    typer.echo("\nRunners:")
    if not config.runners:
        typer.echo("  none defined")
    for name, runner in sorted(config.runners.items()):
        typer.echo(f"  {name:12} {runner.mode.value:8} {runner.argv}")

    typer.echo("\nProjects:")
    if not config.projects:
        typer.echo("  none configured")
    for pid, settings in sorted(config.projects.items()):
        state = "enabled " if settings.enabled else "disabled"
        typer.echo(
            f"  {pid:20} {state}  runner={settings.runner or '-'}  "
            f"posture={settings.posture.value}  "
            f"clean_tree={settings.require_clean_tree}  auto={settings.auto_dispatch}"
        )

    limits = config.limits
    typer.echo(
        f"\nLimits:         max_concurrent_runs={limits.max_concurrent_runs}  "
        f"run_timeout_seconds={limits.run_timeout_seconds}  "
        f"session_stale_seconds={limits.session_stale_seconds}"
    )
    typer.echo(
        f"Auto-dispatch:  per_task_per_day={limits.auto.per_task_per_day}  "
        f"per_task_lifetime={limits.auto.per_task_lifetime}  "
        f"cooldown_seconds={limits.auto.cooldown_seconds}"
    )

    if project_id:
        try:
            resolution = assert_dispatch_permitted(project_id)
        except DispatchError as exc:
            typer.secho(f"\n{project_id}: refused ({exc.reason}) - {exc}", fg=typer.colors.YELLOW)
        else:
            typer.echo(
                f"\n{project_id}: permitted - runner '{resolution.runner.name}' "
                f"({resolution.runner.mode.value}), posture {resolution.settings.posture.value}"
            )


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


def _resolve_actor(config: dict, actor: Optional[str]) -> str:
    """Resolve who is acting, preferring an explicit --actor over the configured human.

    An unattributed state change is worse than a refused one, so there is no
    fallback to an OS username: either the caller says who they are or the
    project has named a default_user, and otherwise the command stops.
    """
    if actor:
        return actor
    default_user = config.get("default_user")
    if default_user:
        return str(default_user)
    typer.secho(
        "No actor to attribute this to. Pass --actor, or set default_user in "
        ".agentjobs/config.yaml.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@app.command()
def promote(
    task_id: str,
    actor: Optional[str] = typer.Option(
        None, "--actor", help="Who is promoting. Defaults to the project's default_user."
    ),
    note: Optional[str] = typer.Option(
        None,
        "--note",
        help="Optional log body. Omit it and the manager writes its own sentence.",
    ),
) -> None:
    """Promote a draft to ready, making it claimable.

    This is the only exit from draft. Whether the spec is finished is the caller's
    judgement, not this command's -- `agentjobs validate` is where completeness is
    argued about.
    """
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    manager = _build_manager(base_dir)
    resolved_actor = _resolve_actor(config, actor)

    try:
        task = manager.promote_task(task_id, actor=resolved_actor, body=note)
    except ValueError as error:
        # Covers both TaskNotFoundError and the refused transition. Neither is a
        # bug, so neither should reach the user as a traceback.
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(f"✅ Promoted {task.id}: {task.display_status}")


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
