"""Stand up the Dashboard's slot board on its own port, with throwaway data.

task-092 replaces the Dashboard's next-up panel with a board of exactly
``max_concurrent_runs`` cells: each one holds either the run occupying that slot or the
next queued task with a Dispatch button. The states worth comparing are states nobody
can construct by hand on a real machine -- you cannot make three agents run to order,
and you certainly cannot make one of them belong to a project you are not looking at --
so this seeds each of them and hands over a URL.

    python scripts/slot_board_sandbox.py [port] [--idle] [--finishing] [--alarm]
                                         [--ceiling N] [--unconfigured]

Six things to look at, and the comparison is the point:

    default            two runs in two different projects and one free cell. The
                       foreign run keeps its own project name and links into *that*
                       project; a finish in another project is a card of its own,
                       beyond the three slots, because it holds no run slot (task-352).
    --idle             nothing running: three free cells offering three *different*
                       tasks, each with its own Dispatch button. This is the state the
                       board exists for.
    --attended         no dispatched runs, one chat session working task-101 (task-354).
                       Compare with ``--idle``: the same three free cells and the same
                       three Dispatch buttons, plus a card for the session -- because it
                       holds its task, not a slot. Open task-101 to see the other half:
                       the task page names the session and withholds the Dispatch button
                       instead of offering to start a second agent on it.
    --finishing        no runs, two finishes: one in the gate for a task in this
                       project, one queued behind it on the merge runway. This is the
                       state task-352 was filed from -- the badge read 0 and the Runs tab
                       said nothing was running -- so it is the one to compare with
                       ``--idle``: same three free cells, plus the two finish cards.
    --alarm            a task stopped on a human. The alert keeps the top of the page
                       and the board keeps its shape below it -- with every Dispatch
                       button withheld, which is task-081's rule stated for a grid.
    --ceiling N        any cell count. `1` is the board as a single panel; `9` is past
                       the drawing limit, where the board draws six and says what it is
                       not drawing rather than growing a scroll.
    --unconfigured     no dispatch.yaml at all, so the ceiling is a default nobody
                       chose. The board refuses to draw cells from a number like that
                       and shows the queue instead, under the sentence saying why.

Run two of them on two ports to see both halves at once:

    python scripts/slot_board_sandbox.py 8906
    python scripts/slot_board_sandbox.py 8907 --idle

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

DEFAULT_PORT = 8906
DEFAULT_CEILING = 3


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def build_project(
    root: Path,
    *,
    project_id: str,
    name: str,
    tasks: List[Tuple[str, str]],
    alarm: bool,
    claimed: Tuple[str, ...] = (),
) -> Path:
    """One throwaway project with a queue in it, and optionally something alarming.

    ``claimed`` is the tasks a seeded run is pretending to work. They are claimed for
    real rather than left `ready`, because a dispatch claims its task and a fixture that
    skipped that step would put the same task in two cells -- a state the board handles
    (see ``boardLayout``) but which is not what a person is here to look at.
    """
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle
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
            summary=f"{title}. Seeded so a free cell has something real to offer.",
            description=(
                "Seeded by scripts/slot_board_sandbox.py. Nothing here is real work; the "
                "record exists so a cell has a task to show and a button to press. It has "
                "a working description because a task that cannot brief an agent gets a "
                "link to its own page instead of a Dispatch button, which is a different "
                "state and not the one this fixture is for."
            ),
            lifecycle=Lifecycle.READY,
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
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(180), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def seed_lock(home: Path, name: str, text: str) -> None:
    """One lock file, in the words ``LockHolder.parse`` reads."""
    directory = home / "runs" / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.lock").write_text(text, encoding="utf-8")


def seed_finish(
    home: Path,
    *,
    finish_id: str,
    task_id: str,
    project_id: str,
    done_steps: Tuple[str, ...],
    started_seconds_ago: int,
) -> None:
    """One finish record, far enough along that ``read_finish_status`` names its step.

    The lock alone makes a finish appear; the record is what says *where* it is. A finish
    infers the step in flight from the last one it recorded, so seeding preflight,
    runway and rebase as done puts it in the gate, and seeding preflight alone leaves it
    queued for the runway -- the two states worth looking at side by side.
    """
    import json

    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    started = _ago(started_seconds_ago)
    (directory / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "finish_id": finish_id,
                "task_id": task_id,
                "project_id": project_id,
                "outcome": "running",
                "started_at": started,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        json.dumps(
            {
                "ts": started,
                "kind": "finish_step",
                "finish_id": finish_id,
                "step": step,
                "ok": True,
                "skipped": False,
                "detail": "seeded",
                "seconds": 1.0,
            }
        )
        for step in done_steps
    ]
    (directory / "phases.jsonl").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


HERE_TASKS = [
    ("task-101", "Teach the queue to explain itself"),
    ("task-102", "Collapse the two dependency banners into one"),
    ("task-103", "Stop the importer double-counting reruns"),
    ("task-104", "Give the exporter a dry run"),
    ("task-105", "Retire the third copy of the date parser"),
    ("task-106", "Name the two things both called `state`"),
    ("task-107", "Make the seed script idempotent"),
    ("task-108", "Drop the unused compatibility shim"),
]
ELSEWHERE_TASKS = [
    ("task-501", "Rewrite the ingest pipeline"),
    ("task-502", "Retire the legacy export path"),
]


def dispatch_config(ceiling: int) -> Dict[str, Any]:
    """A machine that will admit to one runner, with both projects switched on.

    The runner sleeps rather than exiting, so pressing a real Dispatch button seeds a
    real run into a real free cell -- which is the one thing about this board that a
    fixture cannot fake convincingly. ``actor`` is required: a runner writes as an
    identity the project configures, and a dispatch whose runner would act as
    ``sandbox-sleeper`` is refused before any run exists (task-159).
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
        # `runner` is not optional here: a project enabled for dispatch that names
        # neither a runner nor a group cannot dispatch, and the board would then show
        # its closed-gate line instead of the Dispatch buttons this fixture is for.
        "projects": {
            "sandbox-here": {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
            "sandbox-elsewhere": {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
        },
    }


def main() -> None:
    argv = sys.argv[1:]
    idle = "--idle" in argv
    finishing = "--finishing" in argv
    attended = "--attended" in argv
    alarm = "--alarm" in argv
    unconfigured = "--unconfigured" in argv
    ceiling = DEFAULT_CEILING
    if "--ceiling" in argv:
        index = argv.index("--ceiling")
        ceiling = int(argv[index + 1])
        del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-slot-board-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.dispatch.ledger import runway_lock_name
    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    # A finish's task is claimed too: a task at the merge gate is `active`, not `ready`,
    # and a free cell offering the task whose merge is on the card beside it would be
    # the duplicate the board exists to avoid.
    running_here: Tuple[str, ...] = (
        ("task-101",) if attended else ("task-102",) if finishing else () if idle else ("task-101",)
    )
    running_elsewhere: Tuple[str, ...] = (
        () if attended else ("task-501",) if finishing else () if idle else ("task-501",)
    )
    here = build_project(
        root,
        project_id="sandbox-here",
        name="Here",
        tasks=HERE_TASKS,
        alarm=alarm,
        claimed=running_here,
    )
    elsewhere = build_project(
        root,
        project_id="sandbox-elsewhere",
        name="Elsewhere",
        tasks=ELSEWHERE_TASKS,
        alarm=False,
        claimed=running_elsewhere,
    )
    registry.add(here, project_id="sandbox-here", name="Here")
    registry.add(elsewhere, project_id="sandbox-elsewhere", name="Elsewhere")
    if not unconfigured:
        (home / "dispatch.yaml").write_text(
            yaml.safe_dump(dispatch_config(ceiling), sort_keys=False), encoding="utf-8"
        )

    def seed_attended() -> None:
        """The state task-354 was filed from: a task being worked in a chat window.

        Seeded as the claim itself writes it -- ``mode: interactive``, the session's own
        id and directory, no posture and no pid -- so the board, the badge, the Runs tab
        and the task page all read exactly what they would in the real thing. The point
        of comparing this with ``--idle`` is that the three free cells are the same:
        a session holds its task, not a slot.
        """
        seed_run(
            home,
            run_id="run_chat01",
            task_id="task-101",
            project_id="sandbox-here",
            mode="interactive",
            driver="claude",
            agent="claude",
            origin="claimed",
            status="running",
            session_id="0feedf93-6af3-40bf-832a-81f722fdb841",
            cwd=str(here),
            started_at=_ago(900),
        )
        print("[board] seeded a chat session working task-101", flush=True)

    def seed_finishing() -> None:
        """The state task-352 was filed from: no runs, one finish in the gate, one queued.

        Both finishes are real enough for the server to read their step: the first has
        preflight, runway and rebase behind it and is therefore in the gate, holding
        the repository's runway; the second has only its preflight done and is waiting
        for that runway. That is exactly the pair the live dashboard showed on
        2026-09-06 -- task-092 gating, task-341 queued -- as "Nothing is running".
        """
        pid = os.getpid()
        seed_finish(
            home,
            finish_id="fin_gating",
            task_id="task-102",
            project_id="sandbox-here",
            done_steps=("preflight", "runway", "rebase"),
            started_seconds_ago=250,
        )
        seed_lock(
            home,
            "task-102",
            f"pid={pid} run= kind=finish finish=fin_gating started={_ago(250)}",
        )
        seed_lock(
            home,
            runway_lock_name(here),
            f"pid={pid} run= kind=runway finish=fin_gating started={_ago(248)}",
        )
        seed_finish(
            home,
            finish_id="fin_queued",
            task_id="task-501",
            project_id="sandbox-elsewhere",
            done_steps=("preflight",),
            started_seconds_ago=110,
        )
        seed_lock(
            home,
            "task-501",
            f"pid={pid} run= kind=finish finish=fin_queued started={_ago(110)}",
        )
        print("[board] seeded a finish in the gate and a finish queued for the runway", flush=True)

    def seed_activity() -> None:
        """Write the fake runs and locks, a moment after the server is up.

        Deferred deliberately, exactly as ``live_runs_sandbox`` defers its own: the
        server reconciles the ledger when it boots and correctly concludes any session
        run it has never heard of, so a run seeded before that is `interrupted` before
        the first page loads.
        """
        seed_run(
            home,
            run_id="run_here01",
            task_id="task-101",
            project_id="sandbox-here",
            mode="session",
            posture="auto",
            status="running",
            session_id="here01",
        )
        # The second run is in the *other* project on purpose. Slots are the machine's,
        # so an occupied cell routinely holds work you are not looking at -- and the
        # cell has to name that project and link into it rather than into this one.
        seed_run(
            home,
            run_id="run_elsewhere01",
            task_id="task-501",
            project_id="sandbox-elsewhere",
            mode="session",
            posture="autonomous",
            status="parked",
            session_id="elsewhere01",
            started_at=_ago(2_700),
        )
        seed_lock(
            home,
            "task-502",
            f"pid={os.getpid()} run= kind=finish finish=fin_sandbox started={_ago(45)}",
        )
        seed_lock(
            home,
            runway_lock_name(elsewhere),
            f"pid={os.getpid()} run= kind=runway finish=fin_sandbox started={_ago(45)}",
        )
        print("[board] seeded two live runs, a finish and a runway", flush=True)

    if attended:
        threading.Timer(2.0, seed_attended).start()
    elif finishing:
        threading.Timer(2.0, seed_finishing).start()
    elif not idle:
        threading.Timer(2.0, seed_activity).start()

    import uvicorn

    from agentjobs.api.main import app

    shape = "idle -- every cell free" if idle else "two of the cells busy"
    if finishing:
        shape = "no runs -- one finish in the gate, one queued for the runway"
    if attended:
        shape = "no dispatched runs -- one chat session working a task"
    if unconfigured:
        shape = "no dispatch config -- a queue rather than a board"
    if alarm:
        shape += ", with an alert holding the top of the page"
    print(f"[board] slot board ({shape}), ceiling {ceiling}", flush=True)
    print(f"[board]   Dashboard  http://127.0.0.1:{port}/app/p/sandbox-here", flush=True)
    print(f"[board]   Runs tab   http://127.0.0.1:{port}/app/p/sandbox-here/runs", flush=True)
    print(f"[board]   Elsewhere  http://127.0.0.1:{port}/app/p/sandbox-elsewhere", flush=True)
    if not idle:
        print(
            "[board]   Compare with the idle half: python scripts/slot_board_sandbox.py "
            f"{port + 1} --idle",
            flush=True,
        )
    if not finishing:
        print(
            "[board]   And with a merge in progress: python scripts/slot_board_sandbox.py "
            f"{port + 2} --finishing",
            flush=True,
        )
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
