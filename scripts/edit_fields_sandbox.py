"""Stand up the task page so the new Fields editor can be judged by hand, on its own port.

task-230 makes a task's authoring fields editable from the browser: priority, tags,
effort, category and title. The tests cover the wiring, the patch payload, the refusal
path and the tap targets. What none of them can answer is whether the *grooming loop*
works on a person -- whether opening a task on a phone, raising its priority and fixing
a tag is genuinely fifteen seconds, or whether it is three taps too many and you would
still rather ask an agent. That is a doing question, so this is for doing it.

**Both states are seeded, because the change makes the surface behave differently in
two and comparing them is the review**:

    a full task       `task-001` has four tags, a category, an effort and a long spec.
                      Editing it is the ordinary case: open Fields, change something,
                      save. Notice what the completion offers when you add a tag.

    a bare task       `task-002` has no tags, no effort and a one-line spec. This is the
                      state that used to have nothing to look at, and the form has to be
                      as usable filling fields in as changing them. "No tags yet." is
                      the empty state -- judge whether it reads as an invitation.

    parked at review  `task-003` is at human/review with a real ask. **This is the
                      property worth checking by hand**: change its priority and the
                      review panel above must not move, blink or disappear. An edit is
                      not a workflow move, and this is the screen that proves it.

    a stale page      `task-004` exists so you can make a conflict happen. Open it in
                      two tabs, start an edit in one, save something in the other, then
                      save the first. The refusal should name the field that moved,
                      not two timestamps, and your typing should still be in the boxes.

Everything here is throwaway. Click anything, including the destructive controls --
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME.

    python scripts/edit_fields_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **At 390x844 first**, because the phone is the case that motivates the whole task.
    Open Fields on `task-001`. Every control should be thumb-sized, the page should not
    scroll sideways, and the tag chips should wrap rather than run off the edge.
  * **Where the section sits.** It is directly under the metadata strip, above the
    review panel, collapsed to one row. On `task-003` that puts it above the thing you
    came to read. Say if that is the wrong trade.
  * **Adding a tag.** Type into the box and the completion should offer words this
    project already uses. Return should finish the word, not save the whole edit.
    "gui, api" typed in one go should become two chips.
  * **Saving nothing.** Change a value and change it back: Save should go dim. A patch
    that names a field nobody changed is a log entry that lies.
  * **The conflict, done for real** on `task-004` with two tabs. This is the one
    behaviour a screenshot cannot show.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8912

LONG_SPEC = """The queue's band arithmetic assigns a position from the band's own range,
and two tasks that enter the same band in the same second can be handed the same number.
It is not theoretical: it happened twice in one afternoon while an epic walk was starting
children, and the repair tool guessed which came first.

The fix is to take the queue lock across the read and the write rather than around the
write alone. That is a small change and a careful one, because the lock is already held
by the reband path and taking it twice would deadlock.
"""


def seed(manager) -> None:
    """Four tasks: a full one, a bare one, one parked at review, and one to conflict on."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    manager.create_task(
        id="task-001",
        title="Rework the queue's band arithmetic",
        summary="Two tasks entering one band in the same second can be handed the same position.",
        description=LONG_SPEC,
        priority=Priority.HIGH,
        lifecycle=Lifecycle.READY,
        actor="claude",
        category="infrastructure",
        effort="most of a day",
        tags=["queue", "reliability", "backend", "grooming"],
    )

    # The other half of the comparison. Nothing to change, everything to fill in.
    manager.create_task(
        id="task-002",
        title="Attachment thumbnails are served at full size",
        summary="A 4MB screenshot is sent whole to draw a 64px thumbnail.",
        description="Seeded bare on purpose: no tags, no effort, one line of spec.",
        priority=Priority.MEDIUM,
        lifecycle=Lifecycle.READY,
        actor="claude",
        category="general",
    )

    # Parked at human/review, so the "an edit is not a workflow move" claim can be
    # checked against the one screen where breaking it would be obvious.
    manager.create_task(
        id="task-003",
        title="Webhook retries have no ceiling",
        summary="A failing endpoint is retried until the process is restarted.",
        description="A bounded retry with a dead-letter entry on the task.",
        priority=Priority.MEDIUM,
        lifecycle=Lifecycle.READY,
        actor="claude",
        category="reliability",
        effort="half a day",
        tags=["webhooks"],
    )
    manager.claim_task("task-003", agent="claude")
    manager.handoff(
        "task-003",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Retries are now bounded at five with exponential backoff, and a failed "
            "delivery writes a dead-letter entry naming the endpoint and the status. "
            "The branch is green and rebased. Read the diff and approve or send it back."
        ),
    )

    # Somewhere to make a revision conflict happen with two tabs.
    manager.create_task(
        id="task-004",
        title="Log entries longer than a screen have no fold",
        summary="Open this in two tabs to make a stale edit happen on purpose.",
        description=(
            "Start an edit here in one tab, save a different field from the other, then "
            "save the first. The refusal should name the field that moved."
        ),
        priority=Priority.MEDIUM,
        lifecycle=Lifecycle.READY,
        actor="claude",
        category="ux",
        effort="an hour",
        tags=["gui"],
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


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-edit-fields-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-edit-fields", "Sandbox: editing a task's fields"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[review] edit-fields sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   a full task        {base}/task-001", flush=True)
    print(f"[review]   a bare task        {base}/task-002", flush=True)
    print(f"[review]   parked at review   {base}/task-003", flush=True)
    print(f"[review]   two tabs, conflict {base}/task-004", flush=True)
    print("[review] Start at 390x844. Ctrl-C stops it and deletes everything.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
