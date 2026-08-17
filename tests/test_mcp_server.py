"""Protocol and process behaviour for the ``agentjobs mcp`` STDIO server.

The domain tool inventory is empty at this layer, so these tests are about the
boundary: what the client is told at initialize, where bytes go, and how startup
refuses an unusable service.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from agentjobs.__version__ import __version__
from agentjobs.client import TaskClient
from agentjobs.mcp import compat, config, errors, instructions, results, server, tools
from agentjobs.models_v2 import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
class TestConfig:
    def test_defaults_when_nothing_is_supplied(self):
        resolved = config.McpConfig.resolve(env={})
        assert resolved.base_url == config.DEFAULT_BASE_URL
        assert resolved.timeout == config.DEFAULT_TIMEOUT

    def test_environment_supplies_both_settings(self):
        resolved = config.McpConfig.resolve(
            env={config.BASE_URL_ENV: "http://example.test:9000/", config.TIMEOUT_ENV: "12"}
        )
        assert resolved.base_url == "http://example.test:9000"
        assert resolved.timeout == 12.0

    def test_explicit_arguments_beat_the_environment(self):
        resolved = config.McpConfig.resolve(
            base_url="http://argv.test",
            timeout=5.0,
            env={config.BASE_URL_ENV: "http://env.test", config.TIMEOUT_ENV: "99"},
        )
        assert resolved.base_url == "http://argv.test"
        assert resolved.timeout == 5.0

    @pytest.mark.parametrize("value", ["0", "-1", str(config.MAX_TIMEOUT + 1)])
    def test_unusable_timeouts_are_refused(self, value):
        with pytest.raises(config.ConfigError):
            config.McpConfig.resolve(env={config.TIMEOUT_ENV: value})

    def test_non_numeric_timeout_names_the_variable(self):
        with pytest.raises(config.ConfigError, match=config.TIMEOUT_ENV):
            config.McpConfig.resolve(env={config.TIMEOUT_ENV: "soon"})


# ----------------------------------------------------------------------------
# Version policy
# ----------------------------------------------------------------------------
class TestVersionPolicy:
    def test_identical_versions_are_compatible(self):
        assert compat.check_version(client_version="0.1.0", server_version="0.1.4") is None

    def test_below_one_the_minor_is_the_breaking_axis(self):
        message = compat.check_version(client_version="0.1.0", server_version="0.2.0")
        assert message is not None
        assert "0.1.0" in message and "0.2.0" in message

    def test_above_one_the_minor_may_skew(self):
        assert compat.check_version(client_version="1.4.0", server_version="1.9.2") is None

    def test_above_one_a_major_mismatch_fails(self):
        assert compat.check_version(client_version="1.4.0", server_version="2.0.0") is not None

    def test_an_unparseable_version_is_not_quietly_accepted(self):
        message = compat.check_version(client_version="0.1.0", server_version="dev")
        assert message is not None

    def test_schema_mismatch_names_the_repair(self):
        message = compat.check_schema(client_schema=2, server_schema=1)
        assert message is not None
        assert "migrate-schema" in message


# ----------------------------------------------------------------------------
# Startup probe against a real API application
# ----------------------------------------------------------------------------
@pytest.fixture()
def live_client(running_service):
    """A TaskClient pointed at the real HTTP service, not a mock.

    Section 9 of the design is explicit that this layer is proven against a freshly
    started AgentJobs service. A mocked TaskClient would pass while the service
    served a shape the probe cannot read, which is the exact failure the probe is
    for.
    """
    client = TaskClient(running_service, timeout=10.0)
    yield client
    client.close()


class _ClientWithoutVersionRoute(TaskClient):
    """Stands in for an AgentJobs old enough to have no /api/version route.

    It asks the live service for a path that genuinely does not exist, so the probe
    sees a real 404 from a real server rather than a fabricated exception.
    """

    def service_version(self):
        return self._request("GET", "/api/version-from-a-newer-release").json()


class TestStartupProbe:
    def test_version_endpoint_reports_package_and_schema(self, live_client):
        payload = live_client.service_version()
        assert payload == {"version": __version__, "schema_version": SCHEMA_VERSION}

    def test_probe_succeeds_against_the_real_service(self, live_client):
        info = compat.probe_service(
            live_client, client_version=__version__, client_schema=SCHEMA_VERSION
        )
        assert info.version == __version__
        assert info.schema_version == SCHEMA_VERSION
        assert isinstance(info.project_ids, tuple)

    def test_unreachable_service_says_so_and_does_not_start_one(self):
        # Port 1 is reserved and never listening; a connection error here is the
        # transport failure the probe has to translate, not a mocked stand-in.
        client = TaskClient("http://127.0.0.1:1", timeout=1.0)
        with pytest.raises(compat.StartupError) as caught:
            compat.probe_service(client, client_version=__version__, client_schema=SCHEMA_VERSION)
        client.close()
        message = str(caught.value)
        assert "agentjobs serve" in message
        assert "does not start one for you" in message

    def test_version_skew_is_refused_at_startup(self, live_client):
        with pytest.raises(compat.StartupError, match="version mismatch"):
            compat.probe_service(live_client, client_version="9.9.9", client_schema=SCHEMA_VERSION)

    def test_a_project_record_missing_required_fields_is_refused(self, monkeypatch, live_client):
        monkeypatch.setattr(live_client, "list_projects", lambda: [{"id": "p", "name": "P"}])
        with pytest.raises(compat.StartupError, match="tasks_directory"):
            compat.probe_service(
                live_client, client_version=__version__, client_schema=SCHEMA_VERSION
            )

    def test_a_service_returning_an_empty_version_body_is_refused(self, monkeypatch, live_client):
        monkeypatch.setattr(live_client, "service_version", dict)
        with pytest.raises(compat.StartupError, match="predates the /api/version endpoint"):
            compat.probe_service(
                live_client, client_version=__version__, client_schema=SCHEMA_VERSION
            )

    def test_a_404_on_the_version_route_is_not_reported_as_unreachable(self, running_service):
        """An older AgentJobs is running, not absent, and the message must say so.

        Observed for real: a service started before /api/version existed answered
        /api/health and 404'd the version probe, and the first draft told the reader
        to start a server that was visibly already running. Driven through a genuine
        404 -- a route the old build simply does not have -- because the branch keys
        on the status code, which a stubbed return value cannot produce.
        """
        client = _ClientWithoutVersionRoute(running_service, timeout=10.0)
        with pytest.raises(compat.StartupError) as caught:
            compat.probe_service(client, client_version=__version__, client_schema=SCHEMA_VERSION)
        client.close()
        message = str(caught.value)
        assert "predates the /api/version endpoint" in message
        assert "not reachable" not in message


# ----------------------------------------------------------------------------
# Shared result and error adapters
# ----------------------------------------------------------------------------
class TestResultAdapters:
    def test_success_carries_structure_and_a_summary(self):
        content, structured = results.success({"ok": True}, "One task listed.")
        assert structured == {"ok": True}
        assert [block.text for block in content] == ["One task listed."]

    def test_failure_is_an_error_result_that_still_carries_structure(self):
        error = errors.ToolError(
            code=errors.ErrorCode.REVISION_CONFLICT,
            message="Stale revision.",
            task_id="task-001-x",
            current_task={"id": "task-001-x"},
        )
        result = results.failure(error)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["code"] == "revision_conflict"
        assert result.structuredContent["retryable"] is False
        assert result.structuredContent["current_task"] == {"id": "task-001-x"}
        # A failure must never read as an empty success to a text-only client.
        assert result.content and result.content[0].text.strip()

    def test_absent_details_are_omitted_not_nulled(self):
        payload = errors.ToolError(
            code=errors.ErrorCode.TASK_NOT_FOUND, message="No such task."
        ).to_payload()
        assert set(payload) == {"code", "message", "retryable"}

    @pytest.mark.parametrize(
        "code,expected",
        [
            (errors.ErrorCode.LOCK_TIMEOUT, True),
            (errors.ErrorCode.SERVICE_UNAVAILABLE, True),
            (errors.ErrorCode.INVALID_TRANSITION, False),
            (errors.ErrorCode.INVALID_INPUT, False),
        ],
    )
    def test_retryability_follows_the_code(self, code, expected):
        assert errors.ToolError(code=code, message="x").retryable is expected

    def test_every_code_validates_against_the_published_error_schema(self):
        import jsonschema

        for code in errors.ErrorCode:
            payload = errors.ToolError(code=code, message="x").to_payload()
            jsonschema.validate(instance=payload, schema=errors.ERROR_SCHEMA)

    def test_read_and_mutation_annotations_differ_where_it_matters(self):
        read = results.read_only_annotations("Read")
        write = results.mutation_annotations("Write", destructive=True)
        assert read.readOnlyHint is True and read.destructiveHint is False
        assert write.readOnlyHint is False and write.destructiveHint is True


# ----------------------------------------------------------------------------
# Tool registry
# ----------------------------------------------------------------------------
class TestToolRegistry:
    def test_the_foundation_registry_is_empty(self):
        assert tools.ToolRegistry().declarations() == []

    def test_an_unknown_tool_is_a_structured_error_naming_the_alternatives(self):
        registry = tools.ToolRegistry()
        with pytest.raises(errors.ToolError) as caught:
            registry.get("task_teleport")
        assert caught.value.code is errors.ErrorCode.INVALID_INPUT
        assert "none registered" in (caught.value.suggested_action or "")

    def test_duplicate_registration_is_refused(self):
        definition = _stub_definition("task_stub")
        registry = tools.ToolRegistry([definition])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(definition)

    def test_a_registered_tool_is_declared_with_both_schemas(self):
        registry = tools.ToolRegistry([_stub_definition("task_stub")])
        declared = registry.declarations()[0]
        assert declared.name == "task_stub"
        assert declared.inputSchema["type"] == "object"
        assert declared.outputSchema is not None


def _stub_definition(name: str) -> tools.ToolDefinition:
    async def handler(arguments):
        return results.success({"echo": dict(arguments)}, "echoed")

    return tools.ToolDefinition(
        name=name,
        title="Stub",
        description="Echoes its arguments.",
        input_schema={"type": "object", "additionalProperties": True, "properties": {}},
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["echo"],
            "properties": {"echo": {"type": "object"}},
        },
        annotations=results.read_only_annotations("Stub"),
        handler=handler,
    )


# ----------------------------------------------------------------------------
# Initialize contract
# ----------------------------------------------------------------------------
class TestInitializeContract:
    def test_the_server_identifies_itself_with_the_installed_version(self):
        options = server.initialization_options(server.build_server(tools.ToolRegistry()))
        assert options.server_name == "agentjobs"
        assert options.server_version == __version__

    def test_tools_capability_is_advertised(self):
        options = server.initialization_options(server.build_server(tools.ToolRegistry()))
        assert options.capabilities.tools is not None

    def test_the_leading_rule_survives_a_512_character_truncation(self):
        prefix = instructions.SERVER_INSTRUCTIONS[: instructions.LEADING_RULE_BUDGET]
        assert "task YAML is generated state" in prefix
        assert "projects_list" in prefix
        assert "claim, handoff, release, and close" in prefix
        assert "Reading task YAML is allowed" in prefix


# ----------------------------------------------------------------------------
# In-process protocol round trip
# ----------------------------------------------------------------------------
async def _round_trip(registry: tools.ToolRegistry, call: tuple[str, dict] | None):
    """Drive one initialize/list/call exchange over in-memory streams."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.build_server(registry),
        raise_exceptions=False,
    ) as session:
        listed = await session.list_tools()
        called = None
        if call is not None:
            called = await session.call_tool(call[0], call[1])
        return listed, called


class TestStdoutOwnership:
    def test_sys_stdout_is_redirected_while_the_protocol_holds_the_real_one(self, capsys):
        with server.protocol_stdout() as stream:
            assert sys.stdout is sys.stderr
            print("a stray print from anywhere in the process")
            assert stream is not sys.stdout
        assert sys.stdout is not sys.stderr

        captured = capsys.readouterr()
        assert "a stray print" in captured.err
        assert "a stray print" not in captured.out

    def test_the_real_stream_is_restored_and_still_usable_afterwards(self, capsys):
        with server.protocol_stdout():
            pass
        print("normal output works again")
        assert "normal output works again" in capsys.readouterr().out


class TestProtocolRoundTrip:
    def test_an_empty_server_lists_no_tools(self):
        listed, _ = anyio.run(_round_trip, tools.ToolRegistry(), None)
        assert listed.tools == []

    def test_a_tool_call_returns_structured_content_and_text(self):
        registry = tools.ToolRegistry([_stub_definition("task_stub")])
        _, called = anyio.run(_round_trip, registry, ("task_stub", {"a": 1}))
        assert called is not None
        assert called.isError is False
        assert called.structuredContent == {"echo": {"a": 1}}
        assert isinstance(called.content[0], types.TextContent)

    def test_an_unknown_tool_returns_the_structured_error_not_an_empty_result(self):
        _, called = anyio.run(_round_trip, tools.ToolRegistry(), ("task_teleport", {}))
        assert called is not None
        assert called.isError is True
        assert called.structuredContent is not None
        assert called.structuredContent["code"] == "invalid_input"


# ----------------------------------------------------------------------------
# The packaged command, as a real subprocess
# ----------------------------------------------------------------------------
def _stdio_params(env_overrides: dict[str, str]) -> StdioServerParameters:
    env = dict(os.environ)
    env.update(env_overrides)
    # Unbuffered so a stray write would reach us in the same order the process made
    # it, rather than being hidden by buffering at exit.
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentjobs.cli", "mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture(scope="module")
def running_service(tmp_path_factory):
    """A real AgentJobs HTTP service in a child process.

    Given its own ``AGENTJOBS_HOME`` for the same reason the suite-wide fixture
    isolates the in-process registry: this is a separate process, so it would
    otherwise read the developer's real machine registry and serve their real
    projects.
    """
    import socket
    import time

    from agentjobs.projects import HOME_ENV

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = dict(os.environ)
    env[HOME_ENV] = str(tmp_path_factory.mktemp("service-home"))
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
        yield url
    finally:
        process.terminate()
        process.wait(timeout=30)


class TestPackagedCommand:
    def test_initialize_over_real_stdio_publishes_name_version_and_instructions(
        self, running_service
    ):
        async def exercise():
            params = _stdio_params({config.BASE_URL_ENV: running_service})
            with anyio.fail_after(90):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        listed = await session.list_tools()
                        return initialized, listed

        initialized, listed = anyio.run(exercise)

        assert initialized.serverInfo.name == "agentjobs"
        assert initialized.serverInfo.version == __version__
        assert initialized.capabilities.tools is not None
        assert initialized.instructions is not None
        assert "task YAML is generated state" in initialized.instructions[:512]
        assert listed.tools == []

    def test_stdout_carries_only_json_rpc(self, running_service):
        """Every stdout line must parse as JSON-RPC.

        This is the assertion that would catch a banner, a warning, or a dependency's
        stray print -- each of which corrupts the stream in a way that surfaces as an
        unrelated client-side parse error.
        """
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
        env[config.BASE_URL_ENV] = running_service
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
        assert lines, "the server produced no protocol output"
        for line in lines:
            parsed = json.loads(line)
            assert parsed["jsonrpc"] == "2.0"

        # The connection banner proves diagnostics are emitted, and emitted to stderr.
        assert "Connected to AgentJobs" in completed.stderr

    def test_startup_against_a_missing_service_fails_on_stderr(self):
        env = dict(os.environ)
        env[config.BASE_URL_ENV] = "http://127.0.0.1:1"
        env[config.TIMEOUT_ENV] = "2"

        completed = subprocess.run(
            [sys.executable, "-m", "agentjobs.cli", "mcp"],
            cwd=str(REPO_ROOT),
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=90,
        )

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "agentjobs serve" in completed.stderr
        assert "Traceback" not in completed.stderr


# ----------------------------------------------------------------------------
# Architectural boundary
# ----------------------------------------------------------------------------
FORBIDDEN_NAMES = frozenset({"TaskManager", "TaskStorage"})
FORBIDDEN_MODULES = frozenset({"manager", "storage"})


class TestBoundary:
    def test_the_mcp_package_never_imports_the_manager_or_storage(self):
        """The write path is REST-only by construction, not by convention.

        Parsed rather than grepped: the docstrings in this package name both classes
        while describing the boundary, and a substring search would either fail on
        the prose or have to be loosened until it stopped catching anything. The AST
        walk covers deferred imports inside a function too, which is where the rule
        would realistically be broken by accident.
        """
        offenders: list[str] = []
        for path in (REPO_ROOT / "src" / "agentjobs" / "mcp").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    tail = (node.module or "").rsplit(".", 1)[-1]
                    if tail in FORBIDDEN_MODULES:
                        offenders.append(f"{path.name}:{node.lineno} imports {node.module}")
                    offenders.extend(
                        f"{path.name}:{node.lineno} imports {alias.name}"
                        for alias in node.names
                        if alias.name in FORBIDDEN_NAMES
                    )
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.name}:{node.lineno} imports {alias.name}"
                        for alias in node.names
                        if alias.name.rsplit(".", 1)[-1] in FORBIDDEN_MODULES
                    )
                elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}:{node.lineno} references {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}:{node.lineno} references {node.attr}")
        assert offenders == []
