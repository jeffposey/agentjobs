"""Stand up the machine-wide live-run surfaces on their own port, with fake runs.

task-328 adds two places to look at what is running: a Runs tab with a count badge, and
a one-line capacity row at the foot of the Dashboard's statistics card. Both read
``GET /api/runs/live``, which is machine-wide -- so the thing worth seeing is a run from
one project appearing while you are looking at another, and that is what this seeds.

Two projects, and every state the surfaces can render:

    sandbox-alpha     three tasks, one of them with a live session run
    sandbox-beta      two tasks, one with a live run and one being merged by a
                      scripted finish, plus that repository's merge runway

So the Runs tab shows runs from **both** projects however you got to it, each row
linking into its own project; the badge reads the machine's count from every page; and
the "Also on this machine" section shows the finish and the runway that hold locks but
no run slots.

    python scripts/live_runs_sandbox.py [port] [--idle]

``--idle`` seeds no runs at all, which is the other half of the comparison: the badge
must read **0** rather than disappear, and the Dashboard row must say the machine is
quiet rather than vanish. Run one of each side by side on two ports to compare them
without constructing either state by hand.

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

DEFAULT_PORT = 8904


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def build_project(root: Path, *, project_id: str, name: str, tasks: List[Tuple[str, str]]) -> Path:
    """One throwaway project with the named tasks in it."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(TaskStorage(project_root / "tasks"))
    for task_id, title in tasks:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}.",
            description=(
                "Seeded by scripts/live_runs_sandbox.py. Nothing here is real work; the "
                "record exists so a run has a task to point at."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
    return project_root


def seed_run(home: Path, *, run_id: str, **meta: Any) -> None:
    """One run directory, in the shape ``ledger.read_run`` reads."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(180), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def seed_lock(home: Path, name: str, text: str) -> None:
    """One lock file, in the words ``LockHolder.parse`` reads."""
    directory = home / "runs" / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.lock").write_text(text, encoding="utf-8")


ALPHA_TASKS = [
    ("task-101", "Teach the queue to explain itself"),
    ("task-102", "Collapse the two dependency banners into one"),
    ("task-103", "Stop the importer double-counting reruns"),
]
BETA_TASKS = [
    ("task-501", "Rewrite the ingest pipeline"),
    ("task-502", "Retire the legacy export path"),
]


def main() -> None:
    argv = sys.argv[1:]
    idle = "--idle" in argv
    if idle:
        argv.remove("--idle")
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-live-runs-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.dispatch.ledger import runway_lock_name
    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    alpha = build_project(root, project_id="sandbox-alpha", name="Alpha", tasks=ALPHA_TASKS)
    beta = build_project(root, project_id="sandbox-beta", name="Beta", tasks=BETA_TASKS)
    registry.add(alpha, project_id="sandbox-alpha", name="Alpha")
    registry.add(beta, project_id="sandbox-beta", name="Beta")

    # A ceiling of three, so "2 of 3 slots busy" has a spare slot in it and the row is
    # not permanently at its limit.
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "enabled": True, "runners": {}, "limits": {"max_concurrent_runs": 3}}
        ),
        encoding="utf-8",
    )

    def seed_activity() -> None:
        """Write the fake runs and locks.

        Deferred until after startup, and that is not a workaround for a bug: the
        server reconciles the ledger when it boots, asks the session manager about every
        live session run it finds, and correctly concludes the ones it has never heard
        of. A seeded run written before that is `interrupted` before the first page
        loads. Writing them a moment later is exactly what a real dispatch does.
        """
        seed_run(
            home,
            run_id="run_alpha01",
            task_id="task-101",
            project_id="sandbox-alpha",
            mode="session",
            posture="auto",
            status="running",
            session_id="alpha01",
        )
        seed_run(
            home,
            run_id="run_beta01",
            task_id="task-501",
            project_id="sandbox-beta",
            mode="session",
            posture="autonomous",
            status="parked",
            session_id="beta01",
            started_at=_ago(1_500),
        )
        # A finish and a runway: real machine activity with no run record, which is why
        # they are listed separately rather than counted as occupied slots.
        seed_lock(
            home,
            "task-502",
            f"pid={os.getpid()} run= kind=finish finish=fin_sandbox started={_ago(45)}",
        )
        seed_lock(
            home,
            runway_lock_name(beta),
            f"pid={os.getpid()} run= kind=runway finish=fin_sandbox started={_ago(45)}",
        )
        print("[runs] seeded two live runs, a finish and a runway", flush=True)

    if not idle:
        threading.Timer(2.0, seed_activity).start()

    import uvicorn

    from agentjobs.api.main import app

    state = "idle -- nothing running" if idle else "busy -- runs in two projects"
    print(f"[runs] live-run sandbox ({state}) at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[runs]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-alpha", flush=True)
    print(f"[runs]   Runs tab   http://127.0.0.1:{port}/app/p/sandbox-alpha/runs", flush=True)
    print(f"[runs]   Beta side  http://127.0.0.1:{port}/app/p/sandbox-beta", flush=True)
    if not idle:
        print(
            "[runs]   Compare with the idle half: python scripts/live_runs_sandbox.py "
            f"{port + 1} --idle",
            flush=True,
        )
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
