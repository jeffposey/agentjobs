"""Stand up a task the machine has promised to start, beside one it has not (task-476).

Since task-459 a dispatch can be authorised and then wait for a free slot. The slot board
showed the waiting entry; the task did not -- its own record read ``Ready``, so the page a
person actually opens said nothing was happening to it. The tests assert the exact labels
and the id the cancel button sends. What they cannot answer is whether the distinction
*reads*: whether a glance at a list separates the task nothing is going to happen to from
the one an agent starts on the moment a slot frees.

**All four states are seeded, because comparing them is the review:**

    nothing queued      ``task-001``. Reads **Ready**, and its page offers Dispatch. This
                        is the control: it is what every one of the others used to look
                        like.

    next in line        ``task-002``. Reads **Queued**. A slot frees, this one starts.
                        Its page offers to cancel the waiting entry instead of offering
                        to start a second agent on the same repository.

    third in line       ``task-003``. Also reads **Queued**. The place in line is on
                        ``queued_dispatch.position`` and in the dispatch panel's prose,
                        not in the chip.

    nothing being tried ``task-004``. Also reads **Queued**. An open usage-limit incident
                        is holding every start on that credential off, so the entry keeps
                        its place and is not being attempted at all -- which its page says
                        in a sentence, naming the incident.

Everything here is throwaway. Click anything, including the destructive controls --
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives under
a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME and its own dispatch configuration.

    python scripts/queued_status_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **The task list, glanced at rather than studied.** Four ready rows, three of which the
    machine has already promised to start. If the eye cannot separate them, the wording is
    not doing its job and that is worth saying.
  * **Whether one word is enough.** All three queued rows read the same, and what
    separates them -- second in line, not being tried at all -- is on each task's own
    page rather than in the chip. If a glance at the list now needs a click it did not
    need before, that is worth saying.
  * **task-002's page**, where Dispatch used to be. The panel now names the entry and
    offers one button: *Cancel the queued dispatch*. Press it -- the row goes back to
    **Ready** in front of you, and the task's log gains a note saying nothing ran.
  * **task-004's page**, which says which incident is holding it and that it keeps its
    place. Whether that reads as reassurance or as jargon is a judgement.
  * **The dashboard**, which draws the same badge on its own lists.
  * **At 390x844.** "Queued" is a word longer than "Ready"; check the rows still sit on
    the same number of lines on a phone.

Nothing drives the queue here: the server is started with its lifespan off, so the
dispatch poller -- which would correctly drain the queue the moment a slot freed -- never
runs, and the three entries sit where they were put for as long as the server does. That
is the only piece of the real machinery this sandbox leaves out.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 8912

PROJECT_ID = "sandbox-queued"
PROJECT_NAME = "Sandbox: what a queued dispatch says it is"

ORDINARY = [
    "Rework the queue's band arithmetic",
    "Attachment thumbnails are served at full size",
]

#: A credential all three ordinary entries share, and one only the paused entry uses.
#: `Profile.credential_key` reads the driver, the Claude home and the *names* of the auth
#: environment variables set -- so a runner carrying one of those names is a different
#: subscription as far as the start gate is concerned, which is what lets this sandbox
#: show a paused entry beside two that are not.
PAUSED_RUNNER = "fake-other-credential"


def dispatch_config(home: Path, runner_script: Path) -> None:
    """A dispatch configuration that makes the panel real without starting anything."""
    runner: dict[str, Any] = {
        "mode": "session",
        "actor": "claude",
        "argv": [sys.executable, str(runner_script), "{prompt}"],
    }
    config = {
        "version": 1,
        "enabled": True,
        "runners": {
            "fake": runner,
            PAUSED_RUNNER: {**runner, "env": {"ANTHROPIC_API_KEY": "sandbox-only"}},
        },
        "projects": {
            PROJECT_ID: {
                "enabled": True,
                "runner": "fake",
                "posture": "auto",
                "require_clean_tree": False,
            }
        },
        # One slot, and nothing to free it. `shadow` rather than `active` because an
        # active controller would empty this sandbox's queue on its first tick, and the
        # three entries sitting where they were put is the whole exhibit.
        "limits": {"max_concurrent_runs": 1, "dispatch_queue_limit": 10},
        "execution": {"controller": "shadow"},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def seed(manager: Any) -> dict[str, int]:
    """Four ready tasks, one per state, plus a couple of rows to sit them among."""
    from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority

    authorised: dict[str, int] = {}

    def ready(task_id: str, title: str, summary: str) -> None:
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
            task_id,
            actor="Jeff Posey",
            type=LogEntryType.NOTE,
            body="Go ahead and work this.",
        )
        authorised[task_id] = task.log[-1].id

    ready(
        "task-001",
        "Nothing has been asked of this one",
        "The control. No dispatch of it is waiting, so it reads Ready and offers Dispatch.",
    )
    ready(
        "task-002",
        "A dispatch of this one is next in line",
        "One slot frees and this starts. Its page offers to call the waiting entry off.",
    )
    ready(
        "task-003",
        "A dispatch of this one is behind another",
        "Two dispatches go first. The place is counted over the machine's whole queue.",
    )
    ready(
        "task-004",
        "A dispatch of this one is not being tried at all",
        "An open usage-limit incident holds every start on its credential off.",
    )
    for index, title in enumerate(ORDINARY, start=10):
        manager.create_task(
            id=f"task-{index:03d}",
            title=title,
            summary="Seeded so the four rows above sit in a list.",
            description="Seeded for review.",
            priority=Priority.MEDIUM,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )
    return authorised


def queue(home: Path, manager: Any, authorised: dict[str, int]) -> None:
    """Three real queue entries, written by the code a full machine writes them with."""
    from agentjobs.dispatch import queue as dispatch_queue
    from agentjobs.dispatch.guards import DispatchRequest
    from agentjobs.projects import ProjectRegistry

    project = ProjectRegistry(home).get(PROJECT_ID)
    # In this order, because FIFO is what decides the place in line each one reads.
    for task_id, runner in (
        ("task-002", None),
        ("task-003", None),
        ("task-004", PAUSED_RUNNER),
    ):
        dispatch_queue.enqueue(
            home,
            project,
            DispatchRequest(task_id=task_id, caused_by=authorised[task_id], runner=runner),
            manager=manager,
            queued_by="Jeff Posey",
            api_base="http://127.0.0.1:9",
        )


def pause_one_credential(home: Path) -> str:
    """Open a usage-limit incident on the credential only ``task-004`` would spend.

    Written straight into the incident book, the way ``IncidentBook.join`` writes one.
    Producing a real one would mean starting a run and having it hit a real usage limit,
    which is not a thing a review sandbox can arrange.
    """
    from agentjobs.dispatch.config import load_dispatch_config
    from agentjobs.dispatch.journal import journal
    from agentjobs.dispatch.start_pause import profile_for_runner

    config = load_dispatch_config(home)
    assert config is not None
    profile = profile_for_runner(config.runners[PAUSED_RUNNER])
    incident_id = "inc_sandbox_usage_limit"
    moment = datetime.now(timezone.utc)
    resets = (moment + timedelta(hours=2)).replace(second=0, microsecond=0)
    with journal(home).transaction("sandbox-incident") as connection:
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, "
            "state, opened_at, next_probe_at, resets_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?,?)",
            (
                incident_id,
                "usage_limit",
                profile.key,
                json.dumps(profile.as_json()),
                moment.isoformat(),
                moment.isoformat(),
                resets.isoformat(),
                moment.isoformat(),
            ),
        )
    return incident_id


def build(root: Path) -> tuple[Path, Any, dict[str, int]]:
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
    return project_root, manager, seed(manager)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-queued-status-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_root, manager, authorised = build(root)
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=PROJECT_NAME)

    runner_script = root / "never_runs.py"
    runner_script.write_text("print('this sandbox never starts anything')\n", encoding="utf-8")
    dispatch_config(home, runner_script)
    queue(home, manager, authorised)
    incident = pause_one_credential(home)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] queued-status sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   all four states       {base}/tasks?status=all", flush=True)
    print(f"[review]   nothing queued        {base}/tasks/task-001", flush=True)
    print(f"[review]   next in line          {base}/tasks/task-002", flush=True)
    print(f"[review]   third in line         {base}/tasks/task-003", flush=True)
    print(f"[review]   start paused          {base}/tasks/task-004  ({incident})", flush=True)
    print(f"[review]   the slot board        http://127.0.0.1:{port}/app/runs", flush=True)
    print(
        "[review] Glance at the list first. Then cancel task-002's entry. Then 390x844.", flush=True
    )
    # `lifespan="off"`, which is the one thing here that is not the real server. The
    # lifespan starts the dispatch poller, the poller drives the controller, and the
    # controller drains the queue the moment a slot is free -- correctly, because that is
    # what a queued dispatch is waiting for. A sandbox whose exhibit empties itself thirty
    # seconds in is no exhibit, so nothing drives the queue here and the three entries sit
    # where they were put. Everything a browser touches is served by the real application.
    serve(app, port=port, lifespan="off")


if __name__ == "__main__":
    main()
