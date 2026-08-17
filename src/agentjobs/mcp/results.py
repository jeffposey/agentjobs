"""Shared adapters between domain payloads and MCP tool results.

Every tool returns the same two things: ``structuredContent`` for clients that
consume structured results, and a short human-readable text block for clients that
do not. Centralising the conversion is what keeps the two from disagreeing, and what
keeps a failing tool from degrading into an empty success -- section 2 of the design
forbids turning a broken or ambiguous record into an empty result.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from mcp import types

from .errors import ToolError

#: A tool result the SDK will split into ``content`` and ``structuredContent``.
ToolOutput = Tuple[List[types.ContentBlock], Dict[str, Any]]


def success(payload: Dict[str, Any], summary: str) -> ToolOutput:
    """Build a successful tool result from a structured payload and a summary line.

    The summary is not a rendering of the payload -- a client without structured
    result support should get the one sentence that matters, not a reformatted dump
    of JSON it could have read itself.
    """
    return [types.TextContent(type="text", text=summary)], payload


def failure(error: ToolError) -> types.CallToolResult:
    """Build an ``isError`` result that still carries structured content.

    Returned as a ``CallToolResult`` rather than raised, because the SDK's own
    exception path produces a text-only error and drops the structured payload the
    design requires. Returning the result object also skips the SDK's output-schema
    validation, which is correct: an error body is not an instance of the tool's
    success schema.
    """
    payload = error.to_payload()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=True,
    )


def read_only_annotations(title: str) -> types.ToolAnnotations:
    """Annotate a tool that only reads.

    Annotations are hints for client UX -- they are never authorisation. The server
    enforces nothing by setting them, and a client is free to ignore them.
    """
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def mutation_annotations(title: str, *, destructive: bool = False) -> types.ToolAnnotations:
    """Annotate a tool that writes.

    ``idempotentHint`` is true for every mutation here because they all carry a
    caller-generated ``operation_id`` and replay rather than write twice.
    """
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=True,
        openWorldHint=False,
    )
