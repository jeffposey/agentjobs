"""Stand up every task status on its own port, with throwaway data (task-562, task-578).

task-562 gives every surface one status vocabulary and one colour per category. The
thing to review is the whole table at once -- one task in every state -- and the same
words and colours on the other surfaces that draw them: the task page, the dashboard's
Recently finished, the Runs tab, and the slot board's queued and epic-walk rails.

    python scripts/status_vocabulary_sandbox.py [port]

What to look at:

  * **The task list** (`?status=all`), sorted into the seven colours:
    green Ready · brown Queued, Starting · blue Working · purple Landing ·
    red Needs spec / review / decision / approval / input, Error ·
    pink Blocked, On hold, Quota · yellow Draft ·
    grey: Completed solid, and Superseded / Cancelled / Duplicate hollow.
    Every word and colour comes from src/agentjobs/status_vocabulary.json.
    Each task's title names the state it was seeded into.
  * **No icons** (task-578): the data file names one per status, and no chip draws it.
  * **Any task page**: the header chip and the "Work state" card say the same word in the
    same colour as the list, and the reason line carries what left the chip (the
    blocker, the quota reset time in your zone, the finish step). task-005 is the
    Landing task: its finish panel reads "Landing this task".
  * **The dashboard**: Recently finished shows the four closed tasks in grey, three
    hollow. The slot board's queued rail is headed "Queued". The "Epics being
    walked" rail has one walk each Walking (blue), Waiting (pink) and Grounded (red),
    with no violet on the cards.
  * **The Runs tab**: Working blue, Starting brown, Waiting on you red, Landing purple,
    Feedback blue, and No output (orange, a process word).

**Two things here are drawn, not real**, and both are machine state a review sandbox
cannot arrange on demand: the three epic walks (a real walk dispatches real children)
and the run-health words (a real run's health comes from a live session). Both are
served by patching one builder in the running app, so what a browser receives goes
through the real response models and the real React components. Everything about the
task chips is the real server: records written through the manager's verbs, a real
queue, a real finish's files on disk.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME``, deleted when this process stops.
Stop it with Ctrl-C.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_PORT = 8962
PROJECT_ID = "sandbox-status"
PROJECT_NAME = "Sandbox: one status vocabulary"
USER = "Jeff Posey"


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def dispatch_config(home: Path, runner_script: Path) -> None:
    """A machine with a queue and finishes switched on, whose controller starts nothing."""
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
        "limits": {"max_concurrent_runs": 4, "dispatch_queue_limit": 10},
        "execution": {"controller": "shadow"},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def seed(manager: Any) -> Dict[str, int]:
    """One task per state in the design table. Returns the authorising entries to queue."""
    from agentjobs.models_v2 import (
        AUTH_RECOVERY_MARKER,
        Ball,
        BallReason,
        Lifecycle,
        LogEntryType,
        Outcome,
    )

    def task(task_id: str, title: str, *, lifecycle: Any = Lifecycle.READY, **extra: Any) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"Seeded to read: {title.split(' — ')[0]}.",
            description="Seeded by scripts/status_vocabulary_sandbox.py. Nothing here is real work.",
            lifecycle=lifecycle,
            actor="claude",
            category="ux",
            **extra,
        )

    authorised: Dict[str, int] = {}

    task("task-001", "Ready — green, can start")
    for task_id, title in (
        ("task-002", "Queued — brown, a dispatch is waiting for a slot"),
        ("task-003", "Starting — brown, the queue is putting it through the gates"),
    ):
        task(task_id, title)
        record = manager.add_log_entry(
            task_id, actor=USER, type=LogEntryType.NOTE, body="Go ahead and work this."
        )
        authorised[task_id] = record.log[-1].id

    task("task-004", "Working — blue, an agent is on it")
    manager.claim_task("task-004", agent="claude")

    task("task-005", "Landing — purple, a scripted finish is merging it")
    manager.claim_task("task-005", agent="claude")

    for number, reason in enumerate(("spec", "review", "decision", "approval", "input"), start=6):
        task_id = f"task-{number:03d}"
        task(task_id, f"Needs {reason} — red, the ball is with you")
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason(reason),
            ball_prompt=f"Seeded: this one needs your {reason}.",
        )

    task("task-011", "Error — red, half of a needs cycle")
    task("task-012", "Error — red, the other half of the cycle")
    manager.update_task(
        "task-011", dependencies=[{"task": "task-012", "type": "needs"}], actor="claude"
    )
    manager.update_task(
        "task-012", dependencies=[{"task": "task-011", "type": "needs"}], actor="claude"
    )

    task("task-013", "Blocked — pink, needs task-001 to close first")
    manager.update_task(
        "task-013", dependencies=[{"task": "task-001", "type": "needs"}], actor="claude"
    )

    task("task-014", "On hold — pink, a person stopped it")
    manager.claim_task("task-014", agent="claude")
    manager.handoff(
        "task-014",
        actor=USER,
        ball=Ball.AGENT,
        ball_reason=BallReason.HOLD,
        ball_prompt="Held until the design question is answered.",
    )

    task("task-015", "Ready — an epic nobody holds, whose children are open")
    task("task-016", "Ready — a child of task-015", parent="task-015")

    task("task-017", "Quota — pink, parked on a usage limit that clears by itself")
    manager.claim_task("task-017", agent="claude")
    resets = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
    manager.handoff(
        "task-017",
        actor="dispatcher",
        ball=Ball.EXTERNAL,
        ball_reason=BallReason.SERVICE,
        ball_prompt=f"The usage limit resets at {resets.isoformat()}. Nothing to do.",
        data={
            AUTH_RECOVERY_MARKER: {
                "incident": "inc_sandbox",
                "run_id": "run_sandbox",
                "action": "park",
                "kind": "usage_limit",
                "resets_at": resets.isoformat(),
            }
        },
    )

    task("task-018", "Blocked — pink, parked on a third party")
    manager.claim_task("task-018", agent="claude")
    manager.handoff(
        "task-018",
        actor="claude",
        ball=Ball.EXTERNAL,
        ball_reason=BallReason.SERVICE,
        ball_prompt="The vendor's API has answered 503 since this morning.",
    )

    # Drafts are born human/spec and read "Needs spec" (task-006's state). "Draft" is a
    # draft handed somewhere other than a person, here to an agent to write the spec.
    task("task-019", "Draft — yellow, handed to an agent to specify", lifecycle=Lifecycle.DRAFT)
    manager.handoff(
        "task-019",
        actor=USER,
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt="Draft the spec for this one.",
    )

    for number, outcome in enumerate(Outcome, start=20):
        task_id = f"task-{number:03d}"
        word = outcome.value.capitalize()
        look = "solid" if outcome is Outcome.COMPLETED else "hollow"
        task(task_id, f"{word} — grey, {look}")
        manager.close_task(
            task_id, actor="claude", outcome=outcome, archive=outcome is Outcome.DUPLICATE
        )

    # Three epics for the walk rail, each with a child so the counts read sensibly.
    # A walk runs on a claimed parent, so a walked epic reads Working; a grounded walk
    # hands its parent to a person, so that one reads Needs decision.
    for number, state in ((30, "walking"), (32, "waiting"), (34, "grounded")):
        epic = f"task-{number:03d}"
        task(epic, f"Epic being walked — {state}")
        task(f"task-{number + 1:03d}", f"A child of the {state} epic", parent=epic)
        manager.claim_task(epic, agent="claude")
        if state == "grounded":
            manager.handoff(
                epic,
                actor="dispatcher",
                ball=Ball.HUMAN,
                ball_reason=BallReason.DECISION,
                ball_prompt="The walk grounded: a child is parked on a person.",
            )
    return authorised


def queue(home: Path, manager: Any, authorised: Dict[str, int]) -> None:
    """Two real queue entries, then the second put into ``starting`` as a tick would."""
    from agentjobs.dispatch import queue as dispatch_queue
    from agentjobs.dispatch.guards import DispatchRequest
    from agentjobs.dispatch.journal import journal
    from agentjobs.projects import ProjectRegistry

    project = ProjectRegistry(home).get(PROJECT_ID)
    for task_id in ("task-002", "task-003"):
        dispatch_queue.enqueue(
            home,
            project,
            DispatchRequest(task_id=task_id, caused_by=authorised[task_id]),
            manager=manager,
            queued_by=USER,
            api_base="http://127.0.0.1:9",
        )
    with journal(home).transaction("sandbox-starting") as connection:
        connection.execute(
            "UPDATE dispatch_queue SET status = 'starting' WHERE task_id = ?", ("task-003",)
        )


def finish(home: Path) -> None:
    """A live finish on task-005, in the shapes ``dispatch.finish`` leaves on disk."""
    from agentjobs.dispatch.ledger import locks_root

    directory = home / "finishes" / "fin_sandbox01"
    directory.mkdir(parents=True)
    meta = {
        "finish_id": "fin_sandbox01",
        "task_id": "task-005",
        "project_id": PROJECT_ID,
        "outcome": "running",
        "started_at": _ago(3),
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    lines: list[dict[str, Any]] = [
        {"kind": "finish_preflight", "branch": "feat/task-005-sandbox", "worktree": str(home)},
        {"kind": "finish_step", "step": "preflight", "ok": True, "seconds": 1.2},
        {"kind": "finish_step", "step": "rebase", "ok": True, "seconds": 2.9},
        {"kind": "gate_stage_started", "stage": "pytest", "stages_run": 6, "stages_total": 10},
    ]
    with (directory / "phases.jsonl").open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps({"ts": _ago(1), **line}) + "\n")
    locks = locks_root(home)
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{PROJECT_ID}~task-005.lock").write_text(
        f"pid={os.getpid()} run=run_sandbox kind=finish finish=fin_sandbox01 started={_ago(3)}",
        encoding="utf-8",
    )


RUN_HEALTH = {
    "run_working": ("task-004", "working"),
    "run_starting": ("task-001", "starting"),
    "run_parked": ("task-007", "parked"),
    "run_finishing": ("task-005", "finishing"),
    "run_handback": ("task-004", "handback"),
    # A process word rather than a task status, which keeps a colour of its own.
    "run_silent": ("task-014", "silent"),
}


def runs(home: Path) -> None:
    """Run records for the Runs tab: every health word that names a task status, and No output."""
    for run_id, (task_id, _health) in RUN_HEALTH.items():
        directory = home / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "task_id": task_id,
            "project_id": PROJECT_ID,
            "mode": "batch",
            "posture": "auto",
            "status": "running",
            "pid": os.getpid(),
            "started_at": _ago(4),
        }
        (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")


def patch_the_drawn_parts() -> None:
    """Serve three walks and the run-health words -- see the module docstring."""
    from agentjobs.api.routes import runs as runs_route

    original_run_view = runs_route._run_view

    def run_view(record: Any, *args: Any, **kwargs: Any) -> Any:
        view = original_run_view(record, *args, **kwargs)
        drawn = RUN_HEALTH.get(record.run_id)
        return view.model_copy(update={"health": drawn[1]}) if drawn else view

    def walk(parent: str, *, grounded: bool, resumes: bool) -> Any:
        child = f"task-{int(parent[-3:]) + 1:03d}"
        return runs_route.EpicWalkView(
            walk_id=f"walk_{parent}",
            project_id=PROJECT_ID,
            project_name=PROJECT_NAME,
            parent_task_id=parent,
            parent_task_title=f"Epic being walked — {parent}",
            parent_task_url=f"/p/{PROJECT_ID}/tasks/{parent}",
            started_at=_ago(40),
            children_total=1,
            children_completed=0,
            children_in_flight=0 if grounded else 1,
            children_remaining=1 if grounded else 0,
            grounded=grounded,
            grounded_reason="child_parked" if grounded else "",
            grounded_word="a child is parked on a person" if grounded else "",
            waiting_on_task_id=child if resumes else "",
            waiting_on_task_title=f"A child of {parent}" if resumes else "",
            waiting_on_task_url=f"/p/{PROJECT_ID}/tasks/{child}" if resumes else "",
            resumes_by_itself=resumes,
        )

    def walk_views(*_args: Any, **_kwargs: Any) -> Any:
        return [
            walk("task-030", grounded=False, resumes=False),
            walk("task-032", grounded=True, resumes=True),
            walk("task-034", grounded=True, resumes=False),
        ]

    runs_route._run_view = run_view
    runs_route._walk_views = walk_views


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-status-vocabulary-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)
    sys.path.insert(0, str(Path(__file__).parent))

    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.projects import ProjectRegistry
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=PROJECT_NAME, user=USER), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))
    authorised = seed(manager)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=PROJECT_NAME)

    runner_script = root / "never_runs.py"
    runner_script.write_text("print('this sandbox never starts anything')\n", encoding="utf-8")
    dispatch_config(home, runner_script)
    queue(home, manager, authorised)
    finish(home)
    runs(home)
    patch_the_drawn_parts()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[status] status vocabulary sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[status]   every state     {base}/tasks?status=all", flush=True)
    print(f"[status]   the dashboard   {base}", flush=True)
    print(f"[status]   the Runs tab    {base}/runs", flush=True)
    print(f"[status]   a task page     {base}/tasks/task-017", flush=True)
    print(f"[status]   Landing         {base}/tasks/task-005", flush=True)
    # Lifespan off, as the queued-status sandbox does: the poller would drain the queue
    # and reap the seeded runs and finish, and those sitting where they were put is the
    # exhibit. Everything a browser touches is served by the real application.
    try:
        serve(app, port=port, lifespan="off")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
