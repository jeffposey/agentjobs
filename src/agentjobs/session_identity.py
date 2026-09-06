"""Who is asking: the agent session behind a request, read off its own environment.

A Claude Code session -- the desktop app's, a terminal's, a background one -- publishes
its session uuid to every child process as ``CLAUDE_CODE_SESSION_ID``. The MCP server and
the CLI are such children, so a claim they send can carry the session that made it, and
the server can write a run record for it (task-354). Nothing here shells out or checks
anything: this is the *claim*, and the poller is what verifies a session is still there.

Kept out of the ``dispatch`` package on purpose, because :mod:`agentjobs.client` reads it
and must stay importable without the manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
"""The full session uuid Claude Code publishes into a session's process environment."""

DISPATCHED_RUN_ENV = "AGENTJOBS_RUN_ID"
"""Set inside a dispatched run. Such a session already has a run record, so it is not one."""


@dataclass(frozen=True)
class SessionIdentity:
    """One interactive session, as it names itself."""

    session_id: str
    cwd: str
    driver: str = "claude"

    @classmethod
    def from_environment(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        cwd: Optional[Path] = None,
    ) -> Optional["SessionIdentity"]:
        """This process's session, or ``None`` when it is not inside one.

        ``None`` is the ordinary answer for a human at the CLI, a test, and the desktop
        dashboard's own server. It is also the answer inside a dispatched run: that
        session was started by AgentJobs, has a run record already, and must not be
        given a second one by the claim it makes on arrival.
        """
        values = env if env is not None else os.environ
        if (values.get(DISPATCHED_RUN_ENV) or "").strip():
            return None
        raw = (values.get(SESSION_ID_ENV) or "").strip()
        if not raw:
            return None
        return cls(session_id=raw, cwd=str(cwd or Path.cwd()))
