"""Stand up a machine paused on a usage limit, on its own port, with throwaway data (task-463).

Once slots stay full, the thing that decides how much work a machine does is the
subscription. A run that hits the limit while it is running has been recovered since
task-417; what this fixture is about is the two starters with nobody watching them -- the
dispatch queue and the pull mode -- **not** taking off into a closed sky, and taking off
again by themselves when it reopens.

The state worth looking at is the machine declining to do something, which is precisely
the state nobody can construct by hand and nobody can screenshot from a test. So this
seeds an open incident against this machine's own credential and hands over a URL.

    python scripts/usage_pause_sandbox.py [port] [--resume-after N] [--other-credential]

Three halves, and the comparison is the whole fixture:

    default              an open `usage_limit` incident against the credential the
                         sandbox runner would spend, with a reset 40 minutes out. One
                         run is parked on it -- the one that hit the limit -- and two
                         slots stand free while a queued dispatch and an armed project
                         both sit. The board draws
                         "Paused until <your local time>: usage limit on
                         sandbox-sleeper" over both rails, the queued row reads
                         `paused`, and the armed row says its bound is untouched.

    --resume-after 60    the same machine, with the incident closed 60 seconds in --
                         which is what task-417's probe does after a real reset. Watch
                         the notice go and both the queued entry and the pull mode start
                         on the next tick, with nobody clicking. **This is the half that
                         shows the feature rather than the refusal.**

    --other-credential   an incident of the same kind and the same age against a *Codex*
                         credential. Nothing is paused; the queue and the pull mode
                         start normally. It is the fixture for "a limit on one
                         subscription does not ground a runner on another", which is
                         otherwise indistinguishable from the gate being broken.

Run them together to compare:

    python scripts/usage_pause_sandbox.py 8916
    python scripts/usage_pause_sandbox.py 8917 --resume-after 60
    python scripts/usage_pause_sandbox.py 8918 --other-credential

**What to try, in the order that makes sense.**

    1. Open the Dashboard on the default half. Two rails, both with the same notice
       above them, and no run starting however long you wait. The machine is not stuck:
       it is declining, and saying so.
    2. Read the reset time. It is *your* clock, not UTC -- the server sends UTC and the
       browser renders it, because the reset is a wall-clock fact to whoever is waiting.
    3. Press Dispatch on any task. It starts. A person who is there to read the park is
       not the thing this gate is for, and the asymmetry is deliberate.
    4. Open the `--resume-after` half and leave it a minute. The notice goes and runs
       start. Nothing was clicked, and no second probe exists -- the gate simply reads
       the incident book every tick, so a closed incident is a tick that finds nothing.
    5. Open the `--other-credential` half. Same incident, different subscription, and
       the machine carries on.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_PORT = 8916
CEILING = 3

PROJECT_ID = "sandbox-quota"
ARMER = "Jeff Posey"
RUNNER = "sandbox-sleeper"
INCIDENT_ID = "inc_sandbox_usage"

TASKS: List[Tuple[str, str]] = [
    ("task-301", "Teach the importer to skip its own writes"),
    ("task-302", "Collapse the two dependency banners into one"),
    ("task-303", "Stop the exporter rounding the last column"),
    ("task-304", "Give the date parser one home"),
    ("task-305", "Retire the second copy of the slug helper"),
    ("task-306", "Make the CSV reader admit what it skipped"),
    ("task-307", "Give the retry loop a ceiling"),
    ("task-308", "Stop the log writer opening the file twice"),
]
"""Enough that the pull mode still has somewhere to go after the resume.

The pull-mode sandbox learned this the hard way: a fixture is opened whenever the person
gets to it, not when it was started, and one that has already spent its bound is a
fixture that has finished being interesting.
"""

PARKED = "task-308"
"""The run that hit the limit and opened the incident, which is how a real one begins.

It is last in the backlog so it is not also the thing the queue or the pull mode would
have started next -- the fixture should show the *pause*, not a task that is busy.
"""

QUEUED = "task-301"
"""The one a person queued by name. It is first in the backlog too, so after the resume
the queue starts it and the pull mode -- which yields the whole tick to that queue --
takes the next one."""


def build_project(root: Path) -> Path:
    """One throwaway project whose backlog is a real, ordered queue."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle, LogEntryType
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="Usage Pause Sandbox", user=ARMER), sort_keys=False
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))

    for task_id, title in TASKS:
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"{title}. Seeded so there is something real to start, or not start.",
            description=(
                "Seeded by scripts/usage_pause_sandbox.py. Nothing here is real work; it "
                "has a working description so that a start which does not happen is "
                "explained by the pause and by nothing else."
            ),
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.add_log_entry(
            task_id,
            actor=ARMER,
            type=LogEntryType.NOTE,
            body="Seeded so this task can also be dispatched by hand, which is the half "
            "the pause deliberately does not touch.",
        )
    return project_root


def dispatch_config(port: int) -> Dict[str, Any]:
    """A machine with one runner that sleeps, and the project switched on."""
    return {
        "version": 1,
        "enabled": True,
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            RUNNER: {
                "argv": [sys.executable, "-c", "import time; time.sleep(600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": CEILING, "dispatch_queue_limit": 20},
        "projects": {
            PROJECT_ID: {
                "enabled": True,
                "require_clean_tree": False,
                "runner": RUNNER,
            },
        },
    }


def seed_incident(home: Path, run_id: str, *, other_credential: bool) -> datetime:
    """One open ``usage_limit`` incident, in task-417's own table.

    Written as a row rather than by driving a real refusal, for the reason the fixture
    exists at all: a genuine usage limit cannot be asked for on demand, and the shape of
    the row is exactly what a real one leaves behind -- ``inc_7ffcc0210a984e39`` on this
    machine on 2026-09-18, opened 21:58Z with a 23:40Z reset.

    The **profile** is the load-bearing part. The pause is judged per credential, so an
    incident carrying a credential this machine would not spend pauses nothing, which is
    what ``--other-credential`` seeds and why that half is worth having.

    **A waiter is seeded with it**, and that is not decoration. task-417 closes an
    incident with no remaining waiter and probes nothing further, which is correct --
    an incident exists to get parked runs moving again, and one with nothing parked on it
    is over. The first version of this fixture wrote the incident alone and watched the
    server's own recovery tick close it within ten seconds, which looked exactly like the
    gate not working. A real usage limit is always opened *by* a parked run, so the
    fixture seeds one.

    And the waiter has to name a **real, live run**, which is why this is called after a
    real dispatch rather than before the server starts. task-417 reconciles every active
    waiter against the world each tick and removes one whose run record is gone or has
    ended, which closes the incident by the same rule -- and the app's startup reconcile
    settles a hand-written record before any of that. Two earlier versions of this
    fixture were closed by one or the other within ten seconds, each looking exactly like
    the gate not working.

    The parked run also holds a slot, which is the right picture rather than a detail: a
    machine with a free slot and a reason not to fill it is what the board is for.
    """
    from agentjobs.dispatch import start_pause
    from agentjobs.dispatch.auth_recovery import Profile
    from agentjobs.dispatch.config import load_dispatch_config
    from agentjobs.dispatch.journal import journal

    if other_credential:
        profile = Profile(
            driver="codex", executable=(), model=None, claude_home="/a/different/login"
        )
    else:
        config = load_dispatch_config(home)
        assert config is not None
        profile = start_pause.profile_for_runner(config.runners[RUNNER])

    opened = datetime.now(timezone.utc)
    resets_at = opened + timedelta(minutes=40)

    with journal(home).transaction("sandbox-incident") as connection:
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, "
            "state, opened_at, next_probe_at, resets_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?,?)",
            (
                INCIDENT_ID,
                "usage_limit",
                profile.key,
                json.dumps(profile.as_json()),
                opened.isoformat(),
                resets_at.isoformat(),
                resets_at.isoformat(),
                opened.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO auth_waiter(incident_id, run_id, project_id, task_id, "
            "session_id, stall_at, status, detail_json, joined_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'waiting',?,?,?)",
            (
                INCIDENT_ID,
                run_id,
                PROJECT_ID,
                PARKED,
                "sandbox-session",
                opened.isoformat(),
                json.dumps(
                    {
                        "message": "You've hit your session limit, resets 6:40pm "
                        "(America/Chicago)",
                        "resets_at": resets_at.isoformat(),
                    }
                ),
                opened.isoformat(),
                opened.isoformat(),
            ),
        )
    return resets_at


def main() -> None:
    argv = sys.argv[1:]
    other_credential = "--other-credential" in argv
    resume_after = 0
    if "--resume-after" in argv:
        index = argv.index("--resume-after")
        resume_after = int(argv[index + 1])
        del argv[index : index + 2]
    argv = [item for item in argv if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-usage-pause-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    project_root = build_project(root)
    registry.add(project_root, project_id=PROJECT_ID, name="Usage Pause Sandbox")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(port), sort_keys=False), encoding="utf-8"
    )
    # Named before the server starts only so the banner can print it; the row itself is
    # written after the parked run exists.
    resets_at = datetime.now(timezone.utc) + timedelta(minutes=40)

    def seed_the_waiting_work() -> None:
        """Park one run, queue one dispatch, arm the project -- after the server is up.

        Deferred as a whole, and the order inside it matters. The parked run has to be a
        *real* dispatch before the incident names it as a waiter, or the server's own
        reconcile settles the record and task-417 closes the incident for having nothing
        parked on it. And everything is deferred so that the first tick which sees any of
        it is a tick of the **running server**: nothing in this process starts, or
        declines to start, anything.
        """
        from agentjobs.dispatch import pull as dispatch_pull
        from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
        from agentjobs.dispatch.queue import enqueue
        from agentjobs.execution.store import BOUND_OPEN
        from agentjobs.manager import TaskManager
        from agentjobs.models_v2 import LogEntryType
        from sandbox_store import sandbox_store

        project = ProjectRegistry(home).get(PROJECT_ID)
        manager = TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID))

        parked = manager.get_task(PARKED)
        assert parked is not None
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(
                task_id=PARKED,
                caused_by=[e for e in parked.log if e.type is LogEntryType.NOTE][-1].id,
            ),
            home=home,
            api_base=f"http://127.0.0.1:{port}",
        )
        seed_incident(home, handle.run_id, other_credential=other_credential)
        print(
            f"[quota] {PARKED} is running as {handle.run_id} and is the run that hit the "
            "limit; the incident names it as its waiter.",
            flush=True,
        )

        # ``enqueue`` rather than ``dispatch_or_queue``, because the entry has to *be*
        # in the queue for the rail to be the subject. ``dispatch_or_queue`` queues only
        # on a full machine, and a fixture whose machine was full would hide the thing
        # worth seeing: a free slot the pause is declining to fill.
        task = manager.get_task(QUEUED)
        assert task is not None
        authorising = [entry for entry in task.log if entry.type is LogEntryType.NOTE][-1]
        enqueue(
            home,
            project,
            DispatchRequest(task_id=QUEUED, caused_by=authorising.id),
            manager=manager,
            queued_by=ARMER,
            api_base=f"http://127.0.0.1:{port}",
        )
        dispatch_pull.arm(
            home,
            project,
            project.load_config(),
            armed_by=ARMER,
            bound_kind=BOUND_OPEN,
        )
        print(
            f"[quota] {QUEUED} queued by {ARMER}, and the project armed until disarmed. "
            + (
                "The incident is on another credential, so both start normally."
                if other_credential
                else "Neither will start while the incident is open."
            ),
            flush=True,
        )

    def clear_the_incident() -> None:
        """Close it the way task-417's probe closes one after a real reset."""
        from agentjobs.dispatch.auth_recovery import book_for

        book_for(home).close(
            INCIDENT_ID,
            state="closed",
            reason="the sandbox's reset arrived",
            now=datetime.now(timezone.utc),
        )
        print(
            "[quota] the incident is closed. Nothing else was touched -- the next tick "
            "will simply not find it, and both starters take off.",
            flush=True,
        )

    threading.Timer(2.0, seed_the_waiting_work).start()
    if resume_after:
        threading.Timer(2.0 + resume_after, clear_the_incident).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    local = resets_at.astimezone().strftime("%I:%M %p").lstrip("0")
    if other_credential:
        shape = "an incident on a *different* credential -- nothing is paused"
    elif resume_after:
        shape = f"paused, resuming in {resume_after}s"
    else:
        shape = f"paused until {local}, which is what the board should say"

    print(f"[quota] usage-limit pause sandbox ({shape})", flush=True)
    print(f"[quota]   Dashboard  http://127.0.0.1:{port}/app/p/{PROJECT_ID}", flush=True)
    print(f"[quota]   Runs tab   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/runs", flush=True)
    print(f"[quota]   Dispatch   http://127.0.0.1:{port}/app/p/{PROJECT_ID}/dispatch", flush=True)
    print(f"[quota]   Live JSON  http://127.0.0.1:{port}/api/runs/live", flush=True)
    if not other_credential and not resume_after:
        print(
            "[quota]   Watch it resume instead: python scripts/usage_pause_sandbox.py "
            f"{port + 1} --resume-after 60",
            flush=True,
        )
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
