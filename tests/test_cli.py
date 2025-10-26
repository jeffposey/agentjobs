"""CLI integration tests for AgentJobs."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentjobs import TaskStatus
from agentjobs.cli import app

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
