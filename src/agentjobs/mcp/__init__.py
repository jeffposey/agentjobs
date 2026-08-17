"""The AgentJobs MCP server.

A thin facade over the running AgentJobs HTTP service:

    agent -> MCP over STDIO -> TaskClient -> project-scoped REST -> TaskManager
    -> TaskStorage

Nothing in this package imports ``TaskManager`` or ``TaskStorage``. The authoritative
write path stays behind the REST service, so an MCP write is validated, locked, and
logged by exactly the same code as a CLI or GUI write.

Named ``agentjobs.mcp`` while depending on the top-level ``mcp`` SDK. Python 3's
absolute imports keep those apart: inside this package, ``import mcp`` still means
the SDK.

Deliberately re-exports nothing. Importing a name here would run this module -- and
therefore the SDK import -- for anyone who touches any part of the package, including
``agentjobs.mcp.config``, which the CLI reads to build its `--base-url` option on
every single command. Import from the submodule you actually need.
"""

from __future__ import annotations
