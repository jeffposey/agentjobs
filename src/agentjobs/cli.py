"""Typer-powered CLI entry point for AgentJobs."""

from __future__ import annotations

import copy
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, List, Optional

import typer
import yaml

from .actors import actor_kinds
from .exposure import Visibility, visibility_of
from .dispatch.auth import read_auth_stall
from .dispatch.address import (
    configured_api_base,
    probe_api_base,
    resolve_api_base_detail,
)
from .dispatch.config import (
    DispatchConfig,
    DispatchError,
    Posture,
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
from .dispatch.scaffold import EXAMPLE_CONFIG, write_example_config
from .manager import MoveOutcome, QueueEntry, QueueListing, TaskManager
from .mcp.config import BASE_URL_ENV as MCP_BASE_URL_ENV
from .mcp.config import TIMEOUT_ENV as MCP_TIMEOUT_ENV
from .migration import migrate_tasks
from .migration.reporter import MigrationReporter
from .models_v2 import Ball, DispatchMode, Lifecycle, Outcome, Priority
from .playbooks import (
    PLAYBOOK_SUFFIX,
    PlaybookError,
    PlaybookListing,
    UnknownPlaybookError,
    install_references,
    list_playbooks,
    read_playbook,
    resolve_playbooks_dir,
)
from .playbooks.run import PlaybookDispatchRefused, PlaybookRunError, run_playbook
from .project_setup import (
    DEFAULT_CONFIG,
    MCP_CONFIG_FILENAME,
    build_project_config,
    ensure_mcp_server_entry,
    initialize_project,
)
from .projects import Project, ProjectError, ProjectRegistry, default_home
from .queue import REPAIR_COMMAND, QueueCorruptionError
from .cutover import back_up, cut_over, export_project
from .cutover import preview as cutover_preview
from .cutover import roll_back
from .cutover import status as cutover_status
from .cutover import restore_backup, verify_backup
from .quotation import scan_task
from .sqlstore import CorpusAlreadyImported, QuotationPolicyError
from .storage_config import BACKENDS, FILES, SQLITE, StorageSettings, load_storage_settings
from .storage_split import SplitError, split_project
from .storage import TaskStorage, corpus_snapshot
from .store_factory import (
    TaskManagerLike,
    dispatch_manager_for,
    provision_project_database,
    task_manager_for,
)


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


def _refuse_if_not_files(project_id: str, command: str) -> None:
    """Stop a file-era command from silently reading a directory nothing writes.

    ``validate`` and ``quotations`` are about *files*: one checks that each parses and
    is canonical, the other reads their prose. After a cutover the directory may still
    be sitting in the checkout, frozen at the moment of the migration -- so both would
    keep working, keep passing, and keep answering about a corpus that has moved on.
    That is a worse failure than an error, because nothing in the output says the answer
    is stale.
    """
    if not load_storage_settings().on_sqlite(project_id):
        return
    typer.secho(
        f"{project_id!r} is served from the AgentJobs database, so `{command}` has no "
        "files to read. Anything still under the tasks directory is a frozen copy from "
        "before the cutover.",
        fg=typer.colors.RED,
    )
    typer.echo(
        "  The database enforces every consistency rule as a constraint, so a record "
        "that would fail validation cannot be a row."
    )
    typer.echo("  `agentjobs storage status` says where the records are.")
    raise typer.Exit(code=1)


def _build_manager(base_dir: Path) -> TaskManagerLike:
    """A manager for the project this directory belongs to.

    **Resolved through the registry first, and the directory only as a fallback.** The
    registry is what knows a project's *id*, and the id is what says which backend holds
    its tasks -- so a command that went straight to ``base_dir/tasks`` would happily read
    a directory the machine no longer considers authoritative and report a backlog that
    is months out of date. Observed in the task-311 sandbox: with the project migrated
    and the whole corpus still sitting in a worktree, ``agentjobs list`` answered from
    the files and ``agentjobs create`` wrote one. That is the coupling this migration
    exists to remove, so the resolution has to happen here rather than at each call site.

    The fallback is the case the registry cannot answer: a directory that is not inside
    any registered project. That is on files by definition -- a cutover is recorded
    against a registered id -- so reading it is right.
    """
    try:
        project = ProjectRegistry().resolve_default(base_dir)
    except ProjectError:
        return TaskManager(TaskStorage(_resolve_tasks_dir(base_dir, _load_config(base_dir))))
    return task_manager_for(project)


def _mcp_base_url(port: int) -> tuple[str, bool]:
    """Where a project's MCP server should be told AgentJobs is listening.

    Two sources, and the order matters. First the machine's standing answer --
    ``AGENTJOBS_API_BASE``, then ``api_base:`` in ``~/.agentjobs/dispatch.yaml`` -- which
    is the same value dispatch already resolves for the address it hands an agent, so a
    machine that serves on a non-default port states that once and both stop being
    wrong. Otherwise loopback on the port this project was just configured for, which is
    what a single-project machine running the defaults is actually serving.

    The second element is True when the machine had nothing to say, so a caller can
    point at ``api_base`` instead of leaving someone to discover a dead port from a
    session that silently has no tools.
    """
    configured = configured_api_base()
    if configured:
        return configured, False
    return f"http://127.0.0.1:{port}", True


def _write_mcp_entry(base_dir: Path, port: int) -> None:
    """Give a freshly initialized project its MCP server entry, but never fail over it.

    A project is initialized and usable whether or not this file lands, exactly as it is
    whether or not registration succeeds -- so a malformed ``.mcp.json`` someone else
    owns is reported and stepped over rather than aborting the command.
    """
    base_url, guessed = _mcp_base_url(port)
    try:
        written = ensure_mcp_server_entry(base_dir, base_url)
    except ProjectError as exc:
        typer.echo(f"⚠️  No MCP server entry written: {exc}")
        return
    if written is None:
        typer.echo(
            f"   {MCP_CONFIG_FILENAME} already declares an 'agentjobs' server; left as it is."
        )
        return
    typer.echo(
        f"   Wrote {MCP_CONFIG_FILENAME} pointing at {base_url} — MCP tools for agents here."
    )
    if guessed:
        typer.echo(
            "   If AgentJobs serves on another port, set 'api_base' in "
            f"{default_home() / 'dispatch.yaml'} and rerun 'agentjobs project mcp-setup'."
        )


def _existing_task_files(base_dir: Path, tasks_dir: str) -> list[Path]:
    """Task YAML already sitting in the directory being initialized.

    Read before anything is written, because the answer changes what ``init`` has to
    say: a corpus here is somebody's backlog, and a fresh project on the database will
    not read a byte of it.
    """
    configured = Path(tasks_dir)
    resolved = configured if configured.is_absolute() else base_dir / configured
    try:
        return sorted(resolved.glob("*.yaml"))
    except OSError:
        return []


def _report_existing_corpus(found: list[Path], project_id: Optional[str]) -> None:
    """Name an existing corpus and the command that brings it in (task-399).

    Neither ignored nor absorbed. Importing it silently would mean ``init`` deciding
    that whatever is in this directory is now this project's backlog, quotation policy
    and history reconciliation included -- which is what ``storage cutover`` does under
    a preview, a backup and a field-by-field verification, and none of that belongs in
    a command somebody runs to make a config file.
    """
    if not found:
        return
    where = found[0].parent
    typer.secho(
        f"⚠️  {len(found)} task file(s) already in {where}. This project's records are "
        "rows in its database, so nothing here is read.",
        fg=typer.colors.YELLOW,
    )
    target = f" --project {project_id}" if project_id else ""
    typer.echo(
        f"   To bring them in: 'agentjobs storage preview{target}', then "
        f"'agentjobs storage cutover{target}'."
    )


def _provision_database(project: Project) -> None:
    """Put a freshly registered project on the database, but never fail init over it."""
    try:
        database = provision_project_database(project)
    except Exception as exc:  # noqa: BLE001 - a usable project must not hinge on this
        typer.secho(f"⚠️  No database created: {exc}", fg=typer.colors.YELLOW)
        typer.echo("   'agentjobs storage status' says where this project stands.")
        return
    typer.echo(f"   Records live in {database} — no task files, nothing to commit.")


@app.command()
def init(
    project_name: Optional[str] = typer.Option(None, help="Project display name."),
    tasks_dir: Optional[str] = typer.Option(None, help="Relative path for task YAML files."),
    prompts_dir: Optional[str] = typer.Option(None, help="Relative path for prompt files."),
    port: Optional[int] = typer.Option(None, help="Default port for the web UI."),
    user: Optional[str] = typer.Option(None, help="Your actor id, recorded on your actions."),
    backend: str = typer.Option(
        SQLITE,
        "--backend",
        help="Where this project's records live: 'sqlite' (default) or 'files' (legacy).",
    ),
) -> None:
    """Initialize AgentJobs in current directory.

    The project's records are rows in a database of its own under
    ``~/.agentjobs/databases/``, and no task directory is created (task-399). A new
    install therefore starts where every migrated project ends up, instead of building
    a corpus it has to be walked through migrating later.

    ``--backend files`` is the old behaviour, kept while the file backend still exists.
    """
    import getpass

    if backend not in BACKENDS:
        typer.secho(
            f"Unknown backend {backend!r}. Known backends: {', '.join(BACKENDS)}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    on_files = backend == FILES

    base_dir = Path.cwd()
    if (base_dir / CONFIG_FILE).exists():
        typer.secho(
            f"Refusing to initialize {base_dir}: {CONFIG_FILE} already exists; "
            "no files were changed.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    project_name = project_name or typer.prompt("Project name")
    # Asked only when files are what this project will be served from. On the database
    # the field still names where an import would read, and asking a new user to choose
    # a directory nothing writes to is ceremony that teaches the wrong model.
    if on_files:
        tasks_dir = tasks_dir or typer.prompt("Tasks dir", default="tasks")
    tasks_dir = tasks_dir or "tasks"
    prompts_dir = prompts_dir or typer.prompt("Prompts dir", default="prompts")
    port = port or int(typer.prompt("Port", default="8765"))
    # Asked at init because a project with no human configured records every review
    # action anonymously, and nobody goes looking for that setting afterwards.
    user = user or typer.prompt("Your user id", default=getpass.getuser().lower())

    found = [] if on_files else _existing_task_files(base_dir, tasks_dir)

    config = build_project_config(
        project_name=project_name,
        tasks_directory=tasks_dir,
        prompts_directory=prompts_dir,
        port=port,
        user=user,
    )
    try:
        initialize_project(base_dir, config, create_tasks_directory=on_files)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _ensure_gitignore(base_dir)
    typer.echo("✅ AgentJobs initialized successfully!")
    _write_mcp_entry(base_dir, port)

    # Register on the machine so one server can serve this project alongside others.
    # A registration failure must not fail init -- the project is initialized either
    # way, and it stays usable from its own directory.
    project = None
    try:
        project = ProjectRegistry().add(base_dir)
    except ProjectError as exc:
        typer.echo(f"⚠️  Not registered for multi-project use: {exc}")
    else:
        typer.echo(f"   Registered as '{project.id}' — visible in 'agentjobs project list'.")

    if on_files:
        _report_existing_corpus(found, project.id if project else None)
        return

    if project is None:
        # The storage map is keyed on a project id, so there is nothing to record for a
        # project that has none. Saying so beats leaving a config that promises a
        # database nothing will ever create.
        typer.secho(
            "⚠️  Without a registration this project has no database; it falls back to "
            "task files. Register it with 'agentjobs project add .' and rerun.",
            fg=typer.colors.YELLOW,
        )
        return

    _provision_database(project)
    _report_existing_corpus(found, project.id)
    typer.echo(
        f"   Start the server to work it: 'agentjobs open' (or 'agentjobs serve "
        f"--port {port}')."
    )


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
    _warn_if_bundle_missing()
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
    with suppress(ProjectError):
        _refuse_if_not_files(ProjectRegistry().resolve_default(base_dir).id, "validate")
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


def _warn_if_bundle_missing() -> None:
    """Say once, to stderr, that this checkout cannot serve `/app/`.

    A warning rather than a refusal: REST, MCP and every other command work without a
    bundle. The point is that the sentence arrives at the terminal that started the
    server, instead of as a JSON 404 in a browser tab.
    """
    from .api.spa import bundle_status

    sentence = bundle_status()
    if sentence:
        typer.echo(f"Warning: {sentence}", err=True)


def _echo_startup_log(path: Optional[Path]) -> None:
    """Relay whatever a server we started said before it gave up, then delete the file.

    Silent when we did not start the server (nothing was captured) or when it said
    nothing, so the caller can always call it and only ever adds signal.
    """
    if path is None:
        return
    try:
        captured = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        captured = ""
    if captured:
        typer.echo("", err=True)
        typer.echo("The server said:", err=True)
        typer.echo(captured, err=True)
    with suppress(OSError):
        path.unlink()


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


#: How long a graceful stop is given before the server is killed, in seconds.
#:
#: Uvicorn stops accepting, finishes what is in flight and runs the lifespan's shutdown
#: -- which is where the SQLite store is closed and the WAL checkpointed. Three seconds
#: is generous for a request set that is one dashboard poll deep, and short enough that
#: `agentjobs restart` is still a pause a client rides through rather than a wait.
GRACEFUL_STOP_SECONDS = 3.0


def _stop_server(pid: int, port: int) -> bool:
    """Ask the server to exit, and only kill it if it will not.

    **On POSIX this is a real drain.** ``SIGTERM`` reaches uvicorn's signal handler, so
    the process stops accepting, finishes its in-flight requests and runs the lifespan's
    shutdown before exiting. ``SIGKILL`` follows only if it is still listening after
    :data:`GRACEFUL_STOP_SECONDS`.

    **On Windows it is not, and that is stated rather than pretended.** There is no way
    for an unrelated process to ask a console process to exit: ``taskkill`` without
    ``/F`` posts ``WM_CLOSE``, which a windowless console process never receives, and a
    console control event can only be sent within a process group. So the stop is
    forced, and two things make that acceptable rather than merely unavoidable:

    -   **The store survives it.** WAL plus ``synchronous=FULL`` means a committed
        transaction is on disk before the commit returns, and an uncommitted one is
        rolled back when the database is next opened. A killed server loses no write
        that any client was told had happened.
    -   **The client rides it out.** A refused connection is retried on a bounded
        backoff keyed on the same ``operation_id``, so a request caught by the kill is
        re-sent rather than lost, and the ledger replays rather than writing twice.

    What is genuinely lost on Windows is the ``PRAGMA optimize`` and the WAL checkpoint
    the lifespan would have run, which cost the next start a few milliseconds.

    Returns True when the port is free afterwards.
    """
    import platform
    import signal
    import subprocess
    import time

    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
    else:
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + GRACEFUL_STOP_SECONDS
    while time.monotonic() < deadline:
        if _find_process_by_port(port) is None:
            return True
        time.sleep(0.1)

    if platform.system() != "Windows":
        # Guarded rather than annotated: `signal.SIGKILL` does not exist on Windows,
        # so naming it unconditionally is an AttributeError at import on the platform
        # this repository is developed on.
        with suppress(ProcessLookupError):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.2)
    return _find_process_by_port(port) is None


@app.command()
def stop(
    port: int = typer.Option(8765, help="Port number of server to stop."),
) -> None:
    """Stop the running web server."""
    import subprocess

    pid = _find_process_by_port(port)

    if pid is None:
        typer.echo(f"No server found running on port {port}.")
        return

    typer.echo(f"Stopping server (PID {pid}) on port {port}...")

    try:
        if _stop_server(pid, port):
            typer.echo("✓ Server stopped successfully.")
        else:
            typer.echo(f"Server on port {port} is still listening.", err=True)
            raise typer.Exit(1)
    except (subprocess.CalledProcessError, OSError) as e:
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
    import subprocess

    host = _validated_bind_host(host)
    # Stop existing server if running
    pid = _find_process_by_port(port)
    if pid is not None:
        typer.echo(f"Stopping existing server (PID {pid})...")
        try:
            # Drained rather than killed where the platform allows it, so a client
            # mid-request rides the restart out instead of losing the write. See
            # `_stop_server` for what Windows can and cannot promise here.
            if _stop_server(pid, port):
                typer.echo("✓ Server stopped.")
            else:
                typer.echo("Warning: the old server is still listening.", err=True)
        except (subprocess.CalledProcessError, OSError):
            typer.echo("Warning: Failed to stop existing server.", err=True)

    # Start new server
    typer.echo(f"🚀 Starting AgentJobs server at http://{host}:{port}")
    _warn_if_bundle_missing()
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
    """Open the primary React UI in a browser, starting the server if needed.

    A listening socket is not a served application, and this command used to treat the
    two as the same thing: it waited for the port, opened a browser, and exited 0
    whatever was -- or was not -- behind it. Three different failures reached the user
    as a JSON error in a tab something had already opened. So the port is now the
    beginning of the check rather than the end of it, and each of those failures is
    reported at the terminal that asked, with a non-zero exit:

    * nothing answers -- including a server that refused to start, whose reason is
      captured rather than lost to a console with no window;
    * something answers and is not AgentJobs, which is a reused port;
    * AgentJobs answers and has no frontend bundle, which is a clone that has never run
      `npm run build` (see `spa.bundle_status`).
    """
    import platform
    import subprocess
    import sys
    import tempfile
    import time
    import webbrowser

    host = _validated_bind_host(host)
    server_url = f"http://{host}:{port}"
    app_url = f"{server_url}/app/"
    pid = _find_process_by_port(port)
    startup_log: Optional[Path] = None

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

        # The child's stderr goes to a file rather than to a console nobody can see.
        # A refusal to start -- the wrong-checkout banner is the one that costs a
        # forensic session -- is the single most useful thing this command can report,
        # and until now it was written to a window created with CREATE_NO_WINDOW.
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed after the probe
            mode="w+",
            encoding="utf-8",
            prefix="agentjobs-serve-",
            suffix=".log",
            delete=False,
        )
        startup_log = Path(handle.name)
        with handle:
            if platform.system() == "Windows":
                # Start server in a new window (minimized)
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=handle,
                    creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
                )
            else:
                # Start server in background
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=handle,
                )

        # Wait for server to start
        typer.echo("Waiting for server to initialize...")
        max_retries = 10
        for _ in range(max_retries):
            time.sleep(1)
            if _find_process_by_port(port) is not None:
                break
    else:
        typer.echo(f"Server already running (PID {pid})")

    probe = probe_api_base(server_url)

    if not probe.answered:
        typer.echo(f"Nothing is serving AgentJobs at {server_url} ({probe.detail}).", err=True)
        _echo_startup_log(startup_log)
        raise typer.Exit(1)

    if not probe.is_agentjobs:
        typer.echo(
            f"Something other than AgentJobs is listening on port {port} "
            f"({probe.detail}). Choose another port with --port, or stop that service.",
            err=True,
        )
        raise typer.Exit(1)

    payload = probe.payload or {}
    if payload.get("frontend_bundle") == "missing":
        from .api.spa import bundle_status

        typer.echo(
            bundle_status() or "The React frontend bundle is missing.",
            err=True,
        )
        typer.echo(
            f"The API is up at {server_url}; only /app/ is unavailable. " "No browser was opened.",
            err=True,
        )
        raise typer.Exit(1)

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
    ready: bool = typer.Option(
        False,
        "--ready",
        help="Promote the new task to ready immediately, making it claimable.",
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Who is creating. Defaults to the project's default_user. Used by --ready.",
    ),
) -> None:
    """Create a new task as a draft, or as ready with --ready.

    A task is born `draft` and a draft is not claimable, which is the correct default
    for a record whose spec is still being written -- but it surprised anyone who ran
    `create` and then `list --lifecycle ready` and saw nothing. `--ready` is the
    one-command form for a task that needs no drafting; `agentjobs promote` is the
    same step taken later.
    """
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    manager = _build_manager(base_dir)

    title = title or typer.prompt("Title")
    description = (
        description if description is not None else typer.prompt("Description", default="")
    )

    # No id is reserved in advance when the server owns them: it allocates one inside
    # the transaction that creates the task, which is the only place two concurrent
    # creates cannot pick the same number.
    task_id = id
    if task_id is None and getattr(manager.storage, "supports_task_files", True):
        task_id = manager.storage.generate_task_id()
    task = manager.create_task(
        id=task_id,
        title=title,
        description=description,
        priority=priority,
        category=category,
    )
    if ready:
        manager.promote_task(task.id, actor=_resolve_actor(config, actor))
        typer.echo(f"✅ Created {task.id}.yaml (ready — claimable now)")
    else:
        typer.echo(
            f"✅ Created {task.id}.yaml (draft — not claimable until "
            f"`agentjobs promote {task.id}`)"
        )


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
    storage_dir: Optional[str] = typer.Option(
        None,
        help="Directory for task storage. Defaults to the project's configured tasks_directory.",
    ),
) -> None:
    """Interactive agent workflow: get task, display prompt, mark complete.

    The default storage directory is the project's configured ``tasks_directory``, the
    same one every other command resolves through ``_build_manager``. It used to be a
    literal ``./tasks``, so in a project that configures anything else -- this
    repository configures ``tasks/agentjobs`` -- ``work`` reported "No tasks available"
    from an empty directory it had just created, while ``next`` answered correctly.
    """
    base_dir = Path.cwd()
    if storage_dir is None:
        manager = _build_manager(base_dir)
    else:
        target_dir = Path(storage_dir)
        if not target_dir.is_absolute():
            target_dir = base_dir / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        manager = TaskManager(TaskStorage(target_dir))

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
    """List registered projects, and which of them are local to this machine.

    The CLI runs as the person at the machine and sees every project regardless, so
    ``local-only`` here is a report rather than a restriction. It is the one place the
    setting is visible without opening five config files, which is what an operator
    needs after marking a project local: a way to check it took.
    """
    projects = ProjectRegistry().list_projects()
    if not projects:
        typer.echo("No projects registered. Run 'agentjobs project add <path>'.")
        return
    for project in projects:
        notes = []
        if not project.root.is_dir():
            notes.append("missing")
        if visibility_of(project.load_config()) is Visibility.LOCAL:
            notes.append("local-only")
        suffix = f"  [{', '.join(notes)}]" if notes else ""
        typer.echo(f"{project.id:20} {project.name:30} {project.root}{suffix}")


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


@project_app.command("mcp-setup")
def project_mcp_setup(
    path: str = typer.Argument(".", help="Project directory to wire up."),
    url: Optional[str] = typer.Option(
        None, "--url", help="Where AgentJobs is listening. Defaults to this machine's answer."
    ),
) -> None:
    """Declare the AgentJobs MCP server in a project's own `.mcp.json`.

    `agentjobs init` does this for a new project. This command exists for the ones
    registered before it did, for a checkout that never had the file, and for a project
    whose recorded address stopped being true -- all of which present the same way: an
    agent working there comes up with no AgentJobs tools and quietly falls back to the
    CLI.

    An existing `agentjobs` entry is reported and left alone; correcting one means
    deleting it first, deliberately, rather than having a command overwrite a pinned
    interpreter or port on your behalf.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        typer.secho(f"Not a directory: {root}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if url:
        base_url, guessed = url.strip().rstrip("/"), False
    else:
        config = _load_config(root)
        port = int(config.get("gui", {}).get("port") or 8765)
        base_url, guessed = _mcp_base_url(port)

    try:
        written = ensure_mcp_server_entry(root, base_url)
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if written is None:
        typer.echo(
            f"ℹ️  {root / MCP_CONFIG_FILENAME} already declares an 'agentjobs' "
            "server; nothing was changed."
        )
        return
    typer.echo(f"✅ Wrote {written}, pointing at {base_url}.")
    typer.echo("   Start a new agent session there; MCP servers are resolved at session start.")
    if guessed:
        typer.echo(
            "   That port is a guess from the project's own config. If AgentJobs serves "
            f"elsewhere, set 'api_base' in {default_home() / 'dispatch.yaml'} or pass --url."
        )


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
    group: Optional[str] = typer.Option(
        None,
        "--group",
        help="Runner group name from ~/.agentjobs/dispatch.yaml. Not with --runner.",
    ),
) -> None:
    """Allow dispatch for one project, using a runner or group this machine defines."""
    try:
        ProjectRegistry().get(project_id)
        settings = set_project_enabled(project_id, True, runner=runner, group=group)
    except (ProjectError, DispatchError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    against = f"group '{settings.group}'" if settings.group else f"runner '{settings.runner}'"
    typer.echo(f"✅ Dispatch enabled for '{project_id}' using {against}.")
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
    group: Optional[str] = typer.Option(
        None,
        "--group",
        help="Runner group to pick from, overriding the project's. Must already exist.",
    ),
    posture: Optional[str] = typer.Option(
        None,
        "--posture",
        help=(
            "Posture for this run only, overriding the project default and the task's "
            "own field. Refused above the project's max_posture."
        ),
    ),
) -> None:
    """Start an agent on a task, if every gate permits it.

    **There is no signed-in user here, so this path is unchanged by task-188.** The
    browser's Dispatch button names the person clicking it and the server writes their
    authorising entry before the run; a shell has nobody to name, and inventing one --
    the project's ``default_user``, or ``$USER`` -- would put a signature on the record
    that no person put there. So this command keeps the original rule: the task's newest
    log entry (or the one ``--caused-by`` names) must have been written by a configured
    human, and a task whose newest entry is an agent's is refused with
    ``not_human_clocked``.

    To satisfy it, write the entry as yourself first -- the ``Add a note`` control on
    the task page, or the MCP ``task_log_append`` tool with your own actor id -- then
    dispatch. Giving the CLI a ``--as`` flag was considered and left alone: it is the
    same trust model the HTTP path already has, but it is a new surface for authorising
    runs and nothing currently needs it.
    """
    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    manager = dispatch_manager_for(project)
    try:
        chosen_posture = Posture(posture) if posture else None
    except ValueError:
        names = ", ".join(sorted(item.value for item in Posture))
        typer.secho(f"--posture must be one of {names}, not {posture!r}.", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(
                task_id=task_id, caused_by=caused_by, group=group, posture=chosen_posture
            ),
        )
    except (DispatchError, DispatchRunError) as exc:
        reason = getattr(exc, "reason", "dispatch_failed")
        typer.secho(f"Refused ({reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ Dispatched {task_id} as run {handle.run_id} ({handle.mode.value}).")
    # Always, not only when something overrode the default: "which of the three sources
    # decided what this run may do" is the question task-308 exists so that nobody has
    # to reconstruct, and a line that appears only sometimes trains a reader to skim it.
    if handle.posture is not None:
        typer.echo(f"   Envelope: {handle.posture.describe()}.")
    # Printed because a wrong address is otherwise silent: the agent cannot read its
    # task, so it cannot report that it could not read its task. There is no HTTP
    # request here to derive one from, so this is AGENTJOBS_API_BASE, or `api_base:` in
    # ~/.agentjobs/dispatch.yaml, or the fallback -- and which one it landed on is worth
    # seeing at the moment you spend money on the run.
    typer.echo(f"   Agent told AgentJobs is at {handle.api_base}.")
    if handle.group:
        typer.echo(f"   Runner '{handle.runner}', chosen from group '{handle.group}'.")
    if handle.session_id:
        typer.echo(f"   Session {handle.session_id} — the CLI assigned that id, not us.")
    typer.echo(f"   Run directory: {handle.directory.path}")
    if handle.mode is DispatchMode.BATCH:
        # A session belongs to its CLI's own session manager, but a batch process needs
        # this process's supervisor to write its terminal record.  Returning here used
        # to end the daemon thread with the CLI, leaving a completed child permanently
        # marked running until a server restart misclassified it as interrupted.
        assert handle.supervisor is not None
        typer.echo("   Waiting for the batch run to record its outcome.")
        handle.supervisor.join()


@dispatch_app.command("child")
def dispatch_child(
    task_id: str = typer.Argument(..., help="Child task to start, on its epic's authority."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
) -> None:
    """Start one child of an active epic, on the authorisation a human gave the epic.

    The rule that every dispatch traces to a human act is unchanged and is still what
    runs here (design section 2). What changes is which task the human clicked: they
    clicked the epic, and its children were named on its record at the moment they did.
    This command finds that person's entry on the parent, writes an authorising entry on
    the child naming them, the parent and the entry, and dispatches on the stored row
    like anything else.

    It refuses a child whose parent is not active, whose parent nobody human ever
    authorised, or which has already spent its attempts under that authorisation -- two,
    the first run and one retry. Re-authorising the epic starts a fresh budget, and
    leaves a record that somebody chose to.

    ``dispatch walk`` is this in a loop and is what an unattended epic should use. Reach
    for this one when you want a single child started and to watch it yourself.
    """
    from agentjobs.dispatch.epic import EpicError
    from agentjobs.models_v2 import DispatchTrigger

    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    manager = dispatch_manager_for(project)
    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(
                task_id=task_id,
                trigger=DispatchTrigger.CHILD,
                on_behalf_of_parent=True,
            ),
        )
    except EpicError as exc:
        typer.secho(f"Refused ({exc.reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except (DispatchError, DispatchRunError) as exc:
        reason = getattr(exc, "reason", "dispatch_failed")
        typer.secho(f"Refused ({reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ Dispatched {task_id} as run {handle.run_id} ({handle.mode.value}).")
    typer.echo(f"   Agent told AgentJobs is at {handle.api_base}.")
    typer.echo(f"   Run directory: {handle.directory.path}")
    if handle.mode is DispatchMode.BATCH:
        # The same join `dispatch run` makes, for the same reason: a batch run is watched
        # by a thread in *this* process, so returning here would end that thread with the
        # CLI and leave a finished run reading `running` for ever. `dispatch walk` needs
        # no equivalent -- it stays in this process until the child is settled anyway --
        # and this command was leaking one run per invocation until it was noticed by a
        # second `dispatch child` being refused `live_run_exists` by the first.
        assert handle.supervisor is not None
        typer.echo("   Waiting for the batch run to record its outcome.")
        handle.supervisor.join()


@dispatch_app.command("walk")
def dispatch_walk(
    parent_id: str = typer.Argument(..., help="Active epic whose children should be worked."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    poll_seconds: float = typer.Option(
        None, "--poll", help="Seconds between reads of the running child's task record."
    ),
    child_hours: float = typer.Option(
        None, "--child-hours", help="Backstop: give up on one child after this long."
    ),
    max_children: Optional[int] = typer.Option(
        None, "--max-children", help="Stop after starting this many, however many remain."
    ),
    max_concurrent: Optional[int] = typer.Option(
        None,
        "--max-concurrent",
        help=(
            "Children in flight at once. Defaults to this machine's "
            "limits.max_concurrent_runs, and can only narrow it."
        ),
    ),
    posture: Optional[str] = typer.Option(
        None,
        "--posture",
        help=(
            "Posture to start every child of this walk at, overriding what the epic "
            "would pass down. Refused above the project's max_posture."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Say what would be walked and in what order; start nothing."
    ),
) -> None:
    """Fly an epic's independent children in parallel, grounding the fleet on a bad one.

    Every child whose dependencies are satisfied and which is not already running is
    started, up to this machine's concurrent-run ceiling; each is watched to a finish
    through its own task record and judged. A child that closed ``completed`` ran its own
    objective gate and its own merge. Anything else stops all further takeoffs -- a child
    is never skipped, because a sibling that depended on it would then be building on a
    gap with nobody awake to notice -- while children already in flight are watched down
    rather than killed, none of them being able to depend on the one that failed.

    **Takeoff and landing are different resources.** Children work in parallel because
    their worktrees are independent, and merge one at a time because ``main`` is not:
    each queues for the repository's merge runway inside its own finish, so the commit
    that lands is always the commit its gate verified. ``--max-concurrent`` narrows the
    fleet; nothing widens it past ``limits.max_concurrent_runs``.

    **It never closes the parent.** No open child remaining is not the same as the
    parent's acceptance criteria being met, and that judgement is the one step of this
    loop that is not mechanical. The walk writes what every child did onto the parent and
    hands back; you decide.

    **Children inherit the epic's posture without being told to** (task-316): if a
    person dispatched this parent at a posture, that is what its children are started
    at, and the line printed before the walk says so. ``--posture`` is for the case
    where there is nothing to inherit -- a walk run from a shell against an epic nobody
    dispatched -- and it wins over the inherited value when both exist. It is refused
    above the project's ``max_posture``, exactly as ``dispatch --posture`` is.

    Exit 0 means every open child is done. Exit 1 means it stopped for cause, the parent
    holds the reason, and the parent's ball is with a human. Exit 2 means it could not
    start at all.
    """
    from agentjobs.dispatch.epic import (
        WalkSettings,
        EpicError,
        describe_settings,
        frontier,
        inherited_posture,
        open_children,
        walk_epic,
        walk_handoff_prompt,
        walk_report,
    )
    from agentjobs.models_v2 import Ball, BallReason, LogEntryType

    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    manager = dispatch_manager_for(project)
    parent = manager.get_task(parent_id)
    if parent is None:
        typer.secho(f"No task {parent_id!r} in project {project.id!r}.", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    try:
        chosen_posture = Posture(posture) if posture else None
    except ValueError:
        names = ", ".join(sorted(item.value for item in Posture))
        typer.secho(f"--posture must be one of {names}, not {posture!r}.", fg=typer.colors.RED)
        raise typer.Exit(code=2) from None

    # The walk's own writes are an agent's, not a human's, so they are attributed to the
    # identity this project's runner claims tasks as -- the same one the children will
    # write under. `default_user` would be wrong in the way that matters: it would put a
    # person's name on an entry no person wrote.
    try:
        resolution = assert_dispatch_permitted(project.id)
    except DispatchError as exc:
        typer.secho(
            f"Refused ({getattr(exc, 'reason', 'dispatch_refused')}): {exc}", fg=typer.colors.RED
        )
        raise typer.Exit(code=2) from exc
    actor = resolution.runner.actor_id
    # Named explicitly rather than left to a default. This is the home whose ledger the
    # walk reads to tell a child that died from one that is thinking, and it has to be
    # the same home the children's runs are written to.
    config_path = resolution.config.path
    home = config_path.parent if config_path is not None else default_home()

    settings = WalkSettings()
    if poll_seconds is not None:
        settings.poll_seconds = poll_seconds
    if child_hours is not None:
        settings.child_timeout_seconds = child_hours * 3600.0
    settings.max_children = max_children
    # **The machine's ceiling is the default and the maximum, not a second number to keep
    # in step with** (task-223). A child is an ordinary dispatch and is counted against
    # `limits.max_concurrent_runs` by `dispatch_task`, so a walk allowed more than that
    # would simply be refused a slot -- which the walk now treats as backpressure and
    # waits on, but which would still mean this flag promised something the machine had
    # already decided against. `--max-concurrent` narrows it and cannot widen it.
    ceiling = resolution.limits.max_concurrent_runs
    settings.max_concurrent = min(max_concurrent, ceiling) if max_concurrent else ceiling

    remaining = open_children(manager, parent.id)
    typer.echo(f"Walking {parent.id}: {parent.title}")
    typer.echo(f"  open children: {', '.join(c.id for c in remaining) or 'none'}")
    for line in describe_settings(
        settings, posture=chosen_posture, inherited=inherited_posture(parent)
    ):
        typer.echo(f"  {line}")

    if dry_run:
        # The frontier as it stands *now*, and no further. Which children become eligible
        # after these land depends on what they do to the dependency graph, so printing a
        # whole running order the walk has not committed to would be a prediction dressed
        # as a plan. What is printed is exactly what would take off on the next tick.
        upcoming = frontier(manager, parent.id)[: settings.max_concurrent]
        typer.echo(
            f"  starting now: {', '.join(child.id for child in upcoming) or 'nothing claimable'}"
        )
        typer.secho("Dry run: nothing was started.", fg=typer.colors.YELLOW)
        return

    try:
        result = walk_epic(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            parent_id=parent.id,
            home=home,
            settings=settings,
            posture=chosen_posture,
            on_event=lambda message: typer.echo(f"  {message}"),
        )
    except EpicError as exc:
        typer.secho(f"Refused ({exc.reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    except DispatchError as exc:
        typer.secho(
            f"Refused ({getattr(exc, 'reason', 'dispatch_failed')}): {exc}", fg=typer.colors.RED
        )
        raise typer.Exit(code=2) from exc

    # Written whichever way it ended, and written before anything is printed: if this
    # process dies in the next second the record still says what the walk did.
    manager.add_log_entry(
        parent.id,
        actor=actor,
        type=LogEntryType.PROGRESS,
        body=walk_report(result),
    )
    typer.echo("")
    typer.echo(result.summary())

    if result.stop.is_success:
        typer.secho(
            "✅ Every open child is done. The parent is still open on purpose: evaluate "
            "its acceptance criteria against the children's evidence and close it "
            "yourself.",
            fg=typer.colors.GREEN,
        )
        return

    refreshed = manager.get_task(parent.id)
    if refreshed is not None and refreshed.is_open and refreshed.ball is not Ball.HUMAN:
        manager.handoff(
            parent.id,
            actor=actor,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt=walk_handoff_prompt(result),
        )
    typer.secho(
        f"Stopped: {result.stop.value}. The parent holds the reason and its ball is with "
        "a human.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@dispatch_app.command("example")
def dispatch_example(
    write: bool = typer.Option(
        False,
        "--write",
        help="Write it to ~/.agentjobs/dispatch.yaml. Refuses if anything is there.",
    ),
) -> None:
    """Show a starting dispatch.yaml, with runner groups and every option commented.

    Prints by default. This is the only route by which AgentJobs will put a dispatch
    config on disk, it happens only when you type --write, and it refuses to overwrite:
    a config that appeared on its own would defeat the whole reason this file is the
    record of what may execute here.

    What it writes is switched off at every level -- no master switch, no projects -- so
    it cannot leave a machine able to dispatch that was not able to before.
    """
    if not write:
        typer.echo(EXAMPLE_CONFIG)
        return

    try:
        path = write_example_config()
    except DispatchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ Wrote a starting dispatch config to {path}.")
    typer.secho(
        "   It is switched off. Edit the runners to match this machine, then set "
        "'enabled: true'.",
        fg=typer.colors.YELLOW,
    )


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


@dispatch_app.command("auth-check")
def dispatch_auth_check(
    session_id: Optional[str] = typer.Argument(
        None, help="One session id. Omit to check every live session run."
    ),
) -> None:
    """Say whether a session died on an expired login. Exits 1 when one has.

    The state this answers for is invisible everywhere else: a session killed by an
    expired credential ends its turn and reports `idle`/`done`, exactly like one that
    finished its work (task-224). The dispatch poller checks this by itself for runs it
    started -- this command is for the children an agent supervisor starts, which are
    that supervisor's own subprocesses and appear in no ledger.

    The exit code is the point. A supervisor's watch loop can branch on it without
    parsing anything, and the branch matters: an auth-stalled child looks exactly like a
    child that died, and restarting it is the one response guaranteed not to work.
    """
    if session_id:
        checked = [(session_id, read_auth_stall(session_id))]
    else:
        checked = [
            (record.session_id, read_auth_stall(record.session_id, since=record.started_at))
            for record in live_runs(default_home())
            if record.is_session and record.session_id
        ]

    if not checked:
        typer.echo("No live session runs to check.")
        return

    stalled = [(name, stall) for name, stall in checked if stall is not None]
    for name, stall in checked:
        if stall is None:
            typer.echo(f"✅ {name}: no expired login in its transcript.")
        else:
            typer.secho(
                f"⛔ {name}: stopped on an expired login at {stall.at.isoformat()}.",
                fg=typer.colors.RED,
            )
            typer.echo(f"   {stall.log_path}")
    if stalled:
        typer.secho(
            "Check which failure this is before logging in. In a fresh process, not "
            "inside a stalled session, run:\n"
            '  claude -p "Reply with exactly: AUTH_OK"\n'
            "If it answers, the credential is fine and the sessions are merely stuck -- "
            "wake each with a message and skip the login. If it fails to authenticate, "
            "run `claude auth login` first, then wake them. Either way they resume in "
            "place.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)


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
    """Remove the job state of finished sessions, freeing the pids they still hold.

    A run that has ended still occupies a row in the session manager's ledger. This is
    the command that clears it.

    It no longer removes worktrees, and the change is deliberate (task-186): dispatch
    stopped passing `-w`, so a dispatched session owns no worktree. The one a dispatched
    agent makes for itself is the agent's to remove, and `git worktree list` is the
    inventory. See `DispatchLedger.reap`.

    A reap that is **refused** is reported rather than forced -- passing `-f` here would
    delete exactly the thing worth keeping, in the case where a session AgentJobs did not
    start does own a worktree with work in it.
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
        typer.echo(f"\n{kept} session(s) not removed. Read why above before reaping again.")


def _report_agent_address() -> None:
    """Say what a CLI dispatch would tell an agent, and whether anything is there.

    This is a status command's whole job for this value, and it did not report it at
    all until task-193 -- so the only place the address was ever shown was
    ``dispatch run``, one line after the money was spent. A wrong one is invisible
    everywhere else by construction: an agent that cannot reach AgentJobs cannot report
    that it cannot reach AgentJobs.

    The probe is not optional here. Printing an address flatly is what this command
    already did by printing nothing, and an address a reader cannot check is an address
    a reader assumes is fine.
    """
    resolved = resolve_api_base_detail(None)
    probe = probe_api_base(resolved.value)
    typer.echo(f"Agent address:  {resolved.value}  ({resolved.describe_source()})")
    if probe.answered and probe.is_agentjobs:
        typer.echo(f"                {probe.detail}")
        return
    if probe.answered:
        typer.secho(
            f"                ⚠️  {probe.detail}. Something is listening there and it "
            "is not this application.",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(
        f"                ❌ {probe.detail}. A dispatch from this shell is refused "
        "until it does; a run given this address would go quiet rather than fail.",
        fg=typer.colors.RED,
    )


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
    _report_agent_address()

    if config is None:
        return

    typer.echo("\nRunners:")
    if not config.runners:
        typer.echo("  none defined")
    for name, runner in sorted(config.runners.items()):
        typer.echo(f"  {name:12} {runner.mode.value:8} {runner.argv}")

    if config.runner_groups:
        typer.echo("\nRunner groups:")
        for name, group in sorted(config.runner_groups.items()):
            marker = "  (machine default)" if name == config.default_group else ""
            typer.echo(f"  {name}{marker}")
            if group.description:
                typer.echo(f"    {group.description}")
            for member in group.members:
                state = "on " if member.enabled else "off"
                note = f"  # {member.note}" if member.note else ""
                typer.echo(f"    [{state}] {member.runner}{note}")

    typer.echo("\nProjects:")
    if not config.projects:
        typer.echo("  none configured")
    for pid, settings in sorted(config.projects.items()):
        state = "enabled " if settings.enabled else "disabled"
        against = (
            f"group={settings.group}" if settings.group else f"runner={settings.runner or '-'}"
        )
        typer.echo(
            f"  {pid:20} {state}  {against}  "
            f"posture={settings.posture.value}  "
            f"max_posture={settings.ceiling.value}  "
            f"merge={settings.posture.merge_policy.value}  push={settings.push}  "
            f"finish={'on' if settings.finish.enabled else 'off'}  "
            f"finish_escalation="
            f"{'dispatches' if settings.finish.dispatch_on_escalation else 'parks'}  "
            f"clean_tree={settings.require_clean_tree}  auto={settings.auto_dispatch}"
        )

    limits = config.limits
    typer.echo(
        f"\nLimits:         max_concurrent_runs={limits.max_concurrent_runs}  "
        f"run_timeout_seconds={limits.run_timeout_seconds}  "
        f"session_stale_seconds={limits.session_stale_seconds}  "
        f"session_stall_seconds={limits.session_stall_seconds}"
    )
    typer.echo(
        f"Auto-dispatch:  per_task_per_day={limits.auto.per_task_per_day}  "
        f"per_task_lifetime={limits.auto.per_task_lifetime}  "
        f"cooldown_seconds={limits.auto.cooldown_seconds}"
    )

    if project_id:
        # Reported whether or not dispatch is permitted right now: an agent woken after
        # an approval needs to know whether the scripted finish already ran the merge,
        # and that question is independent of whether a new run could start (task-305).
        named = config.projects.get(project_id)
        finish_state = "off" if named is None else ("on" if named.finish.enabled else "off")
        typer.echo(f"\n{project_id}: finish={finish_state}")

        try:
            resolution = assert_dispatch_permitted(project_id)
        except DispatchError as exc:
            typer.secho(f"{project_id}: refused ({exc.reason}) - {exc}", fg=typer.colors.YELLOW)
        else:
            chosen = resolution.selection
            via = f" from group '{chosen.group}' ({chosen.source.value})" if chosen else ""
            typer.echo(
                f"{project_id}: permitted - runner '{resolution.runner.name}'{via} "
                f"({resolution.runner.mode.value}), posture "
                f"{resolution.settings.posture.value} (max "
                f"{resolution.settings.ceiling.value}), merge "
                f"{resolution.settings.posture.merge_policy.value}, push "
                f"{resolution.settings.push}"
            )
            if chosen:
                for candidate in chosen.candidates:
                    if candidate.skipped_because is not None:
                        detail = f" - {candidate.detail}" if candidate.detail else ""
                        typer.echo(
                            f"    skipped {candidate.runner}: "
                            f"{candidate.skipped_because.value}{detail}"
                        )


run_app = typer.Typer(
    name="run",
    help="Tell AgentJobs about a session it did not start.",
)
app.add_typer(run_app)


@run_app.command("register")
def run_register(
    task_id: str = typer.Option(..., "--task", help="Task this session is working."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    session_id: Optional[str] = typer.Option(
        None,
        "--session",
        help="Session id, when the runner's CLI does not publish one to its own session.",
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Identity to write the note as. Defaults to the project runner's actor.",
    ),
) -> None:
    """Put this session in the run ledger so the stall protections reach it.

    Every one of them -- the permission park, the auth-expiry handoff, the settle on a
    session that finished without handing off, the stall report -- is keyed on a run
    record, and a session nobody dispatched has none. This writes one, and the poller
    then follows the session exactly as it follows a dispatched run.

    **Safe to run unconditionally**, which is the point: a dispatched session recognises
    itself from ``AGENTJOBS_RUN_ID``, says it is already known, and writes nothing. So
    ALLAGENTS.md can say "register" with no exception for an agent to evaluate.

    It refuses rather than guessing when the claim cannot be checked -- an unknown or
    dead session id, a session listed under a different root, an interactive session a
    person is sitting in. A run record naming a session nothing can follow is worse than
    none, because it looks covered.
    """
    from .dispatch.registration import Registration, register_session, registration_lines

    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    manager = task_manager_for(project)
    try:
        result: Registration = register_session(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            task_id=task_id,
            session_id=session_id,
            actor=actor,
        )
    except DispatchError as exc:
        reason = getattr(exc, "reason", "registration_refused")
        typer.secho(f"Refused ({reason}): {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    for line in registration_lines(result):
        typer.echo(line)


storage_app = typer.Typer(
    name="storage",
    help="Move a project's tasks into the server's database, and back out again.",
)
app.add_typer(storage_app)


def _storage_project(project_id: Optional[str]) -> Project:
    """Resolve the project a storage command acts on, or exit naming the ambiguity."""
    registry = ProjectRegistry()
    try:
        return registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


def _refuse_while_serving(port: int) -> None:
    """Stop before touching the store if a server is holding it.

    Quiescing the writers is the operator's act -- stop the server, stop the agents --
    and this checks the one part that can be checked rather than assumed. A second
    process opening the database is not corruption, because SQLite is safe under it,
    but it is a second writer during a migration, and the import's guarantee that an
    interruption leaves nothing behind is about its own transaction, not somebody
    else's.
    """
    pid = _find_process_by_port(port)
    if pid is None:
        return
    typer.secho(
        f"A server is listening on port {port} (PID {pid}). Stop it first: "
        f"'agentjobs stop --port {port}'. This command holds the database as the "
        "single writer, and would be a second one.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@storage_app.command("status")
def storage_status(
    project_id: Optional[str] = typer.Option(None, "--project", help="One project, or all."),
) -> None:
    """Say where each project's tasks actually are, counted rather than assumed."""
    registry = ProjectRegistry()
    projects = [_storage_project(project_id)] if project_id else registry.list_projects()
    if not projects:
        typer.echo("No projects are registered.")
        return

    settings = load_storage_settings()
    known = "" if settings.database.exists() else " (none yet)"
    typer.echo(f"default database: {settings.database}{known}")
    typer.echo(
        f"configuration: {settings.path}" + ("" if settings.path.exists() else " (none yet)")
    )
    typer.echo("")
    for line in cutover_status(projects, settings=settings):
        rows = "-" if line.rows is None else str(line.rows)
        files = "-" if line.files is None else str(line.files)
        typer.echo(f"{line.project_id}: {line.backend}  rows={rows} files={files}")
        # Where this project's records are is now a per-project answer, so it is printed
        # per project rather than once at the top (task-400).
        missing = "" if line.database_exists else " (none yet)"
        typer.echo(f"    database {line.database}{missing}")
        if line.shared_with:
            typer.echo(
                f"    shares that file with {', '.join(line.shared_with)} -- "
                f"'agentjobs storage split --project {line.project_id}' gives it one of "
                "its own"
            )
        if line.cutover_at:
            typer.echo(f"    cut over {line.cutover_at} from {line.source}")
        if line.backend == FILES:
            # A new project is created on the database (task-399), so a project still
            # on files is one registered before that and never migrated. Saying so here
            # is the "told to import" half of not breaking it silently: it keeps working
            # until the file backend goes, and this is where an operator looks.
            typer.echo(
                f"    still on task files — 'agentjobs storage cutover --project "
                f"{line.project_id}' moves it, and a new project starts on the database"
            )


@storage_app.command("preview")
def storage_preview(
    project_id: Optional[str] = typer.Option(None, "--project"),
    backfill_git: bool = typer.Option(
        False, "--backfill-git", help="Also mine the task files' git history."
    ),
    allow_quotations: bool = typer.Option(
        False,
        "--allow-quotations",
        help="Import records that quote a person verbatim, naming every one.",
    ),
) -> None:
    """Run the real import against a throwaway database and report what would happen.

    Writes nothing that survives the command. This is where a malformed record, a
    quoted remark or a history that will not reconcile is found, and finding it here
    costs nothing.
    """
    project = _storage_project(project_id)
    try:
        imported, verified = cutover_preview(
            project,
            backfill_git=backfill_git,
            enforce_quotation_policy=not allow_quotations,
        )
    except QuotationPolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        typer.echo("\nFix with 'agentjobs redact', or re-run with --allow-quotations.")
        raise typer.Exit(code=1) from exc

    typer.echo(imported.render())
    typer.echo("")
    typer.echo(verified.render())
    if not verified.ok:
        raise typer.Exit(code=1)


@storage_app.command("cutover")
def storage_cutover(
    project_id: Optional[str] = typer.Option(None, "--project"),
    port: int = typer.Option(8765, help="Port to check for a running server."),
    replace: bool = typer.Option(
        False, "--replace", help="Empty this project in the database and import it again."
    ),
    backfill_git: bool = typer.Option(
        True, "--backfill-git/--no-backfill-git", help="Mine the task files' git history."
    ),
    allow_quotations: bool = typer.Option(False, "--allow-quotations"),
    reporting_tz: str = typer.Option(
        "UTC", "--reporting-tz", help="IANA zone name for day bucketing, e.g. America/Chicago."
    ),
    database: Optional[Path] = typer.Option(
        None, "--database", help="Put this project's records in this file."
    ),
    shared_database: bool = typer.Option(
        False,
        "--shared-database",
        help="Use the machine's default database instead of a file of this project's own.",
    ),
) -> None:
    """Back up, import, verify, and only then make the database authoritative.

    ``--backfill-git`` is on by default here and nowhere else: the backfill reads the
    git history of the task files, so it must happen before those files are retired,
    and the cutover is the last moment anybody is looking.

    The project gets a database of its own unless you say otherwise, so a shared file is
    something you asked for rather than what happened by accident.
    """
    project = _storage_project(project_id)
    _refuse_while_serving(port)
    if database is not None and shared_database:
        typer.secho(
            "--database and --shared-database name two different files. Pick one.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    try:
        result = cut_over(
            project,
            replace=replace,
            backfill_git=backfill_git,
            enforce_quotation_policy=not allow_quotations,
            reporting_tz=reporting_tz,
            database=database,
            shared_database=shared_database,
        )
    except QuotationPolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        typer.echo("\nNothing was imported. Fix with 'agentjobs redact', then run this again.")
        raise typer.Exit(code=1) from exc
    except CorpusAlreadyImported as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if result.backup:
        typer.echo(f"backed up to {result.backup}")
    typer.echo(result.imported.render())
    typer.echo("")
    typer.echo(result.verified.render())
    if not result.ok:
        typer.secho(
            "\nNothing was switched: the project is still served from its files. The "
            "import is in the database for inspection; re-run with --replace once the "
            "cause is fixed.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    typer.secho(f"\n{project.id} is now served from {result.database}.", fg=typer.colors.GREEN)
    typer.echo("Start the server, then check the dashboard before retiring anything:")
    typer.echo(f"    agentjobs serve --port {port}")
    typer.echo(
        "Enrol this database in your backups now -- 'agentjobs storage backup' writes a "
        "verifiable snapshot, and it is the only copy of the reconstructed history."
    )


@storage_app.command("split")
def storage_split(
    project_id: Optional[str] = typer.Option(None, "--project"),
    port: int = typer.Option(8765, help="Port to check for a running server."),
    into: Optional[Path] = typer.Option(
        None, "--into", help="Write the new database here instead of the default location."
    ),
) -> None:
    """Move one project's records out of a shared database into a file of its own.

    Backs up the source first, copies, verifies the copy field by field, records the new
    location, and only then removes the rows from the shared file. A verification that
    does not pass stops before anything is recorded or removed, and says what differed.
    """
    project = _storage_project(project_id)
    _refuse_while_serving(port)
    try:
        report = split_project(project, destination=into)
    except SplitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(report.render())
    if not report.ok:
        typer.secho(
            f"\nNothing was moved: {project.id} is still served from {report.source}. The "
            f"copy is at {report.destination} for inspection; remove it once you have "
            "looked, then run this again.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    typer.secho(f"\n{project.id} is now served from {report.destination}.", fg=typer.colors.GREEN)
    typer.echo("Enrol the new file in your backups; the old one no longer holds these records.")


@storage_app.command("rollback")
def storage_rollback(
    project_id: Optional[str] = typer.Option(None, "--project"),
    port: int = typer.Option(8765, help="Port to check for a running server."),
    into: Optional[Path] = typer.Option(
        None, "--into", help="Write the files here instead of the recorded source."
    ),
) -> None:
    """Put a project back on its files, keeping everything written since the cutover.

    The export runs first and against the store's current state, so a handoff made
    after the cutover is on disk before anything is switched back. The database is not
    deleted: a rollback is a decision that can itself be wrong.
    """
    project = _storage_project(project_id)
    _refuse_while_serving(port)
    report = roll_back(project, tasks_dir=into)
    typer.echo(report.render())
    typer.secho(f"{project.id} is served from its files again.", fg=typer.colors.GREEN)


@storage_app.command("export")
def storage_export(
    destination: Path = typer.Argument(..., help="Directory to write the task files into."),
    project_id: Optional[str] = typer.Option(None, "--project"),
) -> None:
    """Write the store's current state out as task YAML, with its attachment bytes.

    An interchange artifact somebody asked for. Nothing calls this on a write and
    nothing commits what it produces -- an automatically maintained YAML mirror would
    be the second authority this migration removed.
    """
    project = _storage_project(project_id)
    report = export_project(project, destination)
    typer.echo(report.render())


def _databases_in_play(settings: StorageSettings, project_id: Optional[str]) -> List[Path]:
    """The files a backup or a restore is about, in registration order.

    Every project's, unless one is named. Distinct, because two projects sharing a file
    must not produce two snapshots of it -- the second would refuse, backups never being
    overwritten. Only files that exist: a project on the file backend has none.
    """
    registry = ProjectRegistry()
    if project_id:
        ids = [_storage_project(project_id).id]
    else:
        ids = [project.id for project in registry.list_projects()]
    return [path for path in settings.databases(ids) if path.exists()]


@storage_app.command("backup")
def storage_backup(
    destination: Optional[Path] = typer.Option(None, "--into", help="Where to write the snapshot."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Back up just this project's database."
    ),
) -> None:
    """Take a verifiable snapshot of each database, safe to run while one is in use.

    Every project's file after task-400, since a machine now has more than one. Naming a
    project narrows it to that project's, and ``--into`` needs the choice to be down to
    a single file before it can mean anything.
    """
    settings = load_storage_settings()
    databases = _databases_in_play(settings, project_id)
    if not databases:
        typer.echo("There is no database on this machine yet; nothing to back up.")
        return
    if destination is not None and len(databases) > 1:
        typer.secho(
            f"--into names one file and {len(databases)} databases would be backed up. "
            "Add --project to say which, or drop --into and let each snapshot be written "
            "beside the database it came from.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    failed = False
    for source in databases:
        written = back_up(settings, destination=destination, database=source)
        if written is None:  # pragma: no cover - existence was checked above
            continue
        report = verify_backup(written)
        typer.echo(f"database: {source}")
        typer.echo(f"snapshot: {written}")
        typer.echo(f"manifest: {written}.manifest.json")
        typer.echo(report.render())
        failed = failed or not report.ok
    if failed:
        raise typer.Exit(code=1)


@storage_app.command("verify")
def storage_verify(
    snapshot: Path = typer.Argument(..., help="A snapshot written by 'storage backup'."),
) -> None:
    """Open a snapshot read-only, in isolation, and say whether it may be restored."""
    report = verify_backup(snapshot)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


@storage_app.command("restore")
def storage_restore(
    snapshot: Path = typer.Argument(..., help="A snapshot written by 'storage backup'."),
    port: int = typer.Option(8765, help="Port to check for a running server."),
    force: bool = typer.Option(False, "--force", help="Restore over a database that is newer."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Restore over this project's database."
    ),
) -> None:
    """Put a snapshot back, refusing rather than proceeding if it does not verify.

    Whatever it replaces is moved aside rather than deleted, so a restore of the wrong
    snapshot is itself recoverable.

    A snapshot is a whole file, so which file it goes over has to be unambiguous: name a
    project unless this machine has exactly one database. Guessing here would replace one
    project's records with another's, and the move-aside would be the only thing between
    that and losing them.
    """
    _refuse_while_serving(port)
    settings = load_storage_settings()
    databases = _databases_in_play(settings, project_id)
    if len(databases) > 1:
        typer.secho(
            "This machine has more than one database and a snapshot replaces a whole "
            "file. Say which with --project. Candidates:\n"
            + "\n".join(f"  {path}" for path in databases),
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    target = databases[0] if databases else settings.database

    report = restore_backup(snapshot, settings=settings, force=force, database=target)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)
    typer.secho(f"restored {snapshot} to {target}", fg=typer.colors.GREEN)


queue_app = typer.Typer(
    name="queue",
    help="Read and change the order work is handed out in.",
)
app.add_typer(queue_app)

#: The listing is written for an 80-column terminal, because that is the width a
#: terminal is when nobody has widened it, and this list is meant to be read.
_WIDTH = 80
_INDENT = "        "


def _fit(value: str, width: int) -> str:
    """``value`` cut to ``width``, ending in an ellipsis when it had to be cut."""
    return value if len(value) <= width else value[: width - 1] + "\u2026"


def _provenance_line(entry: QueueEntry) -> Optional[str]:
    """``placed by Ada (human, anchor: strong) - "..."``, or nothing.

    Printed only for a task that has actually been moved, which is a minority of any
    band -- a listing that said "placed by nobody" under every untouched task would
    double its length to state the default.

    The reason is quoted rather than summarised because it is the part that says whose
    decision an agent-written move was carrying, and a run reading this list has to be
    able to weigh that sentence. It is cut to the line, as every other field here is;
    the API returns it whole.
    """
    move = entry.last_move
    if move is None:
        return None
    marks = [move.kind or "kind unknown"]
    if move.anchor:
        marks.append(f"anchor: {move.anchor}")
    head = f"placed by {move.actor} ({', '.join(marks)})"
    if move.body:
        head = f'{head} - "{move.body}"'
    return _fit(head, _WIDTH - len(_INDENT))


def _band_heading(band: str, entries: List[QueueEntry]) -> str:
    """``HIGH  (54 open, 12 claimable)``, or ``(empty)`` when the band has nobody."""
    if not entries:
        return f"{band.upper()}  (empty)"
    claimable = sum(1 for entry in entries if entry.claimable)
    return f"{band.upper()}  ({len(entries)} open, {claimable} claimable)"


def _render_listing(
    listing: QueueListing, *, only_band: Optional[str], claimable_only: bool
) -> List[str]:
    """The listing as lines, band by band.

    Two lines per task -- number and id, then the title -- plus a line naming the rule
    that excluded it when it is not claimable, and a line saying where its place came
    from when something moved it. One line per task would have to drop either the id or
    the title at this width, and both are the point: the id is what you type next and
    the title is what lets you judge the order without opening anything.
    """
    lines: List[str] = []
    for band in listing.bands:
        if only_band is not None and band.band != only_band:
            continue
        entries = [entry for entry in band.entries if entry.claimable or not claimable_only]
        if claimable_only and not entries:
            continue
        if lines:
            lines.append("")
        lines.append(_band_heading(band.band, entries))
        lines.append("-" * min(_WIDTH, len(lines[-1])))
        for entry in entries:
            marker = " " if entry.claimable else "!"
            position = "?" if entry.queue_position is None else str(entry.queue_position)
            lines.append(f"{marker} {position:>5}  {_fit(entry.task, _WIDTH - 8)}")
            lines.append(f"{_INDENT}{_fit(entry.title, _WIDTH - len(_INDENT))}")
            if entry.reason:
                lines.append(f"{_INDENT}{_fit('not claimable: ' + entry.reason, _WIDTH - 8)}")
            provenance = _provenance_line(entry)
            if provenance:
                lines.append(f"{_INDENT}{provenance}")
    return lines


def _report_problems(listing: QueueListing) -> None:
    """Print what is wrong with the queue, before anything that depends on it."""
    if not listing.problems:
        return
    typer.secho(
        f"\u26a0\ufe0f  The queue is broken in {len(listing.problems)} place(s):",
        fg=typer.colors.RED,
        err=True,
    )
    for problem in listing.problems:
        typer.secho(f"   {problem.render()}", fg=typer.colors.RED, err=True)
    typer.secho(f"   Repair it with: {REPAIR_COMMAND}", fg=typer.colors.RED, err=True)


def _report_move_warnings(outcome: MoveOutcome) -> None:
    """Print what the queue-move check found, after the move it is about.

    **After, and on stderr.** The move has already landed and is authoritative -- the
    check reports and never refuses -- so the success line goes out first and stays
    parseable for a script that pipes it. A move that earns nothing prints nothing,
    which is the normal case.
    """
    if not outcome.warnings:
        return
    count = len(outcome.warnings)
    typer.secho(
        f"⚠️  {count} thing{'' if count == 1 else 's'} worth knowing about that move:",
        fg=typer.colors.YELLOW,
        err=True,
    )
    for warning in outcome.warnings:
        typer.secho(f"   {warning.message}", fg=typer.colors.YELLOW, err=True)
    if outcome.undo is not None:
        typer.secho(
            "   Undo it with: agentjobs queue move "
            f"{outcome.task.id} --{outcome.undo.kind}"
            + (f" {outcome.undo.target}" if outcome.undo.target else ""),
            fg=typer.colors.YELLOW,
            err=True,
        )


@queue_app.command("list")
def queue_list(
    band: Optional[Priority] = typer.Option(
        None, "--band", help="Show only this band. Default is every band."
    ),
    claimable: bool = typer.Option(
        False, "--claimable", help="Show only tasks an agent could take right now."
    ),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Judge claimability for this agent, so eligibility applies."
    ),
) -> None:
    """The whole backlog in the order it will be handed out. The reviewable copy.

    Every band is shown, empty ones included, because "critical is empty" is a fact
    worth stating rather than leaving to be inferred from a missing heading. Anything
    not claimable is marked ``!`` and carries the rule that excluded it. Anything that
    has been moved says who moved it, whether config calls that id a person, the reason
    they recorded, and whether they kept the place over a warning -- which is what a
    ``reorder`` run reads to find the positions it must order around.

    **This reports a broken queue rather than refusing to render one** -- you have to
    be able to see a broken queue in order to fix it. It is ``agentjobs next`` that
    declines to answer, because that is the one that would otherwise hand somebody the
    wrong task.
    """
    base_dir = Path.cwd()
    manager = _build_manager(base_dir)
    listing = manager.queue_listing(agent=agent, actors=actor_kinds(_load_config(base_dir)))
    _report_problems(listing)
    lines = _render_listing(
        listing, only_band=band.value if band else None, claimable_only=claimable
    )
    if not lines:
        typer.echo("No tasks in the queue.")
        return
    for line in lines:
        typer.echo(line)


@queue_app.command("move")
def queue_move(
    task_id: str = typer.Argument(..., help="Task to move."),
    before: Optional[str] = typer.Option(None, "--before", help="Put it ahead of this task."),
    after: Optional[str] = typer.Option(None, "--after", help="Put it behind this task."),
    top: bool = typer.Option(False, "--top", help="Put it first in its band."),
    bottom: bool = typer.Option(False, "--bottom", help="Put it last in its band."),
    with_children: bool = typer.Option(
        False, "--with-children", help="Carry its open same-band descendants along."
    ),
    actor: Optional[str] = typer.Option(
        None, "--actor", help="Who is moving it. Defaults to the project's default_user."
    ),
    note: Optional[str] = typer.Option(
        None, "--note", help="Log body. Omit it and the manager writes its own sentence."
    ),
) -> None:
    """Move a task within its band. Exactly one of --before/--after/--top/--bottom.

    There is no way to type a position, here or anywhere else. A number chosen without
    knowing what else is in the band is how two tasks end up sharing one; naming a
    neighbour or an end cannot go wrong that way.
    """
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    manager = _build_manager(base_dir)
    resolved_actor = _resolve_actor(config, actor)
    try:
        outcome = manager.move_with_warnings(
            task_id,
            before=before,
            after=after,
            top=top,
            bottom=bottom,
            with_children=with_children,
            actor=resolved_actor,
            body=note,
        )
    except ValueError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    task = outcome.task
    typer.echo(f"\u2705 {task.id} is now {task.priority.value}/{task.queue_position}")
    _report_move_warnings(outcome)


@queue_app.command("reprioritize")
def queue_reprioritize(
    task_id: str = typer.Argument(..., help="Task to reprioritize."),
    to: Priority = typer.Option(..., "--to", help="The band to move it into."),
    before: Optional[str] = typer.Option(None, "--before", help="Put it ahead of this task."),
    after: Optional[str] = typer.Option(None, "--after", help="Put it behind this task."),
    top: bool = typer.Option(False, "--top", help="Put it first in the target band."),
    actor: Optional[str] = typer.Option(
        None, "--actor", help="Who is deciding. Defaults to the project's default_user."
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Log body for the entry."),
) -> None:
    """Change a task's band, and where it lands inside it.

    With no placement it joins the bottom of the target band. Children are not carried:
    moving an epic to `critical` does not make each of its subtasks critical, and each
    of those is a decision that deserves its own entry.
    """
    base_dir = Path.cwd()
    config = _load_config(base_dir)
    manager = _build_manager(base_dir)
    resolved_actor = _resolve_actor(config, actor)
    try:
        task = manager.reprioritize(
            task_id,
            to,
            before=before,
            after=after,
            top=top,
            actor=resolved_actor,
            body=note,
        )
    except ValueError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"\u2705 {task.id} is now {task.priority.value}/{task.queue_position}")


@queue_app.command("check")
def queue_check(
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero when problems are found, for scripts and CI."
    ),
) -> None:
    """Report every queue rule broken anywhere in the corpus. Never raises.

    Exits 0 even when it finds problems, because this is the command you run *to look
    at* a broken queue, and a checker that fails the shell you are debugging in is a
    checker you stop running. ``--strict`` is the CI form; ``agentjobs validate``
    covers the same rules and already exits non-zero on findings.
    """
    manager = _build_manager(Path.cwd())
    problems = manager.check_queue()
    if not problems:
        typer.secho("\u2705 The queue is sound: no problems in any band.", fg=typer.colors.GREEN)
        return
    typer.secho(f"{len(problems)} problem(s):", fg=typer.colors.RED)
    for problem in problems:
        typer.secho(f"  {problem.render()}", fg=typer.colors.RED)
    typer.echo(f"\nRepair with: {REPAIR_COMMAND}")
    if strict:
        raise typer.Exit(code=1)


@queue_app.command("repair")
def queue_repair() -> None:
    """Give every open task a place again, and say exactly what was guessed.

    Everything it assigned is printed, because a duplicate position carries no record
    of who was meant to be first -- so that tie-break is arbitrary by necessity, and
    naming it is what makes the guess reviewable instead of silent. Read the ASSIGNED
    block afterwards; those are the ones a human should agree with.
    """
    manager = _build_manager(Path.cwd())
    report = manager.repair_queue()
    typer.echo(report.render())
    if not report.changed:
        typer.secho("\nNothing needed repairing.", fg=typer.colors.GREEN)
        return
    typer.secho(
        "\nReview the assignments above -- they were guessed, not recovered.",
        fg=typer.colors.YELLOW,
    )


@queue_app.command("compact")
def queue_compact(
    band: Priority = typer.Argument(..., help="The band to renumber."),
) -> None:
    """Renumber one band back to 100, 200, 300..., changing nobody's place.

    Cosmetic, and explicit only. Nothing compacts on its own: a background process
    quietly rewriting forty task files is exactly the kind of thing somebody should
    have to type.
    """
    manager = _build_manager(Path.cwd())
    moved = manager.compact_band(band)
    if not moved:
        typer.echo(f"Band '{band.value}' is already compact.")
        return
    # Where each task ended up, not every write it took to get there: a renumber is
    # planned in up to two passes so no intermediate state holds a duplicate, and a
    # task moved by both is written twice. Printing both reads as one task in two
    # places, which is exactly what a compaction never does.
    landed = dict(moved)
    for task_id, position in sorted(landed.items(), key=lambda item: item[1]):
        typer.echo(f"  {position:>5}  {task_id}")
    typer.secho(
        f"\u2705 Renumbered {len(landed)} task(s) in '{band.value}'.", fg=typer.colors.GREEN
    )


@app.command("next")
def next_task(
    priority: Optional[Priority] = typer.Option(None, "--priority", help="Restrict to one band."),
    agent: Optional[str] = typer.Option(None, "--agent", help="Judge eligibility for this agent."),
    parent: Optional[str] = typer.Option(
        None, "--parent", help="Restrict to the children of one epic (task-022)."
    ),
    why: bool = typer.Option(
        False, "--why", help="Also print every open task ahead of it, and why each was skipped."
    ),
) -> None:
    """The task that stands first in line: which one, and on request why.

    **Exits non-zero when the queue is broken**, printing the offending ids and the
    repair command rather than answering from a field that happens to be intact. That
    is the whole of design section 8: a queue that quietly answers while corrupt trains
    everybody to ignore corruption, and an agent silently working the wrong task leaves
    no trace anywhere.
    """
    manager = _build_manager(Path.cwd())
    try:
        task = manager.get_next_task(priority=priority, agent=agent, parent=parent)
        explanation = (
            manager.explain_next(priority=priority, agent=agent, parent=parent) if why else None
        )
    except QueueCorruptionError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if task is None:
        typer.echo("Nothing is claimable right now.")
    else:
        typer.echo(f"{task.id}  [{task.priority.value}/{task.queue_position}]")
        typer.echo(f"  {_fit(task.title, _WIDTH - 2)}")

    if explanation is None:
        return
    if explanation.empty_bands_above:
        typer.echo(f"\nEmpty bands above: {', '.join(explanation.empty_bands_above)}")
    if not explanation.skipped:
        typer.echo("\nNothing was ahead of it.")
        return
    typer.echo(f"\nAhead of it, and why each was skipped ({len(explanation.skipped)}):")
    for item in explanation.skipped:
        position = "?" if item.queue_position is None else str(item.queue_position)
        typer.echo(f"  {position:>5}  {_fit(item.task, _WIDTH - 8)}")
        typer.echo(f"{_INDENT}{_fit(item.reason, _WIDTH - len(_INDENT))}")


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
    with suppress(ProjectError):
        _refuse_if_not_files(ProjectRegistry().resolve_default(base_dir).id, "migrate-schema")
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


@app.command()
def finish(
    task_id: str = typer.Argument(..., help="Approved task to rebase, gate, merge and close."),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    approver: Optional[str] = typer.Option(
        None, "--approver", help="Who approved this, for the merge message and the log."
    ),
    posture_release: bool = typer.Option(
        False,
        "--posture-release",
        help="Merge on the project's posture rather than on a human approval (task-021).",
    ),
) -> None:
    """Run the scripted post-approval finish, with no agent in the loop (task-241).

    This is what the Approve button starts in a detached process; running it by hand is
    the same code and is how a finish that escalated is retried once its cause is fixed.

    It stops at the first thing it cannot do safely -- a conflicting rebase, a red gate,
    a server it cannot show is serving the merged code -- writing where it stopped onto
    the task and handing the ball back. Exit code 0 means merged, closed and verified;
    1 means it stopped and the task says where; 2 means the task was never a candidate
    and nothing happened.

    Without ``--posture-release`` it merges only what a person approved. With it, a
    dispatched run merges its own work on the strength of the project's posture, and the
    posture is checked here: anything but ``autonomous`` exits 2 having touched nothing.
    See ALLAGENTS.md on the merge gate for when that is the right flag to be passing.
    """
    from agentjobs.dispatch.finish import APPROVAL, DECLINED, ESCALATED, POSTURE, finish_task
    from agentjobs.dispatch.phases import RUN_ID_ENV

    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if approver is None:
        # A posture release has no approver, so it must not default to a word that reads
        # like one. It names the run instead, which is what a reader of the merge would
        # go and look up. The run id is in the environment of anything a dispatch spawned.
        run_id = os.environ.get(RUN_ID_ENV) if posture_release else None
        approver = (
            f"run {run_id}" if run_id else ("a dispatched run" if posture_release else "a human")
        )

    manager = task_manager_for(project)
    result = finish_task(
        manager=manager,
        project=project,
        task_id=task_id,
        approver=approver,
        authority=POSTURE if posture_release else APPROVAL,
    )
    typer.echo(result.render())
    if result.outcome == DECLINED:
        typer.secho(f"Nothing done ({result.reason}).", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2)
    if result.outcome == ESCALATED:
        typer.secho(
            f"Stopped at {result.steps[-1].step} ({result.reason}). The task says where.",
            fg=typer.colors.RED,
        )
        if result.dispatched_run_id:
            typer.echo(f"   Escalated into run {result.dispatched_run_id}.")
        elif result.escalation_dispatch:
            # Said out loud because the silence is the failure task-340 repairs: from a
            # shell, a stop that started nothing looked exactly like one that did.
            typer.secho(
                f"   No run was started ({result.escalation_dispatch}); the ball is on a "
                "human and the task says what the next move is.",
                fg=typer.colors.YELLOW,
            )
        raise typer.Exit(code=1)
    typer.secho(f"✅ {result.detail}", fg=typer.colors.GREEN)


@app.command()
def branches(
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    base: str = typer.Option("main", "--base", help="The branch merges land on."),
) -> None:
    """List local branches: which the base already contains, and how old the rest are.

    **It deletes nothing, and never will.** Task-293 fixed the cause -- the scripted
    finish now retires the branch it merged -- but a branch merged by hand, or one whose
    finish escalated after merging, is still left behind and nothing says it is there.
    This is the part that says so. Deleting on somebody's behalf is a different act with
    a different risk: several agents work one clone and none of them can see the others,
    so a branch that looks abandoned from here may be the one somebody is mid-task on,
    and git holds nothing that tells the two apart.

    Ages come from the *author* date of the oldest commit the base does not contain, so
    a branch that has been rebased four times still reports how long it has really been
    open. Read them against ENGINEERING.md on branch lifetime: a long-lived branch is
    where rebase conflicts come from, and it is usually waiting rather than working.

    Always exits 0. Leftover branches are untidy, not broken, and a report that fails a
    script over tidiness would end up suppressed.
    """
    from agentjobs.branch_report import survey_branches

    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    manager = task_manager_for(project)
    report = survey_branches(project.root, manager, base=base)
    if not report.rows:
        typer.echo(f"No local branches besides {base}.")
        return

    if report.litter:
        typer.secho(
            f"{len(report.litter)} branch(es) {base} already contains, with no worktree:",
            fg=typer.colors.YELLOW,
        )
        for row in report.litter:
            typer.echo(f"  {row.name:<48} {row.task_id or '-':<10} {row.task_state}")
        typer.echo("\n  Delete with: git branch -d <name>   (-d, never -D)")
    else:
        typer.secho(f"Nothing {base} contains is left behind.", fg=typer.colors.GREEN)

    if report.in_flight:
        typer.echo(f"\n{len(report.in_flight)} branch(es) {base} does not contain, oldest first:")
        for row in report.in_flight:
            age = f"{row.age_days:.1f}d" if row.age_days is not None else "-"
            where = str(row.worktree) if row.worktree else "no worktree"
            typer.echo(
                f"  {row.name:<48} {age:>7}  {row.task_id or '-':<10} "
                f"{row.task_state:<24} {where}"
            )


playbook_app = typer.Typer(
    name="playbook",
    help="Read the reusable briefs this project keeps for recurring work.",
)
app.add_typer(playbook_app)


def _playbooks_dir(base_dir: Path) -> Path:
    """This project's playbooks directory, resolved from its own config."""
    return resolve_playbooks_dir(base_dir, _load_config(base_dir))


def _report_playbook_problems(listing: PlaybookListing) -> None:
    """Print the files that are in the directory and are not playbooks.

    Before the valid ones, and on stderr, for the reason the queue prints its problems
    first: a file that fails validation and is then omitted from the listing reads as a
    playbook nobody ever wrote, and its author is the one person who needs to hear
    about it.
    """
    if not listing.problems:
        return
    typer.secho(
        f"\u26a0\ufe0f  {len(listing.problems)} problem(s) in {listing.directory}:",
        fg=typer.colors.RED,
        err=True,
    )
    for problem in listing.problems:
        typer.secho(f"   {problem.render()}", fg=typer.colors.RED, err=True)


@playbook_app.command("list")
def playbook_list() -> None:
    """Every playbook this project holds, with what each one is for.

    Reads files. It does not run anything, and there is no command here that does:
    starting a playbook run is human-gated and arrives with instantiation.
    """
    directory = _playbooks_dir(Path.cwd())
    listing = list_playbooks(directory)
    _report_playbook_problems(listing)
    if not listing.exists:
        typer.echo(f"No playbooks directory yet ({directory}).")
        typer.echo("   Copy the shipped references in with: agentjobs playbook init")
        return
    if not listing.playbooks:
        typer.echo(f"No playbooks in {directory}.")
        typer.echo("   Copy the shipped references in with: agentjobs playbook init")
        return
    width = max(len(playbook.name) for playbook in listing.playbooks)
    for playbook in listing.playbooks:
        contract = playbook.contract
        typer.echo(f"{playbook.name:<{width}}  {_fit(playbook.description, _WIDTH - width - 2)}")
        typer.echo(
            f"{' ' * width}  target: {contract.target.value}"
            f"   difficulty: {contract.difficulty.value}"
            f"   gates: {len(contract.gates)}"
        )


@playbook_app.command("show")
def playbook_show(
    name: str = typer.Argument(..., help="Playbook name, which is its filename stem."),
    contract_only: bool = typer.Option(
        False, "--contract", help="Print the frontmatter contract and omit the brief."
    ),
) -> None:
    """One playbook: its frontmatter contract, then its brief."""
    directory = _playbooks_dir(Path.cwd())
    try:
        playbook = read_playbook(directory, name)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except UnknownPlaybookError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.echo("Run 'agentjobs playbook list' to see what this project holds.", err=True)
        raise typer.Exit(code=2) from exc
    except PlaybookError as exc:
        typer.secho(f"{name} does not validate:", fg=typer.colors.RED, err=True)
        for finding in exc.findings:
            typer.secho(f"   {finding.render()}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"{playbook.name}  ({playbook.path})")
    typer.echo("-" * min(_WIDTH, len(playbook.name) + len(str(playbook.path)) + 4))
    typer.echo(
        yaml.safe_dump(
            playbook.contract.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=False,
        ).rstrip()
    )
    if contract_only:
        return
    typer.echo("")
    typer.echo(playbook.body.strip())


@playbook_app.command("run")
def playbook_run(
    name: str = typer.Argument(..., help="Playbook name, which is its filename stem."),
    task_id: Optional[str] = typer.Option(
        None,
        "--task",
        help="For a task-target playbook: the task to run it against. Refused otherwise.",
    ),
    group: Optional[str] = typer.Option(
        None,
        "--group",
        help="Runner group to pick from, overriding the project's. Must already exist.",
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project", help="Registered project id. Defaults to the one you are in."
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help=(
            "Who is asking. Creates the run task for a project-target playbook and "
            "must be a configured human. Required for those; unused by a task-target "
            "playbook, which creates nothing."
        ),
    ),
) -> None:
    """Run a playbook: create its run task if it has one, then dispatch an agent at it.

    **This spends money on a model, on this machine, now.** Every dispatch gate applies
    unchanged -- the master switch, the sentinel file, per-project enablement, the
    concurrency cap, the clean-tree rule and the human-clocked rule -- and naming a
    playbook widens none of them (playbooks design section 6.3).

    Authorisation is task-188's, and this path adds nothing to it. A **project-target**
    playbook creates a run task attributed to ``--actor``; that creation entry is the
    newest entry on the record, and it is what the human-clocked rule then reads. A
    **task-target** playbook creates nothing, so the rule is applied to the target
    task's own newest entry, exactly as ``agentjobs dispatch run`` applies it -- write
    the note as yourself first if the newest entry there is an agent's.

    ``--actor`` is therefore not a way to authorise a run from a shell. It says who is
    creating a task, the same thing it says on ``agentjobs create`` and ``promote``,
    it is checked against the project's configured humans before anything is written,
    and it cannot reach a task it did not just create. There is no flag here that signs
    for a dispatch of an existing task on somebody's behalf.

    **It has no ``default_user`` fallback, unlike every other ``--actor`` in this CLI.**
    That is the one deliberate departure. The entry it writes is a moment later read as
    the authorisation for spending money, and an entry attributed to whoever the config
    happens to name -- rather than to whoever typed the command -- is exactly the
    signature task-188 refuses to invent. A person naming themselves costs one flag; an
    unattributable run costs the whole point of the human-clocked rule.
    """
    registry = ProjectRegistry()
    try:
        project = registry.get(project_id) if project_id else registry.resolve_default()
    except ProjectError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    config = project.load_config()
    manager = dispatch_manager_for(project)
    try:
        result = run_playbook(
            manager=manager,
            project=project,
            project_config=config,
            name=name,
            task_id=task_id,
            group=group,
            # Only ever the creator of the run task, and never defaulted. `authorized_by`
            # is task-188's browser path and is deliberately not offered here; see the
            # docstring for both.
            created_by=actor,
            surface="the command line",
        )
    except PlaybookRunError as exc:
        typer.secho(f"Refused ({exc.reason}): {exc}", fg=typer.colors.RED, err=True)
        if exc.reason == "no_authorizing_human":
            # The refusal message is read by the API and by this command, so it cannot
            # name a flag. Here is the one place that can.
            typer.secho(
                "   Pass --actor <id>, naming a human under 'actors:' in "
                ".agentjobs/config.yaml.",
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(code=2) from exc
    except PlaybookDispatchRefused as exc:
        typer.secho(f"Refused ({exc.reason}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except (DispatchError, DispatchRunError) as exc:
        reason = getattr(exc, "reason", "dispatch_failed")
        typer.secho(f"Refused ({reason}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    handle = result.handle
    if result.created_run_task:
        typer.echo(f"✅ Created run task {result.task_id} from playbook '{name}'.")
    typer.echo(f"✅ Dispatched {result.task_id} as run {handle.run_id} ({handle.mode.value}).")
    typer.echo(f"   Brief: {result.pointer.path} ({result.pointer.digest}).")
    typer.echo(f"   Agent told AgentJobs is at {handle.api_base}.")
    if handle.group:
        typer.echo(f"   Runner '{handle.runner}', chosen from group '{handle.group}'.")
    if handle.session_id:
        typer.echo(f"   Session {handle.session_id} — the CLI assigned that id, not us.")
    typer.echo(f"   Run directory: {handle.directory.path}")
    if handle.mode is DispatchMode.BATCH:
        # Same reason as `dispatch run`: a batch run's terminal record is written by
        # this process's supervisor thread, so returning here would leave a completed
        # child marked running until something else noticed.
        assert handle.supervisor is not None
        typer.echo("   Waiting for the batch run to record its outcome.")
        handle.supervisor.join()


@playbook_app.command("init")
def playbook_init() -> None:
    """Copy the shipped reference playbooks into this project, never overwriting one.

    Per file: a project that has tuned its own copy of one reference and has never
    seen another should be able to take the second without the first being touched.
    From the moment a copy exists it is authoritative -- a brief behind a name must
    never depend on which version of AgentJobs is installed.
    """
    base_dir = Path.cwd()
    directory = _playbooks_dir(base_dir)
    result = install_references(directory)
    for name in result.written:
        typer.echo(f"   wrote {name}{PLAYBOOK_SUFFIX}")
    for name in result.kept:
        typer.echo(f"   kept  {name}{PLAYBOOK_SUFFIX} (already here; not overwritten)")
    if result.wrote_nothing:
        typer.echo(f"Nothing to write: {directory} already has every shipped playbook.")
        return
    typer.echo(f"\u2705 {len(result.written)} playbook(s) copied into {directory}.")
    typer.echo("   They are yours now: edit them, and commit them with the project.")


@app.command()
def quotations(
    task_id: Optional[str] = typer.Argument(
        None, help="One task to scan. Omit it to scan the whole corpus."
    ),
    storage_dir: Optional[str] = typer.Option(
        None,
        "--storage-dir",
        help="Directory of task YAML. Defaults to the project's configured tasks_directory.",
    ),
) -> None:
    """List record regions that quote a person verbatim instead of paraphrasing them.

    The operator-facing half of the check that warns an author at the write, fails the
    gate over `tasks/`, and refuses an import -- see `agentjobs.quotation` for what the
    detector can and cannot see. Fix each region it names with `agentjobs redact`.

    Exits 1 when anything is found, so it can be used as a check of its own. It prints
    a short excerpt of each remark, which is what makes a false positive recognisable
    without opening the file; nothing it prints is written anywhere.
    """
    base_dir = Path.cwd()
    if storage_dir is None:
        # Resolved rather than composed, so it reads the live records after a cutover
        # instead of the frozen copy beside them. An explicit --storage-dir still means
        # exactly that directory, which is how you inspect an export.
        source: Any = _build_manager(base_dir).storage
    else:
        tasks_dir = Path(storage_dir)
        if not tasks_dir.is_absolute():
            tasks_dir = base_dir / tasks_dir
        source = TaskStorage(tasks_dir)

    if task_id:
        task = source.load_task(task_id)
        if task is None:
            typer.secho(f"Task '{task_id}' not found.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        tasks = [task]
    else:
        tasks = source.list_tasks()

    found = 0
    for task in tasks:
        for remark in scan_task(task):
            found += 1
            excerpt = " ".join(remark.text.split())[:100]
            typer.echo(f"{task.id}  {remark.locator()}")
            typer.echo(f"    {excerpt}")
    if not found:
        typer.echo(f"\u2713 {len(tasks)} task record(s) scanned; no quoted remarks.")
        return
    typer.echo(
        f"\n{found} quoted remark(s) across {len(tasks)} task record(s). "
        "A task record states what somebody meant, not the words they used. "
        "Rewrite each as a paraphrase and apply it with `agentjobs redact`."
    )
    raise typer.Exit(code=1)


@app.command()
def redact(
    task_id: str,
    field: str = typer.Option(
        ...,
        "--field",
        help="Region to replace: title, ball_prompt, spec.<name>, or log[<id>].body.",
    ),
    reason: str = typer.Option(
        ..., "--reason", help="Why the text had to go. Recorded on the task."
    ),
    replacement: Optional[str] = typer.Option(
        None, "--replacement", help="The text to put there instead."
    ),
    replacement_file: Optional[str] = typer.Option(
        None,
        "--replacement-file",
        help="A file holding the replacement text. Use this for anything multi-line.",
    ),
    actor: Optional[str] = typer.Option(
        None, "--actor", help="Who is redacting. Defaults to the project's default_user."
    ),
) -> None:
    """Replace one prose region of a task with a stated redaction (task-376).

    The supported answer to content that must not persist -- a verbatim quotation of a
    person in a repository with a public remote, most often. **It is the only way to
    change a log entry**, which is append-only by design and therefore has no other
    answer to text that should never have been written; the removal is itself recorded,
    as a note naming the region, the reason, the actor and how many characters went.

    You supply the replacement, and it should say what the removed text meant. A
    redaction that loses why a task exists is a worse record, not a safer one.

    `agentjobs quotations` names the regions worth looking at. Pass the replacement in a
    file for anything longer than a sentence -- shells mangle multi-line arguments, and
    a mangled redaction is a second edit to a record you are already editing by hand.
    """
    if (replacement is None) == (replacement_file is None):
        typer.secho("Pass exactly one of --replacement or --replacement-file.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if replacement_file is not None:
        path = Path(replacement_file)
        try:
            replacement = path.read_text(encoding="utf-8")
        except OSError as error:
            typer.secho(f"Cannot read {path}: {error}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from error

    base_dir = Path.cwd()
    config = _load_config(base_dir)
    manager = _build_manager(base_dir)
    resolved_actor = _resolve_actor(config, actor)

    try:
        task = manager.redact(
            task_id,
            field=field,
            replacement=replacement or "",
            reason=reason,
            actor=resolved_actor,
        )
    except ValueError as error:
        # An unaddressable region, an empty reason, a task that is not there. None of
        # them is a bug, so none of them should reach the operator as a traceback.
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    remaining = [remark for remark in scan_task(task) if remark.field == field]
    typer.echo(f"\u2705 Redacted {field} on {task.id}; the removal is recorded on the task.")
    if remaining:
        typer.secho(
            f"   {len(remaining)} quoted remark(s) still in {field}. "
            "The replacement quotes a person too.",
            fg=typer.colors.YELLOW,
        )


if __name__ == "__main__":
    app()
