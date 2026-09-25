"""Stand up the machine-wide live-run surfaces on their own port, with fake runs.

task-328 added a Runs tab with a count badge; task-588 retired the tab and folded the
badge into the Dashboard tab itself -- a red dot for what is waiting on you, a green dot
for what is being worked. The runs half reads
``GET /api/runs/live``, which is machine-wide -- so the thing worth seeing is a run from
one project counting while you are looking at another, and that is what this seeds.

Two projects, and every state the surfaces can render:

    sandbox-alpha     three tasks, one of them with a live session run and two
                      handed to a human for review, so the red dot reads 2
    sandbox-beta      two tasks, one with a live run and one being merged by a
                      scripted finish, plus that repository's merge runway

So the Dashboard tab says 2 waiting on you and 3 being worked (two runs and the finish)
on every page of Alpha, and ``/app/p/sandbox-alpha/runs`` lands on the Dashboard.

    python scripts/live_runs_sandbox.py [port] [--idle]

``--idle`` seeds no runs and hands nothing to a human, which is the other half of the
comparison: both counts must read **0** rather than disappear. Run one of each side by
side on two ports to compare them without constructing either state by hand.

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


def build_project(
    root: Path,
    *,
    project_id: str,
    name: str,
    tasks: List[Tuple[str, str]],
    waiting: Tuple[str, ...] = (),
) -> Path:
    """One throwaway project with the named tasks in it, ``waiting`` handed to a human."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))
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
    for task_id in waiting:
        # Through the verbs, the way a real handoff puts work on a person.
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Seeded so the header's red dot has something to count.",
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
    alpha = build_project(
        root,
        project_id="sandbox-alpha",
        name="Alpha",
        tasks=ALPHA_TASKS,
        waiting=() if idle else ("task-102", "task-103"),
    )
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
            merge_mode="review",
            status="running",
            session_id="alpha01",
        )
        seed_run(
            home,
            run_id="run_beta01",
            task_id="task-501",
            project_id="sandbox-beta",
            mode="session",
            merge_mode="automerge",
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

    def seed_once_serving() -> None:
        """Seed once the server answers, rather than on a fixed delay.

        A fixed two seconds was enough when this was written. Startup now does more
        before it reconciles -- a second, tailnet listener among it -- so by 2026-09-24
        the seeded runs landed *before* the sweep and were concluded as interrupted on
        the first page load. Answering a request means the sweep is behind it.
        """
        import time
        import urllib.request

        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        time.sleep(1.0)
        seed_activity()

    if not idle:
        threading.Thread(target=seed_once_serving, daemon=True).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    state = "idle -- nothing running" if idle else "busy -- runs in two projects"
    print(f"[runs] live-run sandbox ({state}) at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[runs]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-alpha", flush=True)
    print(
        f"[runs]   Old Runs   http://127.0.0.1:{port}/app/p/sandbox-alpha/runs (redirects)",
        flush=True,
    )
    print(f"[runs]   Beta side  http://127.0.0.1:{port}/app/p/sandbox-beta", flush=True)
    if not idle:
        print(
            "[runs]   Compare with the idle half: python scripts/live_runs_sandbox.py "
            f"{port + 1} --idle",
            flush=True,
        )
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
