"""Stand up the finished Tasks-surface shell on its own port, for driving by hand.

task-235 replaced the Tasks surface with a list region and a detail region beside it:
a collapsible tree you select from (task-238), a detail panel fitted to the region it
sits in (task-239), a device-class breakpoint rather than a width one (task-236), and
the filter controls folded behind a button (task-356). Every one of those is a thing
you can only judge by looking at it and using it, and the gestures that matter --
a drag, an Alt+Up, an arrow key, a window dragged across the breakpoint -- are the
ones no automated instrument can honestly report on (ENGINEERING.md, Verification;
task-225).

So this seeds a backlog that has all of the states at once, on a port of its own, and
hands over a list of nine things to try.

    task-100   the epic. Thirteen children, three of them with children of their own,
               so folding it hides two levels rather than one -- and the fold has a
               grandchild to hide, which is the case a one-level tree cannot show. Its
               record carries a forty-entry log, several long fields and a
               deliberately unbreakable 400-character token, so the detail panel runs
               out of room the way a real epic makes it. Its dependency graph is seven
               nodes wide in its first layer, which is wider than the detail region at
               900px: the graph has to scroll inside its own frame rather than push
               the panel sideways.

    task-143   a grandchild, two levels down. This is the deep link for check 3 --
               fold task-100, paste this, and the tree has to reveal it and select it.

    task-102   parked on human/review with a full review request. It is what the
               Dashboard's attention panel points at, so it is the task to click from
               the Dashboard for check 8.

    task-2xx   forty filler tasks across every band, which is the only reason the list
               region scrolls at all. A sidebar of fourteen rows cannot show you
               whether a sidebar stays put.

    a broken   `task-666.yaml` is not valid YAML, so the "task files could not be
    file       loaded" banner shows. task-237 moved that banner outside both regions,
               and the point of seeding it is to see it stay above them both instead
               of scrolling away inside one.

Everything here is throwaway. Click anything, including approve, reject, dispatch and
delete -- nothing touches the live corpus, the 8876 dashboard, or its registry. The
data lives under a temporary directory this process deletes when it stops, including
its own AGENTJOBS_HOME.

    python scripts/shell_sandbox.py [port] [--trace]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

**For a tablet**, publish this port over the tailnet from a second terminal and take it
down again afterwards:

    tailscale serve --bg --https=8443 http://127.0.0.1:8910
    tailscale serve --https=8443 off

That is the workstation's own MagicDNS name on a spare port, deliberately not the
`svc:agentjobs` Service the live dashboard uses -- the sandbox must not be reachable at
the address the real one answers on.

**`--trace` injects the drag trace** from `review_queue_sandbox.py`: a panel that
records whether a press landed on a reorder handle, which drag events fired, and what
the reorder request answered. It is off by default here and on there, because that
sandbox is about one gesture and this one is about a layout -- a fixed 400px panel over
the bottom-right corner is the wrong instrument to judge a detail region through. Turn
it on when a gesture appears not to land, which is the moment its evidence is worth the
corner it costs. One caveat in this shell: the trace reports `window.scrollY`, and the
two-region shell scrolls its regions rather than the document, so its "the page did NOT
scroll" line is expected here and says nothing about the list's own scrolling.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

try:  # Run as `python scripts/shell_sandbox.py`, or as a module. Both have to work.
    from scripts.review_queue_sandbox import InjectDragTrace, add_trace_routes
except ModuleNotFoundError:  # pragma: no cover - depends on how it was invoked
    from review_queue_sandbox import (  # type: ignore[import-not-found,no-redef]
        InjectDragTrace,
        add_trace_routes,
    )

DEFAULT_PORT = 8910

PROJECT_ID = "sandbox-shell"
PROJECT_NAME = "Sandbox: the Tasks-surface shell"

PARAGRAPH = (
    "This paragraph is here to be long, because a working specification on a real task "
    "in this repository runs to several hundred words and the question a two-region "
    "shell has to answer is what happens to those words when the region holding them "
    "is most of a 2560px monitor. Prose set in two-hundred-character lines is not read, "
    "it is scanned and abandoned; the reader loses the start of the next line every "
    "time they reach the end of one. "
)

LONG_TOKEN = "unbroken-token-" + ("x" * 400)

#: The epic's children, and what each one needs. The first layer is deliberately seven
#: wide: at 900px the detail region is narrower than that, so the graph is the block
#: that proves the wide sections scroll inside themselves instead of widening the panel.
CHILDREN: list[tuple[str, str, list[str]]] = [
    ("task-101", "Settle the breakpoint, and the burger menu it collides with", []),
    ("task-107", "Keep the header nav where it was", []),
    ("task-108", "Decide what an empty detail region says", []),
    ("task-110", "Audit the surfaces that keep the full width", []),
    ("task-111", "Name the regions for assistive technology", []),
    ("task-112", "Choose where the corpus banners live", []),
    ("task-113", "Write down the rule for a phone", []),
    ("task-102", "Give the Tasks surface two regions", ["task-101"]),
    ("task-103", "Nest the detail route inside the list route", ["task-101"]),
    ("task-104", "The task sidebar: a collapsible tree you select from", ["task-101"]),
    ("task-105", "Fit the detail panel to the main region", ["task-102", "task-103"]),
    ("task-106", "Hide the filter controls behind a button", ["task-104"]),
    (
        "task-109",
        "Drive the whole shell by hand before any of it merges",
        ["task-105", "task-106", "task-107", "task-108"],
    ),
]

#: Grandchildren, so folding the epic hides two levels. task-143 is the deep link.
GRANDCHILDREN: list[tuple[str, str, str]] = [
    ("task-141", "The fold state, and where a browser keeps it", "task-104"),
    ("task-142", "Arrow keys over the visible rows", "task-104"),
    ("task-143", "Reveal a folded ancestor when a link names its child", "task-104"),
    ("task-144", "The measure, and the blocks that ignore it", "task-105"),
    ("task-145", "The filter badge, when nothing matches", "task-106"),
]

#: Forty rows of ordinary backlog, which is the only reason the list region scrolls.
#: Spread across the bands so a drag has somewhere to land and Alt+Up has room to move.
FILLER: list[tuple[str, str]] = [
    ("critical", "Rotate the leaked API key"),
    ("critical", "Restore the nightly backup job"),
    ("high", "Explain why a task is next"),
    ("high", "Reorder the backlog from a phone"),
    ("high", "Retire the legacy Jinja routes"),
    ("high", "Queue position on the schema"),
    ("high", "Webhook signatures, and what verifies them"),
    ("high", "A run that parks on a permission prompt"),
    ("high", "Attachment storage outside the YAML"),
    ("high", "The dispatch group nobody configured"),
    ("high", "Stall detection when a session dies quietly"),
    ("high", "Make the CLI say which checkout it imported"),
    ("high", "Playbooks that outlive a session"),
    ("high", "The merge runway, and what queues on it"),
    ("high", "A second opinion on the queue repair"),
    ("medium", "Tidy the CLI help text"),
    ("medium", "Document the dispatch config"),
    ("medium", "Cache the corpus for one request"),
    ("medium", "Name the actors from one registry"),
    ("medium", "A slot board that fits a phone"),
    ("medium", "Report where dispatched time goes"),
    ("medium", "Trim the context budget again"),
    ("medium", "Benchmark the open-a-task interaction"),
    ("medium", "Teach the gate to resume from a stage"),
    ("medium", "An offline shell that says it is offline"),
    ("medium", "Version the generated client"),
    ("medium", "Front-door secret rotation"),
    ("medium", "The identity registry, per machine"),
    ("medium", "Exports that regenerate instead of drifting"),
    ("medium", "One index for the docs"),
    ("low", "Rename the sample project"),
    ("low", "Spell-check the schema reference"),
    ("low", "A favicon that is not the default"),
    ("low", "Sort the tag vocabulary"),
    ("low", "Retire the unused CSS"),
    ("low", "A shorter README"),
    ("low", "Consistent date formatting in the CLI"),
    ("low", "Drop the last jQuery reference"),
    ("low", "Archive the 2026-08 audit notes"),
    ("low", "Say what a draft is, once"),
]


def seed(manager) -> None:
    """The epic, its tree, the filler, and the states the shell renders differently."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority

    bands = {
        "critical": Priority.CRITICAL,
        "high": Priority.HIGH,
        "medium": Priority.MEDIUM,
        "low": Priority.LOW,
    }

    def make(task_id, title, priority, **kwargs):
        manager.create_task(
            id=task_id,
            title=title,
            summary=kwargs.pop("summary", f"{title}. Seeded for a layout review."),
            description=kwargs.pop(
                "description",
                "Nothing here is real work and every control on it is safe to press.",
            ),
            priority=priority,
            lifecycle=kwargs.pop("lifecycle", Lifecycle.READY),
            actor="claude",
            **kwargs,
        )

    # ---------------------------------------------------------------- the epic
    make(
        "task-100",
        "Adopt the list-and-detail shell every other app on the screen already uses",
        Priority.HIGH,
        summary=(
            "The epic. Fold it in the sidebar and it has two levels of descendants to "
            "hide; open its record and it has a wide graph, a long log and prose that "
            "runs out of room."
        ),
        description="## The working specification\n\n" + (PARAGRAPH * 4),
        category="ux",
        tags=["frontend", "layout", "react"],
        spec={
            "intent": PARAGRAPH,
            "constraints": "- " + PARAGRAPH * 2,
            "out_of_scope": "- " + PARAGRAPH,
            "context": [
                {"path": "frontend/src/App.tsx", "why": PARAGRAPH},
                {"path": "frontend/src/components/TaskList.tsx", "why": "The list region."},
                {"path": "frontend/src/components/TaskDetail.tsx", "why": "The detail region."},
            ],
        },
        acceptance=[
            {"id": f"ac-{n}", "text": f"Criterion {n}. " + PARAGRAPH, "status": "pending"}
            for n in range(1, 5)
        ],
        deliverables=[{"path": "frontend/src/App.tsx", "note": "The shell.", "status": "pending"}],
        links=[{"url": "https://example.invalid/shell", "title": "The design note", "rel": "doc"}],
    )

    for task_id, title, needs in CHILDREN:
        make(
            task_id,
            title,
            Priority.HIGH,
            parent="task-100",
            dependencies=[
                {"task": need, "type": "needs", "note": "In sequence."} for need in needs
            ],
        )

    for task_id, title, parent in GRANDCHILDREN:
        make(task_id, title, Priority.MEDIUM, parent=parent)

    # Forty entries, so the log is several screens on its own and the record header has
    # something to stay pinned above. One of them carries a token nothing can break --
    # the input most likely to widen the panel rather than scroll inside it.
    for n in range(1, 41):
        body = PARAGRAPH * 2 if n % 4 == 0 else f"Working pass {n}. Nothing was decided."
        if n == 17:
            body = "A log entry carrying a token nothing can break:\n\n" + LONG_TOKEN
        manager.add_log_entry(
            "task-100",
            actor="claude",
            type="decision" if n % 5 == 0 else "progress",
            body=body,
        )

    # ------------------------------------------------- the one parked on a human
    # The first child is done, which is what makes the ones that need it claimable --
    # and gives the tree a closed row, so the sidebar is not uniformly open.
    manager.close_task("task-101", actor="claude", outcome=Outcome.COMPLETED)

    manager.claim_task("task-102", agent="claude")
    manager.handoff(
        "task-102",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Both regions are on screen and each owns its own scrolling. Open a task "
            "from the list and confirm the list does not move. " + PARAGRAPH
        ),
    )

    # A second agent-held task, so the list shows more than one ball state.
    manager.claim_task("task-104", agent="claude")

    # ------------------------------------------------------------- the backlog
    for index, (band, title) in enumerate(FILLER):
        make(f"task-2{index:02d}", title, bands[band])

    # Closed work, which sorts behind the whole live queue rather than by band.
    make("task-300", "Migrate the corpus to schema v2", Priority.CRITICAL)
    manager.close_task("task-300", actor="claude", outcome=Outcome.COMPLETED)
    make("task-301", "The Alpine dashboard", Priority.MEDIUM)
    manager.close_task("task-301", actor="claude", outcome=Outcome.CANCELLED)

    # A draft, so the Dashboard's "awaiting your input" table has a row.
    make("task-302", "Sketch the mobile navigation", Priority.MEDIUM, lifecycle=Lifecycle.DRAFT)
    manager.handoff(
        "task-302",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.SPEC,
        ball_prompt="Decide whether this is worth doing at all.",
    )


def break_a_file(tasks_dir: Path) -> None:
    """Leave one file the loader cannot parse, so the banner has something to say.

    By hand, exactly as a bad merge or a stray editor would do it. Every verb in the
    system refuses to produce this state, which is why it cannot be seeded any other
    way.
    """
    (tasks_dir / "task-666.yaml").write_text(
        "schema: 2\n"
        "id: task-666\n"
        "title: A file a bad merge left behind\n"
        "lifecycle: ready\n"
        "<<<<<<< HEAD\n"
        "ball: agent\n"
        "=======\n"
        "  ball: [human\n"
        ">>>>>>> feat/task-235-shell\n",
        encoding="utf-8",
    )


def build(root: Path) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name=PROJECT_NAME, user="Jeff Posey"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tasks_dir = project_root / "tasks"
    seed(TaskManager(sandbox_store(tasks_dir)))
    break_a_file(tasks_dir)
    return project_root


def main() -> None:
    argv = [arg for arg in sys.argv[1:] if arg != "--trace"]
    trace = "--trace" in sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-shell-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    ProjectRegistry(home).add(build(root), project_id=PROJECT_ID, name=PROJECT_NAME)

    import uvicorn

    from agentjobs.api.main import app

    if trace:
        app.add_middleware(InjectDragTrace)
        add_trace_routes(app)

    base = f"http://127.0.0.1:{port}/app/p/{PROJECT_ID}"
    print(f"[review] shell sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   the Tasks surface  {base}/tasks", flush=True)
    print(f"[review]   the epic           {base}/tasks/task-100", flush=True)
    print(f"[review]   the deep link      {base}/tasks/task-143", flush=True)
    print(f"[review]   the Dashboard      {base}", flush=True)
    if trace:
        print("[review]   drag trace ON; gestures also POST to /review/trace", flush=True)
    print("[review] Data is throwaway and lives under", root, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
