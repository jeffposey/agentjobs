"""Stand up the task list's touch reorder on its own port, with throwaway data (task-589).

The grip beside each task used to drag with a mouse only, through the HTML5
drag-and-drop API, which a touch browser does not start from a finger. task-589 gives
it a pointer-events drag for a finger or a pen; a mouse keeps the HTML5 drag it had.

    python scripts/touch_reorder_sandbox.py [port]

What to try, on the tablet:

  * **Drag a grip** (the six-dot handle) up or down. The row fades, a line shows where
    it will land, and the list follows your finger.
  * **Hold it at the bottom or top edge** of the list. The list scrolls and the line
    keeps moving over the rows passing under your finger. There are 40 tasks in `low`,
    so there is room to scroll.
  * **Swipe anywhere on a row except the grip.** The list scrolls; nothing drags.
  * **Tap a grip without moving.** Nothing moves.
  * **Drag across a band header** (from HIGH into LOW). The same "Confirm a priority
    change" prompt a mouse drop raises.

**On the tailnet URL the drop is refused** -- a sandbox is not behind the tailnet front
door, so every write from it is refused by design (`scripts/sandbox_serve.py`). The
gesture, the line and the scroll are all reviewable there; the move itself is not saved.
The loopback URL saves it, and `e2e/queue-touch.spec.ts` proves the saved order survives
a reload. The real check is the tablet against the live dashboard after the merge.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME``, deleted when this process stops.
Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 8971
PROJECT_ID = "sandbox-touch"
PROJECT_NAME = "Sandbox: touch reorder"
USER = "Jeff Posey"


def seed(manager: Any) -> None:
    from agentjobs.models_v2 import Lifecycle, Priority

    number = 0

    def task(title: str, priority: Any) -> None:
        nonlocal number
        number += 1
        manager.create_task(
            id=f"task-{number:03d}",
            title=title,
            summary=f"Seeded for the touch reorder review: {title}.",
            description="Seeded by scripts/touch_reorder_sandbox.py. Nothing here is real work.",
            priority=priority,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )

    for index in range(1, 6):
        task(f"High {index}: drag me within the high band", Priority.HIGH)
    for index in range(1, 41):
        task(f"Low {index}: a long band to scroll through", Priority.LOW)


def build(root: Path) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=PROJECT_NAME, user=USER), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID)))
    return project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-touch-reorder-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    ProjectRegistry(home).add(build(root), project_id=PROJECT_ID, name=PROJECT_NAME)

    from sandbox_serve import review_base, serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    print(
        f"[review] touch reorder sandbox at {review_base(port)}/app/p/{PROJECT_ID}/tasks",
        flush=True,
    )
    print(
        f"[review]   loopback (saves moves): http://127.0.0.1:{port}/app/p/{PROJECT_ID}/tasks",
        flush=True,
    )
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
