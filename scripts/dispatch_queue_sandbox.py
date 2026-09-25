"""Stand up the machine's dispatch queue on its own port, with throwaway data.

task-459 lets a dispatch that finds every slot taken wait for one instead of being
refused. The two states worth comparing are states nobody can construct by hand -- you
cannot make the machine genuinely full to order, and you certainly cannot make a slot
free at the moment you are looking at it -- so this seeds each of them and hands over a
URL.

    python scripts/dispatch_queue_sandbox.py [port] [--free] [--drain N] [--ceiling N]

Two halves, and the comparison is the point:

    default (full)   the ceiling is 1, one run is holding the slot, and two dispatches
                     are waiting behind it. The Dashboard draws the board's single busy
                     cell and, under it, the waiting rail: each card naming its task,
                     its place in line, who queued it and how long it has waited, with a
                     Cancel button. Open one of the waiting tasks and press Dispatch:
                     the machine is full, so the refusal appears -- with "Queue it for
                     the next free slot" under it, which is the affordance this task
                     ships. Pressing it puts a third card on the rail.

    --free           the same machine with nothing running and nothing waiting: three
                     free cells, three Dispatch buttons, and no rail at all. This is
                     what the board looked like before task-459 and still looks like
                     when nothing is queued -- the rail is not a permanent fixture.

    --drain N        (with the default half) conclude the run holding the slot N seconds
                     after the server is up. The board is the only place this can be
                     watched: the busy cell empties, the tick offers the slot to the
                     head of the rail, every dispatch gate is judged *then*, and the
                     card turns into a run. Try `--drain 20`, and watch without
                     touching anything.

Run both halves at once to compare:

    python scripts/dispatch_queue_sandbox.py 8908
    python scripts/dispatch_queue_sandbox.py 8909 --free

**What to try, beyond looking at it.**

    * Cancel a waiting card. It goes, the task gets a note saying so, and nothing ran.
    * Cancel the *running* card from the Runs tab while two are waiting, and watch the
      head of the rail take the slot within a poll.
    * Put a waiting task on hold from its review panel, then free the slot. The entry
      keeps its place rather than being thrown away -- a hold is lifted by a person, so
      it is a wait and not a refusal -- and the one behind it goes first.
    * Close a waiting task. When the slot frees, that entry is refused at the gate
      instead of starting, dequeues itself, and writes the refusal on the task.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_PORT = 8908
DEFAULT_CEILING = 3
FULL_CEILING = 1


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


TASKS: List[Tuple[str, str]] = [
    ("task-101", "Teach the queue to explain itself"),
    ("task-102", "Collapse the two dependency banners into one"),
    ("task-103", "Stop the importer double-counting reruns"),
    ("task-104", "Give the exporter a dry run"),
    ("task-105", "Retire the third copy of the date parser"),
]


def build_project(root: Path, *, project_id: str, name: str, claimed: Tuple[str, ...]) -> Path:
    """One throwaway project with a queue of real, briefable tasks in it.

    ``claimed`` is what a seeded run is pretending to work. Claimed for real rather than
    left ``ready``, because a dispatch claims its task and a fixture that skipped that
    would offer the same task in a free cell as well.
    """
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle, LogEntryType
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
            summary=f"{title}. Seeded so a cell and a queue card have something real to show.",
            description=(
                "Seeded by scripts/dispatch_queue_sandbox.py. Nothing here is real work. "
                "It has a working description because a task that cannot brief an agent "
                "gets a link to its own page instead of a Dispatch button, which is a "
                "different state and not the one this fixture is for."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        # A human's entry, because a dispatch is caused by one and the button on the task
        # page would otherwise be refused before anything about the queue was reached.
        manager.add_log_entry(
            task_id,
            actor="Jeff Posey",
            type=LogEntryType.NOTE,
            body="Seeded so this task can be dispatched without writing a note first.",
        )
    for task_id in claimed:
        manager.claim_task(task_id, agent="claude")
    return project_root


def dispatch_config(ceiling: int, port: int) -> Dict[str, Any]:
    """A machine that will admit to one runner, with the project switched on.

    The runner sleeps rather than exiting, so a dispatch that starts from the board --
    or from the rail, when a slot frees -- becomes a real run in a real cell.
    """
    return {
        "version": 1,
        "enabled": True,
        # This sandbox is not on the default port, so it has to say where it serves --
        # exactly as a real deployment does. Without it a queued start would tell its
        # agent to look at :8765 and be refused by the reachability gate, which is the
        # right refusal and not the thing this fixture is for.
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            "sandbox-sleeper": {
                "argv": [sys.executable, "-c", "import time; time.sleep(600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": ceiling, "dispatch_queue_limit": 20},
        "projects": {
            "sandbox-queue": {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
        },
    }


def seed_run(home: Path, *, run_id: str, **meta: Any) -> None:
    """One run directory, in the shape ``ledger.read_run`` reads."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(180), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def main() -> None:
    argv = sys.argv[1:]
    free = "--free" in argv
    drain = 0
    if "--drain" in argv:
        index = argv.index("--drain")
        drain = int(argv[index + 1])
        del argv[index : index + 2]
    ceiling = DEFAULT_CEILING if free else FULL_CEILING
    if "--ceiling" in argv:
        index = argv.index("--ceiling")
        ceiling = int(argv[index + 1])
        del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-queue-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    project_root = build_project(
        root,
        project_id="sandbox-queue",
        name="Queue Sandbox",
        claimed=() if free else ("task-101",),
    )
    registry.add(project_root, project_id="sandbox-queue", name="Queue Sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(ceiling, port), sort_keys=False), encoding="utf-8"
    )

    def seed_busy_machine() -> None:
        """One run holding the only slot, and two dispatches waiting behind it.

        Deferred a moment after the server is up, exactly as the other board sandboxes
        defer theirs: the server reconciles the ledger when it boots and would correctly
        conclude a session run it has never heard of.

        The queue entries are written through the journal's own verb, so they are the
        same rows a real click writes -- and they are seeded *after* the run, because a
        queue whose machine was not yet full would simply start.
        """
        from datetime import datetime as _datetime

        from agentjobs.execution.factory import execution_db_path
        from agentjobs.execution.store import ExecutionStore

        seed_run(
            home,
            run_id="run_holding",
            task_id="task-101",
            project_id="sandbox-queue",
            mode="session",
            merge_mode="review",
            status="running",
            session_id="holding01",
        )
        # Each entry is written by a store whose clock is set back, so the rail has two
        # different waiting times to show rather than two identical ones -- which is what
        # makes "how long has this been sitting" legible at a glance. The rows are
        # otherwise exactly what a click writes: the journal's own verb, no shortcuts.
        for task_id, who, seconds in (
            ("task-102", "Jeff Posey", 260),
            ("task-103", "Jeff Posey", 95),
        ):
            moment = _datetime.fromisoformat(_ago(seconds))

            def at(fixed: datetime = moment) -> datetime:
                return fixed

            store = ExecutionStore(execution_db_path(home), clock=at)
            try:
                store.enqueue_dispatch(
                    "sandbox-queue",
                    task_id,
                    request={
                        "task_id": task_id,
                        "trigger": "manual",
                        "authorized_by": "Jeff Posey",
                    },
                    queued_by=who,
                    limit=20,
                )
            finally:
                store.close()
        print("[queue] seeded one run holding the slot and two dispatches waiting", flush=True)

    def free_the_slot() -> None:
        """End the run holding the slot, so the head of the rail takes it.

        By writing the run's own meta, because that is what this fixture's run *is*: a
        directory with no journal row, which is how a run that predates the journal --
        or one seeded here -- is judged. ``effective_live_runs`` asks the journal first
        and falls back to the meta when it has no row, so a terminal status here is the
        slot being free, in the one place the ceiling is counted from.
        """
        seed_run(
            home,
            run_id="run_holding",
            task_id="task-101",
            project_id="sandbox-queue",
            mode="session",
            merge_mode="review",
            status="finished",
            session_id="holding01",
        )
        print(
            "[queue] the run holding the slot has ended -- watch the rail's first card "
            "become a run within a poll",
            flush=True,
        )

    if not free:
        threading.Timer(2.0, seed_busy_machine).start()
        if drain:
            threading.Timer(2.0 + drain, free_the_slot).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    shape = (
        "nothing running, nothing waiting -- three free cells and no rail"
        if free
        else f"full at a ceiling of {ceiling}, two dispatches waiting"
    )
    print(f"[queue] dispatch queue sandbox ({shape})", flush=True)
    print(f"[queue]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-queue", flush=True)
    print(f"[queue]   Runs tab   http://127.0.0.1:{port}/app/p/sandbox-queue/runs", flush=True)
    print(
        f"[queue]   A waiting task  http://127.0.0.1:{port}/app/p/sandbox-queue/tasks/task-104",
        flush=True,
    )
    if not free:
        print(
            "[queue]   Compare with the free half: python scripts/dispatch_queue_sandbox.py "
            f"{port + 1} --free",
            flush=True,
        )
    if drain:
        print(f"[queue]   The slot frees in {drain}s. Watch the board.", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
