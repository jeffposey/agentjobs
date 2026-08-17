"""The tool registry the MCP server dispatches through.

The registry is the seam between protocol plumbing (this module and ``server``) and
the domain tools. It exists already, empty, so that project routing, read tools, and
mutation tools each add handlers to one inventory instead of each teaching the server
about itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Union

from mcp import types

from .errors import ErrorCode, ToolError
from .results import ToolOutput

#: A handler receives already-JSON-Schema-validated arguments. It returns a normal
#: result, or a ``CallToolResult`` when it needs to report a structured failure.
ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Union[ToolOutput, types.CallToolResult]]]


@dataclass(frozen=True)
class ToolDefinition:
    """One tool: what the client sees, and what runs when it is called."""

    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    annotations: types.ToolAnnotations
    handler: ToolHandler

    def to_tool(self) -> types.Tool:
        """Render the client-visible declaration."""
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            inputSchema=self.input_schema,
            outputSchema=self.output_schema,
            annotations=self.annotations,
        )


class ToolRegistry:
    """An ordered, name-unique collection of tool definitions."""

    def __init__(self, definitions: Optional[List[ToolDefinition]] = None) -> None:
        """Start empty, or from a prepared list."""
        self._definitions: Dict[str, ToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        """Add a tool, refusing a duplicate name."""
        if definition.name in self._definitions:
            raise ValueError(f"Tool {definition.name!r} is already registered.")
        self._definitions[definition.name] = definition

    def __len__(self) -> int:
        """Number of registered tools."""
        return len(self._definitions)

    def __contains__(self, name: object) -> bool:
        """Whether a tool name is registered."""
        return name in self._definitions

    @property
    def names(self) -> List[str]:
        """Registered tool names, in registration order."""
        return list(self._definitions)

    def declarations(self) -> List[types.Tool]:
        """Every tool as the client sees it, in registration order."""
        return [definition.to_tool() for definition in self._definitions.values()]

    def get(self, name: str) -> ToolDefinition:
        """Look up a tool, raising a structured error for an unknown name."""
        try:
            return self._definitions[name]
        except KeyError:
            known = ", ".join(self._definitions) or "none registered"
            raise ToolError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Unknown tool {name!r}.",
                suggested_action=f"Call one of: {known}.",
            ) from None
