"""Run AgentJobs against a real temporary project for the Playwright path."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
import yaml

from agentjobs.project_setup import build_project_config

PORT_ENV = "AGENTJOBS_E2E_PORT"
CHECKOUT = Path(__file__).resolve().parents[2]


def resolve_port() -> int:
    """Take the port from the environment, and refuse to invent one.

    ``playwright.config.ts`` derives a port from the checkout's path and passes it
    here, so several worktrees can run the gate at once without contending for one
    socket. A default in this file would be a second opinion about which port is in
    play: the config would watch one address while the server bound another, and the
    run would time out looking at nothing. There is no default for that reason.
    """
    raw = os.environ.get(PORT_ENV)
    if raw is None or raw.strip() == "":
        raise SystemExit(
            f"{PORT_ENV} is not set, so this server does not know which port to bind.\n"
            f"It is normally started by Playwright, which derives the port for {CHECKOUT} "
            "and passes it in. To run it by hand, set the variable first:\n"
            f"    {PORT_ENV}=20000 poetry run python e2e/run_server.py"
        )
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{PORT_ENV} must be a port number, got {raw!r}.") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"{PORT_ENV} must be between 1 and 65535, got {port}.")
    return port


def write_dispatch_config(home: Path) -> None:
    """Define one runner this machine will admit to, and enable nothing.

    The master switch is on and the project is deliberately *off*, so the browser path
    starts where a real machine starts: dispatch installed, this project not yet
    trusted with it. Turning it on is the first thing the spec does, which is the only
    way to prove the toggle actually writes this file.

    The runner sleeps rather than exiting, so a run is still live when the page renders
    and the cancel button has something real to stop. ``require_clean_tree`` is off
    because the temporary project is not a git repository at all -- the clean-tree gate
    has its own tests, and standing up a repo here would only test git.

    ``actor`` is not decoration. A runner is named for the invocation and writes as an
    identity from the project's ``actors:``, and task-159's guard refuses a dispatch
    whose runner would act as an id the project does not configure. Without it this
    runner would act as ``e2e-sleeper``, the dispatch is refused before any run exists,
    and the spec's run list is empty rather than wrong -- which is exactly how this
    harness broke. ``claude`` is one of the actors ``build_project_config`` writes.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "e2e-sleeper": {
                        "argv": [sys.executable, "-c", "import time; time.sleep(120)"],
                        "actor": "claude",
                    },
                },
                "projects": {"_local": {"enabled": False, "require_clean_tree": False}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def main() -> None:
    """Pin a fresh project before importing the app, then serve until Playwright exits."""
    port = resolve_port()
    # Named, not just bound: a failure here is read alongside Playwright's own line
    # about the same port, and between them they say which checkout owns it.
    print(f"[e2e] serving {CHECKOUT} on http://127.0.0.1:{port}", flush=True)
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
        write_dispatch_config(root / ".agentjobs-home")
        uvicorn.run(
            "agentjobs.api.main:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
