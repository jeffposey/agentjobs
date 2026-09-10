"""Stand up the bounded Dashboard on its own port, with throwaway data.

task-294 bounds the Dashboard to a frame of exactly one viewport. The thing worth
driving by hand is not that it is short today -- it is that it *stays* one screen when
the project underneath it is not small, which is a state you cannot get to on a fresh
project and would not want to construct on the real corpus. So this seeds forty open
tasks, a queue, drafts and a log full of activity, and hands over a URL.

    python scripts/one_screen_sandbox.py [port] [--alarm] [--ceiling N] [--busy]

What to look at, and the comparison is the point:

    default            a calm project with forty open tasks. The slot board is the top
                       of the page, the two lists below it are a bounded tail with
                       their own scroll, and the browser window does not scroll at all.
                       Resize the window, or open Chrome's device toolbar and pick an
                       iPhone: the page never grows a scrollbar of its own.
    --alarm            a task stopped on a human. The alert takes the top and the board
                       follows it as a status readout with its Dispatch buttons
                       withheld (task-081's rule), and the frame still holds.
    --ceiling N        how many cells the board draws. `1` is a single panel and the
                       tail gets most of the screen; `6` is the board's cap and on a
                       phone it takes nearly all of it, which is the one case where the
                       board scrolls inside its own region rather than fitting.
    --busy             two runs occupying cells, so the board is not all free cells.

**The Tasks tab is the other half of the comparison, and it is deliberately unchanged.**
The frame is the Dashboard's alone: click Tasks and the window scrolls again, exactly as
it did before. That is what keeps drag-to-reorder working, because the helper behind it
scrolls with `window.scrollBy` and would have gone silent if the document had stopped
being the scroller everywhere. Drag a row in the backlog there to confirm it by hand.

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
from typing import Any, Dict, Tuple

import yaml

DEFAULT_PORT = 8908
DEFAULT_CEILING = 3

#: How many open tasks the project carries.
#:
#: Forty, because `active_tasks` is uncapped by the server -- it is every open task --
#: and the Dashboard used to render all of them. On this repository's own corpus that
#: was the four thousand pixels that made the page eight screens tall on a phone.
CROWD = 40

TITLES = [
    "Teach the queue to explain itself",
    "Collapse the two dependency banners into one",
    "Stop the importer double-counting reruns",
    "Give the exporter a dry run",
    "Retire the third copy of the date parser",
    "Name the two things both called `state`",
    "Make the seed script idempotent",
    "Drop the unused compatibility shim",
    "Put the retry budget where the caller can see it",
    "Say which of the two clocks a timestamp came from",
]


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def build_project(root: Path, *, alarm: bool, claimed: Tuple[str, ...]) -> Path:
    """One throwaway project big enough that an unbounded Dashboard would not fit."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / "sandbox-crowded"
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="A crowded project", user="Jeff Posey"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks"))

    bands = [Priority.CRITICAL, Priority.HIGH, Priority.MEDIUM, Priority.LOW]
    for index in range(CROWD):
        manager.create_task(
            id=f"task-{100 + index}",
            title=f"{TITLES[index % len(TITLES)]} ({index + 1})",
            summary=(
                "Seeded so the Dashboard's active list and its queue have something "
                "real in them. Nothing here is work."
            ),
            description=(
                "Seeded by scripts/one_screen_sandbox.py. It has a working description "
                "because a task that cannot brief an agent gets a link to its own page "
                "instead of a Dispatch button, which is a different cell and not the "
                "one this fixture is for."
            ),
            lifecycle=Lifecycle.READY,
            priority=bands[index % len(bands)],
            actor="claude",
        )

    # Drafts, so the route that replaced the count strip's `+N in backlog` link has
    # something at the end of it: View all -> the Tasks surface -> Status: Draft.
    for index in range(6):
        manager.create_task(
            id=f"task-{300 + index}",
            title=f"An idea that still needs a decision ({index + 1})",
            summary="A draft. Nothing is blocked by it; it needs a call before it is work.",
            description="Seeded so the drafts route has something at the end of it.",
            lifecycle=Lifecycle.DRAFT,
            actor="claude",
        )

    for task_id in claimed:
        manager.claim_task(task_id, agent="claude")

    if alarm:
        # Through the verbs, because a task cannot be *created* active -- and because
        # doing it any other way would skip the log entries that make it look real.
        manager.create_task(
            id="task-900",
            title="A branch is sitting at the merge gate",
            summary="Work has stopped on this until somebody reviews it.",
            description="Seeded so the alert rung has something to be alarmed about.",
            lifecycle=Lifecycle.READY,
            priority=Priority.CRITICAL,
            actor="claude",
        )
        manager.claim_task("task-900", agent="claude")
        manager.handoff(
            "task-900",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Approve the branch, or say what needs changing.",
            body="Seeded by the sandbox so the alert rung has something to be alarmed about.",
        )
    return project_root


def seed_run(home: Path, *, run_id: str, **meta: Any) -> None:
    """One run directory, in the shape ``ledger.read_run`` reads."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(240), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def dispatch_config(ceiling: int) -> Dict[str, Any]:
    """A machine that will admit to one runner, with the sandbox project switched on.

    The runner sleeps rather than exiting, so pressing a real Dispatch button seeds a
    real run into a real cell. ``actor`` is required: a runner writes as an identity the
    project configures, and a dispatch whose runner would act as ``sandbox-sleeper`` is
    refused before any run exists (task-159).
    """
    return {
        "version": 1,
        "enabled": True,
        "runners": {
            "sandbox-sleeper": {
                "argv": [sys.executable, "-c", "import time; time.sleep(600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": ceiling},
        "projects": {
            "sandbox-crowded": {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
        },
    }


def main() -> None:
    argv = sys.argv[1:]
    alarm = "--alarm" in argv
    busy = "--busy" in argv
    ceiling = DEFAULT_CEILING
    if "--ceiling" in argv:
        index = argv.index("--ceiling")
        ceiling = int(argv[index + 1])
        del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-one-screen-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    claimed: Tuple[str, ...] = ("task-100", "task-101") if busy else ()
    project = build_project(root, alarm=alarm, claimed=claimed)
    registry.add(project, project_id="sandbox-crowded", name="A crowded project")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(ceiling), sort_keys=False), encoding="utf-8"
    )

    def seed_activity() -> None:
        """Write the fake runs a moment after the server is up.

        Deferred deliberately, exactly as the other sandboxes defer theirs: the server
        reconciles the ledger when it boots and correctly concludes any session run it
        has never heard of, so a run seeded before that is `interrupted` before the
        first page loads.
        """
        for index, task_id in enumerate(claimed):
            seed_run(
                home,
                run_id=f"run_busy{index}",
                task_id=task_id,
                project_id="sandbox-crowded",
                mode="session",
                posture="auto",
                status="running",
                session_id=f"busy{index}",
            )
        print(f"[frame] seeded {len(claimed)} live runs", flush=True)

    if busy:
        threading.Timer(2.0, seed_activity).start()

    import uvicorn

    from agentjobs.api.main import app

    shape = "calm" if not alarm else "with an alert holding the top of the page"
    print(
        f"[frame] a project with {CROWD} open tasks and 6 drafts, {shape}, "
        f"board ceiling {ceiling}",
        flush=True,
    )
    print(f"[frame]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-crowded", flush=True)
    print(
        f"[frame]   Tasks      http://127.0.0.1:{port}/app/p/sandbox-crowded/tasks"
        "   (still scrolls, deliberately -- drag a row here)",
        flush=True,
    )
    print("[frame] Resize the window, or open the device toolbar and pick a phone.", flush=True)
    if not alarm:
        print(
            f"[frame]   The alarming half: python scripts/one_screen_sandbox.py "
            f"{port + 1} --alarm",
            flush=True,
        )
    print(
        f"[frame]   The tallest board: python scripts/one_screen_sandbox.py "
        f"{port + 2} --ceiling 6",
        flush=True,
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
