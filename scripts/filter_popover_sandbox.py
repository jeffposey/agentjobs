"""Stand up the Tasks surface so the filter button can be judged by hand, on its own port.

task-356 takes the permanent four-control filter panel off the top of the list and puts
the three selects behind a button. The measurement is on the task -- the list spent 358px
above its first row and now spends 135px, at the sidebar's 320px floor -- and the tests
cover the wiring, the URL round trip and the keyboard. What none of them can answer is
whether the *indicator* works on a person: whether a list hiding most of the backlog
still reads as filtered rather than as empty. That is a looking question, so this is for
looking at.

**Both states are seeded, because the change makes the surface behave differently in
two and comparing them is the review**:

    nothing filtered   Twenty-odd open tasks, the button plain, no badge. This is what
                       almost every visit looks like, and the thing to notice is how
                       much of the column is list.

    filtered to
    nothing            `?status=closed&priority=critical` matches none of them. The list
                       says "No tasks match these filters" and the button carries a 2.
                       **This is the defect the badge exists to prevent** -- without it
                       this screen is indistinguishable from an empty backlog. Judge the
                       badge here: is it loud enough, sitting where it does?

    filtered to
    something          `?priority=high` keeps a few rows, badge 1. The in-between case,
                       where a filtered list looks perfectly normal and the button is
                       the only thing saying it is not the whole story.

Everything here is throwaway. Click anything, including the destructive controls --
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME.

    python scripts/filter_popover_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **At 1400x900** -- the two-region shell. Open the button. The popover is over the
    list rather than shoving it down, Escape closes it, and focus lands back on the
    button so the next Tab goes somewhere sensible.
  * **Narrow to 700x800**, the 320px sidebar. The search box and the button share one
    line and neither is squeezed to uselessness. Row one is on screen.
  * **At 390x844** -- a phone. The popover opens inside the window, under the pinned
    bar and not behind it, and the page does not scroll sideways to hold it.
  * **The filtered-to-nothing URL, glanced at rather than studied.** If your eye reads
    "there is no work here" before it reads the badge, the badge is not doing its job
    and that is worth saying.
  * **Set two filters in one visit.** The popover deliberately stays open across a
    change so that is one trip rather than two -- but it does sit over the top of the
    list while it is open. Escape or a click outside puts it away.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8909

# Enough rows that the list is longer than any window this is opened in -- the space the
# header used to take is only legible against a list that runs off the bottom.
BACKLOG = [
    ("Rework the queue's band arithmetic", "high"),
    ("Attachment thumbnails are served at full size", "medium"),
    ("Dispatch output scrolls away from the tail", "medium"),
    ("A run that dies mid-gate leaves its runway held", "high"),
    ("Playbook front matter is not validated", "low"),
    ("The dependency graph draws a cycle as a straight line", "medium"),
    ("Webhook retries have no ceiling", "high"),
    ("Schema v1 records fail with a stack trace, not a sentence", "medium"),
    ("The registry accepts two projects with one id", "critical"),
    ("Log entries longer than a screen have no fold", "low"),
    ("`agentjobs next` does not say why it skipped a task", "medium"),
    ("Attachments survive a task being deleted", "medium"),
    ("The queue repair tool guesses silently", "high"),
    ("Bench numbers are not comparable across machines", "low"),
    ("A parked run reports as live for ten minutes", "high"),
    ("Actor vocabulary is not enforced on the CLI path", "medium"),
    ("Closing a parent with open children is allowed", "medium"),
    ("The OpenAPI export drifts from the generated client", "low"),
    ("Search does not reach the log bodies", "medium"),
    ("Restarting the server loses in-flight dispatch state", "high"),
    ("Task ids are not stable across a migration", "critical"),
    ("The phone shell drops the project switcher", "medium"),
]


def seed(manager) -> None:
    """A backlog to filter, and two closed rows so `status=closed` is not empty by luck."""
    from agentjobs.models_v2 import Lifecycle, Outcome, Priority

    for index, (title, priority) in enumerate(BACKLOG, start=1):
        manager.create_task(
            id=f"task-{index:03d}",
            title=title,
            summary=f"Seeded so the list has something to filter. {title}.",
            description="Nothing here is real work and every control on it is safe to press.",
            priority=Priority(priority),
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )

    # Closed rows, so `status=closed` on its own returns something. The filtered-to-
    # nothing URL pairs it with `priority=critical`, which these deliberately are not:
    # an empty result has to come from the *combination*, or the fixture would be
    # proving that one filter matches nothing rather than that the badge is legible.
    for index, title in enumerate(
        ["A finished piece of work", "Something that was cancelled"], start=90
    ):
        task_id = f"task-{index:03d}"
        manager.create_task(
            id=task_id,
            title=title,
            summary="Closed, so the closed filter has something to show.",
            description="Seeded.",
            priority=Priority.LOW,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )
        manager.claim_task(task_id, agent="claude")
        manager.close_task(
            task_id,
            actor="claude",
            outcome=Outcome.COMPLETED if index == 90 else Outcome.CANCELLED,
            body="Seeded closed.",
        )


def build(root: Path, *, project_id: str, name: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(TaskStorage(project_root / "tasks")))
    return project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-filter-popover-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-filters", "Sandbox: the filter button"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[review] filter-popover sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   nothing filtered      {base}", flush=True)
    print(f"[review]   filtered to nothing   {base}?status=closed&priority=critical", flush=True)
    print(f"[review]   filtered to something {base}?priority=high", flush=True)
    print("[review] Open the button at 1400x900, then 700x800, then 390x844.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
