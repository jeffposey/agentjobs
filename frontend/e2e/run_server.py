"""Run AgentJobs against a real temporary project for the Playwright path."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn


def main() -> None:
    """Pin a fresh project before importing the app, then serve until Playwright exits."""
    with TemporaryDirectory(prefix="agentjobs-e2e-") as directory:
        root = Path(directory)
        os.environ["AGENTJOBS_PROJECT_ROOT"] = str(root)
        os.environ["AGENTJOBS_HOME"] = str(root / ".agentjobs-home")
        uvicorn.run(
            "agentjobs.api.main:app",
            host="127.0.0.1",
            port=18940,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
