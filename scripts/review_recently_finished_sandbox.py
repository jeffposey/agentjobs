"""Stand up the Dashboard's recently-finished region on its own port (task-460).

**Two servers, because the region is machine-wide.** It lists what closed across every
project this caller can see, so every project on one server shows the *same* region --
which means "closures present" and "none in the window" cannot be two projects. They are
two ports, each with its own throwaway home:

    python scripts/review_recently_finished_sandbox.py 8897            # work landed
    python scripts/review_recently_finished_sandbox.py 8898 --quiet    # a quiet week

Both are seeded because the change makes one screen behave two ways, and a reviewer who
only ever sees the populated half cannot tell a region from wallpaper.

Each server carries two projects, and the second is the point of the first: the landed
list contains a row belonging to the *other* project, so the reader can see that what
landed overnight is routinely somewhere other than the Dashboard they are looking at.
The row names its own project and links into it -- click it and the app should go to
that project's task, not to a missing task in this one.

The landed fixture also carries the row this whole design turns on: a task closed a
month ago and edited two minutes ago. Ordered by ``updated`` it would head the list
claiming to be the newest thing that happened; ordered by ``closed_at`` it is not in the
window at all, which is what should be on screen.

The quiet server is not an empty project -- both its projects are full of finished work.
It is the realistic empty state: a store with plenty of history and none of it news.

Nothing here touches the live corpus. Everything lives under a temporary directory that
is deleted when this process stops, including its own ``AGENTJOBS_HOME`` registry, so the
8876 dashboard and its registry are not involved at all.

Stop each with Ctrl-C, or by killing the process.

Read it at a phone width too -- 390px, the viewport this dashboard is actually read at
over Tailscale. That is where the region has the least room and where its rows wrap.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

DEFAULT_PORT = 8897

MAIN = "sandbox-main"
OTHER = "sandbox-other"


def _backdate(store, task_id: str, when: datetime) -> None:
    """Put one record's closure where the window can see it.

    ``close`` stamps the moment it runs, so a fixture about a *window* cannot be built
    through the verb -- the same reason ``tests/support.py`` has ``set_closed_at``.
    """
    with store.database.write() as connection:
        connection.execute(
            "UPDATE task SET closed_at = ? WHERE project_id = ? AND task_id = ?",
            (when.isoformat().replace("+00:00", "Z"), store.project_id, task_id),
        )


def _restamp_updated(store, task_id: str, when: datetime) -> None:
    """Move one record's ``updated`` without touching its closure.

    This is what an edit after closing does -- a correction, a redaction, a late decision
    entry -- and it is the whole reason this region does not order by ``updated``.
    """
    with store.database.write() as connection:
        connection.execute(
            "UPDATE task SET updated_at = ? WHERE project_id = ? AND task_id = ?",
            (when.isoformat().replace("+00:00", "Z"), store.project_id, task_id),
        )


def seed(manager, store, *, landed: bool, second: bool) -> None:
    """One project's worth of open work, plus the closures its server is meant to show.

    ``landed`` is the server's mode; ``second`` says which of its two projects this is,
    because the cross-project row has to come from the other one.
    """
    from agentjobs.models_v2 import Lifecycle, Outcome, Priority

    now = datetime.now(timezone.utc)

    def make(task_id, title, priority=Priority.MEDIUM):
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}.",
            description="Seeded for a recently-finished review. Nothing here is real work.",
            priority=priority,
            lifecycle=Lifecycle.READY,
            actor="claude",
        )

    def finish(task_id, title, *, ago: timedelta, outcome=Outcome.COMPLETED):
        make(task_id, title)
        manager.close_task(task_id, actor="claude", outcome=outcome)
        _backdate(store, task_id, now - ago)

    # Open work, so the rest of the Dashboard has something to draw and the region is
    # measured beside a real board rather than on an otherwise blank page.
    offset = 20 if second else 10
    make(f"task-0{offset}", "Teach the queue to explain itself", Priority.HIGH)
    make(f"task-0{offset + 1}", "Reorder the backlog from a phone", Priority.HIGH)
    make(f"task-0{offset + 2}", "Retire the legacy Jinja routes")

    if not landed:
        # Plenty of history, none of it inside the window. The empty state a real
        # machine reaches, rather than a project nothing has ever happened in.
        finish(f"task-9{offset}", "Publish the storage guide", ago=timedelta(days=21))
        finish(f"task-9{offset + 1}", "Split the importer", ago=timedelta(days=40))
        return

    if second:
        # The one row in the other project, so the region can be seen doing the thing it
        # exists for. Two hours ago puts it in the middle of the list rather than at an
        # end, where a reader might read its position as a grouping.
        finish("task-201", "Rotate the signing key", ago=timedelta(hours=2))
        finish("task-202", "Retire the old export", ago=timedelta(days=11))
        return

    finish("task-101", "Rebuild the frontend after a merge", ago=timedelta(minutes=9))
    finish("task-102", "Register a run against its session id", ago=timedelta(hours=3))
    finish(
        "task-103",
        "Replace the Alpine dashboard",
        ago=timedelta(hours=20),
        outcome=Outcome.SUPERSEDED,
    )
    finish("task-104", "Keep the merge runway single-file", ago=timedelta(days=2))

    # The row that decides whether the region is honest. Closed a month ago, edited two
    # minutes ago: ordered by `updated` it would head the list as the newest thing that
    # happened, and ordered by `closed_at` it is not in the window at all.
    finish("task-105", "Closed a month ago, corrected this morning", ago=timedelta(days=32))
    _restamp_updated(store, "task-105", now - timedelta(minutes=2))


def build(root: Path, *, project_id: str, name: str, landed: bool, second: bool) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    store = sandbox_store(project_root / "tasks", project_id=project_id)
    seed(TaskManager(store), store, landed=landed, second=second)
    return project_root


def main() -> None:
    landed = "--quiet" not in sys.argv[1:]
    argv = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    port = int(argv[0]) if argv else (DEFAULT_PORT if landed else DEFAULT_PORT + 1)
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-finished-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    # A ceiling, so the Dashboard draws its slot board and the region is reviewed on the
    # real page rather than on one missing its largest element. Three is the common
    # workstation, and the same number `dashboard-one-screen.spec.ts` measures at.
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "enabled": False, "limits": {"max_concurrent_runs": 3}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    registry = ProjectRegistry(home)
    projects = [
        (MAIN, "Sandbox: the project you are looking at", False),
        (OTHER, "Sandbox: the other project entirely", True),
    ]
    for project_id, name, second in projects:
        registry.add(
            build(root, project_id=project_id, name=name, landed=landed, second=second),
            project_id=project_id,
            name=name,
        )

    state = "work landed" if landed else "a quiet week"
    print(f"[review] recently-finished sandbox ({state}) at http://127.0.0.1:{port}/app/")
    print(f"[review]   dashboard: http://127.0.0.1:{port}/app/p/{MAIN}")
    if landed:
        print(f"[review]   one row belongs to {OTHER}; clicking it must leave this project")
        print("[review]   'Closed a month ago, corrected this morning' must NOT be listed")
        print(f"[review] the other state: python {Path(__file__).name} {port + 1} --quiet")
    else:
        print("[review]   both projects have history; none of it is inside the window")
    print("[review] read it at 390px wide too; that is where the rows wrap.")
    print(f"[review] throwaway data under {root}", flush=True)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
