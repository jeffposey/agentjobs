"""Stand up the actions menu on its own port so it can be looked at and pressed.

task-168 put an actions menu in the top-right of the AgentJobs header: a kebab that
opens About and the API docs, and the shell task-345 and task-170 add their entries to.
Everything worth reviewing about it is paint and gesture -- whether the kebab reads as
"actions" rather than as a second hamburger, whether the popup lands somewhere a thumb
can reach, whether opening it shoves the page down -- and none of that survives a diff.

    python scripts/actions_menu_sandbox.py [port] [--tailnet]

``--tailnet`` binds this machine's Tailscale address instead of loopback, so the same
URL opens on a phone on the tailnet. That is not a nicety here: the menu's whole reason
for existing is to hold things a phone cannot otherwise reach, and a resized desktop
window is not a thumb. It is opt-in because the default has to stay loopback -- nothing
in a sandbox is meant to be reachable by accident.

Both states the change is about are seeded, so neither has to be constructed by hand:

* **a desktop width** -- the whole nav row inline, the kebab alone at the right end.
  Widen past 1256px for it.
* **a phone width** -- the nav burger at the left, the kebab at the right, and the two
  never both open. Narrow below 1256px, or open the same URL on a phone.

There is a long project name and a task handed to a human on purpose: the switcher at
its widest and the attention badge showing are the case the one-line bar is measured
against, and a four-character project reviews a row the real one never has.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

DEFAULT_PORT = 8917

TASKS: List[Tuple[str, str]] = [
    ("task-101", "Teach the queue to explain itself"),
    ("task-102", "Collapse the two dependency banners into one"),
    ("task-103", "Stop the importer double-counting reruns"),
]


def tailnet_address() -> Optional[str]:
    """This machine's Tailscale IPv4, or ``None`` when Tailscale is not up.

    Asked of the CLI rather than read from an interface list, because that is the one
    answer that is wrong for the right reason when Tailscale is installed but logged
    out -- an address on the adapter that no peer can route to.
    """
    for candidate in (
        Path("C:/Program Files/Tailscale/tailscale.exe"),
        Path("tailscale"),
    ):
        try:
            result = subprocess.run(
                [str(candidate), "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        address = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        if result.returncode == 0 and address:
            return address
    return None


def build_project(root: Path, *, project_id: str, name: str) -> Path:
    """One throwaway project, with a few tasks so the pages behind the bar are not bare."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))
    for task_id, title in TASKS:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}.",
            description=(
                "Seeded by scripts/actions_menu_sandbox.py. Nothing here is real work; "
                "the record exists so the pages behind the header have something on them."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
    # One task stopped on a person, so the attention badge is showing. The badge is
    # 34px of the row the actions trigger now shares, so a bar reviewed without it is
    # the narrow case rather than the one the breakpoint holds for.
    manager.handoff(
        "task-101",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt="Decide whether the kebab reads as actions rather than as navigation.",
    )
    return project_root


def main() -> None:
    argv = sys.argv[1:]
    tailnet = "--tailnet" in argv
    positional = [argument for argument in argv if not argument.startswith("--")]
    port = int(positional[0]) if positional else DEFAULT_PORT

    host = "127.0.0.1"
    if tailnet:
        address = tailnet_address()
        if address is None:
            print("[menu] Tailscale is not reporting an address; staying on loopback", flush=True)
        else:
            host = address

    root = Path(tempfile.mkdtemp(prefix="agentjobs-actions-menu-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    # A long-ish name on purpose: the project switcher is the widest thing in the bar
    # and the first thing squeezed, so a four-character sandbox name would review the
    # bar at a width the real one never has.
    project = build_project(root, project_id="sandbox-menu", name="Actions Menu Sandbox")
    registry.add(project, project_id="sandbox-menu", name="Actions Menu Sandbox")

    base = f"http://{host}:{port}/app/p/sandbox-menu"
    print(f"[menu] actions-menu sandbox at {base}", flush=True)
    print("[menu]   the kebab is at the top-right of the header, at every width", flush=True)
    print("[menu]   press it: About, and a link to the API docs", flush=True)
    print("[menu]   About names the running version, this server, and the project", flush=True)
    print("[menu]   wide (>=1256px): the nav row is inline and the kebab is alone", flush=True)
    print("[menu]   narrow (<1256px): nav burger left, kebab right, never both open", flush=True)
    print(
        f"[menu]   task detail, a long page to check the popup overlays: {base}/tasks/task-101",
        flush=True,
    )
    if host == "127.0.0.1":
        print("[menu]   --tailnet binds the Tailscale address instead, for a phone", flush=True)
    else:
        print("[menu]   reachable from any device on the tailnet, phone included", flush=True)
    print("[menu] Ctrl-C stops it; the temporary corpus goes with it", flush=True)

    import uvicorn

    from agentjobs.api.main import app

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
