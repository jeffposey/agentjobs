"""Stand up a task record big enough to judge the detail panel's fit, on its own port.

task-239 fits `TaskDetail` into the bounded region task-237 gave it. Nothing about that
change is visible in a diff and nothing about it is visible on a short record either:
the questions are whether prose holds a readable measure on a 2560px monitor, whether
the wide blocks use the room instead of being cut to the same measure, whether the
header stays put while a long log scrolls under it, and whether anything is clipped at
the narrowest landscape window the shell admits. All four need a record with enough in
it to run out of room.

So one is seeded:

    task-201   an umbrella, parked on human/review, with

               * a working specification of several hundred words per field, which is
                 what the measure is for
               * forty log entries, several of them long, which is what the pinned
                 header is for
               * four children with dependency edges between them, so the dependency
                 graph renders wide and has to scroll inside itself
               * a deliberately unbroken 400-character line in the log, which is the
                 thing most likely to push the panel sideways

    task-202   a bare record next to it: no children, no log to speak of, three-word
               fields. The comparison is the fixture. A layout that only looks right
               when it is full is not right.

Everything here is throwaway. Click anything, including the destructive controls --
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME.

    python scripts/detail_fit_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **At 2560px wide.** The working spec, the log bodies and the review prompt stop at
    a readable measure. The dependency graph, the log's frame and the relationship
    cards do not -- they use the width. If everything stops at the same place, the
    measure has been put on the sections instead of on the text.
  * **Scroll the record to the bottom.** The task id, its title and its status are
    still on screen. The list beside it has not moved.
  * **At 900x700.** Nothing is cut off on the right, and the page has no horizontal
    scrollbar. The dependency graph scrolls sideways *inside its own dashed frame*.
  * **At 390x844** -- a phone. One thing at a time, no pinned record header, and the
    document scrolls as it always did. The header is pinned to a region, and on a phone
    there is no region to pin it to.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8898

PARAGRAPH = (
    "This paragraph exists to be long. A working specification on a real task in this "
    "repository runs to several hundred words per field, and the question the layout "
    "has to answer is what happens to those words when the region holding them is two "
    "thousand pixels wide. Prose set in two-hundred-character lines is not read, it is "
    "scanned and abandoned, and the reader loses the start of the next line every time "
    "they reach the end of one. "
)

LONG_LINE = "unbroken-token-" + ("x" * 380)


def seed(manager) -> None:
    """One full record, one empty one, and the children the graph needs."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    manager.create_task(
        id="task-201",
        title="A record with enough in it to run the panel out of room",
        summary=(
            "Seeded so the detail panel can be judged at a width. Nothing here is real "
            "work and every control on it is safe to press."
        ),
        description="## The working specification\n\n" + (PARAGRAPH * 4),
        priority=Priority.CRITICAL,
        lifecycle=Lifecycle.READY,
        actor="claude",
        category="ux",
        tags=["frontend", "layout", "react"],
        spec={
            "constraints": "- " + PARAGRAPH * 2,
            "out_of_scope": "- " + PARAGRAPH,
            "intent": PARAGRAPH,
            "context": [
                {"path": "frontend/src/components/TaskDetail.tsx", "why": PARAGRAPH},
                {"path": "frontend/src/App.tsx", "why": "The shell the panel sits in."},
            ],
        },
        acceptance=[
            {"id": f"ac-{n}", "text": f"Criterion {n}. " + PARAGRAPH, "status": "pending"}
            for n in range(1, 4)
        ],
        deliverables=[
            {
                "path": "frontend/src/components/TaskDetail.tsx",
                "note": "The panel.",
                "status": "pending",
            }
        ],
        links=[{"url": "https://example.invalid/design", "title": "The design note", "rel": "doc"}],
    )

    for n, child in enumerate(
        [
            "Child one: the measure",
            "Child two: the pinned header",
            "Child three: the wide blocks",
            "Child four: the full-width surfaces",
        ],
        start=1,
    ):
        manager.create_task(
            id=f"task-21{n}",
            title=child,
            summary=f"{child}.",
            description="Seeded as a child so the dependency graph has something to draw.",
            priority=Priority.MEDIUM,
            lifecycle=Lifecycle.READY,
            actor="claude",
            parent="task-201",
            dependencies=(
                [{"task": f"task-21{n - 1}", "type": "needs", "note": "In sequence."}]
                if n > 1
                else []
            ),
        )

    # Forty entries, so the log is several screens deep on its own. Some short, some
    # long, one carrying a token no line-breaking algorithm can split -- which is the
    # input most likely to widen the panel rather than scroll inside something.
    for n in range(1, 41):
        body = PARAGRAPH * 2 if n % 4 == 0 else f"Working pass {n}. Nothing was decided."
        if n == 17:
            body = "A log entry carrying a token nothing can break:\n\n" + LONG_LINE
        manager.add_log_entry(
            "task-201",
            actor="claude",
            type="decision" if n % 5 == 0 else "progress",
            body=body,
        )

    manager.claim_task("task-201", agent="claude")
    manager.handoff(
        "task-201",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "The whole panel is on screen, which is the point of the fixture. " + PARAGRAPH
        ),
    )

    manager.create_task(
        id="task-202",
        title="A bare record",
        summary="Three fields and nothing else.",
        description="Nothing to say.",
        priority=Priority.LOW,
        lifecycle=Lifecycle.READY,
        actor="claude",
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
    root = Path(tempfile.mkdtemp(prefix="agentjobs-detail-fit-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-fit", "Sandbox: the detail panel's fit"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[review] detail-fit sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   the full record   {base}/task-201", flush=True)
    print(f"[review]   the bare one      {base}/task-202", flush=True)
    print("[review] Widen to 2560, then narrow to 900x700, then 390x844.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
