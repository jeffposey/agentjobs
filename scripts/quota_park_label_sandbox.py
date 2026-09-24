"""Stand up the two external parks side by side, on their own port, for looking at.

task-456 splits one label in two. Every ``external``/``service`` park used to read
"Blocked on a service" in the red a blockage gets, including a session waiting out a
usage limit -- which AgentJobs probes and resumes by itself, with nobody doing anything.
The tests assert the exact strings and the filter's wiring. What they cannot answer is
whether the distinction *reads*: whether a glance at the list now separates the task that
needs somebody from the one that does not.

**Both states are seeded, because the change makes the surface behave differently in two
and comparing them is the review:**

    a quota park        Parked by an auth-recovery-shaped handoff on a ``usage_limit``
                        incident with a reset time two hours out. Reads
                        "Waiting on quota reset (HH:MM UTC)", in the muted tint a wait
                        gets. **Nothing needs doing to this task.**

    a vendor park       The same ball and reason, parked by a person with no marker.
                        Reads "Blocked on a service", in red. **This one is somebody's
                        problem.**

    a spend limit       Parked on a *person* by the same machinery, because only the
                        account owner can raise it. Reads "Needs input". It is here to
                        prove the change did not make every auth-shaped park look
                        self-healing.

Everything here is throwaway. Click anything, including the destructive controls --
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME.

    python scripts/quota_park_label_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **The task list, glanced at rather than studied.** Three parked rows. If your eye
    cannot pick out which one needs you inside a second, the tint or the wording is not
    doing its job and that is worth saying.
  * **The reset time.** It is UTC and says so, because a label derived on the server
    cannot know your zone. Whether that reads as useful or as noise in a 194px column is
    a judgement, and the full timestamp is in the prompt line underneath either way.
  * **The two filter options.** Status -> Blocked shows the vendor only; Status ->
    Waiting on a reset shows the quota park only. Before this they shared one option, so
    neither could be reached on its own.
  * **The dashboard's open-task list**, which draws the same badge. Same question.
  * **Each task's own page**, where the label sits beside the priority pill.
  * **At 390x844.** The label is longer than "Blocked" was; check it does not push the
    row into three lines on a phone.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 8911

ORDINARY = [
    "Rework the queue's band arithmetic",
    "Attachment thumbnails are served at full size",
    "Search does not reach the log bodies",
]


def seed(manager: Any) -> None:
    """One park of each kind, each written the way the real machinery writes it."""
    from agentjobs.models_v2 import AUTH_RECOVERY_MARKER, Ball, BallReason, Lifecycle, Priority

    resets = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)

    def claimed(task_id: str, title: str, summary: str, priority: Priority) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            summary=summary,
            description="Seeded for review. Nothing here is real work.",
            priority=priority,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ops",
        )
        manager.claim_task(task_id, agent="claude")

    claimed(
        "task-001",
        "A session waiting out its usage limit",
        "Parked by auth recovery on a usage limit. Nobody is needed; it resumes itself.",
        Priority.HIGH,
    )
    manager.handoff(
        "task-001",
        actor="dispatcher",
        ball=Ball.EXTERNAL,
        ball_reason=BallReason.SERVICE,
        ball_prompt=(
            "Session sandbox-0001 hit its usage limit. The limit resets at "
            + resets.isoformat()
            + ". AgentJobs will probe after the reset and resume this same session once, "
            "with its own saved options. Nothing to do unless you want it sooner."
        ),
        data={
            AUTH_RECOVERY_MARKER: {
                "incident": "inc_sandbox_quota",
                "run_id": "run_sandbox_quota",
                "action": "park",
                "kind": "usage_limit",
                "resets_at": resets.isoformat(),
            }
        },
    )

    claimed(
        "task-002",
        "A third party has been down since this morning",
        "Parked on a service by a person. This one is somebody's problem.",
        Priority.HIGH,
    )
    manager.handoff(
        "task-002",
        actor="Jeff Posey",
        ball=Ball.EXTERNAL,
        ball_reason=BallReason.SERVICE,
        ball_prompt=(
            "Their API has answered 503 since 08:00. Nothing to do here until it is back."
        ),
    )

    claimed(
        "task-003",
        "A session stopped on a spend limit",
        "Parked on a person, because only the account owner can raise a spend limit.",
        Priority.CRITICAL,
    )
    manager.handoff(
        "task-003",
        actor="dispatcher",
        ball=Ball.HUMAN,
        ball_reason=BallReason.INPUT,
        ball_prompt=(
            "Session sandbox-0003 hit a spend limit. Only the account owner can raise it. "
            "AgentJobs re-probes every 15 minutes and resumes this same session once the "
            "model answers; no Answer or Dispatch is needed after the limit is raised."
        ),
        data={
            AUTH_RECOVERY_MARKER: {
                "incident": "inc_sandbox_spend",
                "run_id": "run_sandbox_spend",
                "action": "park",
                "kind": "spend_limit",
            }
        },
    )

    # A few ordinary rows, so the three parked ones are picked out of a list rather than
    # being the whole list -- which is the gesture under review.
    for index, title in enumerate(ORDINARY, start=10):
        manager.create_task(
            id=f"task-{index:03d}",
            title=title,
            summary="Seeded so the parked rows sit in a list.",
            description="Seeded for review.",
            priority=Priority.MEDIUM,
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )


def build(root: Path, *, project_id: str, name: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(sandbox_store(project_root / "tasks", project_id=project_id)))
    return project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-quota-park-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-parks", "Sandbox: what a park says it is"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}"
    print(f"[review] quota-park-label sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   all three parks       {base}/tasks?status=all", flush=True)
    print(f"[review]   a real blocker only   {base}/tasks?status=external", flush=True)
    print(f"[review]   a self-clearing wait  {base}/tasks?status=reset", flush=True)
    print(f"[review]   the quota park's page {base}/tasks/task-001", flush=True)
    print("[review] Glance at the list first, then open the filters. Then 390x844.", flush=True)
    serve(app, port=port)


if __name__ == "__main__":
    main()
