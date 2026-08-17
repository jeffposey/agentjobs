"""Assembling the tool inventory the MCP server publishes.

One place that knows which tools exist. Handlers close over the client rather than
looking one up, so a tool cannot reach a service the server did not connect to and
prove compatible at startup.
"""

from __future__ import annotations

from ..client import TaskClient
from .read_tools import read_tool_definitions
from .tools import ToolRegistry


def build_registry(client: TaskClient) -> ToolRegistry:
    """Every tool this server offers, bound to one connected client."""
    registry = ToolRegistry()
    for definition in read_tool_definitions(client):
        registry.register(definition)
    return registry
