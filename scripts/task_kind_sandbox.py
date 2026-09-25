"""Stand up a task's kind on its own port, with throwaway data (task-593).

task-593 renders the `kind` field task-592 added: a dashed-outline pill with an icon
and the word, marking design rows in the list, naming both kinds in the record header,
heading a design's review as a design review, and a fourth filter, `?kind=`.

    python scripts/task_kind_sandbox.py [port]

What to look at -- one design and one implementation task at each state:

  * **Ready** -- task-001 (design) and task-002 (implementation). In the list only
    task-001 carries a mark.
  * **Needs review** -- task-003 (design) is headed *Design review* with the mark;
    task-004 (implementation) reads *Needs review*, exactly as before. Approve says the
    same thing on both: its wording is task-001's, not this task's.
  * **A design and what implements it** -- task-005 (design) says *Implemented by:
    task-006, task-007*; task-006 says *Implements: task-005 -- ...*. task-007 needs
    task-005 too, and also needs task-002, which is not a design and so is not named.
  * **The filter** -- `?kind=design` and `?kind=implementation`; the badge counts it.
  * **Phone width** -- at 390px the mark wraps with the other chips, icon and word
    together.

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

DEFAULT_PORT = 8993
PROJECT_ID = "sandbox-kind"
PROJECT_NAME = "Sandbox: task kind"
USER = "Jeff Posey"


def seed(manager: Any) -> None:
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, TaskKind

    def task(task_id: str, title: str, kind: Any = None, **extra: Any) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"Seeded for the task-kind review: {title}.",
            description="Seeded by scripts/task_kind_sandbox.py. Nothing here is real work.",
            priority=extra.pop("priority", Priority.HIGH),
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
            kind=kind,
            **extra,
        )

    def to_review(task_id: str) -> None:
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Seeded: look at the heading and the header chips.",
        )

    task("task-001", "Design pass: how a ready design task looks", TaskKind.DESIGN)
    task("task-002", "Implementation: a ready task with no kind set")

    task("task-003", "Design pass waiting on your review", TaskKind.DESIGN)
    to_review("task-003")
    task("task-004", "Implementation waiting on your review", TaskKind.IMPLEMENTATION)
    to_review("task-004")

    task("task-005", "Design pass: how a task is known to be a design task", TaskKind.DESIGN)
    task(
        "task-006",
        "Add a kind field to the task schema, end to end",
        dependencies=[{"task": "task-005", "type": "needs"}],
    )
    task(
        "task-007",
        "Show a task's kind in the React UI",
        priority=Priority.MEDIUM,
        dependencies=[
            {"task": "task-005", "type": "needs"},
            {"task": "task-002", "type": "needs"},
        ],
    )


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
    root = Path(tempfile.mkdtemp(prefix="agentjobs-task-kind-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    ProjectRegistry(home).add(build(root), project_id=PROJECT_ID, name=PROJECT_NAME)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] task-kind sandbox at {base}/tasks", flush=True)
    print(f"[review]   design only:     {base}/tasks?kind=design", flush=True)
    print(f"[review]   design review:   {base}/tasks/task-003", flush=True)
    print(f"[review]   implemented by:  {base}/tasks/task-005", flush=True)
    print(f"[review]   implements:      {base}/tasks/task-006", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
