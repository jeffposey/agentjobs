"""The ``agentjobs mcp`` STDIO server.

Protocol plumbing only. The tool inventory lives in :mod:`agentjobs.mcp.tools` and is
populated by the domain children of the MCP program; this module owns process
behaviour: where bytes go, what the client is told at initialize, and how startup
fails.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from io import TextIOWrapper
from typing import Any, Iterator, Mapping, Optional, TextIO, Union

import anyio
import jsonschema
import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from ..__version__ import __version__
from ..client import TaskClient
from ..models_v2 import SCHEMA_VERSION
from .compat import ServiceInfo, StartupError, probe_service
from .config import McpConfig
from .errors import ErrorCode, FieldError, ToolError
from .instructions import SERVER_INSTRUCTIONS
from .inventory import build_registry
from .results import ToolOutput, failure
from .tools import ToolDefinition, ToolRegistry

SERVER_NAME = "agentjobs"

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def protocol_stdout() -> Iterator[TextIO]:
    """Hand the real stdout to the protocol and point ``sys.stdout`` at stderr.

    A single stray ``print`` -- ours, a dependency's, or a warning that some library
    routes to stdout -- corrupts the JSON-RPC stream and takes the whole session down
    with an error the user cannot trace back to its source. Filtering our own output
    would only cover our own code, so instead the real stream is taken away from
    everyone and given to the transport alone: after this, writing to ``sys.stdout``
    is harmless because it *is* stderr.
    """
    original = sys.stdout
    stream = TextIOWrapper(original.buffer, encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    try:
        yield stream
    finally:
        sys.stdout = original
        with contextlib.suppress(ValueError, OSError):
            stream.flush()
            # detach() rather than close(): the wrapper is ours, but the file
            # descriptor underneath it belongs to the process.
            stream.detach()


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record and warning to stderr.

    ``force=True`` because anything imported before us may already have installed a
    root handler, and a handler pointed at stdout is exactly the failure this
    function exists to prevent.
    """
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.captureWarnings(True)


def validate_arguments(definition: ToolDefinition, arguments: Mapping[str, Any]) -> None:
    """Check tool arguments against the tool's published input schema.

    Raises :class:`ToolError` naming the offending path, so a schema failure carries
    the same ``invalid_input`` code and ``field_errors`` as every other refusal. The
    alternative -- letting the SDK validate -- produces a bare sentence and no code,
    which is exactly the case an agent most needs to branch on, because it is the one
    it can fix without asking anybody.
    """
    validator = jsonschema.Draft202012Validator(definition.input_schema)
    problems = sorted(validator.iter_errors(dict(arguments)), key=lambda item: list(item.path))
    if not problems:
        return
    raise ToolError(
        code=ErrorCode.INVALID_INPUT,
        message=(
            f"Arguments for {definition.name!r} do not match its schema: " f"{problems[0].message}"
        ),
        field_errors=[
            FieldError(
                path=".".join(str(part) for part in problem.path) or "(root)",
                message=problem.message,
            )
            for problem in problems[:5]
        ],
        suggested_action=(
            f"Re-read the inputSchema published for {definition.name!r} and send only "
            "the fields it declares."
        ),
    )


def build_server(registry: ToolRegistry) -> Server[Any, Any]:
    """Create the MCP server bound to a tool registry."""
    server: Server[Any, Any] = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return registry.declarations()

    # validate_input=False because the SDK's own check reports a schema failure as
    # text with no structuredContent, which is the one class of error an agent could
    # not branch on. Validating here instead makes every failure -- malformed
    # arguments included -- arrive in the same shape with the same codes.
    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str, arguments: Optional[Mapping[str, Any]]
    ) -> Union[ToolOutput, types.CallToolResult]:
        try:
            definition = registry.get(name)
            validate_arguments(definition, arguments or {})
            return await definition.handler(arguments or {})
        except ToolError as exc:
            return failure(exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled error in tool %s", name)
            return failure(
                ToolError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"Tool {name!r} failed unexpectedly: {exc}",
                    suggested_action="Check the MCP server's stderr log for details.",
                )
            )

    return server


def initialization_options(server: Server[Any, Any]) -> InitializationOptions:
    """Describe this server to the client at initialize time."""
    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=__version__,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
        instructions=SERVER_INSTRUCTIONS,
    )


def connect(config: McpConfig) -> tuple[TaskClient, ServiceInfo]:
    """Open a client to the configured service and prove it is usable.

    Raises :class:`StartupError` when it is not. The client is closed on failure so a
    refused startup leaves no open socket behind.
    """
    client = TaskClient(config.base_url, timeout=config.timeout)
    try:
        info = probe_service(
            client,
            client_version=__version__,
            client_schema=SCHEMA_VERSION,
        )
    except StartupError:
        client.close()
        raise
    return client, info


async def serve_stdio(registry: ToolRegistry, stream: TextIO) -> None:
    """Run the MCP server over an already-claimed stdout stream until shutdown."""
    server = build_server(registry)
    options = initialization_options(server)
    async with mcp.server.stdio.stdio_server(stdout=anyio.wrap_file(stream)) as (
        read_stream,
        write_stream,
    ):
        await server.run(read_stream, write_stream, options)


def run(config: Optional[McpConfig] = None) -> int:
    """Entry point for ``agentjobs mcp``. Returns a process exit code.

    Startup failures are reported on stderr and exit non-zero rather than raising: a
    traceback in an MCP client's error pane is noise, and the useful content is the
    one sentence saying which service was unreachable or incompatible.
    """
    settings = config or McpConfig.resolve()
    configure_logging()

    try:
        client, info = connect(settings)
    except StartupError as exc:
        print(f"agentjobs mcp: {exc}", file=sys.stderr)
        return 1

    logger.info(
        "Connected to AgentJobs %s (task schema v%s) at %s; %d project(s) available.",
        info.version,
        info.schema_version,
        info.base_url,
        len(info.project_ids),
    )

    registry = build_registry(client)
    logger.info("Serving %d tool(s): %s.", len(registry), ", ".join(registry.names))

    try:
        with protocol_stdout() as stream:
            anyio.run(serve_stdio, registry, stream)
    except KeyboardInterrupt:  # pragma: no cover - interactive interrupt
        logger.info("Interrupted; shutting down.")
    finally:
        client.close()
    return 0
