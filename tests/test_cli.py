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


def test_cli_init_create_list_show(tmp_path: Path, monkeypatch) -> None:
    """Exercise the main CLI commands end-to-end."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init"],
        input="Test Project\ntasks\nprompts\n9000\njeff\n",
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert (tmp_path / "tasks").exists()
    assert (tmp_path / ".agentjobs" / "config.yaml").exists()

    create_result = runner.invoke(
        app,
        ["create"],
        input="Sample Task\nExample description\n",
        catch_exceptions=False,
    )
    assert create_result.exit_code == 0
    task_files = list((tmp_path / "tasks").glob("*.yaml"))
    assert len(task_files) == 1
    task_file = task_files[0]
    assert task_file.exists()
    task_id = task_file.stem

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

    # Setup: Initialize and create a task
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\njeff\n")
    runner.invoke(app, ["create"], input="Work Task\nDescription\n")

    # Manually move the task to ready/agent-available so it can be picked up
    import yaml

    task_file = next((tmp_path / "tasks").glob("*.yaml"))
    content = yaml.safe_load(task_file.read_text())
    content["lifecycle"] = "ready"
    content["ball"] = "agent"
    content["ball_reason"] = "available"
    content.pop("ball_prompt", None)
    task_file.write_text(yaml.safe_dump(content, sort_keys=False))

    # Run work command with mocked inputs
    # Inputs: Confirm Start (y), Confirm Complete (y), Summary
    result = runner.invoke(
        app, ["work", "--agent", "MyAgent"], input="y\ny\nFixed the bug\n", catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "TASK: Work Task" in result.stdout
    assert "Task claimed" in result.stdout
    assert "closed: completed" in result.stdout

    # Verify task state on disk
    task_file = next((tmp_path / "tasks").glob("*.yaml"))
    content = task_file.read_text()
    assert "lifecycle: closed" in content
    assert "outcome: completed" in content
    assert "Fixed the bug" in content


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

    # Setup: Initialize
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\njeff\n")

    # Create PLANNED/HIGH task
    runner.invoke(
        app,
        ["create", "--priority", "high", "--title", "High Task"],
        input="\n",  # default description
    )

    # Create COMPLETED/LOW task (create as draft/medium default, then update manually to simulate state)
    runner.invoke(app, ["create", "--priority", "low", "--title", "Low Task"], input="\n")

    # Find the Low Task file and close it
    import yaml

    for task_file in (tmp_path / "tasks").glob("*.yaml"):
        content = yaml.safe_load(task_file.read_text())
        if content["title"] == "Low Task":
            content["lifecycle"] = "closed"
            content["outcome"] = "completed"
            for key in ("ball", "ball_reason", "ball_prompt"):
                content.pop(key, None)
            task_file.write_text(yaml.safe_dump(content, sort_keys=False))
            break

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
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\njeff\n")

    result = runner.invoke(app, ["show", "non-existent-id"])
    assert result.exit_code == 1
    assert "Task 'non-existent-id' not found" in result.stdout


def _init_project() -> None:
    """Run init with the same answers the other tests in this file use."""
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\njeff\n")


def _only_task(tmp_path: Path) -> dict:
    """Load the single task file back off disk, which is where the truth is."""
    import yaml

    task_file = next((tmp_path / "tasks").glob("*.yaml"))
    loaded = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_promote_moves_draft_to_ready(tmp_path: Path, monkeypatch) -> None:
    """A draft promoted from the CLI becomes claimable, and the log says who did it."""
    monkeypatch.chdir(tmp_path)
    _init_project()
    runner.invoke(app, ["create", "--title", "Draft Task"], input="\n")

    task_id = next((tmp_path / "tasks").glob("*.yaml")).stem
    assert _only_task(tmp_path)["lifecycle"] == "draft"

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Promoted" in result.stdout

    content = _only_task(tmp_path)
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
    _init_project()
    runner.invoke(app, ["create", "--title", "Draft Task"], input="\n")
    task_id = next((tmp_path / "tasks").glob("*.yaml")).stem

    result = runner.invoke(
        app,
        ["promote", task_id, "--actor", "codex", "--note", "Spec reviewed and finished."],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    entry = _only_task(tmp_path)["log"][-1]
    assert entry["actor"] == "codex"
    assert entry["body"] == "Spec reviewed and finished."


def test_promote_refuses_a_non_draft_without_a_traceback(tmp_path: Path, monkeypatch) -> None:
    """Promoting an already-promoted task is an expected refusal, not a crash."""
    monkeypatch.chdir(tmp_path)
    _init_project()
    runner.invoke(app, ["create", "--title", "Draft Task"], input="\n")
    task_id = next((tmp_path / "tasks").glob("*.yaml")).stem

    assert runner.invoke(app, ["promote", task_id]).exit_code == 0
    log_length_before = len(_only_task(tmp_path)["log"])

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 1
    assert "is not a draft" in result.stdout
    assert "Traceback" not in result.stdout
    # The refused attempt left no trace: same lifecycle, no extra log entry.
    after = _only_task(tmp_path)
    assert after["lifecycle"] == "ready"
    assert len(after["log"]) == log_length_before


def test_promote_missing_task_reports_not_found(tmp_path: Path, monkeypatch) -> None:
    """A bad task id reads the same as it does from `show`."""
    monkeypatch.chdir(tmp_path)
    _init_project()

    result = runner.invoke(app, ["promote", "task-nope"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Task 'task-nope' not found" in result.stdout


def test_promote_without_an_actor_refuses_rather_than_guessing(tmp_path: Path, monkeypatch) -> None:
    """With no default_user and no --actor, refuse instead of writing an anonymous
    transition -- an unattributed state change is worse than a refused one."""
    monkeypatch.chdir(tmp_path)
    # No init at all, so the default config applies and default_user is null.
    runner.invoke(app, ["create", "--title", "Draft Task"], input="\n")
    task_id = next((tmp_path / "tasks").glob("*.yaml")).stem

    result = runner.invoke(app, ["promote", task_id], catch_exceptions=False)

    assert result.exit_code == 1
    assert "No actor" in result.stdout
    assert _only_task(tmp_path)["lifecycle"] == "draft"


def test_load_config_fallback(tmp_path: Path, monkeypatch) -> None:
    """Verify that commands work with default config if not initialized."""
    monkeypatch.chdir(tmp_path)

    # Don't run init. Just try to list tasks (which loads config).
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No tasks found" in result.stdout
