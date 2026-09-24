"""Stand up a task list and a run board with both chip motions (task-570, task-577).

Two motions, two meanings, and each must appear only where it belongs. **Orbit** -- a
comet running round the border -- is "something is happening right now", backed by a live
fact. **Flash** -- an uneven glow that catches and dies -- is "waiting on you", every chip
in the red needs_you category. So this seeds **both sides of every row** in one project:

    python scripts/chip_motion_sandbox.py [port]

    task-001  a finish in the gate                    Finishing        orbit
    task-002  a dispatch going through the gates      Starting         orbit
    task-003  a dispatch waiting for a slot           Queued           still
    task-004  a run producing output                  Working          orbit
    task-005  a run whose session is starting         Working          orbit
    task-006  claimed, and no run at all              Working          STILL -- the catch
    task-007  a run parked on a permission prompt     Working          still
    task-008  a run with feedback waiting for it      Working          still
    task-009  handed to review, session still open    Needs review     flash
    task-010  on hold                                 On hold          still
    task-011  waiting on another task                 Blocked          still
    task-012  merged                                  Completed        still
    task-013  needs its spec written                  Needs spec       flash
    task-014  needs a call                            Needs decision   flash
    task-015  needs an answer                         Needs input      flash
    task-016  needs a sign-off                        Needs approval   flash

Five flashing chips on one list is deliberate: it is how dense the flash gets on a real
day, and density is the thing to judge.

The Runs tab shows the run side: Finishing, Working and Starting orbit; Waiting on you
(task-007's parked run) flashes; Feedback stays still. task-009's session is still
producing output, so its run chip orbits while its task chip, Needs review, flashes.
task-012's session outlived its task: its run chip reads "Completed" in closed grey, the
task's own word, where it used to read a private "Work done" (task-577).

task-004 and task-006 are the comparison that matters for the orbit. Their records are
identical -- active, agent, work, same owner -- and only one has a live run behind it.

**Reduced motion:** DevTools -> More tools -> Rendering -> "Emulate CSS media feature
prefers-reduced-motion" -> reduce. Every chip stops; the moving ones keep a still outer
ring.

The runs, locks, finish and queue entry are the shapes the ledger, ``dispatch.finish``
and the dispatch queue write, read by the real readers -- nothing is spawned. The locks
name this process's pid, so the real liveness rule reports them live. Nothing touches the
live corpus or the 8876 dashboard: everything lives under a temporary ``AGENTJOBS_HOME``,
deleted when this process stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULT_PORT = 8924

PROJECT_ID = "sandbox-motion"
PROJECT_NAME = "Sandbox: which status chips move"


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def dispatch_config(home: Path, runner_script: Path) -> None:
    """Enough configuration for the Runs tab and the queue; `shadow`, so nothing starts."""
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
        "limits": {"max_concurrent_runs": 6, "dispatch_queue_limit": 10},
        "execution": {"controller": "shadow"},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def hold_lock(
    home: Path, task_id: str, *, run_id: str = "", kind: str = "dispatch", finish_id: str = ""
) -> None:
    """The task's run lock, held by this live process."""
    from agentjobs.dispatch.ledger import locks_root

    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{PROJECT_ID}~{task_id}.lock").write_text(
        f"pid={os.getpid()} run={run_id} kind={kind} finish={finish_id} started={_ago(3)}",
        encoding="utf-8",
    )


def write_run(
    home: Path,
    run_id: str,
    *,
    task_id: str,
    status: str,
    lock: bool = True,
    handback_pending: Optional[int] = None,
    slot_released: bool = False,
) -> None:
    """A dispatched session's run directory, as the ledger writes one."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": PROJECT_ID,
        "mode": "session",
        "agent": "claude",
        "runner": "fake",
        "posture": "auto",
        "status": status,
        "session_id": f"sess-{run_id}",
        "started_at": _ago(12),
    }
    if handback_pending is not None:
        meta["handback_pending"] = handback_pending
    if slot_released:
        meta["slot_released_at"] = _ago(2)
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    if lock:
        hold_lock(home, task_id, run_id=run_id)


def live_finish(home: Path, task_id: str) -> None:
    """A finish in the gate, holding the task's lock."""
    directory = home / "finishes" / "fin_motion01"
    directory.mkdir(parents=True)
    meta = {
        "finish_id": "fin_motion01",
        "task_id": task_id,
        "project_id": PROJECT_ID,
        "outcome": "running",
        "started_at": _ago(3),
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    lines: list[Dict[str, Any]] = [
        {"kind": "finish_preflight", "branch": f"feat/{task_id}-x", "worktree": str(home)},
        {"kind": "finish_step", "step": "preflight", "ok": True, "seconds": 1.4},
        {"kind": "finish_step", "step": "runway", "ok": True, "seconds": 0.2},
        {"kind": "finish_step", "step": "rebase", "ok": True, "seconds": 3.1},
        {"kind": "gate_stage_started", "stage": "pytest", "stages_run": 6, "stages_total": 10},
    ]
    with (directory / "phases.jsonl").open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps({"ts": _ago(1), **line}) + "\n")
    hold_lock(home, task_id, kind="finish", finish_id="fin_motion01")


ROWS = [
    ("task-001", "A finish is merging this one", "Finishing, backed by a live finish: the orbit."),
    (
        "task-002",
        "A dispatch of this one is going through the gates",
        "Starting, backed by the queue: the orbit.",
    ),
    (
        "task-003",
        "A dispatch of this one is waiting for a slot",
        "Queued: still. Nothing is happening yet.",
    ),
    (
        "task-004",
        "A run is producing output on this one",
        "Working, backed by a working run: the orbit.",
    ),
    (
        "task-005",
        "A run for this one is still starting",
        "Working, backed by a starting run: the orbit.",
    ),
    ("task-006", "Claimed, with no run behind it", "Working on the record alone: must stay still."),
    (
        "task-007",
        "Its run is parked on a permission prompt",
        "Working, but the run is waiting: still.",
    ),
    (
        "task-008",
        "Feedback is waiting for its run",
        "Working, but the run has feedback queued: still.",
    ),
    (
        "task-009",
        "Handed to review with its session still open",
        "Needs review: the flash, live run or not.",
    ),
    ("task-010", "On hold", "On hold: still."),
    ("task-011", "Waiting on another task", "Blocked: still."),
    ("task-012", "Merged an hour ago", "Completed: still."),
    ("task-013", "Its spec needs writing", "Needs spec: the flash."),
    ("task-014", "It needs a call from you", "Needs decision: the flash."),
    ("task-015", "It needs an answer from you", "Needs input: the flash."),
    ("task-016", "It needs your sign-off", "Needs approval: the flash."),
]


def seed(manager: Any) -> Dict[str, int]:
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome, Priority

    authorised: Dict[str, int] = {}
    for task_id, title, summary in ROWS:
        manager.create_task(
            id=task_id,
            title=title,
            summary=summary,
            description="Seeded for review. Nothing here is real work.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ops",
        )
        task = manager.add_log_entry(
            task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go ahead and work this."
        )
        authorised[task_id] = task.log[-1].id

    for task_id in ("task-001", "task-004", "task-005", "task-006", "task-007", "task-008"):
        manager.claim_task(task_id, agent="claude")
    for task_id in (
        "task-009",
        "task-010",
        "task-011",
        "task-012",
        "task-013",
        "task-014",
        "task-015",
        "task-016",
    ):
        manager.claim_task(task_id, agent="claude")
    for task_id, reason, prompt in (
        ("task-013", BallReason.SPEC, "Write the acceptance criteria."),
        ("task-014", BallReason.DECISION, "Pick one of the two designs."),
        ("task-015", BallReason.INPUT, "Which port should it use?"),
        ("task-016", BallReason.APPROVAL, "Approve the release."),
    ):
        manager.handoff(
            task_id, actor="claude", ball=Ball.HUMAN, ball_reason=reason, ball_prompt=prompt
        )
    manager.handoff(
        "task-009",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Review the change.",
    )
    manager.handoff(
        "task-010",
        actor="claude",
        ball=Ball.AGENT,
        ball_reason=BallReason.HOLD,
        ball_prompt="Parked until the release ships.",
    )
    manager.handoff(
        "task-011",
        actor="claude",
        ball=Ball.EXTERNAL,
        ball_reason=BallReason.DEPENDENCY,
        ball_prompt="Waiting for task-003 to land.",
    )
    manager.close_task("task-012", actor="claude", outcome=Outcome.COMPLETED, body="Merged.")
    return authorised


def queue(home: Path, manager: Any, authorised: Dict[str, int]) -> None:
    """Two real queue entries; the first is then claimed, which is what a tick does."""
    from agentjobs.dispatch import queue as dispatch_queue
    from agentjobs.dispatch.guards import DispatchRequest
    from agentjobs.dispatch.journal import journal
    from agentjobs.projects import ProjectRegistry

    project = ProjectRegistry(home).get(PROJECT_ID)
    entries = {}
    for task_id in ("task-002", "task-003"):
        entries[task_id] = dispatch_queue.enqueue(
            home,
            project,
            DispatchRequest(task_id=task_id, caused_by=authorised[task_id]),
            manager=manager,
            queued_by="Jeff Posey",
            api_base="http://127.0.0.1:9",
        )
    journal(home).claim_queued_dispatch(entries["task-002"].queue_id)


def runs(home: Path) -> None:
    write_run(home, "run_finish01", task_id="task-001", status="running", lock=False)
    live_finish(home, "task-001")
    write_run(home, "run_work0001", task_id="task-004", status="running")
    write_run(home, "run_start001", task_id="task-005", status="starting")
    write_run(home, "run_parked01", task_id="task-007", status="parked")
    write_run(home, "run_handbk01", task_id="task-008", status="running", handback_pending=3)
    write_run(home, "run_review01", task_id="task-009", status="running")
    write_run(home, "run_closed01", task_id="task-012", status="running", slot_released=True)


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
    return project_root, manager


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-chip-motion-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_root, manager = build(root)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=PROJECT_NAME)
    runner_script = root / "never_runs.py"
    runner_script.write_text("print('this sandbox never starts anything')\n", encoding="utf-8")
    dispatch_config(home, runner_script)
    authorised = seed(manager)
    queue(home, manager, authorised)
    runs(home)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] chip-motion sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   the task list        {base}/tasks?status=all", flush=True)
    print(f"[review]   the run board        {base}/runs", flush=True)
    print(f"[review]   the dashboard        http://127.0.0.1:{port}/app/", flush=True)
    print(
        "[review] Orbit: compare task-004 (moves) with task-006 (still). Flash: tasks 009, 013-016.",
        flush=True,
    )
    # `lifespan="off"`: the lifespan starts the dispatch poller, which would reap the
    # live locks and claim the queue this exhibit is made of.
    serve(app, port=port, lifespan="off")


if __name__ == "__main__":
    main()
