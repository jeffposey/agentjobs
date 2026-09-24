"""Stand up the task list's band headers on their own port, with throwaway data (task-563).

task-563 takes the priority chip off every sidebar row and puts a header above each
band instead ("CRITICAL TASKS", "HIGH TASKS", ...), moves the status chip up beside the
task id, and gives the title two lines. Where priority is still shown -- the task page,
the wide table, the slot board -- it is one shared mark: rising bars and an uppercase
word, with no box, so it cannot be read as a status chip.

    python scripts/priority_bands_sandbox.py [port]

What to look at:

  * **The task list** (`/tasks`): a header before each band, CRITICAL / HIGH / MEDIUM /
    LOW, and no priority on any row. The critical epic has a *medium* child, which stays
    nested under it in the critical band.
  * **Titles**: task-004 fits on one line, task-005 needs two, task-006 needs more than
    two and ends in an ellipsis (hover for the rest).
  * **Status beside the id**, in every colour the seed reaches: Ready, Needs review,
    Blocked, Draft, and Completed/Cancelled under `?status=all`.
  * **A filtered view**: `?q=bands` keeps the headers over what survives the filter, and
    `?status=all` adds a separate "HIGH TASKS · CLOSED" group after the open bands.
  * **The narrowest sidebar** (about 700px wide, where the list is a 320px column): the
    id line never wraps; a long status is clipped first. Below that width -- a phone --
    the list is the stacked card layout, whose Priority field shows the shared mark.
  * **Side by side**: any task page shows the status chip and the priority mark next to
    each other in its header. The dashboard's slot board shows the next queued tasks'
    priority with the same mark; nothing else on the dashboard shows priority since
    task-557 took out the Active tasks preview.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME``, deleted when this process stops.
Click anything, including the destructive controls. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 8967
PROJECT_ID = "sandbox-bands"
PROJECT_NAME = "Sandbox: priority as band headers"
USER = "Jeff Posey"

ONE_LINE = "Short title"
TWO_LINES = "A title long enough to need a second line in the sidebar, bands"
MANY_LINES = (
    "A title far longer than two lines can hold at any sidebar width: it wraps once, "
    "then stops with an ellipsis, and the whole of it is still in the tooltip and on the "
    "task page, bands"
)


def dispatch_config(home: Path, runner_script: Path) -> None:
    """Dispatch switched on so the slot board draws, with a controller that starts nothing."""
    config = {
        "version": 1,
        "enabled": True,
        "runners": {
            "fake": {
                "mode": "session",
                "actor": "claude",
                "argv": [sys.executable, str(runner_script), "{prompt}"],
            }
        },
        "projects": {PROJECT_ID: {"enabled": True, "runner": "fake", "posture": "auto"}},
        "limits": {"max_concurrent_runs": 2},
        "execution": {"controller": "shadow"},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def seed(manager: Any) -> None:
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority

    def task(task_id: str, title: str, priority: Any, **extra: Any) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"Seeded for the band-header review: {title}.",
            description="Seeded by scripts/priority_bands_sandbox.py. Nothing here is real work.",
            priority=priority,
            lifecycle=extra.pop("lifecycle", Lifecycle.READY),
            actor="claude",
            category="ux",
            **extra,
        )

    # Critical: an epic with a medium child, and a task waiting on you.
    task("task-001", "Critical epic whose child is medium, bands", Priority.CRITICAL)
    task(
        "task-002",
        "Medium child nested under the critical epic, bands",
        Priority.MEDIUM,
        parent="task-001",
    )
    task("task-003", "Critical, waiting on your review, bands", Priority.CRITICAL)
    manager.claim_task("task-003", agent="claude")
    manager.handoff(
        "task-003",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Seeded: look at this one.",
    )

    # High: the three title lengths.
    task("task-004", ONE_LINE, Priority.HIGH)
    task("task-005", TWO_LINES, Priority.HIGH)
    task("task-006", MANY_LINES, Priority.HIGH)

    # Medium: blocked on another task, and a draft.
    task("task-007", "Medium, blocked on task-004, bands", Priority.MEDIUM)
    manager.update_task(
        "task-007", dependencies=[{"task": "task-004", "type": "needs"}], actor="claude"
    )
    task(
        "task-008",
        "Medium draft with no spec yet, bands",
        Priority.MEDIUM,
        lifecycle=Lifecycle.DRAFT,
    )

    # Low: one ordinary task. No band is empty except in the filtered view.
    task("task-009", "Low, the last band, bands", Priority.LOW)

    # Closed: shown under ?status=all, after every open band.
    task("task-010", "High, completed, bands", Priority.HIGH)
    manager.close_task("task-010", actor="claude", outcome=Outcome.COMPLETED)
    task("task-011", "Critical, cancelled, bands", Priority.CRITICAL)
    manager.close_task("task-011", actor="claude", outcome=Outcome.CANCELLED)


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
    root = Path(tempfile.mkdtemp(prefix="agentjobs-priority-bands-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    ProjectRegistry(home).add(build(root), project_id=PROJECT_ID, name=PROJECT_NAME)
    runner_script = root / "never_runs.py"
    runner_script.write_text("print('this sandbox never starts anything')\n", encoding="utf-8")
    dispatch_config(home, runner_script)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] priority band sandbox at {base}/tasks", flush=True)
    print(f"[review]   filtered:   {base}/tasks?q=bands", flush=True)
    print(f"[review]   with closed: {base}/tasks?status=all", flush=True)
    print(f"[review]   task page:  {base}/tasks/task-003", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
