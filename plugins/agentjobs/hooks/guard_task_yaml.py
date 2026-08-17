#!/usr/bin/env python3
"""Codex `PreToolUse` entry point for the AgentJobs direct-write guard.

Reads one JSON event on stdin, asks `task_write_guard` to decide, and writes one JSON
decision on stdout in Codex's shape:

    {"permission_decision": "allow"}
    {"permission_decision": "deny", "permission_decision_reason": "..."}

Codex is told the answer either way, because its protocol expects a decision on every
event. All the thinking is in `task_write_guard`, which the Claude entry point beside
this one shares; only this serialisation is Codex-specific.

**This is a guardrail, not a security boundary.** The bypasses -- a hosted tool the
hook never observes, a disabled or untrusted hook, an obfuscated script, anything
started outside Codex -- are listed in `task_write_guard`, along with why reading is
deliberately never blocked. Read that module before trusting this one; it is where the
guard actually lives.

Dependency-free and offline by design. The path insertion below is what lets it import
its own neighbour when a client launches it by absolute path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_write_guard import Decision, run  # noqa: E402


def serialise(decision: Dict[str, Any]) -> Optional[str]:
    """Render a guard decision as the JSON object Codex reads."""
    if decision.get("decision") == Decision.DENY:
        return json.dumps(
            {
                "permission_decision": Decision.DENY,
                "permission_decision_reason": decision.get("reason", ""),
            }
        )
    return json.dumps({"permission_decision": Decision.ALLOW})


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read one event, decide, print the decision."""
    del argv
    return run(serialise)


if __name__ == "__main__":
    raise SystemExit(main())
