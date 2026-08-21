"""Stand up the review panel in every state it can be in, on its own port.

task-231 changed one component, and the whole point of the change is that the panel
now shows different verbs depending on why the ball arrived. There is no way to see
that from a diff, and constructing six task states by hand to look at them is exactly
the friction that makes a UI change get approved unseen. So the states are seeded:

    task-101  human/review      Approve — agent may merge / Request Changes / …
    task-102  human/approval    the same verbs, on a gate rather than a code review
    task-103  human/decision    NO Approve. "Answer Questions" instead.
    task-104  human/input       the same as decision
    task-105  human/spec        past draft: no Approve, no Promote, feedback only
    task-106  draft             Promote — make it claimable, and nothing about merging
    task-107  agent/hold        the held panel: the release condition, and Resume

Every one of them is throwaway. Click anything, including the destructive controls --
nothing here touches the live corpus, the 8876 dashboard, or its registry. The data
lives under a temporary directory this process deletes when it stops.

    python scripts/review_panel_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * On task-103 and task-104 there is **no Approve button and no mention of merging**.
    That is the bug: they used to read "✓ Approve — agent may merge" over a prompt
    containing four numbered questions, on a task with no branch.
  * Every button's label matches what the record says afterwards. Press one, then read
    the task's ball_reason and its newest log entry.
  * Approve opens a note box. Leaving it empty must produce exactly the record a plain
    approval always produced; typing in it must not turn the approval into a revision.
  * task-107 offers Resume and no review verbs, and its Dispatch button is gone --
    a held task refuses a dispatch, so offering the button would be a lie.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8897


def seed(manager) -> None:
    """One task per state the panel branches on, and nothing else."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    def make(task_id: str, title: str, prompt: str, **kwargs):
        manager.create_task(
            id=task_id,
            title=title,
            description=(
                "Seeded so the review panel can be looked at in this state. Nothing "
                "here is real work, and every control on it is safe to press."
            ),
            summary=f"{title}.",
            priority=Priority.HIGH,
            lifecycle=kwargs.pop("lifecycle", Lifecycle.READY),
            actor="claude",
            **kwargs,
        )

    def park(task_id: str, reason, prompt: str) -> None:
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=reason,
            ball_prompt=prompt,
        )

    make("task-101", "Review: the drag-to-reorder branch", "")
    park(
        "task-101",
        BallReason.REVIEW,
        "Branch is green and rebased. Read the diff and approve or request changes.",
    )

    make("task-102", "Approval: spend on the second runner", "")
    park(
        "task-102",
        BallReason.APPROVAL,
        "A yes or a no, not a critique. The monthly cost is in the log.",
    )

    make("task-103", "Decision: how the launcher should be started", "")
    park(
        "task-103",
        BallReason.DECISION,
        "Four questions, and none of them is a yes/no:\n\n"
        "1. Should the launcher start on login, or on demand?\n"
        "2. One port per project, or one server for all of them?\n"
        "3. Does the tailnet proxy stay a separate process?\n"
        "4. What happens when the port is already bound?\n\n"
        "This is the shape of prompt that used to render an Approve button. Clicking "
        "it would have recorded an approval with no defined meaning, and the agent "
        "would have had to guess which of the four you had said yes to.",
    )

    make("task-104", "Input: the production database name", "")
    park(
        "task-104",
        BallReason.INPUT,
        "I cannot read this from anywhere. What is the database called in production?",
    )

    make("task-105", "Spec: the notification service, still being written", "")
    park(
        "task-105",
        BallReason.SPEC,
        "Past draft, but the acceptance criteria are still vague. Say what is missing.",
    )

    make(
        "task-106",
        "Draft: retire the legacy Jinja routes",
        "",
        lifecycle=Lifecycle.DRAFT,
    )
    manager.handoff(
        "task-106",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.SPEC,
        ball_prompt="Finish the spec, then promote it so an agent can claim it.",
    )

    make("task-107", "On hold: the epic-autonomy trio", "")
    manager.claim_task("task-107", agent="claude")
    manager.handoff(
        "task-107",
        actor="Jeff Posey",
        ball=Ball.AGENT,
        ball_reason=BallReason.HOLD,
        ball_prompt=(
            "ON HOLD -- do not resume this task until the condition below is met and a "
            "human has released it.\n\n"
            "Wait for the autonomous dispatch fixes before we try this again."
        ),
    )


def build(root: Path, *, project_id: str, name: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(TaskStorage(project_root / "tasks")))
    return project_root


STATES = [
    ("task-101", "human/review", "Approve — agent may merge, Request Changes"),
    ("task-102", "human/approval", "the same verbs, on a gate"),
    ("task-103", "human/decision", "NO Approve. Answer Questions instead."),
    ("task-104", "human/input", "the same as decision"),
    ("task-105", "human/spec", "past draft: no Approve, no Promote"),
    ("task-106", "draft", "Promote, and nothing about merging"),
    ("task-107", "agent/hold", "the release condition, Resume, no Dispatch"),
]


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-panel-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-panel", "Sandbox: the review panel"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[review] review-panel sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for task_id, state, note in STATES:
        print(f"[review]   {state:<16} {note}", flush=True)
        print(f"[review]     {base}/{task_id}", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    print("[review] stop with Ctrl-C; the data is deleted with the process.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
