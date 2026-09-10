"""Stand up the task list on its own port, with throwaway data, for task-341.

Two projects on one server, so the change can be compared rather than just looked at.
Switch between them with the project picker.

    sandbox-density   the rows that broke the layout -- a title longer than the
                      viewport, a review handoff whose `ball_prompt` is a full review
                      request, a blocked task waiting on four others, an epic with
                      children, and a deep sub-task chain, mixed in with ordinary rows
    sandbox-ordinary  the same backlog with none of those: short titles, short prompts.
                      The control, and it matters as much -- a list that only ever
                      holds pathological rows cannot show you that the fix left the
                      normal case alone
    sandbox-drafts    drafts and nothing else, which is the only state in which the
                      Dashboard shows its "Backlog awaiting your input" table. That
                      table shares the component this branch changed, so it is here to
                      be looked at rather than taken on trust

What to look at. Before this branch, the first project rendered its first screen as an
empty void: one task's `ball_prompt` wrapped to 2070px inside the Status column and made
its row 2091px tall, while a long title sized the Task column to 1261px of a 1197px
viewport and pushed Status, Priority, Assigned and Updated off the right-hand edge.
Both are visible here on any window at all -- resize it and the six columns stay
inside the frame, long titles cut with an ellipsis, and a review prompt is two lines
with the rest on hover.

Nothing here touches the live corpus. Everything lives under a temporary directory that
is deleted when this process stops, including its own AGENTJOBS_HOME registry, so the
8876 dashboard and its registry are not involved at all. Click anything, including the
destructive controls.

    python scripts/task_list_density_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process.

This file lives in the repository rather than in the session that wrote it, for the
reason `review_queue_sandbox.py` gives: a sandbox reachable only from one agent's
temporary directory cannot be re-run by whoever has to reproduce its findings.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8898

# The real title that sized the column, from task-339. Kept verbatim rather than
# invented, because the length that broke this is the length worth reviewing against.
LONG_TITLE = (
    "The gate is launched six to nine times per task at seven to twelve minutes each "
    "under contention: one gate per handoff, and a core budget shared between the runs "
    "that overlap"
)

# The shape of a real review handoff -- the one this branch was filed against was
# longer than this. It is prose written for someone with the task open, which is why
# putting it in a list column was never going to work.
LONG_PROMPT = (
    "The Dashboard is a slot board. Branch `feat/task-000-a-worked-example`, five "
    "commits, gate green on the rebased tree with a receipt for the commit underneath "
    "it. ac-1 to ac-7 are met with evidence on the record; ac-8 is met in the sense "
    "that the panel renders, and not in the sense that anybody has watched it under "
    "three concurrent runs, which is the thing to look at first. Read the diff, then "
    "either approve it or say which of those two readings of ac-8 you want before it "
    "merges."
)


def seed(manager, *, pathological: bool) -> None:
    """A backlog with a row of every shape the task list has to render."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority

    def make(task_id, title, priority, lifecycle=Lifecycle.READY, **kwargs):
        manager.create_task(
            id=task_id,
            title=title,
            description="Seeded for a task-list layout review. Nothing here is real work.",
            summary=f"{title}.",
            priority=priority,
            lifecycle=lifecycle,
            actor="claude",
            **kwargs,
        )

    long_title = LONG_TITLE if pathological else "Share a core budget between overlapping gates"
    prompt = LONG_PROMPT if pathological else "Read the diff and approve it."

    # The two rows the defect was reported on: a title wider than the viewport, and a
    # review prompt written for a page rather than a cell. In the control project both
    # are the same rows with ordinary-length text, so the comparison is like for like.
    make("task-001", long_title, Priority.CRITICAL)

    make(
        "task-002", "The Dashboard is a slot board: one card per concurrent run", Priority.CRITICAL
    )
    manager.claim_task("task-002", agent="claude")
    manager.handoff(
        "task-002",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=prompt,
    )

    # Blocked on four things at once: four reasons in one Status cell, which is the
    # other way that column used to grow without limit.
    make("task-010", "Retire the legacy Jinja routes", Priority.HIGH)
    make("task-011", "Rotate the staging keys", Priority.HIGH)
    make("task-012", "Rotate the CI keys", Priority.HIGH)
    make("task-013", "Document the rotation runbook", Priority.HIGH)
    make("task-014", "Ship the credential rotation", Priority.HIGH)
    manager.update_task(
        "task-014",
        actor="claude",
        dependencies=[
            {"task": other, "type": "needs", "note": "Same credentials."}
            for other in ("task-010", "task-011", "task-012", "task-013")
        ],
    )

    # An epic with an open child, and a chain three deep: the Task column carries an
    # indent per level, which is width taken out of the column rather than added to it.
    make("task-020", "Notifications that survive the session", Priority.HIGH)
    make("task-021", "Pick a delivery channel", Priority.HIGH, parent="task-020")
    make("task-022", "Sign the webhook payload", Priority.HIGH, parent="task-021")
    make("task-023", "Publish the receiver's contract", Priority.MEDIUM, parent="task-022")

    # A task id long enough to wrap on its own, which is the one thing in the Task cell
    # that is deliberately *not* truncated -- it would cut the mobile cards too.
    make("task-030-a-slug-long-enough-to-wrap", "Rename the sample project", Priority.MEDIUM)

    make("task-031", "Tidy the CLI help text", Priority.MEDIUM)
    make("task-032", "Document the dispatch config", Priority.MEDIUM)
    make("task-040", "Refresh the screenshots", Priority.LOW)

    # A draft parked on a human for a decision, and closed work, so every badge the
    # Status column can show is somewhere on this screen.
    make("task-050", "Sketch the mobile navigation", Priority.MEDIUM, lifecycle=Lifecycle.DRAFT)
    manager.handoff(
        "task-050",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.SPEC,
        ball_prompt="Decide whether this is worth doing at all.",
    )

    make("task-060", "Migrate the corpus to schema v2", Priority.CRITICAL)
    manager.close_task("task-060", actor="claude", outcome=Outcome.COMPLETED)

    # Enough rows that the list is taller than a window, which is the only state in
    # which "one row is 2091px tall" is something you can see rather than measure.
    for index in range(24):
        make(f"task-1{index:02d}", f"Backlog filler {index + 1}", Priority.LOW)


def seed_drafts(manager) -> None:
    """Drafts only, so the Dashboard reaches the rung that renders a table.

    The next-action ladder shows exactly one panel, and every other rung outranks the
    backlog -- so a project with a single ready task never renders the table this
    branch also touched. Drafts and nothing else is the whole fixture.
    """
    from agentjobs.models_v2 import Lifecycle, Priority

    titles = [
        LONG_TITLE,
        "A draft whose title is long enough to have sized this column before the fix",
        "Sketch the mobile navigation",
        "Decide whether the badge should be red",
        "Rename the sample project",
        "Drop the unused index",
        "Refresh the screenshots",
    ]
    for index, title in enumerate(titles):
        manager.create_task(
            id=f"task-{index + 1:03d}",
            title=title,
            description="Seeded for a task-list layout review. Nothing here is real work.",
            summary=f"{title}.",
            priority=Priority.MEDIUM,
            lifecycle=Lifecycle.DRAFT,
            actor="claude",
        )


def build(root: Path, *, project_id: str, name: str, pathological: bool | None) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks"))
    if pathological is None:
        seed_drafts(manager)
    else:
        seed(manager, pathological=pathological)
    return project_root


def main() -> None:
    argv = sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-density-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    projects = [
        ("sandbox-density", "Sandbox: the rows that broke the layout", True),
        ("sandbox-ordinary", "Sandbox: the same backlog, ordinary text", False),
        ("sandbox-drafts", "Sandbox: drafts only, for the Dashboard's table", None),
    ]
    for project_id, name, pathological in projects:
        registry.add(
            build(root, project_id=project_id, name=name, pathological=pathological),
            project_id=project_id,
            name=name,
        )

    import uvicorn

    from agentjobs.api.main import app

    print(f"[review] task-list density sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for project_id, *_ in projects:
        print(f"[review]   http://127.0.0.1:{port}/app/p/{project_id}/tasks", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
