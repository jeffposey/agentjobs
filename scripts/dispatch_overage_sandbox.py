"""Stand up a full machine on its own port, so the three-way prompt can be driven.

task-461 replaces the ``concurrency_limit`` refusal with a question: **Queue**, **Dispatch
now**, or **Cancel**. The state it appears in is one nobody can construct by hand -- you
cannot make this machine genuinely full to order without spending an afternoon on it --
so this seeds it and hands over a URL.

    python scripts/dispatch_overage_sandbox.py [port] [--over] [--ceiling N]

Two halves, and the comparison is the point:

    default (full)   the ceiling is 1 and one run is holding the slot. Open any ready
                     task and press Dispatch: nothing is sent, and the prompt appears
                     naming the run in the way. Press *Dispatch now* and a second run
                     starts above the ceiling; press *Queue* and it joins the waiting
                     rail instead; press *Cancel*, or Escape, and nothing happens and
                     nothing is written.

    --over           the machine already over its ceiling: one ordinary run and one
                     overage, against a ceiling of 1. This is what the board looks like
                     *after* somebody presses Dispatch now, seeded so the two states can
                     be read side by side -- "1 of 1 slot busy" over one card, against
                     "2 of 1 slot busy · 1 over the ceiling" over two, the second badged.

Run both at once to compare:

    python scripts/dispatch_overage_sandbox.py 8910
    python scripts/dispatch_overage_sandbox.py 8911 --over

**What to try, beyond looking at it.**

    * Press Dispatch and then Escape. The prompt closes, no request is sent, and the
      task's log gains nothing -- reload it and check.
    * Press *Dispatch now*, then go to the Dashboard. Two cards against a ceiling of
      one, the second badged **over the ceiling**, and the capacity sentence counting
      honestly rather than clamping to the limit.
    * Open the overage task's record. Its dispatch entry says the run was started above
      the ceiling and why, because the run directory is on this machine and the task
      record is what travels.
    * Press Dispatch on a *third* task. The prompt comes back naming both runs, and the
      one started as an overage is named as one -- a machine reporting more runs than
      its own limit allows has to be able to say which one explains it.
    * Press *Queue* instead. The dispatch joins the rail exactly as it did before
      task-461; the prompt reuses the queue's own field rather than a second path.

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

DEFAULT_PORT = 8910
CEILING = 1


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


TASKS: List[Tuple[str, str]] = [
    ("task-201", "Teach the importer to skip its own writes"),
    ("task-202", "Collapse the two dependency banners into one"),
    ("task-203", "Give the exporter a dry run"),
    ("task-204", "Retire the third copy of the date parser"),
    ("task-205", "Say which gate refused, not that one did"),
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
            summary=f"{title}. Seeded so a cell and a prompt have something real to show.",
            description=(
                "Seeded by scripts/dispatch_overage_sandbox.py. Nothing here is real "
                "work. It has a working description because a task that cannot brief an "
                "agent gets a link to its own page instead of a Dispatch button, which "
                "is a different state and not the one this fixture is for."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        # A human's entry, because a dispatch is caused by one and the button on the task
        # page would otherwise be refused before anything about the ceiling was reached.
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

    The runner sleeps rather than exiting, so a dispatch started from the prompt becomes
    a real run in a real cell rather than a card that vanishes while you look at it.
    """
    return {
        "version": 1,
        "enabled": True,
        # This sandbox is not on the default port, so it has to say where it serves --
        # exactly as a real deployment does. Without it a started run would tell its
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
            "sandbox-overage": {
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
    over = "--over" in argv
    ceiling = CEILING
    if "--ceiling" in argv:
        index = argv.index("--ceiling")
        ceiling = int(argv[index + 1])
        del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-overage-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    claimed = ("task-201", "task-202") if over else ("task-201",)
    project_root = build_project(
        root,
        project_id="sandbox-overage",
        name="Overage Sandbox",
        claimed=claimed,
    )
    registry.add(project_root, project_id="sandbox-overage", name="Overage Sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(ceiling, port), sort_keys=False), encoding="utf-8"
    )

    def seed_busy_machine() -> None:
        """The run holding the only slot, and -- with ``--over`` -- the one past it.

        Deferred a moment after the server is up, exactly as the other board sandboxes
        defer theirs: the server reconciles the ledger when it boots and would correctly
        conclude a session run it has never heard of.
        """
        seed_run(
            home,
            run_id="run_holding",
            task_id="task-201",
            project_id="sandbox-overage",
            mode="session",
            posture="auto",
            status="running",
            session_id="holding01",
        )
        if over:
            seed_run(
                home,
                run_id="run_overage",
                task_id="task-202",
                project_id="sandbox-overage",
                mode="session",
                posture="auto",
                status="running",
                session_id="overage01",
                started_at=_ago(40),
                # The one field that makes this an overage rather than a miscount.
                over_ceiling=True,
            )
        print(
            "[overage] seeded "
            + ("one run holding the slot and one above the ceiling" if over else "a full machine"),
            flush=True,
        )

    threading.Timer(2.0, seed_busy_machine).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    shape = (
        f"already over its ceiling of {ceiling}: two runs, one of them an overage"
        if over
        else f"full at a ceiling of {ceiling}, with the prompt to drive"
    )
    print(f"[overage] dispatch overage sandbox ({shape})", flush=True)
    print(f"[overage]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-overage", flush=True)
    print(f"[overage]   Runs tab   http://127.0.0.1:{port}/app/p/sandbox-overage/runs", flush=True)
    print(
        f"[overage]   A task to dispatch  "
        f"http://127.0.0.1:{port}/app/p/sandbox-overage/tasks/task-203",
        flush=True,
    )
    if not over:
        print(
            "[overage]   Compare with the already-over half: python "
            f"scripts/dispatch_overage_sandbox.py {port + 1} --over",
            flush=True,
        )
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
