#!/usr/bin/env python3
"""Claude Code `PreToolUse` entry point for the AgentJobs direct-write guard.

Reads one JSON event on stdin, asks `task_write_guard` to decide, and writes Claude's
decision envelope on stdout when the answer is a denial:

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "..."}}

**On allow it prints nothing, and that is a deliberate difference from the Codex entry
point.** Claude's `permissionDecision: "allow"` does not mean "this hook has no
objection" -- it means "skip the permission system for this call". Emitting it on every
event would silently auto-approve every Edit, Write, NotebookEdit and Bash call in the
session, because those are exactly the tools this hook matches. Exiting 0 with no
output is how a hook says it has no opinion and normal permission handling should
continue, so that is what an allow does here. Codex's protocol expects a decision on
every event and has no such reading, which is why its entry point answers both ways.

Exit code is always 0. Claude treats exit 2 as an unconditional block that its own JSON
cannot override, and the guard's fail-open behaviour would be unreachable through it.

**This is a guardrail, not a security boundary.** The bypasses -- a hosted tool the
hook never observes, a disabled or untrusted hook, an obfuscated script, anything
started outside Claude Code -- are listed in `task_write_guard`, along with why reading
is deliberately never blocked. Read that module before trusting this one; it is where
the guard actually lives.

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

HOOK_EVENT_NAME = "PreToolUse"


def serialise(decision: Dict[str, Any]) -> Optional[str]:
    """Render a denial as Claude's envelope; render an allow as silence."""
    if decision.get("decision") != Decision.DENY:
        return None
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT_NAME,
                "permissionDecision": Decision.DENY,
                "permissionDecisionReason": decision.get("reason", ""),
            }
        }
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read one event, decide, print a denial or nothing at all."""
    del argv
    return run(serialise)


if __name__ == "__main__":
    raise SystemExit(main())
