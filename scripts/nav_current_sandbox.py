"""Stand up the primary nav on its own port so the current-tab marking can be seen.

task-336 was reported from a screenshot of the real dashboard: nothing in the bar said
which destination you were on, and the one entry that *was* coloured -- Create, in
`text-blue-300` -- read as the selected tab from the Dashboard. Both halves of that are
paint, so the only way to review the fix is to look at it.

One throwaway project with enough in it that every destination has something to render,
so you can walk the bar and watch the marking follow you:

    python scripts/nav_current_sandbox.py [port]

Then compare the two states this change is about, without constructing either by hand:

* the destination you are on -- brighter text, a blue tint, a hairline ring
* the ones you are not -- muted, and identical to each other, Create included

Narrow the window below 1100px and the same marking appears inside the burger panel,
which is where this app is read on a phone.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import yaml

DEFAULT_PORT = 8907

TASKS: List[Tuple[str, str]] = [
    ("task-101", "Teach the queue to explain itself"),
    ("task-102", "Collapse the two dependency banners into one"),
    ("task-103", "Stop the importer double-counting reruns"),
]


def build_project(root: Path, *, project_id: str, name: str) -> Path:
    """One throwaway project, with a few tasks so the list page is not empty."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(TaskStorage(project_root / "tasks"))
    for task_id, title in TASKS:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}.",
            description=(
                "Seeded by scripts/nav_current_sandbox.py. Nothing here is real work; "
                "the record exists so the pages behind the nav have something on them."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
    return project_root


def main() -> None:
    argv = sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-nav-current-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    # A long-ish name on purpose: the project switcher is the widest thing in the bar
    # and the first thing squeezed, so a four-character sandbox name would review the
    # bar at a width the real one never has.
    project = build_project(root, project_id="sandbox-nav", name="Navigation Sandbox")
    registry.add(project, project_id="sandbox-nav", name="Navigation Sandbox")

    base = f"http://127.0.0.1:{port}/app/p/sandbox-nav"
    print(f"[nav] current-tab sandbox at {base}", flush=True)
    for suffix, label in [
        ("", "Dashboard"),
        ("/tasks", "Tasks"),
        ("/tasks/new", "Create"),
        ("/dispatch", "Dispatch"),
        ("/playbooks", "Playbooks"),
        ("/runs", "Runs"),
    ]:
        print(f"[nav]   {label:<10} {base}{suffix}", flush=True)
    print(f"[nav]   task detail -- Tasks stays marked: {base}/tasks/task-101", flush=True)
    print("[nav] narrow the window under 1100px for the burger panel", flush=True)

    import uvicorn

    from agentjobs.api.main import app

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
