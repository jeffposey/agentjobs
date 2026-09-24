"""Stand up a task list where one task is being merged and another is being worked.

task-509 gives a task whose branch a scripted finish is rebasing, gating and merging its
own status label: *Finishing*. Before it, such a task read *In progress (claude)* --
the same words a task an agent is editing gets, because the record genuinely says the
same thing about both. The two are indistinguishable on every axis a task carries, which
is why the defect existed and why a screenshot of one of them proves nothing.

So this seeds **both, side by side, in one list**, plus the two states that must *not*
get the label:

    python scripts/finishing_status_sandbox.py [port]

    task-001   a finish in the gate            -> Finishing
    task-002   an agent genuinely working it   -> In progress (claude)
    task-003   a finish that merged an hour ago -> Completed
    task-004   a finish whose process is gone  -> In progress (claude)

``task-001`` and ``task-002`` are the comparison. Their ``lifecycle``, ``ball``,
``ball_reason`` and owner are identical, and both have a live process holding their run
lock; the only difference is what that lock is holding it *for*. ``task-003`` and
``task-004`` are acceptance criterion a3 from both ends -- a finish that ended, and one
that never wrote an ending because its machine went away mid-gate.

Three things to look at:

    the list      the four labels in one column, which is the surface the report was
                  about. Then set Status to *Finishing* and watch three of the four go.
    task-001      the page. The chip says Finishing, the panel below says which step,
                  and the Dispatch button is withheld with a sentence saying why -- the
                  server would refuse that click anyway, because the finish holds the
                  task's run lock.
    task-002      the same page for a task an agent is working. Dispatch is withheld
                  there too, for a different reason, and the chip reads differently.

**The finishes are real as far as the reader is concerned.** Their directories, phase
records and lock files are the shapes ``dispatch.finish`` writes, and every label on the
page is computed by ``read_finish_status`` from them -- but nothing is spawned, nothing
is rebased and nothing is merged. Seeding a genuinely live finish would mean running a
real gate against a real branch, which is not a thing a review sandbox can arrange; what
is *not* faked is the rule that decides liveness, which is the lock's.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_PORT = 8922

PROJECT_ID = "sandbox-finishing"
PROJECT_NAME = "Sandbox: what a task being merged says it is"

DEAD_PID = 999_999
"""A pid nothing can be running under, for the finish whose machine went away."""


def dispatch_config(home: Path, runner_script: Path) -> None:
    """Enough dispatch configuration for the task page to offer a Dispatch button.

    Without it the button is absent everywhere and the exhibit -- a button *withheld*,
    with a sentence -- cannot be seen at all.
    """
    config = {
        "version": 1,
        "enabled": True,
        "runners": {
            "fake": {
                "mode": "session",
                "actor": "claude",
                "argv": [sys.executable, str(runner_script), "{prompt}"],
            }
        },
        "projects": {
            PROJECT_ID: {
                "enabled": True,
                "runner": "fake",
                "posture": "auto",
                "require_clean_tree": False,
                "finish": {"enabled": True},
            }
        },
        "limits": {"max_concurrent_runs": 3},
        # `shadow`, so nothing here starts, stops or reaps anything while it is being
        # looked at. The four states sitting where they were put is the whole exhibit.
        "execution": {"controller": "shadow"},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


# ----- the shapes a finish leaves on disk ---------------------------------------------


def write_finish(
    home: Path,
    finish_id: str,
    *,
    task_id: str,
    outcome: str = "running",
    age_minutes: float = 3.0,
    **meta: Any,
) -> Path:
    """A finish directory as ``FinishDirectory`` would have left it."""
    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    payload: Dict[str, Any] = {
        "finish_id": finish_id,
        "task_id": task_id,
        "project_id": PROJECT_ID,
        "outcome": outcome,
        "started_at": started.isoformat(),
    }
    payload.update(meta)
    (directory / "meta.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return directory


def step(directory: Path, name: str, **fields: Any) -> None:
    record(directory, "finish_step", step=name, ok=True, seconds=fields.pop("seconds", 1.0))


def record(directory: Path, kind: str, **fields: Any) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})
    with (directory / "phases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def hold_lock(
    home: Path,
    task_id: str,
    *,
    pid: int,
    kind: str,
    finish_id: str = "",
) -> None:
    """The task lock a finish -- or a run -- holds for the whole of its attempt.

    ``pid`` is this process for the live ones, which is what makes ``stale_lock_reason``
    report them as working: the sandbox server is a real live process, so the liveness
    rule is answered by the real rule rather than by a flag this script sets.
    """
    from agentjobs.dispatch.ledger import locks_root

    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    started = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    (directory / f"{PROJECT_ID}~{task_id}.lock").write_text(
        f"pid={pid} run=run_sandbox kind={kind} finish={finish_id} started={started}",
        encoding="utf-8",
    )


# ----- the four states ----------------------------------------------------------------


def seed(manager: Any) -> None:
    """Four tasks whose records are as alike as the defect made them."""
    from agentjobs.models_v2 import Lifecycle, LogEntryType, Outcome, Priority

    def task(task_id: str, title: str, summary: str, *, category: str = "ops") -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=summary,
            description="Seeded for review. Nothing here is real work.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category=category,
        )
        manager.add_log_entry(
            task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go ahead and work this."
        )

    task(
        "task-001",
        "A finish is merging this one's branch",
        "The exhibit. Approved three minutes ago; the gate is running now.",
    )
    task(
        "task-002",
        "An agent is genuinely working this one",
        "The control. Same lifecycle, same ball, same owner, same live lock.",
    )
    task(
        "task-003",
        "A finish merged this one an hour ago",
        "Finishing means live. A finish that is over is the task's own closed state.",
    )
    task(
        "task-004",
        "A finish for this one died mid-gate",
        "It wrote no ending because nothing was left to write one. Not live either.",
    )
    for index, title in enumerate(
        ("Rework the queue's band arithmetic", "Attachment thumbnails are served full size"),
        start=10,
    ):
        task(
            f"task-{index:03d}",
            title,
            "Seeded so the four rows above sit in a list.",
            category="ux",
        )

    for task_id in ("task-001", "task-002", "task-003", "task-004"):
        manager.claim_task(task_id, agent="claude")
    manager.close_task(
        "task-003",
        actor="claude",
        outcome=Outcome.COMPLETED,
        body="Merged by the scripted finish: 4f21c0a.",
    )


def finishes(home: Path) -> None:
    """One live finish, one that merged, one that died. Plus a run that is only a run."""
    live = write_finish(home, "fin_live0001", task_id="task-001")
    record(live, "finish_preflight", branch="feat/task-001-derive-the-label", worktree=str(home))
    step(live, "preflight", seconds=1.4)
    step(live, "runway", seconds=0.2)
    step(live, "rebase", seconds=3.1)
    # The gate is around 85% of a finish's wall clock, so it is the step a reader will
    # almost always catch one on -- and the one the chip's sentence names.
    record(
        live,
        "gate_stage_started",
        stage="pytest",
        stages_run=6,
        stages_total=10,
    )
    hold_lock(home, "task-001", pid=os.getpid(), kind="finish", finish_id="fin_live0001")

    merged = write_finish(
        home,
        "fin_done0001",
        task_id="task-003",
        outcome="finished",
        age_minutes=64,
        finished_at=(datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
        merge_commit="4f21c0a",
    )
    record(merged, "finish_preflight", branch="feat/task-003-done", worktree=str(home))
    for name in ("preflight", "runway", "rebase", "gate", "merge", "rebuild", "restart", "close"):
        step(merged, name)

    gone = write_finish(home, "fin_gone0001", task_id="task-004", age_minutes=41)
    record(gone, "finish_preflight", branch="feat/task-004-interrupted", worktree=str(home))
    step(gone, "preflight", seconds=1.2)
    step(gone, "rebase", seconds=2.8)
    hold_lock(home, "task-004", pid=DEAD_PID, kind="finish", finish_id="fin_gone0001")

    # The control: a live lock that is a *run*, not a finish. It makes task-002 a
    # candidate for the batched lookup and is then dropped by the confirmation, which is
    # the half of the rule a reader cannot see and a test can.
    hold_lock(home, "task-002", pid=os.getpid(), kind="dispatch")


def build(root: Path) -> tuple[Path, Any]:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name=PROJECT_NAME, user="Jeff Posey"), sort_keys=False
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))
    seed(manager)
    return project_root, manager


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-finishing-status-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_root, _ = build(root)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=PROJECT_NAME)

    runner_script = root / "never_runs.py"
    runner_script.write_text("print('this sandbox never starts anything')\n", encoding="utf-8")
    dispatch_config(home, runner_script)
    finishes(home)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] finishing-status sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   all four states       {base}/tasks?status=all", flush=True)
    print(f"[review]   only the live one     {base}/tasks?status=finishing", flush=True)
    print(f"[review]   being merged          {base}/tasks/task-001", flush=True)
    print(f"[review]   being worked          {base}/tasks/task-002", flush=True)
    print(f"[review]   merged an hour ago    {base}/tasks/task-003", flush=True)
    print(f"[review]   died mid-gate         {base}/tasks/task-004", flush=True)
    print(
        "[review] Read the Status column on the list first: task-001 and task-002 differ "
        "in nothing else. Then open task-001 and look for the Dispatch button.",
        flush=True,
    )
    # `lifespan="off"` for the reason every sandbox here uses it: the lifespan starts the
    # dispatch poller, and a poller would reap the "live" locks this exhibit is made of.
    serve(app, port=port, lifespan="off")


if __name__ == "__main__":
    main()
