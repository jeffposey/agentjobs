"""Stand up the pull mode on its own port, with throwaway data (task-462).

A person arms a project with a bound, and from then on the server fills every free run
slot with whatever the queue says is next. The thing worth looking at is not any one
screen -- it is the machine deciding, twice, with nobody clicking. That is not a state
anyone can construct by hand, so this seeds the machine and hands over a URL.

    python scripts/pull_mode_sandbox.py [port] [--disarmed] [--ceiling N] [--starts N]

**It is meant to still be interesting when you open it.** The armed half is armed
*until disarmed* over a backlog of twelve tasks, so it holds two runs and always has a
next task, however long after starting you get to it. The first version armed for three
starts over five tasks, spent the bound inside a minute and retired itself; the owner
opened it an hour later and correctly reported that there was nothing there. Pass
`--starts N` to get that behaviour deliberately -- it is how you watch a bound run out.

Two halves, and the comparison is the point:

    default (armed)  a ceiling of 2 over twelve briefable tasks, one held and one filed
                     as a title with no spec. Two runs are going within ~15s, in queue
                     order, with nobody clicking. The Dashboard's board shows two busy
                     cells and, under them, a "Pulling from the queue" rail: armed by
                     whom, what is left of the bound, which task is next, and Disarm.

    --disarmed       the identical machine with nothing armed: the Arm control on the
                     Dispatch page, unpressed. This is the half that shows the control
                     asks for a bound and says what the envelope does to your branches.

Run both at once to compare:

    python scripts/pull_mode_sandbox.py 8912
    python scripts/pull_mode_sandbox.py 8913 --disarmed

**What to try, in the order that makes sense.**

    1. Open the Dashboard on the armed half. Two cells are busy and the rail under them
       names what starts next. Nothing was clicked to make that happen.
    2. Go to the Runs tab and Cancel one run. Come back to the Dashboard. Within a poll
       a third run is going -- the task the rail was naming. That is the whole feature.
    3. Go to the Queue page, drag a different task to the top of its band, come back.
       The rail names the task you moved. Reordering the queue is how you steer it.
    4. Press Disarm on the rail. The rail goes. Check the Runs tab: the runs that were
       going are still going, because each was authorised on its own.
    5. Open a task the mode started. Its newest note is an authorising entry in *your*
       name saying which arming bought the run, and the `dispatch` entry under it reads
       `trigger: pull`. That entry is what the human-clocked check passed on.
    6. Open `task-205`, which was filed as a title with no spec. The mode reached it,
       could not start it, wrote why on its record, and moved to the next task rather
       than stopping.

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

PROJECT_ID = "sandbox-pull"
ARMER = "Jeff Posey"

TASKS: List[Tuple[str, str]] = [
    ("task-201", "Teach the importer to skip its own writes"),
    ("task-202", "Collapse the two dependency banners into one"),
    ("task-203", "Stop the exporter rounding the last column"),
    ("task-204", "Give the date parser one home"),
    ("task-206", "Retire the second copy of the slug helper"),
    ("task-207", "Make the CSV reader admit what it skipped"),
    ("task-208", "Give the retry loop a ceiling"),
    ("task-209", "Stop the log writer opening the file twice"),
    ("task-210", "Name the two things called `state`"),
    ("task-211", "Teach the diff viewer about renames"),
    ("task-212", "Drop the second timezone helper"),
    ("task-213", "Say which column the parser choked on"),
]
"""Twelve, and the count is the point rather than padding.

A fixture is looked at whenever the person gets to it, which is not when it was
started. The first version of this seeded five tasks and armed for three starts: it
spent its bound within a minute, retired itself, and anybody arriving an hour later
found an empty board and no rail -- a fixture that had already finished being
interesting. At a ceiling of two this many tasks keeps two runs going and something in
`next` for as long as anyone is likely to be looking.
"""

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
    # No bound by default, which is the opposite of what the *control* should default to
    # and right for the same reason: a control defaults to the safest thing, and a
    # fixture defaults to the thing that is still there when somebody opens it.
    starts = 0
    for flag in ("--ceiling", "--starts"):
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
        from agentjobs.execution.store import BOUND_OPEN, BOUND_STARTS

        project = ProjectRegistry(home).get(PROJECT_ID)
        dispatch_pull.arm(
            home,
            project,
            project.load_config(),
            armed_by=ARMER,
            bound_kind=BOUND_STARTS if starts else BOUND_OPEN,
            bound_starts=starts or None,
        )
        print(
            f"[pull] armed by {ARMER} "
            + (f"for {starts} starts" if starts else "until disarmed")
            + ". Nothing has started yet -- the server's tick does that.",
            flush=True,
        )

    if not disarmed:
        threading.Timer(2.0, arm_it).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    shape = (
        "nothing armed -- the Arm control, unpressed"
        if disarmed
        else (
            f"armed for {starts} starts at a ceiling of {ceiling}"
            if starts
            else f"armed until disarmed at a ceiling of {ceiling}"
        )
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
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
