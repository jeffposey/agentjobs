"""Every registered project gets the MCP server entry a dispatched agent needs.

Before task-202 `agentjobs init` wrote a config, a tasks directory and a registry row,
and no MCP wiring at all -- so an agent dispatched into a project AgentJobs had itself
set up came up with none of its tools and fell back to the CLI. The fallback works,
which is why the gap survived so long: nothing failed, the session merely got slower and
several turns more confused. These tests pin the file into existence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app as api_app
from agentjobs.cli import app as cli_app
from agentjobs.dispatch.runner import mcpjson_server_names
from agentjobs.project_setup import (
    MCP_CONFIG_FILENAME,
    ProjectError,
    ensure_mcp_server_entry,
)

runner = CliRunner()


def read_mcp(root: Path) -> Dict[str, Any]:
    document = json.loads((root / MCP_CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


class TestEnsureMcpServerEntry:
    def test_writes_the_console_script_form_when_there_is_no_file(self, tmp_path: Path) -> None:
        written = ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8876")

        assert written == tmp_path / MCP_CONFIG_FILENAME
        entry = read_mcp(tmp_path)["mcpServers"]["agentjobs"]
        # The console script, never an interpreter path: this file is committed and
        # travels to machines where that virtualenv does not exist.
        assert entry["command"] == "agentjobs"
        assert entry["args"] == ["mcp"]
        assert entry["env"]["AGENTJOBS_URL"] == "http://127.0.0.1:8876"

    def test_trims_a_trailing_slash_from_the_address(self, tmp_path: Path) -> None:
        ensure_mcp_server_entry(tmp_path, " http://127.0.0.1:8876/ ")

        url = read_mcp(tmp_path)["mcpServers"]["agentjobs"]["env"]["AGENTJOBS_URL"]
        assert url == "http://127.0.0.1:8876"

    def test_merges_beside_other_servers_and_other_keys(self, tmp_path: Path) -> None:
        (tmp_path / MCP_CONFIG_FILENAME).write_text(
            json.dumps({"mcpServers": {"other": {"command": "other"}}, "extra": True}),
            encoding="utf-8",
        )

        ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8765")

        document = read_mcp(tmp_path)
        assert document["extra"] is True
        assert document["mcpServers"]["other"] == {"command": "other"}
        assert document["mcpServers"]["agentjobs"]["command"] == "agentjobs"

    def test_leaves_an_existing_agentjobs_entry_exactly_as_it_was(self, tmp_path: Path) -> None:
        # AgentJobs' own clone pins a virtualenv interpreter on purpose. Correcting one
        # of these during an unrelated command is how a working setup disappears.
        pinned = {"mcpServers": {"agentjobs": {"command": "C:/venv/python.exe", "args": ["-m"]}}}
        path = tmp_path / MCP_CONFIG_FILENAME
        path.write_text(json.dumps(pinned), encoding="utf-8")
        before = path.read_bytes()

        assert ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8876") is None
        assert path.read_bytes() == before

    @pytest.mark.parametrize(
        "content",
        ["not json at all", '["a", "list"]', '{"mcpServers": "a string"}'],
    )
    def test_refuses_a_file_it_cannot_safely_merge_into(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / MCP_CONFIG_FILENAME
        path.write_text(content, encoding="utf-8")

        with pytest.raises(ProjectError):
            ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8876")
        assert path.read_text(encoding="utf-8") == content

    def test_the_written_name_is_the_one_dispatch_pre_approves(self, tmp_path: Path) -> None:
        # The whole point: dispatch reads these names out of the file and passes them in
        # `enabledMcpjsonServers`, so a background session is never asked to approve a
        # server at a prompt it has no terminal to answer.
        ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8876")

        assert mcpjson_server_names(tmp_path) == ["agentjobs"]


class TestInitWritesTheEntry:
    def _init(self, port: str = "9123") -> None:
        result = runner.invoke(
            cli_app,
            ["init"],
            input=f"Test Project\ntasks\nprompts\n{port}\njeff\n",
        )
        assert result.exit_code == 0, result.output

    def test_a_new_project_is_left_with_working_mcp_wiring(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTJOBS_API_BASE", raising=False)

        self._init()

        entry = read_mcp(tmp_path)["mcpServers"]["agentjobs"]
        assert entry["env"]["AGENTJOBS_URL"] == "http://127.0.0.1:9123"

    def test_the_machines_declared_address_beats_the_projects_port(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A machine serving somewhere other than the CLI default says so once, in the
        # same place dispatch already reads it, and both stop being wrong together.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENTJOBS_API_BASE", "http://127.0.0.1:8876")

        self._init()

        entry = read_mcp(tmp_path)["mcpServers"]["agentjobs"]
        assert entry["env"]["AGENTJOBS_URL"] == "http://127.0.0.1:8876"

    def test_a_malformed_file_is_reported_without_failing_the_init(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / MCP_CONFIG_FILENAME).write_text("{ broken", encoding="utf-8")

        result = runner.invoke(
            cli_app, ["init"], input="Test Project\ntasks\nprompts\n9123\njeff\n"
        )

        assert result.exit_code == 0, result.output
        assert "No MCP server entry written" in result.output
        assert (tmp_path / ".agentjobs" / "config.yaml").exists()


class TestProjectMcpSetupCommand:
    def test_backfills_a_project_registered_before_this_existed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("AGENTJOBS_API_BASE", raising=False)
        root = tmp_path / "legacy"
        (root / ".agentjobs").mkdir(parents=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Legacy", "gui": {"port": 8899}}),
            encoding="utf-8",
        )

        result = runner.invoke(cli_app, ["project", "mcp-setup", str(root)])

        assert result.exit_code == 0, result.output
        url = read_mcp(root)["mcpServers"]["agentjobs"]["env"]["AGENTJOBS_URL"]
        assert url == "http://127.0.0.1:8899"

    def test_an_explicit_url_wins(self, tmp_path: Path) -> None:
        result = runner.invoke(
            cli_app, ["project", "mcp-setup", str(tmp_path), "--url", "http://host:8876/"]
        )

        assert result.exit_code == 0, result.output
        url = read_mcp(tmp_path)["mcpServers"]["agentjobs"]["env"]["AGENTJOBS_URL"]
        assert url == "http://host:8876"

    def test_reports_rather_than_rewrites_an_existing_entry(self, tmp_path: Path) -> None:
        ensure_mcp_server_entry(tmp_path, "http://127.0.0.1:8876")

        result = runner.invoke(cli_app, ["project", "mcp-setup", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "already declares" in result.output

    def test_refuses_a_directory_that_is_not_there(self, tmp_path: Path) -> None:
        result = runner.invoke(cli_app, ["project", "mcp-setup", str(tmp_path / "nope")])

        assert result.exit_code == 1
        assert "Not a directory" in result.output


@pytest.fixture()
def onboarding_client(tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("AGENTJOBS_API_BASE", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()
    with TestClient(api_app) as client:
        yield client
    reset_dependency_cache()


class TestWebInitWritesTheEntry:
    def test_a_project_created_from_the_ui_gets_the_same_wiring(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = tmp_path / "from-the-ui"
        root.mkdir()

        response = onboarding_client.post("/api/projects/init", json={"path": str(root)})

        assert response.status_code == 201, response.text
        entry = read_mcp(root)["mcpServers"]["agentjobs"]
        assert entry["command"] == "agentjobs"
        # Derived from the socket the request arrived on, not from the project's port:
        # this server is the one the new project's agents will be talking to.
        assert entry["env"]["AGENTJOBS_URL"].startswith("http://")

    def test_a_malformed_file_does_not_fail_the_registration(
        self, onboarding_client: TestClient, tmp_path: Path
    ) -> None:
        root = tmp_path / "awkward"
        root.mkdir()
        (root / MCP_CONFIG_FILENAME).write_text("{ broken", encoding="utf-8")

        response = onboarding_client.post("/api/projects/init", json={"path": str(root)})

        assert response.status_code == 201, response.text
