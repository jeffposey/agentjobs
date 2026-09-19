"""Stand up the pull mode on its own port, with throwaway data (task-462).

A person arms a project with a bound, and from then on the server fills every free run
slot with whatever the queue says is next. The thing worth looking at is not any one
screen -- it is the machine deciding, twice, with nobody clicking. That is not a state
anyone can construct by hand, so this seeds the machine and hands over a URL.

    python scripts/pull_mode_sandbox.py [port] [--disarmed] [--ceiling N] [--starts N]

Two halves, and the comparison is the point:

    default (armed)  a ceiling of 2, five briefable tasks in a stored order, one of them
                     held and one of them filed as a title with no spec. The project is
                     armed for three starts. Within a few seconds two runs are going --
                     in queue order -- and the board's "Pulling from the queue" rail says
                     who armed it, what is left of the bound, and which task is next.
                     Nothing was clicked.

    --disarmed       the identical machine with nothing armed: the Arm control on the
                     dispatch settings page, unpressed, with the bound unset. This is
                     what the page looks like before anyone decides anything, and it is
                     the half that shows the control *asks* for a bound rather than
                     defaulting to one.

Run both at once to compare:

    python scripts/pull_mode_sandbox.py 8912
    python scripts/pull_mode_sandbox.py 8913 --disarmed

**What to try, beyond looking at it.**

    * **Watch the third start.** Two runs hold the two slots and one start is left in the
      bound. Cancel one run from the Runs tab; within a poll the third task starts by
      itself, and the rail's bound reads "3 of 3 starts used" before the arming
      disappears. Nothing was clicked except Cancel.
    * **Reorder mid-run, and see the reorder respected.** The rail names what starts
      next. Drag a different task to the top of its band on the Queue page, come back,
      and the rail names the task you moved. That is the whole steering mechanism: the
      stored queue order decides, so `queue move` is how you change its mind.
    * **Disarm with a run going.** Press Disarm on the rail. Takeoffs stop immediately
      and the run that is going keeps going -- it was authorised on its own and has its
      own merge gate. The Runs tab is where to check that.
    * **Read a pulled run's provenance.** Open a task the mode started. Its newest note
      is an authorising entry in *your* name, saying which arming bought the run and what
      bound that arming carries, and the `dispatch` entry beneath it is triggered `pull`.
      That entry is what `assert_human_clocked` passed on -- the same check a click's
      entry passes, not a bypass of it.
    * **The task with no spec.** `task-205` is filed as a title and a hope. The mode
      reaches it, cannot start it, writes why on its record, and moves to the next task
      rather than stopping. Open it and read the note.
    * **The held task.** `task-202` was put on hold, so it is not claimable and the mode
      never offers it at all. Release it from its review panel and it rejoins the queue.
    * **Precedence.** Queue a dispatch of a task by name from its own page while the
      machine is full (the refusal offers "Queue it for the next free slot"). Now free a
      slot. The card you queued starts, and the pull mode waits -- a dispatch somebody
      asked for by name goes first.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_PORT = 8912
DEFAULT_CEILING = 2
DEFAULT_STARTS = 3

PROJECT_ID = "sandbox-pull"
ARMER = "Jeff Posey"

TASKS: List[Tuple[str, str]] = [
    ("task-201", "Teach the importer to skip its own writes"),
    ("task-202", "Collapse the two dependency banners into one"),
    ("task-203", "Stop the exporter rounding the last column"),
    ("task-204", "Give the date parser one home"),
    ("task-206", "Retire the second copy of the slug helper"),
]

THIN = ("task-205", "Something about the cache")
"""A task filed as a title and a hope. It sits between 204 and 206 in the queue and is
what makes "a refusal about the task is walked past, with a note" something a person can
see rather than something a test asserts."""

HELD = "task-202"
"""Put on hold, so it is not claimable and the mode never reaches it at all."""


def build_project(root: Path) -> Path:
    """One throwaway project whose backlog is a real, ordered queue."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import BallReason, Lifecycle, LogEntryType, Ball
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="Pull Mode Sandbox", user=ARMER), sort_keys=False
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))

    ordered = TASKS[:3] + [THIN] + TASKS[3:]
    for task_id, title in ordered:
        briefable = task_id != THIN[0]
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}. Seeded so the pull mode has something real to start.",
            description=(
                (
                    "Seeded by scripts/pull_mode_sandbox.py. Nothing here is real work. It "
                    "has a working description because a task that cannot brief an agent is "
                    "a different state -- and task-205 is the fixture for that one."
                )
                if briefable
                else ""
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        # A human's entry on every task, so the Dispatch button on a task page works
        # too. The pull mode does not need it -- it writes its own authorising entry --
        # but a fixture where half the buttons are refused teaches the wrong thing.
        manager.add_log_entry(
            task_id,
            actor=ARMER,
            type=LogEntryType.NOTE,
            body="Seeded so this task can also be dispatched by hand without writing a note.",
        )

    # The held one: claimed, then stopped by a person. It leaves the claimable queue
    # entirely, which is why the mode never offers it rather than refusing it.
    manager.claim_task(HELD, agent="claude")
    manager.handoff(
        HELD,
        actor=ARMER,
        ball=Ball.AGENT,
        ball_reason=BallReason.HOLD,
        ball_prompt=(
            "ON HOLD -- do not resume this task until the condition below is met and a "
            "human has released it.\n\nSeeded on hold so the pull mode can be seen "
            "passing it by."
        ),
    )
    return project_root


def dispatch_config(ceiling: int, port: int) -> Dict[str, Any]:
    """A machine with one runner that sleeps, and the project switched on.

    The runner sleeps rather than exiting, so a pulled start becomes a real run holding a
    real slot -- which is the whole subject. ``api_base`` is named because this sandbox is
    not on the default port: without it a pulled start would tell its agent to look at
    :8765 and be refused by the reachability gate, which is the right refusal and not the
    thing this fixture is for.
    """
    return {
        "version": 1,
        "enabled": True,
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            "sandbox-sleeper": {
                "argv": [sys.executable, "-c", "import time; time.sleep(600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": ceiling, "dispatch_queue_limit": 20},
        "projects": {
            PROJECT_ID: {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
        },
    }


def main() -> None:
    argv = sys.argv[1:]
    disarmed = "--disarmed" in argv
    ceiling = DEFAULT_CEILING
    starts = DEFAULT_STARTS
    for flag, default in (("--ceiling", None), ("--starts", None)):
        if flag in argv:
            index = argv.index(flag)
            value = int(argv[index + 1])
            if flag == "--ceiling":
                ceiling = value
            else:
                starts = value
            del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-pull-mode-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    project_root = build_project(root)
    registry.add(project_root, project_id=PROJECT_ID, name="Pull Mode Sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(ceiling, port), sort_keys=False), encoding="utf-8"
    )

    def arm_it() -> None:
        """Arm the project through the same function the Arm button calls.

        Deferred a moment after the server is up, so the first tick that sees an armed
        project is a tick of the running server rather than of this process -- which is
        the point of the fixture: nothing here starts a run, the server does.
        """
        from agentjobs.dispatch import pull as dispatch_pull
        from agentjobs.execution.store import BOUND_STARTS

        project = ProjectRegistry(home).get(PROJECT_ID)
        dispatch_pull.arm(
            home,
            project,
            project.load_config(),
            armed_by=ARMER,
            bound_kind=BOUND_STARTS,
            bound_starts=starts,
        )
        print(
            f"[pull] armed for {starts} starts by {ARMER}. Nothing has started yet -- "
            "the server's tick does that.",
            flush=True,
        )

    if not disarmed:
        threading.Timer(2.0, arm_it).start()

    import uvicorn

    from agentjobs.api.main import app

    shape = (
        "nothing armed -- the Arm control, unpressed"
        if disarmed
        else f"armed for {starts} starts at a ceiling of {ceiling}"
    )
    print(f"[pull] pull mode sandbox ({shape})", flush=True)
    print(f"[pull]   Dashboard  http://127.0.0.1:{port}/app/p/{PROJECT_ID}", flush=True)
    print(f"[pull]   Arm/Disarm http://127.0.0.1:{port}/app/p/{PROJECT_ID}/dispatch", flush=True)
    print(f"[pull]   Runs tab   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/runs", flush=True)
    print(f"[pull]   Queue      http://127.0.0.1:{port}/app/p/{PROJECT_ID}/queue", flush=True)
    print(
        f"[pull]   The task with no spec  http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
        f"/tasks/{THIN[0]}",
        flush=True,
    )
    if disarmed:
        print(
            "[pull]   Compare with the armed half: python scripts/pull_mode_sandbox.py "
            f"{port - 1}",
            flush=True,
        )
    else:
        print(
            "[pull]   Compare with the unarmed half: python scripts/pull_mode_sandbox.py "
            f"{port + 1} --disarmed",
            flush=True,
        )
        print(
            "[pull]   Two runs should be going within ~15s. Cancel one from the Runs tab "
            "and watch the third start by itself.",
            flush=True,
        )
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
