"""CLI integration tests for AgentJobs."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agentjobs.cli import app, _ensure_gitignore, _make_output_encoding_safe
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Outcome
from support import task_store

runner = CliRunner()


def test_output_encoding_survives_legacy_codepage_stream(monkeypatch) -> None:
    """Emoji output must not crash when stdout uses a legacy codepage.

    Reproduces the original failure: with stdout redirected to a pipe on a
    default Windows install, the stream encoding is cp1252 and the first emoji
    raises UnicodeEncodeError.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    # Guard clause: confirm the stream really is hostile before the fix.
    try:
        stream.write("âŒ")
        stream.flush()
        pytest.fail("expected cp1252 stream to reject the emoji")
    except UnicodeEncodeError:
        pass

    monkeypatch.setattr("sys.stdout", stream)
    _make_output_encoding_safe()

    stream.write("âŒ No server running.\n")
    stream.flush()

    assert "âŒ" in raw.getvalue().decode("utf-8")


def test_cli_init_writes_config_and_no_task_directory(tmp_path: Path, monkeypatch) -> None:
    """What `init` leaves behind: a config file, and nothing that looks like a corpus."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init"],
        input="Test Project\nprompts\n9000\njeff\n",
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert (tmp_path / ".agentjobs" / "config.yaml").exists()
    # No tasks directory. A new project's records are rows from its first one, so
    # conjuring an empty directory would teach the wrong model (task-399, task-402).
    assert not (tmp_path / "tasks").exists()


def test_cli_create_list_show(tmp_path: Path, monkeypatch) -> None:
    """Exercise the main CLI commands end-to-end."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)

    create_result = runner.invoke(
        app,
        ["create"],
        input="Sample Task\nExample description\n",
        catch_exceptions=False,
    )
    task_id = _created_id(create_result)

    list_result = runner.invoke(
        app,
        ["list"],
        catch_exceptions=False,
    )
    assert list_result.exit_code == 0
    assert task_id in list_result.stdout

    show_result = runner.invoke(
        app,
        ["show", task_id],
        catch_exceptions=False,
    )
    assert show_result.exit_code == 0
    payload = json.loads(show_result.stdout)
    assert payload["schema"] == 2
    assert payload["lifecycle"] == "draft"
    assert payload["ball"] == "human"
    assert payload["title"] == "Sample Task"


def test_work_command_flow(tmp_path: Path, monkeypatch) -> None:
    """Verify the interactive agent workflow (pick task -> start -> complete)."""
    monkeypatch.chdir(tmp_path)

    _init_project(tmp_path)
    created = runner.invoke(app, ["create", "--ready"], input="Work Task\nDescription\n")
    task_id = _created_id(created)

    # Run work command with mocked inputs
    # Inputs: Confirm Start (y), Confirm Complete (y), Summary
    result = runner.invoke(
        app, ["work", "--agent", "MyAgent"], input="y\ny\nFixed the bug\n", catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "TASK: Work Task" in result.stdout
    assert "Task claimed" in result.stdout
    assert "closed: completed" in result.stdout

    # ...and the record says so, read back through the command a person would use.
    shown = _the_task(task_id)
    assert shown["lifecycle"] == "closed"
    assert shown["outcome"] == "completed"
    assert any("Fixed the bug" in (entry.get("body") or "") for entry in shown["log"])


def test_serve_command_args(monkeypatch) -> None:
    """Ensure the serve command correctly parses arguments and calls uvicorn.run."""
    with patch("uvicorn.run") as mock_run:
        result = runner.invoke(
            app,
            ["serve", "--host", "192.168.1.25", "--port", "9000", "--reload"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Starting AgentJobs server at http://192.168.1.25:9000" in result.stdout

        mock_run.assert_called_once_with(
            "agentjobs.api.main:app", host="192.168.1.25", port=9000, reload=True
        )


def test_open_targets_react_app_on_existing_server() -> None:
    with (
        patch("agentjobs.cli._find_process_by_port", return_value=1234),
        patch("webbrowser.open") as browser_open,
    ):
        result = runner.invoke(app, ["open", "--port", "9000"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Opening http://localhost:9000/app/" in result.stdout
    browser_open.assert_called_once_with("http://localhost:9000/app/")


def test_open_starts_installed_python_module_without_poetry() -> None:
    with (
        patch("agentjobs.cli._find_process_by_port", side_effect=[None, 1234]),
        patch("platform.system", return_value="Linux"),
        patch("subprocess.Popen") as process_open,
        patch("time.sleep"),
        patch("webbrowser.open"),
    ):
        result = runner.invoke(app, ["open"], catch_exceptions=False)

    assert result.exit_code == 0
    command = process_open.call_args.args[0]
    assert command[:4] == [sys.executable, "-m", "agentjobs.cli", "serve"]
    assert "poetry" not in command


@pytest.mark.parametrize("command", ["serve", "restart", "open"])
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "*", "+"])
def test_server_commands_refuse_wildcard_binding(command: str, host: str) -> None:
    """No entry point may expose the unauthenticated API on every interface."""
    with patch("uvicorn.run") as mock_run:
        result = runner.invoke(app, [command, "--host", host], catch_exceptions=False)

    assert result.exit_code == 2
    assert "Wildcard binding is refused" in result.output
    mock_run.assert_not_called()


def test_list_tasks_filtering(tmp_path: Path, monkeypatch) -> None:
    """Verify that list correctly filters tasks by status and priority."""
    monkeypatch.chdir(tmp_path)

    _init_project(tmp_path)

    # Create PLANNED/HIGH task
    runner.invoke(
        app,
        ["create", "--priority", "high", "--title", "High Task"],
        input="\n",  # default description
    )

    # Create a low-priority task and close it through the verbs rather than by writing
    # the end state: closing is a transition with its own rules, and a record edited
    # into place would not have gone through any of them. There is no `close` command,
    # so this goes through the manager over the same store the CLI is reading.
    low = runner.invoke(
        app, ["create", "--ready", "--priority", "low", "--title", "Low Task"], input="\n"
    )
    low_id = _created_id(low)
    manager = TaskManager(task_store(tmp_path / "tasks"))
    manager.claim_task(low_id, agent="jeff")
    manager.close_task(low_id, actor="jeff", outcome=Outcome.COMPLETED, body="Done.")

    # Test Filter by Lifecycle
    result_status = runner.invoke(app, ["list", "--lifecycle", "closed"])
    assert result_status.exit_code == 0
    assert "Low Task" in result_status.stdout
    assert "High Task" not in result_status.stdout

    # Test Filter by Priority
    result_priority = runner.invoke(app, ["list", "--priority", "high"])
    assert result_priority.exit_code == 0
    assert "High Task" in result_priority.stdout
    assert "Low Task" not in result_priority.stdout


def test_ensure_gitignore_updates(tmp_path: Path) -> None:
    """Verify that the database file is added to .gitignore if missing."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n__pycache__/\n")

    _ensure_gitignore(tmp_path)

    content = gitignore.read_text()
    assert ".agentjobs/agentjobs.db" in content

    # Run again to ensure no duplication
    _ensure_gitignore(tmp_path)
    content = gitignore.read_text()
    assert content.count(".agentjobs/agentjobs.db") == 1


def test_migrate_command_execution(tmp_path: Path, monkeypatch) -> None:
    """Verify that legacy Markdown tasks are correctly converted to YAML."""
    monkeypatch.chdir(tmp_path)

    source_dir = tmp_path / "legacy_tasks"
    source_dir.mkdir()
    target_dir = tmp_path / "new_tasks"

    # Create a sample legacy markdown task
    md_content = """---
title: Legacy Task
status: todo
priority: high
tags: [legacy, migration]
---

This is a legacy task description.
"""
    (source_dir / "task-1.md").write_text(md_content)

    # Run migrate command
    result = runner.invoke(
        app, ["migrate", str(source_dir / "*.md"), str(target_dir)], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "Migration complete" in result.stdout
    assert "Successful: 1" in result.stdout

    # Verify YAML file creation
    yaml_files = list(target_dir.glob("*.yaml"))
    assert len(yaml_files) == 1

    content = yaml_files[0].read_text()
    assert "title: Legacy Task" in content
    assert "priority: high" in content
    assert "This is a legacy task description" in content


def test_load_test_data(tmp_path: Path, monkeypatch) -> None:
    """Verify that sample test data is loaded correctly."""
    monkeypatch.chdir(tmp_path)

    # Run load_test_data command
    result = runner.invoke(
        app, ["load-test-data", "--storage-dir", "tasks"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "Loaded" in result.stdout
    assert "test tasks" in result.stdout

    # Verify files created
    task_files = list((tmp_path / "tasks").glob("*.yaml"))
    assert len(task_files) > 0

    # Run again to verify update/refresh logic
    result_refresh = runner.invoke(
        app, ["load-test-data", "--storage-dir", "tasks"], catch_exceptions=False
    )
    assert result_refresh.exit_code == 0
    assert "refreshed" in result_refresh.stdout


def test_show_task_not_found(tmp_path: Path, monkeypatch) -> None:
    """Verify error handling when showing a non-existent task."""
    monkeypatch.chdir(tmp_path)

    # Initialize to ensure manager can run
    runner.invoke(app, ["init"], input="Test Project\nprompts\n9000\njeff\n")

    result = runner.invoke(app, ["show", "non-existent-id"])
    assert result.exit_code == 1
    assert "Task 'non-existent-id' not found" in result.stdout


def _init_project(root: Path | None = None, *, default_user: str | None = "jeff") -> None:
    """Configure a project in the working directory, without registering it.

    Written rather than run through ``agentjobs init``, and the difference matters after
    task-402: ``init`` registers the project, and every command in a registered project
    is a service client -- so these cases would need a server running to exercise a
    handful of argument-parsing and attribution decisions. An unregistered directory is
    a real state (a clone on a machine that has never registered it) and the one the CLI
    still answers for itself.

    ``agentjobs init``'s own effects are asserted separately, where they belong.
    """
    import yaml

    base = root or Path.cwd()
    config: dict = {
        "project_name": "Test Project",
        "tasks_directory": "tasks",
        "prompts_directory": "prompts",
        "port": 9000,
    }
    if default_user is not None:
        config["default_user"] = default_user
    (base / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (base / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _the_task(task_id: str) -> dict:
    """The record, read back through the command a person would use."""
    shown = runner.invoke(app, ["show", task_id], catch_exceptions=False)
    assert shown.exit_code == 0, shown.output
    loaded = json.loads(shown.stdout)
    assert isinstance(loaded, dict)
    return loaded


def _created_id(result) -> str:
    """The id `create` reports, which is the only handle it hands back."""
    assert result.exit_code == 0, result.output
    found = [word for word in result.stdout.split() if word.startswith("task-")]
    assert found, result.stdout
    return str(found[0])


def test_promote_moves_draft_to_ready(tmp_path: Path, monkeypatch) -> None:
    """A draft promoted from the CLI becomes claimable, and the log says who did it."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)
    task_id = _created_id(runner.invoke(app, ["create", "--title", "Draft Task"], input="\n"))

    assert _the_task(task_id)["lifecycle"] == "draft"

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Promoted" in result.stdout

    content = _the_task(task_id)
    assert content["lifecycle"] == "ready"
    assert content["ball"] == "agent"
    assert content["ball_reason"] == "available"
    assert content.get("ball_prompt") is None

    entry = content["log"][-1]
    assert entry["type"] == "transition"
    # jeff is the default_user written by init: the promotion is attributed
    # without the caller having to name themselves.
    assert entry["actor"] == "jeff"
    assert entry["body"] == "Promoted by jeff; the spec is finished and it is claimable."


def test_promote_uses_explicit_actor_and_note(tmp_path: Path, monkeypatch) -> None:
    """--actor overrides default_user, and --note replaces the manager's sentence."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)
    task_id = _created_id(runner.invoke(app, ["create", "--title", "Draft Task"], input="\n"))

    result = runner.invoke(
        app,
        ["promote", task_id, "--actor", "codex", "--note", "Spec reviewed and finished."],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    entry = _the_task(task_id)["log"][-1]
    assert entry["actor"] == "codex"
    assert entry["body"] == "Spec reviewed and finished."


def test_promote_refuses_a_non_draft_without_a_traceback(tmp_path: Path, monkeypatch) -> None:
    """Promoting an already-promoted task is an expected refusal, not a crash."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)
    task_id = _created_id(runner.invoke(app, ["create", "--title", "Draft Task"], input="\n"))

    assert runner.invoke(app, ["promote", task_id]).exit_code == 0
    log_length_before = len(_the_task(task_id)["log"])

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 1
    assert "is not a draft" in result.stdout
    assert "Traceback" not in result.stdout
    # The refused attempt left no trace: same lifecycle, no extra log entry.
    after = _the_task(task_id)
    assert after["lifecycle"] == "ready"
    assert len(after["log"]) == log_length_before


def test_promote_missing_task_reports_not_found(tmp_path: Path, monkeypatch) -> None:
    """A bad task id reads the same as it does from `show`."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)

    result = runner.invoke(app, ["promote", "task-nope"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Task 'task-nope' not found" in result.stdout


def test_promote_without_an_actor_refuses_rather_than_guessing(tmp_path: Path, monkeypatch) -> None:
    """With no default_user and no --actor, refuse instead of writing an anonymous
    transition -- an unattributed state change is worse than a refused one."""
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path, default_user=None)
    # No default_user, and no --actor.
    task_id = _created_id(runner.invoke(app, ["create", "--title", "Draft Task"], input="\n"))

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 1
    assert "No actor" in result.stdout
    assert _the_task(task_id)["lifecycle"] == "draft"


def test_load_config_fallback(tmp_path: Path, monkeypatch) -> None:
    """Verify that commands work with default config if not initialized."""
    monkeypatch.chdir(tmp_path)

    # Don't run init. Just try to list tasks (which loads config).
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No tasks found" in result.stdout
