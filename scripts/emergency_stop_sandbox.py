"""Stand up the emergency stop on its own port, with a machine worth stopping (task-573).

The Stop control is only interesting when there is something for it to stop, and on a
real machine pressing it kills real work. This seeds a machine that holds one of each kind
the stop ends, then hands over a URL:

    python scripts/emergency_stop_sandbox.py [port] [--stopped]

    default      a ceiling of 2, full: two live runs, started by hand. One more dispatch
                 waits in the queue for a slot. Pull mode is armed until disarmed. An epic
                 is dispatched, and its detached walk is waiting for a slot. The header
                 shows the red stop glyph left of the +.
    --stopped    the same machine with the sentinel already down, so the stopped state
                 (solid red trigger, red strip under the header, Resume) can be looked at
                 without pressing anything first.

**The runs are Python processes that sleep.** Pressing Stop kills them and nothing else:
there is no agent, no session and no worktree behind any of them.

**What to try.**

    1. Open the Dashboard. Two cells are busy, and the queued dispatch, the pull rail and
       the epic walk are listed under the board.
    2. Press the red octagon left of the +. Cancel once to see that nothing happened.
    3. Press it again and choose Stop everything. The dialog lists what it acted on:
       two runs, the queued dispatch, the pull arming and the epic walk.
    4. Close it. The trigger is solid red and a red strip under the header says dispatch
       is stopped. Visit Tasks, a task page and the Dispatch page: the strip is on each.
    5. Open task-310 (the epic). The stop left a note and a question for you on it.
    6. Press Resume on the strip. Nothing restarts: no run, no pull, no walk. Dispatching
       a task by hand works again.
    7. Phone: the same URL at 375px wide. The trigger is still one tap, left of the +.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME``, deleted when this process stops.
Stop it with Ctrl-C.
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

DEFAULT_PORT = 8931
CEILING = 2

PROJECT_ID = "sandbox-stop"
OWNER = "Jeff Posey"

TASKS: List[Tuple[str, str]] = [
    ("task-301", "Teach the importer to skip its own writes"),
    ("task-302", "Collapse the two dependency banners into one"),
    ("task-303", "Stop the exporter rounding the last column"),
    ("task-304", "Give the date parser one home"),
    ("task-305", "Make the CSV reader admit what it skipped"),
    ("task-306", "Give the retry loop a ceiling"),
    ("task-307", "Stop the log writer opening the file twice"),
]
"""Two started by hand, one queued, and the rest for the pull mode to name as next."""

EPIC = ("task-310", "Split the report builder into stages")
CHILDREN: List[Tuple[str, str]] = [
    ("task-311", "Pull the report's data loading into its own stage"),
    ("task-312", "Pull the report's rendering into its own stage"),
]


def build_project(root: Path) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle, LogEntryType
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="Emergency Stop Sandbox", user=OWNER),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))

    def seed(task_id: str, title: str, parent: str | None = None) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}. Seeded so the emergency stop has something to stop.",
            description="Seeded by scripts/emergency_stop_sandbox.py. Nothing here is real work.",
            lifecycle=Lifecycle.READY,
            actor="claude",
            parent=parent,
        )
        # A human's entry on every task: it is what a dispatch is caused by.
        manager.add_log_entry(
            task_id, actor=OWNER, type=LogEntryType.NOTE, body="Seeded; go ahead."
        )

    for task_id, title in TASKS:
        seed(task_id, title)
    seed(*EPIC)
    for task_id, title in CHILDREN:
        seed(task_id, title, parent=EPIC[0])
    manager.claim_task(EPIC[0], agent="claude")
    manager.add_log_entry(EPIC[0], actor=OWNER, type=LogEntryType.NOTE, body="Walk the children.")
    return project_root


def dispatch_config(port: int) -> Dict[str, Any]:
    """One runner that sleeps, so every start is a real process holding a real slot."""
    return {
        "version": 1,
        "enabled": True,
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            "sandbox-sleeper": {
                "argv": [sys.executable, "-c", "import time; time.sleep(3600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": CEILING, "dispatch_queue_limit": 20},
        "projects": {
            PROJECT_ID: {"enabled": True, "require_clean_tree": False, "runner": "sandbox-sleeper"},
        },
    }


def seed_machine(home: Path, port: int, stopped: bool) -> None:
    """Fill the machine through the functions the buttons call, once the server is up.

    Deferred so the batch runs are supervised by the serving process, which is the one
    that will be asked to stop them.
    """
    from agentjobs.dispatch.config import write_sentinel
    from agentjobs.dispatch.epic import WalkSettings, detach_walk
    from agentjobs.dispatch import pull as dispatch_pull
    from agentjobs.dispatch.guards import IF_FULL_QUEUE, DispatchRequest, dispatch_task
    from agentjobs.dispatch.queue import dispatch_or_queue
    from agentjobs.execution.store import BOUND_OPEN
    from agentjobs.projects import ProjectRegistry
    from agentjobs.store_factory import dispatch_manager_for

    project = ProjectRegistry(home).get(PROJECT_ID)
    config = project.load_config()
    manager = dispatch_manager_for(project)
    api_base = f"http://127.0.0.1:{port}"

    def human_entry(task_id: str) -> int:
        task = manager.get_task(task_id)
        assert task is not None
        return max(entry.id for entry in task.log if entry.actor == OWNER)

    for task_id in ("task-301", "task-302"):
        dispatch_task(
            manager=manager,
            project=project,
            project_config=config,
            request=DispatchRequest(task_id=task_id, caused_by=human_entry(task_id)),
            home=home,
            api_base=api_base,
        )
    dispatch_or_queue(
        manager=manager,
        project=project,
        project_config=config,
        request=DispatchRequest(
            task_id="task-303", caused_by=human_entry("task-303"), if_full=IF_FULL_QUEUE
        ),
        home=home,
        api_base=api_base,
        queued_by=OWNER,
    )
    dispatch_pull.arm(home, project, config, armed_by=OWNER, bound_kind=BOUND_OPEN)
    detach_walk(
        manager=manager,
        project_id=PROJECT_ID,
        parent_id=EPIC[0],
        home=home,
        settings=WalkSettings(max_concurrent=2),
        merge_mode=None,
        actor="claude",
    )
    print(
        "[stop] seeded: 2 live runs, 1 queued dispatch, pull mode armed, 1 epic walk.",
        flush=True,
    )
    if stopped:
        write_sentinel(home, actor=OWNER, source="the sandbox's --stopped flag")
        print("[stop] sentinel written: the stopped state is showing.", flush=True)


def main() -> None:
    argv = sys.argv[1:]
    stopped = "--stopped" in argv
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-emergency-stop-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_root = build_project(root)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name="Emergency Stop Sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(port), sort_keys=False), encoding="utf-8"
    )

    def seed() -> None:
        try:
            seed_machine(home, port, stopped)
        except Exception as exc:  # noqa: BLE001 - a fixture says what went wrong, and serves on
            print(f"[stop] seeding failed: {type(exc).__name__}: {exc}", flush=True)

    threading.Timer(2.0, seed).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    print(f"[stop] emergency stop sandbox ({'stopped' if stopped else 'running'})", flush=True)
    print(f"[stop]   Dashboard  http://127.0.0.1:{port}/app/p/{PROJECT_ID}", flush=True)
    print(
        f"[stop]   The epic   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/tasks/{EPIC[0]}",
        flush=True,
    )
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
