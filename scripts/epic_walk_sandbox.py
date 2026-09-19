"""Stand up a dispatched epic on its own port, with throwaway data.

task-458 stops an epic's dispatch from starting an agent. It hands the children to a
server-hosted walk and concludes in the same call, so the machine's whole ceiling is
available to the children and no idle supervisor sits in a slot. The thing to look at is
a *comparison*, and neither half can be constructed by hand on a real machine: you cannot
make three agents take off to order, and you certainly cannot make one of them park on a
human at the moment you are watching.

    python scripts/epic_walk_sandbox.py [port] [--grounded]

Two states, and the comparison is the point:

    default      the epic is dispatched and three independent children take off. The
                 slot board shows **three child cards and no supervisor card** -- before
                 this, one of those three cells held a session whose only job was to
                 wait, and only two children flew. The epic's own run is in the Runs tab,
                 finished, labelled `epic walk - no agent`.
    --grounded   the same epic, with one of the three children parking on a person after
                 it has taken off. The walk grounds -- no further takeoff -- watches its
                 two siblings down, and hands the parent to human/decision naming that
                 child and its reason. The board is left holding the parked child alone,
                 which is the honest shape: that one has a live session somebody has to
                 answer, and nothing else of the epic is running. There is still no card
                 for the epic itself, which is the whole point of comparing the two.

Run both at once to see them side by side:

    python scripts/epic_walk_sandbox.py 8912
    python scripts/epic_walk_sandbox.py 8913 --grounded

What to check, in both:

  * The Dashboard's slot board. Three child cards flying, or none; never a card for the
    epic itself.
  * The capacity badge. `3 of 3 slots busy` with three children -- the number the old
    shape could not reach, because the supervisor held one.
  * The Runs tab. The epic's own run, `finished`, `epic walk - no agent`, started and
    ended in the same second.
  * The epic's own page. Its `dispatch` entry says mode `walk`, its `dispatch_result`
    names the walk id, and the human note that authorised it is still the newest human
    act on the record -- which is what every child's authorisation is read from.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

DEFAULT_PORT = 8912
CEILING = 3
PROJECT_ID = "sandbox-epic"

CHILDREN: List[Tuple[str, str]] = [
    ("task-201", "Give the importer a dry run"),
    ("task-202", "Name the two things both called `state`"),
    ("task-203", "Retire the third copy of the date parser"),
]

CHILD_DESCRIPTION = (
    "Seeded by scripts/epic_walk_sandbox.py. Nothing here is real work. It has a working "
    "description because a child with none is refused before any run exists -- an "
    "unattended run started on its parent's authorisation would have nothing to work "
    "from and nobody to ask -- and that refusal is a different state from the one this "
    "fixture is for."
)


def build_project(root: Path) -> Path:
    """One throwaway project holding one epic and three independent children."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle, LogEntryType
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="Epic sandbox", user="Jeff Posey"), sort_keys=False
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))
    manager.create_task(
        id="task-200",
        title="Make the importer honest about reruns",
        summary="An epic with three independent children, so all three can fly at once.",
        description=(
            "Seeded by scripts/epic_walk_sandbox.py so a dispatch of this task has "
            "children to hand to a walk. The three below share no dependency, which is "
            "what makes the ceiling the thing being measured."
        ),
        lifecycle=Lifecycle.READY,
        actor="claude",
    )
    for task_id, title in CHILDREN:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}. A child of the epic above.",
            description=CHILD_DESCRIPTION,
            lifecycle=Lifecycle.READY,
            actor="claude",
            parent="task-200",
        )
    # Through the verbs: an epic has to be active before it can be walked, and the note
    # is the human act every child's dispatch is authorised by. Seeding either any other
    # way would skip the log entries the walk actually reads.
    manager.claim_task("task-200", agent="claude")
    manager.add_log_entry(
        "task-200",
        actor="Jeff Posey",
        type=LogEntryType.NOTE,
        body="Go ahead -- walk the children.",
    )
    return project_root


def dispatch_config(runner_argv: List[str], port: int) -> Dict[str, Any]:
    """A machine that will admit to one runner, with the sandbox project switched on.

    The runner sleeps rather than exiting, so a child that takes off stays in the air for
    as long as anybody is looking at it -- which is the one thing about this board a
    seeded run directory cannot fake, because the walk polls the *task record* and a
    fixture that only wrote run directories would land every child instantly.
    """
    return {
        "version": 1,
        "enabled": True,
        "runners": {"sandbox-sleeper": {"argv": runner_argv, "actor": "claude"}},
        "limits": {"max_concurrent_runs": CEILING},
        # This sandbox's own port, not the machine's. Without it every dispatch here is
        # refused: an agent would be told AgentJobs is at the built-in fallback, nothing
        # answers there, and a run that cannot reach AgentJobs cannot report that it
        # cannot reach AgentJobs.
        "api_base": f"http://127.0.0.1:{port}",
        "projects": {
            PROJECT_ID: {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
                "posture": "auto",
            }
        },
    }


def dispatch_epic(home: Path, project_id: str) -> str:
    """Dispatch the epic exactly as the Dispatch button does, and return its run id."""
    from agentjobs.dispatch.epic import parent_authorizing_entry
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
    from agentjobs.projects import ProjectRegistry
    from sandbox_store import sandbox_project_store

    from agentjobs.manager import TaskManager

    project = ProjectRegistry(home=home).get(project_id)
    manager = TaskManager(sandbox_project_store(project))
    parent = manager.get_task("task-200")
    assert parent is not None
    entry = parent_authorizing_entry(parent)
    assert entry is not None
    handle = dispatch_task(
        manager=manager,
        project=project,
        project_config=project.load_config(),
        request=DispatchRequest(task_id="task-200", caused_by=entry.id),
        home=home,
    )
    return handle.run_id


def main() -> None:
    argv = sys.argv[1:]
    grounded = "--grounded" in argv
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-epic-walk-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_root = build_project(root)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name="Epic sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            dispatch_config([sys.executable, "-c", "import time; time.sleep(3600)"], port),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def fly() -> None:
        """Dispatch the epic, then tick the server's walk until the children are up.

        The tick is what the running server would do on its own poll; it is called here
        so the board is already in the state worth looking at by the time the URL is
        opened, rather than filling in over the next half minute while somebody wonders
        whether it is broken.
        """
        from agentjobs.dispatch.epic import advance_hosted_walks
        from agentjobs.dispatch.ledger import DispatchLedger, live_runs
        from agentjobs.manager import TaskManager
        from agentjobs.models_v2 import Ball, BallReason, Outcome
        from sandbox_store import sandbox_project_store

        # The dispatch gate refuses an address nothing answers on, and that address is
        # this process's own server -- so the seed waits for it rather than racing it.
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)

        project = ProjectRegistry(home=home).get(PROJECT_ID)

        run_id = dispatch_epic(home, PROJECT_ID)
        print(f"[epic] dispatched task-200 as {run_id} -- a walk run, already finished", flush=True)

        def manager_now() -> Any:
            """A fresh manager per use.

            The store this opens is closed again by whatever else in the process opened
            the same project's database, so one held across the seed goes out from under
            the next call. A sandbox is the only writer and the file is a few kilobytes,
            so re-opening costs nothing worth keeping a handle for.
            """
            return TaskManager(sandbox_project_store(project))

        def resolve(_project_id: str) -> Any:
            return manager_now(), project

        def tick(times: int = 1) -> List[str]:
            said: List[str] = []
            for _ in range(times):
                lines = advance_hosted_walks(home, resolve=resolve)
                for line in lines:
                    print(f"[epic] {line}", flush=True)
                said.extend(lines)
                if any("all_children_done" in line or "child_needs" in line for line in lines):
                    break
            return said

        tick(3)
        if grounded:
            # The child parks **after** it has taken off, which is the only way a walk
            # ever meets one: a child already parked before the walk started is not
            # eligible, so it is never claimed and never grounds anything. Its siblings
            # are then landed rather than killed -- neither could have depended on the one
            # that stopped the walk, or it would not have been eligible to start.
            manager = manager_now()
            manager.handoff(
                "task-201",
                actor="claude",
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="Approve the branch, or say what needs changing.",
                body="Seeded by the sandbox so the walk has something to ground on.",
            )
            landings: Dict[str, Optional[str]] = {"task-202": None, "task-203": None}
            for row in live_runs(home):
                if row.task_id in landings:
                    landings[row.task_id] = row.run_id
            for landed, landed_run in landings.items():
                manager_now().close_task(landed, actor="claude", outcome=Outcome.COMPLETED)
                if landed_run:
                    # A real child's session ends when its work does. The sandbox's runner
                    # sleeps for an hour instead, so the two that landed are stopped the
                    # way the Cancel button stops one -- otherwise the board would show
                    # three cards for one parked child and two closed tasks, and read as
                    # the walk having left things running.
                    DispatchLedger(home).cancel(landed_run, actor="claude")
            tick(4)
        parent = manager_now().get_task("task-200")
        if parent is not None and parent.ball is not None:
            reason = parent.ball_reason.value if parent.ball_reason else "-"
            print(f"[epic] task-200 ball is now {parent.ball.value}/{reason}", flush=True)

    threading.Timer(0.2, fly).start()

    import uvicorn

    from agentjobs.api.main import app

    shape = (
        "a walk grounded on a child that needs a person"
        if grounded
        else "three children in the air and no supervisor"
    )
    other = "" if grounded else " --grounded"
    print(f"[epic] dispatched epic ({shape}), ceiling {CEILING}", flush=True)
    print(f"[epic]   Dashboard  http://127.0.0.1:{port}/app/p/{PROJECT_ID}", flush=True)
    print(f"[epic]   Runs tab   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/runs", flush=True)
    print(
        f"[epic]   The epic   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/tasks/task-200", flush=True
    )
    print(
        f"[epic]   Compare with the other half: python scripts/epic_walk_sandbox.py "
        f"{port + 1}{other}",
        flush=True,
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
