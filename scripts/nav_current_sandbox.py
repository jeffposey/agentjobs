"""Stand up the primary nav on its own port so the current-tab marking can be seen.

task-336 was reported from a screenshot of the real dashboard: nothing in the bar said
which destination you were on, and the one entry that *was* coloured -- Create, in
`text-blue-300` -- read as the selected tab from the Dashboard. Both halves of that are
paint, so the only way to review the fix is to look at it.

One throwaway project with enough in it that every destination has something to render,
so you can walk the bar and watch the marking follow you:

    python scripts/nav_current_sandbox.py [port] [host]

``host`` defaults to 127.0.0.1. Pass ``0.0.0.0`` to reach it from a phone over the
tailnet, which is the surface this bar is actually read on.

Then compare the two states this change is about, without constructing either by hand:

* the destination you are on -- brighter text, a blue tint, a hairline ring
* the ones you are not -- muted, and identical to each other, Create included

Narrow the window below ``NAV_INLINE_MIN_PX`` and the same marking appears inside the
burger panel, which is where this app is read on a phone.

**task-345 added a third state to compare, and it is the interesting one.** The bar now
carries navigation only: Analytics, Dispatch settings, Playbooks and the API docs moved
into the kebab menu at the right-hand end. So there are now routes the bar has no entry
for, and standing on one of them the honest answer to "where am I" is *nothing marked*.
Walk from Dashboard (marked) to Dispatch settings (nothing marked) and back; before this
change the second of those lit up Dashboard, which was a confident wrong answer. Both
URLs are printed below so the pair can be compared without hunting for them.

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
    # 0.0.0.0 rather than the loopback default puts this on the tailnet, which is how
    # a phone reviews it. It serves throwaway data out of a temporary directory, so
    # there is nothing here to expose.
    host = argv[1] if len(argv) > 1 else "127.0.0.1"

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

    # `0.0.0.0` is a bind address, not somewhere a browser can go, and printing it as
    # a link sends the reader to a page that will not load.
    reachable = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    base = f"http://{reachable}:{port}/app/p/sandbox-nav"
    print(f"[nav] current-tab sandbox at {base}", flush=True)
    if host in {"0.0.0.0", "::"}:
        print(
            f"[nav] also on every interface of this machine at port {port} --"
            " substitute this machine's tailnet address to read it from a phone",
            flush=True,
        )
    print("[nav] in the bar -- one of these is marked wherever you stand:", flush=True)
    for suffix, label in [
        ("", "Dashboard"),
        ("/tasks", "Tasks"),
        ("/tasks/new", "Create"),
        ("/runs", "Runs"),
    ]:
        print(f"[nav]   {label:<18} {base}{suffix}", flush=True)
    print(f"[nav]   task detail -- Tasks stays marked: {base}/tasks/task-101", flush=True)
    print("[nav] behind the kebab at the right-hand end -- and nothing in the bar is", flush=True)
    print("[nav] marked while you are on one of them (task-345):", flush=True)
    for suffix, label in [
        ("/analytics", "Analytics"),
        ("/dispatch", "Dispatch settings"),
        ("/playbooks", "Playbooks"),
    ]:
        print(f"[nav]   {label:<18} {base}{suffix}", flush=True)
    print("[nav]   API Docs           behind the same kebab, at /docs", flush=True)
    print(
        "[nav] narrow the window under NAV_INLINE_MIN_PX for the burger panel;"
        " the kebab is there at every width",
        flush=True,
    )

    import uvicorn

    from agentjobs.api.main import app

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
