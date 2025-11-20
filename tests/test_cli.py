"""CLI integration tests for AgentJobs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner

from agentjobs import TaskStatus, Priority
from agentjobs.cli import app, _ensure_gitignore

runner = CliRunner()


def test_cli_init_create_list_show(tmp_path: Path, monkeypatch) -> None:
    """Exercise the main CLI commands end-to-end."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init"],
        input="Test Project\ntasks\nprompts\n9000\n",
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
    assert payload["status"] == TaskStatus.DRAFT.value
    assert payload["title"] == "Sample Task"


def test_work_command_flow(tmp_path: Path, monkeypatch) -> None:
    """Verify the interactive agent workflow (pick task -> start -> complete)."""
    monkeypatch.chdir(tmp_path)
    
    # Setup: Initialize and create a task
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\n")
    runner.invoke(app, ["create"], input="Work Task\nDescription\n")
    
    # Manually update task status to READY so it can be picked up
    import yaml
    task_file = next((tmp_path / "tasks").glob("*.yaml"))
    content = yaml.safe_load(task_file.read_text())
    content["status"] = "ready"
    task_file.write_text(yaml.safe_dump(content))
    
    # Run work command with mocked inputs
    # Inputs: Confirm Start (y), Confirm Complete (y), Summary
    result = runner.invoke(
        app,
        ["work", "--agent", "MyAgent"],
        input="y\ny\nFixed the bug\n",
        catch_exceptions=False
    )
    
    assert result.exit_code == 0
    assert "TASK: Work Task" in result.stdout
    assert "Task marked IN_PROGRESS" in result.stdout
    assert "marked COMPLETED" in result.stdout
    
    # Verify task status on disk
    task_file = next((tmp_path / "tasks").glob("*.yaml"))
    content = task_file.read_text()
    assert "status: completed" in content
    assert "Fixed the bug" in content


def test_serve_command_args(monkeypatch) -> None:
    """Ensure the serve command correctly parses arguments and calls uvicorn.run."""
    with patch("uvicorn.run") as mock_run:
        result = runner.invoke(
            app,
            ["serve", "--host", "0.0.0.0", "--port", "9000", "--reload"],
            catch_exceptions=False
        )
        
        assert result.exit_code == 0
        assert "Starting AgentJobs server at http://0.0.0.0:9000" in result.stdout
        
        mock_run.assert_called_once_with(
            "agentjobs.api.main:app",
            host="0.0.0.0",
            port=9000,
            reload=True
        )


def test_list_tasks_filtering(tmp_path: Path, monkeypatch) -> None:
    """Verify that list correctly filters tasks by status and priority."""
    monkeypatch.chdir(tmp_path)
    
    # Setup: Initialize
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\n")
    
    # Create PLANNED/HIGH task
    runner.invoke(
        app, 
        ["create", "--priority", "high", "--title", "High Task"], 
        input="\n" # default description
    )
    
    # Create COMPLETED/LOW task (create as draft/medium default, then update manually to simulate state)
    runner.invoke(
        app, 
        ["create", "--priority", "low", "--title", "Low Task"], 
        input="\n"
    )
    
    # Find the Low Task file and update it
    import yaml
    for task_file in (tmp_path / "tasks").glob("*.yaml"):
        content = yaml.safe_load(task_file.read_text())
        if content["title"] == "Low Task":
            content["status"] = "completed"
            task_file.write_text(yaml.safe_dump(content))
            break
            
    # Test Filter by Status
    result_status = runner.invoke(app, ["list", "--status", "completed"])
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
        app,
        ["migrate", str(source_dir / "*.md"), str(target_dir)],
        catch_exceptions=False
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
        app,
        ["load-test-data", "--storage-dir", "tasks"],
        catch_exceptions=False
    )
    
    assert result.exit_code == 0
    assert "Loaded" in result.stdout
    assert "test tasks" in result.stdout
    
    # Verify files created
    task_files = list((tmp_path / "tasks").glob("*.yaml"))
    assert len(task_files) > 0
    
    # Run again to verify update/refresh logic
    result_refresh = runner.invoke(
        app,
        ["load-test-data", "--storage-dir", "tasks"],
        catch_exceptions=False
    )
    assert result_refresh.exit_code == 0
    assert "refreshed" in result_refresh.stdout


def test_show_task_not_found(tmp_path: Path, monkeypatch) -> None:
    """Verify error handling when showing a non-existent task."""
    monkeypatch.chdir(tmp_path)
    
    # Initialize to ensure manager can run
    runner.invoke(app, ["init"], input="Test Project\ntasks\nprompts\n9000\n")
    
    result = runner.invoke(app, ["show", "non-existent-id"])
    assert result.exit_code == 1
    assert "Task 'non-existent-id' not found" in result.stdout


def test_load_config_fallback(tmp_path: Path, monkeypatch) -> None:
    """Verify that commands work with default config if not initialized."""
    monkeypatch.chdir(tmp_path)
    
    # Don't run init. Just try to list tasks (which loads config).
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No tasks found" in result.stdout
