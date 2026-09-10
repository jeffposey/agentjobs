"""Stand up the Dashboard's next-action panel in every state task-337 changed.

The reported defect: on a project with parked drafts, the Dashboard answered "what do I
do next" with a table of every one of them -- 31 rows, each reading `spec`, under a
sentence admitting that nothing is blocked by any of them. The backlog rung outranked
"next up", so that was the answer on precisely the days when nothing needed a person.

Four projects on one server, so the change and everything it must not have broken can be
compared by switching project rather than by constructing states by hand:

    sandbox-reported     the reported shape -- seven parked drafts and ready work. This
                         is the one to look at first: it used to show the drafts table
                         and now shows "Next up" with the head of the queue and a
                         Dispatch button on each row.
    sandbox-drafts-only  parked drafts and nothing claimable. The backlog panel still
                         renders here, because there is nothing better to say -- and it
                         now samples five and links to the rest instead of printing all
                         seven.
    sandbox-blocked      a task actually stopped on a human, plus drafts, plus ready
                         work. The alert rung is untouched and still strict: it must win
                         over both of the above.
    sandbox-no-dispatch  the reported shape on a project where dispatch is switched off.
                         No buttons; one line at the foot of the panel saying why, with
                         a link to the settings. This is the half that must not become
                         a refusal repeated beside every row.

Everything is throwaway. Press anything, including Dispatch: the configs, the registry
and the tasks live under a temporary directory this process deletes when it stops, and
the "agent" a dispatch starts is a script that prints one line and exits. Your real
corpus and the 8876 dashboard are not involved -- this server has its own
``AGENTJOBS_HOME``.

    python scripts/dashboard_next_action_sandbox.py [port]

Stop it with Ctrl-C, and the data goes with it. The port defaults to one of its own so
it cannot be confused with the real dashboard: a second server on the usual port
silently serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * On **reported**, the panel names three tasks in the queue's order -- the same order
    ``agentjobs next`` hands out. Above them is a sentence about the machine: with
    nothing running it reads *Nothing is running on this machine. Starting one of these
    is the useful move.*
  * "Why this one first?" is inside the **first** card, under a hairline, and explains
    that card. It sat after the whole list in a box of its own until Jeff read it as a
    fourth standalone card; open it and check it still reads as belonging to the task
    above it.
  * Press one Dispatch. Within a couple of seconds that sentence should change to *An
    agent is already working. These are next in line.* -- it is read from the same
    machine-wide endpoint the capacity row below uses, so the two must agree.
  * The drafts have not vanished. The statistics card carries *+7 in backlog*, which is
    unconditional and is the whole of the trace now that the panel is suppressed whenever
    there is work to offer.
  * On **drafts-only**, the backlog panel is back, showing five rows and *View all 7
    drafts →*. Compare it with what **reported** shows: same seven drafts, different
    answer, because on one of them there is something better to say.
  * On **blocked**, neither of the calm panels appears. The alarm wins outright, which is
    the one piece of the ladder that did not move.
  * On **no-dispatch**, the three tasks are still listed and still link, and the foot of
    the panel says why there is no button. One line, not three.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_PORT = 8903

#: A harmless script wearing a name that looks like a real runner, so the sentence
#: beside the button is a resolution rather than a caption.
RUNNER = "claude-opus-5"

REPORTED = "sandbox-reported"
DRAFTS_ONLY = "sandbox-drafts-only"
BLOCKED = "sandbox-blocked"
NO_DISPATCH = "sandbox-no-dispatch"

NAMES = {
    REPORTED: "Sandbox: the reported shape -- drafts and ready work",
    DRAFTS_ONLY: "Sandbox: drafts and nothing claimable",
    BLOCKED: "Sandbox: work actually stopped on a human",
    NO_DISPATCH: "Sandbox: the reported shape, dispatch switched off",
}

#: Which projects may dispatch. `no-dispatch` is enabled on the machine and off for the
#: project, which is the state that produces a refusal with something to say -- a project
#: that was never configured produces silence, and silence is not what needs reviewing.
DISPATCHABLE = {REPORTED, DRAFTS_ONLY, BLOCKED}

#: Enough drafts that the sampling is visible. Seven, so "View all 7 drafts" is a
#: sentence about a number the reader can count on the screen it came from.
DRAFT_TITLES = [
    (
        "Atomic task-file writes and stale task-lock reclaim",
        "A killed writer leaves a lock nothing reclaims.",
    ),
    (
        "Drop git merge from the pre-approved allow-list",
        "Decide whether a run may merge without being asked.",
    ),
    (
        "Declare the TaskStorage Protocol with load_raw",
        "Pick the seam before a second backend needs one.",
    ),
    ("Delete the legacy Jinja pages", "They are still mounted and still proxied to the tailnet."),
    (
        "Refuse cross-site requests to the loopback API",
        "A page on any origin can post to 127.0.0.1 today.",
    ),
    ("Copy the mypy cache into worktrees", "A fresh worktree pays nineteen seconds it need not."),
    (
        "Decide what to do about machine identifiers in the corpus",
        "Some are already in a public repository.",
    ),
]

#: The claimable frontier, highest band first. More than the panel shows, on purpose:
#: the cap is part of what is being reviewed.
READY_TITLES = [
    (
        "Contain a render crash so one bad field cannot blank the app",
        "One error boundary per route, so a bad task file loses a card and not the page.",
    ),
    (
        "Ship sourcemaps so a stack trace names a source line",
        "Today a production trace names a minified chunk and an offset.",
    ),
    (
        "Detect a bundle talking to a server it was not built against",
        "The version endpoint knows; nothing compares it to what the bundle expects.",
    ),
    (
        "Stop one long ball_prompt from swallowing a task list row",
        "A four-hundred-character prompt pushes every other column off the screen.",
    ),
    (
        "Generate the next task number from all slugged task ids",
        "The counter reads unslugged ids only, so it can hand out one twice.",
    ),
]


def seed(manager: Any, *, drafts: bool, ready: bool, blocked: bool) -> None:
    """Whichever rungs this project is meant to exercise, and no others."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    def make(title: str, summary: str, priority: Any, **kwargs: Any) -> str:
        task = manager.create_task(
            title=title,
            category="general",
            summary=summary,
            description=(
                "Seeded so the Dashboard's next-action panel can be looked at in this "
                "state. Nothing here is real work, and every control is safe to press "
                "-- the runner is a script that prints one line and exits."
            ),
            priority=priority,
            actor="claude",
            **kwargs,
        )
        return str(task.id)

    if ready:
        for index, (title, summary) in enumerate(READY_TITLES):
            make(
                title,
                summary,
                Priority.HIGH if index < 3 else Priority.MEDIUM,
                lifecycle=Lifecycle.READY,
            )

    if drafts:
        for title, summary in DRAFT_TITLES:
            task_id = make(title, summary, Priority.MEDIUM, lifecycle=Lifecycle.DRAFT)
            manager.handoff(
                task_id,
                actor="claude",
                ball=Ball.HUMAN,
                ball_reason=BallReason.SPEC,
                ball_prompt="Decide whether this should become work.",
            )

    if blocked:
        task_id = make(
            "Approve the pricing page copy",
            "Two variants are written and one has to ship.",
            Priority.HIGH,
            lifecycle=Lifecycle.READY,
        )
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Read the two variants and say which one ships.",
        )


def build(root: Path, *, project_id: str) -> Path:
    """A registered project with a clean git tree and the state under review."""
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    name = NAMES[project_id]
    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    # Tasks are ignored rather than committed: this sandbox writes to them on every
    # click, and a clean-tree gate that shuts the moment you press a button is not one.
    (project_root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
    seed(
        TaskManager(sandbox_store(project_root / "tasks")),
        drafts=True,
        ready=project_id != DRAFTS_ONLY,
        blocked=project_id == BLOCKED,
    )
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "sandbox@example.invalid"],
        ["git", "config", "user.name", "Sandbox"],
        ["git", "add", ".gitignore"],
        ["git", "commit", "-m", "sandbox"],
    ):
        subprocess.run(command, cwd=project_root, capture_output=True, check=True)
    return project_root


def write_dispatch_config(home: Path, root: Path) -> None:
    """One machine, four projects, three of which may start a run."""
    fake = root / "fake-agent.py"
    fake.write_text(
        "import sys, time\n" "print('sandbox agent started:', sys.argv[1:])\n"
        # Long enough that "an agent is already working" is readable on screen, short
        # enough that the sandbox is never left with a run nobody ended.
        "time.sleep(45)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(fake), "{prompt}"]
    projects: Dict[str, Dict[str, object]] = {
        project_id: {
            "enabled": project_id in DISPATCHABLE,
            "runner": RUNNER,
            "posture": "auto",
            "require_clean_tree": False,
        }
        for project_id in NAMES
    }
    config = {
        "version": 1,
        "enabled": True,
        "runners": {RUNNER: {"argv": argv, "actor": "claude"}},
        "projects": projects,
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def serve(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="agentjobs-dashboard-next-action-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    print(f"[review] dashboard next-action sandbox on {port}; data under {root}", flush=True)
    print("[review] stop with Ctrl-C, and the data goes with it.", flush=True)
    order: List[Tuple[str, str]] = [(project_id, NAMES[project_id]) for project_id in NAMES]
    for project_id, name in order:
        registry.add(build(root, project_id=project_id), project_id=project_id, name=name)
        print(f"[review]   http://127.0.0.1:{port}/app/p/{project_id}", flush=True)

    write_dispatch_config(home, root)

    import uvicorn

    from agentjobs.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)


if __name__ == "__main__":
    main()
