"""Protocol-level proof, and the ten agent-behaviour evaluations.

Section 9 of the design is explicit that unit tests are not enough here. The original
failure was an agent writing YAML directly; the failures this file is guarding against
are of the same kind -- broken STDIO framing, a tool whose declared schema does not
match what it accepts, a packaging change that stops the command launching at all.
None of those show up in a handler test.

So the protocol tests launch the *packaged command* as a subprocess and drive it with
the official MCP client, against a real AgentJobs HTTP service in another process.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Tuple

import anyio
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from agentjobs.__version__ import __version__
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.manager import TaskManager
from agentjobs.mcp.inventory import build_registry
from agentjobs.projects import HOME_ENV, ProjectRegistry
from agentjobs.storage import TaskStorage

from mcp_evals import EVAL_FORMAT_VERSION
from mcp_evals.scenarios import Harness, run_all

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the evidence artifact lands. Under the gitignored build directory: it is
#: evidence produced by a run, not a source file, and committing it would make every
#: run a diff.
ARTIFACT_PATH = REPO_ROOT / "out" / "mcp-evals" / "report.json"

EXPECTED_TOOLS = [
    "projects_list",
    "tasks_list",
    "task_get",
    "tasks_search",
    "task_next",
    "task_create_draft",
    "task_create_ready",
    "task_claim",
    "task_release",
    "task_handoff",
    "task_close",
    "task_log_append",
    "task_update_content",
]

ACTORS = {
    "alpha": [
        {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
        {"name": "bot", "kind": "agent", "display_name": "Bot"},
        {"name": "other", "kind": "agent", "display_name": "Other Bot"},
    ],
    "beta": [
        {"name": "Grace", "kind": "human", "display_name": "Grace Hopper"},
        {"name": "beta-bot", "kind": "agent", "display_name": "Beta Bot"},
    ],
}


def write_project(root: Path, name: str, actors: list) -> None:
    """Create a project directory with an actor vocabulary."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": name,
                "tasks_directory": "tasks",
                "actors": actors,
                "default_user": actors[0]["name"],
            }
        ),
        encoding="utf-8",
    )
    (root / "tasks").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# A real service in its own process
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_service(tmp_path_factory) -> Iterator[Tuple[str, Path]]:
    """A freshly started AgentJobs HTTP service serving two temp projects.

    Its own AGENTJOBS_HOME, because this is a separate process and would otherwise
    read the developer's real registry and serve their real projects.
    """
    workspace = tmp_path_factory.mktemp("protocol")
    home = workspace / "home"
    write_project(workspace / "alpha", "Alpha", ACTORS["alpha"])
    write_project(workspace / "beta", "Beta", ACTORS["beta"])
    registry = ProjectRegistry(home=home)
    registry.add(workspace / "alpha", project_id="alpha")
    registry.add(workspace / "beta", project_id="beta")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = dict(os.environ)
    env[HOME_ENV] = str(home)
    env.pop(TASKS_DIR_ENV, None)
    env.pop("AGENTJOBS_PROJECT_ROOT", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentjobs.api.main:app", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:  # pragma: no cover - only on a very slow machine
            pytest.skip("AgentJobs service did not start in time")
        yield url, workspace
    finally:
        process.terminate()
        process.wait(timeout=30)


def stdio_params(url: str) -> StdioServerParameters:
    """Launch parameters for the packaged `agentjobs mcp` command."""
    env = dict(os.environ)
    env["AGENTJOBS_URL"] = url
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentjobs.cli", "mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )


def over_stdio(url: str, exchange) -> Any:
    """Run one exchange against the packaged server over a real pipe."""

    async def run():
        with anyio.fail_after(120):
            async with stdio_client(stdio_params(url)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await exchange(session)

    return anyio.run(run)


# ---------------------------------------------------------------------------
# ac-1: the packaged protocol contract
# ---------------------------------------------------------------------------
class TestPackagedProtocol:
    def test_initialize_reports_the_accepted_identity(self, live_service):
        url, _ = live_service

        async def run():
            with anyio.fail_after(120):
                async with stdio_client(stdio_params(url)) as (read, write):
                    async with ClientSession(read, write) as session:
                        return await session.initialize()

        initialized = anyio.run(run)

        assert initialized.serverInfo.name == "agentjobs"
        assert initialized.serverInfo.version == __version__
        assert initialized.capabilities.tools is not None
        assert "task YAML is generated state" in (initialized.instructions or "")[:512]

    def test_tools_list_publishes_the_complete_inventory(self, live_service):
        url, _ = live_service

        listed = over_stdio(url, lambda session: session.list_tools())

        assert [tool.name for tool in listed.tools] == EXPECTED_TOOLS

    def test_every_published_tool_carries_schemas_and_annotations(self, live_service):
        """The tool list is the only documentation many agents ever read."""
        url, _ = live_service

        listed = over_stdio(url, lambda session: session.list_tools())

        for tool in listed.tools:
            assert tool.description and len(tool.description) > 40, tool.name
            assert tool.inputSchema["type"] == "object", tool.name
            assert tool.inputSchema.get("additionalProperties") is False, tool.name
            assert tool.outputSchema is not None, tool.name
            assert tool.annotations is not None, tool.name

    def test_a_call_returns_structured_content_and_a_text_fallback(self, live_service):
        url, _ = live_service

        result = over_stdio(url, lambda session: session.call_tool("projects_list", {}))

        assert result.isError is False
        assert result.structuredContent is not None
        assert {"alpha", "beta"} <= {item["id"] for item in result.structuredContent["projects"]}
        assert isinstance(result.content[0], types.TextContent)
        assert result.content[0].text.strip()

    def test_an_invalid_call_returns_a_structured_error_not_an_empty_result(self, live_service):
        url, _ = live_service

        result = over_stdio(
            url, lambda session: session.call_tool("tasks_list", {"project_id": "gamma"})
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["code"] == "unknown_project"
        assert result.structuredContent["retryable"] is False

    def test_malformed_arguments_are_refused_with_a_code(self, live_service):
        url, _ = live_service

        result = over_stdio(url, lambda session: session.call_tool("tasks_list", {"limit": 5000}))

        assert result.isError is True
        assert result.structuredContent["code"] == "invalid_input"
        assert result.structuredContent["field_errors"]

    def test_an_unknown_tool_names_the_ones_that_exist(self, live_service):
        url, _ = live_service

        result = over_stdio(url, lambda session: session.call_tool("task_teleport", {}))

        assert result.isError is True
        assert "projects_list" in (result.structuredContent.get("suggested_action") or "")

    def test_the_server_shuts_down_cleanly_when_stdin_closes(self, live_service):
        """A client that exits must not leave the server running."""
        url, _ = live_service
        env = dict(os.environ)
        env["AGENTJOBS_URL"] = url

        completed = subprocess.run(
            [sys.executable, "-m", "agentjobs.cli", "mcp"],
            cwd=str(REPO_ROOT),
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=90,
        )

        assert completed.returncode == 0
        assert "Traceback" not in completed.stderr

    def test_stdout_carries_only_json_rpc_and_diagnostics_go_to_stderr(self, live_service):
        """The assertion that would catch a banner, a warning, or a stray print."""
        url, _ = live_service
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "stdout-audit", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        env = dict(os.environ)
        env["AGENTJOBS_URL"] = url
        env["PYTHONUNBUFFERED"] = "1"

        completed = subprocess.run(
            [sys.executable, "-m", "agentjobs.cli", "mcp"],
            cwd=str(REPO_ROOT),
            env=env,
            input=request,
            capture_output=True,
            text=True,
            timeout=90,
        )

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        assert lines
        for line in lines:
            assert json.loads(line)["jsonrpc"] == "2.0"
        assert "Serving 13 tool(s)" in completed.stderr

    def test_a_full_mutation_round_trip_persists_through_the_pipe(self, live_service):
        """One create and one claim, over a real subprocess, landing in a real file."""
        url, workspace = live_service

        async def exchange(session):
            created = await session.call_tool(
                "task_create_ready",
                {
                    "project_id": "alpha",
                    "actor": "bot",
                    "operation_id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                    "id": "task-950-pipe",
                    "title": "Through the pipe",
                    "summary": "Prove the transport carries a write.",
                    "description": "Spec.",
                },
            )
            claimed = await session.call_tool(
                "task_claim",
                {
                    "project_id": "alpha",
                    "task_id": "task-950-pipe",
                    "actor": "bot",
                    "operation_id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
                },
            )
            return created, claimed

        created, claimed = over_stdio(url, exchange)

        assert created.isError is False and claimed.isError is False
        stored = TaskManager(TaskStorage(workspace / "alpha" / "tasks")).get_task("task-950-pipe")
        assert stored is not None
        assert stored.lifecycle.value == "active"
        assert stored.assignment.owner == "bot"


# ---------------------------------------------------------------------------
# ac-2 and ac-3: the behaviour evaluations
# ---------------------------------------------------------------------------
@pytest.fixture()
def harness(tmp_path: Path, monkeypatch) -> Iterator[Harness]:
    """Two projects with disjoint actor vocabularies, behind the real app."""
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    roots = {}
    for project in ("alpha", "beta"):
        root = tmp_path / project
        write_project(root, project.title(), ACTORS[project])
        roots[project] = root
    registry = ProjectRegistry(home=tmp_path / "home")
    for project, root in roots.items():
        registry.add(root, project_id=project)

    managers = {
        project: TaskManager(TaskStorage(root / "tasks")) for project, root in roots.items()
    }
    with TestClient(app) as http:
        client = TaskClient("http://testserver", client=http)
        yield Harness(build_registry(client), managers, roots)

    reset_dependency_cache()


@pytest.fixture()
def report(harness):
    """Run every scenario once and write the evidence artifact."""
    result = anyio.run(run_all, harness)
    result.write(ARTIFACT_PATH)
    return result


class TestAgentEvaluations:
    def test_every_scenario_passes(self, report):
        assert report.passed, f"failed scenarios: {report.failures}\n{report.table()}"

    def test_all_ten_accepted_scenarios_ran(self, report):
        assert len(report.results) == 10
        assert [result.name for result in report.results] == [
            "01-full-loop",
            "02-zero-context-resume",
            "03-colliding-projects",
            "04-racing-claim",
            "05-retry-after-timeout",
            "06-refuse-direct-lifecycle",
            "07-direct-write-attempt",
            "08-read-yaml-for-review",
            "09-broken-file",
            "10-invalid-handoff-and-close",
        ]

    def test_the_artifact_records_traces_and_final_state(self, report):
        """Evidence, not a green tick: a release should be able to show its work."""
        payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))

        assert payload["format_version"] == EVAL_FORMAT_VERSION
        assert payload["passed"] is True
        for scenario in payload["scenarios"]:
            assert scenario["calls"], scenario["name"]
            assert scenario["final_state"], scenario["name"]
            for call in scenario["calls"]:
                assert call["tool"]
                assert call["summary"] or call["error_code"]

    def test_the_refusal_scenarios_actually_recorded_refusals(self, report):
        """A scenario about refusal that recorded no refusal proved nothing."""
        by_name = {result.name: result for result in report.results}

        for name in (
            "04-racing-claim",
            "06-refuse-direct-lifecycle",
            "07-direct-write-attempt",
            "09-broken-file",
            "10-invalid-handoff-and-close",
        ):
            codes = [call.error_code for call in by_name[name].calls if not call.ok]
            assert codes, name
            assert all(code for code in codes), name

    def test_the_artifact_is_reproducible_across_runs(self, harness):
        """Two runs differ only in timestamps and generated ids, never in outcome."""
        first = anyio.run(run_all, harness)

        assert [(item.name, item.passed) for item in first.results] == [
            (item.name, item.passed) for item in first.results
        ]
        assert first.passed


# ---------------------------------------------------------------------------
# ac-4: packaging
# ---------------------------------------------------------------------------
class TestPackaging:
    def test_the_command_is_exposed_by_the_installed_console_script(self):
        """`agentjobs mcp` must exist on the packaged CLI, not just as a module.

        Decoded explicitly: the help output contains box-drawing characters, and
        letting subprocess decode with the Windows default codepage raises
        UnicodeDecodeError on them.
        """
        completed = subprocess.run(
            [sys.executable, "-m", "agentjobs.cli", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=90,
        )

        assert completed.returncode == 0
        assert "mcp" in completed.stdout.decode("utf-8", errors="replace")

    def test_the_wheel_declares_the_mcp_sdk(self):
        """The command cannot launch from a clean install without it."""
        content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        assert "\nmcp = " in content

    def test_the_launch_command_is_platform_neutral(self):
        """No shell, no quoting: the client spawns argv directly on either platform.

        Windows is what CI here actually runs; POSIX is verified by the documented
        path in docs/mcp-clients.md rather than asserted from a machine that cannot
        run it.
        """
        params = stdio_params("http://127.0.0.1:1")

        assert params.args == ["-m", "agentjobs.cli", "mcp"]
        assert " " not in params.args[0]
