"""A deterministic evaluation harness for the AgentJobs MCP surface.

Section 9 of ``docs/mcp-integration-design.md`` asks for ten realistic scenarios with
recorded tool traces, final task state, and any attempted filesystem write.

**These evaluate the interface, not a model.** Each scenario performs the sequence of
tool calls a correct agent would make -- and, for the scenarios about refusal, the
sequence a *confused* one would make -- then asserts on the trace and the persisted
YAML. A model-driven eval would be more lifelike and far less useful here: it would
fail differently on every run, and a failure would not say whether the model wandered
or the surface is wrong. What is worth guaranteeing is that an agent doing the right
thing succeeds, and an agent doing the wrong thing is stopped with a reason it can
act on. Both of those are properties of this code.

The artifact is the point of the harness. Every call and its outcome is recorded, so
a release has evidence rather than a green tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from mcp import types

from agentjobs.mcp.tools import ToolRegistry

#: Bumped when the scenario set or the evidence format changes, so an artifact from a
#: previous shape is recognisable rather than silently compared against a new one.
EVAL_FORMAT_VERSION = 1


@dataclass
class ToolCall:
    """One recorded tool invocation and what came back."""

    tool: str
    arguments: Dict[str, Any]
    ok: bool
    summary: str
    error_code: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Serialise for the evidence artifact."""
        return {
            "tool": self.tool,
            # Operation ids are random per run; recording them would make every
            # artifact differ from the last for no informative reason.
            "arguments": {
                key: value for key, value in self.arguments.items() if key != "operation_id"
            },
            "ok": self.ok,
            "summary": self.summary,
            "error_code": self.error_code,
        }


@dataclass
class ScenarioResult:
    """The outcome of one scenario."""

    name: str
    intent: str
    passed: bool
    calls: List[ToolCall] = field(default_factory=list)
    final_state: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    failure: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Serialise for the evidence artifact."""
        return {
            "name": self.name,
            "intent": self.intent,
            "passed": self.passed,
            "calls": [call.to_payload() for call in self.calls],
            "final_state": self.final_state,
            "notes": self.notes,
            "failure": self.failure,
        }


class Recorder:
    """Calls the tools under evaluation, recording every one."""

    def __init__(self, registry: ToolRegistry) -> None:
        """Bind the recorder to one built tool registry."""
        self._registry = registry
        self.calls: List[ToolCall] = []

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        """Invoke a tool expecting success, recording the call."""
        result = await self._invoke(tool, arguments)
        if isinstance(result, types.CallToolResult):
            code = (result.structuredContent or {}).get("code")
            raise AssertionError(f"{tool} failed unexpectedly ({code})")
        content, structured = result
        self.calls.append(
            ToolCall(
                tool=tool,
                arguments=dict(arguments),
                ok=True,
                summary=content[0].text if content else "",
            )
        )
        return dict(structured)

    async def expect_refusal(self, tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        """Invoke a tool expecting a structured refusal, recording the call.

        A refusal is a first-class outcome here, not a test failure. Half the point of
        the surface is that the wrong move is stopped with a reason.
        """
        result = await self._invoke(tool, arguments)
        if not isinstance(result, types.CallToolResult):
            raise AssertionError(f"{tool} succeeded but should have been refused")
        body = result.structuredContent or {}
        self.calls.append(
            ToolCall(
                tool=tool,
                arguments=dict(arguments),
                ok=False,
                summary=body.get("message", ""),
                error_code=body.get("code"),
            )
        )
        return body

    async def _invoke(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        from agentjobs.mcp.server import validate_arguments
        from agentjobs.mcp.errors import ToolError
        from agentjobs.mcp.results import failure

        try:
            definition = self._registry.get(tool)
            validate_arguments(definition, arguments)
            return await definition.handler(arguments)
        except ToolError as exc:
            return failure(exc)


@dataclass
class EvalReport:
    """Every scenario's result, ready to write as one artifact."""

    results: List[ScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every scenario passed."""
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> List[str]:
        """Names of the scenarios that failed."""
        return [result.name for result in self.results if not result.passed]

    def to_payload(self) -> Dict[str, Any]:
        """The complete artifact."""
        return {
            "format_version": EVAL_FORMAT_VERSION,
            "generated": datetime.now(tz=timezone.utc).isoformat(),
            "passed": self.passed,
            "scenario_count": len(self.results),
            "scenarios": [result.to_payload() for result in self.results],
        }

    def write(self, path: Path) -> Path:
        """Write the artifact, creating its directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_payload(), indent=2), encoding="utf-8")
        return path

    def table(self) -> str:
        """A pass/fail table for a release note or a terminal."""
        width = max((len(result.name) for result in self.results), default=0)
        lines = [f"{'scenario'.ljust(width)}  result  calls"]
        for result in self.results:
            mark = "pass" if result.passed else "FAIL"
            lines.append(f"{result.name.ljust(width)}  {mark}    {len(result.calls)}")
        return "\n".join(lines)


Scenario = Callable[..., Any]
