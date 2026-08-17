"""Run AgentJobs against a real temporary project for the Playwright path."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
import yaml

from agentjobs.project_setup import build_project_config


def main() -> None:
    """Pin a fresh project before importing the app, then serve until Playwright exits."""
    with TemporaryDirectory(prefix="agentjobs-e2e-") as directory:
        root = Path(directory)
        os.environ["AGENTJOBS_PROJECT_ROOT"] = str(root)
        os.environ["AGENTJOBS_HOME"] = str(root / ".agentjobs-home")
        # A human actor, because every action the UI attributes to a person is
        # refused when the project has none -- so without one, the browser path
        # could only ever exercise the pages that ask nobody to act.
        config_path = root / ".agentjobs" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                build_project_config(project_name="End-to-end project", user="E2E Human"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        uvicorn.run(
            "agentjobs.api.main:app",
            host="127.0.0.1",
            port=18940,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
