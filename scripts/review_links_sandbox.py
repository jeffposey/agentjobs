"""Stand up the review panel with real addresses in it, on its own port.

task-363 is three rendering defects in one screen, and none of them is a thing a test
run can settle. A passing suite shipped all three: the URLs in a handoff rendered as
dead text a phone cannot tap, the addresses a reviewer needed were buried in the fourth
paragraph of the prose, and a question card drew the fieldset's top border straight
through its own heading and numbered it twice.

    task-301  three named link lines -- the convention, and what the card is for
    task-302  five questions: one self-numbered, one long enough to wrap, one bare
    task-303  no addresses in the prose, two in `links[]` as `pr` and `build`
    task-304  a review with no addresses at all -- the card must not appear
    task-305  the task-240 handoff as it was really written, before the convention

Every one of them is throwaway. Press anything, including the destructive controls --
nothing here touches the live corpus, the 8876 dashboard, or its registry. The data
lives under a temporary directory this process deletes when it stops.

    python scripts/review_links_sandbox.py [port] [host] [advertised base URL]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

**To review this on a phone**, which is the screen the change is for, put a proxy in
front rather than widening the bind -- a request arriving from another device has no
proven identity and loses every verb (see ``DEFAULT_HOST``). The third argument is what
the seeded handoffs will call the sandbox, and behind a proxy that is the proxy's name:

    tailscale serve --bg --https=8443 http://127.0.0.1:8913
    python scripts/review_links_sandbox.py 8913 127.0.0.1 https://<host>.ts.net:8443

Turn the proxy off with ``tailscale serve --https=8443 off`` when the review is done;
``--bg`` persists across reboots until you do.

What to look for, since "it renders" is not the property under review:

  * **task-301, on the phone.** The three addresses are in the card, named, and
    **nowhere else** -- the prose refers to them by name and carries no URL at all.
    That card is the first thing to reach for, and the test of it is whether you ever
    have to read the prose to find where to go, or read the same address twice.
  * **task-305 beside it**, which is the same handoff written the old way, with an
    address in the middle of check 3. It stays in the sentence, because lifting it
    would leave a hole and duplicating it is the thing this pass removes. Compare the
    two: 301 is what the convention buys, and `task_handoff` warns an agent that
    writes 305.
  * **task-302, at phone width.** Question 2 is long enough to wrap its heading to two
    or three lines. The fieldset's border must run unbroken above it, not through it --
    that is the defect. Question 3 begins with its own "3." and must render as one
    number, not two, and as **1.** through **5.** in the order the cards appear, not in
    the order the agent numbered them.
  * **The task-017 rules are unchanged**, and this panel is where they would break:
    every question still offers free text, nothing arrives preselected, and one Submit
    writes them all. Tap options, go back, return -- nothing recorded until you press
    "Send answers".
  * **task-303** hoists the pull request and the build and leaves the handbook link in
    the "Links" section further down, where a durable reference belongs.
  * **task-304** shows no card at all. A card that appears empty on an ordinary review
    would be worse than the defect it fixes.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_PORT = 8913

#: Loopback, because that is where a sandbox belongs: it carries no authentication, and
#: the tailnet front door -- which does, and which refuses the three routes that turn
#: the API into arbitrary code execution -- is in front of the *real* server, not this
#: one. See docs/tailnet-front-door.md.
#:
#: **Do not widen this to reach a phone.** It looks like the fix and it is not: a
#: connection from another device resolves to no proven identity at all, because
#: ``principals.is_local`` is loopback and nothing else (task-363, measured). The
#: reviewer would get a page they can read and no verb they can press -- worse than no
#: sandbox, because it looks like the feature is broken. Put a proxy in front instead,
#: so the request reaches this process from loopback:
#:
#:     tailscale serve --bg --https=8443 http://127.0.0.1:8913
DEFAULT_HOST = "127.0.0.1"

#: What the seeded handoffs call this sandbox, which is **not** always where it is
#: bound. Behind a proxy the two differ, and the addresses this sandbox writes into its
#: own task records have to be the ones the reviewer's browser can resolve -- on a
#: phone, the proxy's name, never ``127.0.0.1``. Getting this wrong is invisible on the
#: desktop that started it and total on the device it was stood up for.
#:
#:     python scripts/review_links_sandbox.py 8913 127.0.0.1 https://host.ts.net:8443
DEFAULT_BASE = ""

#: The task-240 review request of 2026-09-06 rewritten to the convention: every
#: address on its own named line, and the prose referring to them by name.
NAMED_PROMPT = """The Tasks-surface shell sandbox is up, on its own port, with throwaway data.

Three checks, and the third needs a specific task rather than the list:

  1. Open a task from the sidebar and confirm the list keeps its scroll position.
  2. Narrow the window to phone width; the panel should restack, not scroll sideways.
  3. Open the task page below and confirm the log expands.

Nothing here is live. Stop it with Ctrl-C when you are done, and say whether the shell
is worth keeping before I merge the branch.

Desktop shell: {base}/app/
Tablet: {base}/app/?w=1024
Task page for check 3: {base}/app/p/sandbox-shell/tasks/task-143"""

#: The same handoff as it was really written, which is the screenshot that filed
#: task-363: one address per line with a bare label, and one in the middle of a
#: sentence. Kept so the two can be looked at side by side.
UNNAMED_PROMPT = """The Tasks-surface shell sandbox is up, on its own port, with throwaway data.

Desktop: {base}/app/
Tablet:  {base}/app/?w=1024

Three checks, and the third needs a specific task rather than the list:

  1. Open a task from the sidebar and confirm the list keeps its scroll position.
  2. Narrow the window to phone width; the panel should restack, not scroll sideways.
  3. Open {base}/app/p/sandbox-shell/tasks/task-143 and confirm the log expands.

Nothing here is live. Stop it with Ctrl-C when you are done, and say whether the shell
is worth keeping before I merge the branch."""

#: Five questions, each seeded for one thing the card has to survive. The bodies are
#: the shapes the corpus actually contains, not invented awkwardness.
QUESTIONS: List[Dict[str, Any]] = [
    {
        "body": "Which port should the shell sandbox default to?",
        "options": [
            {
                "label": "8910",
                "description": "Clear of the dashboard and of every other sandbox.",
                "recommended": True,
            },
            {
                "label": "8898",
                "description": "Shared with the answering sandbox; they cannot both run.",
            },
        ],
    },
    {
        # Long on purpose: this is the one whose heading wraps to two or three lines,
        # and a wrapped legend is exactly where the border used to be ruled through it.
        "body": (
            "When the sandbox seeds a task whose log is long enough to need the "
            "expand-all control, should the panel open every entry on arrival, open "
            "only the newest, or keep the collapsed default the real dashboard uses "
            "so that what you are reviewing is the same screen a reviewer gets?"
        ),
        "options": [
            {
                "label": "Keep the collapsed default",
                "description": "Same screen as the dashboard.",
                "recommended": True,
            },
            {"label": "Expand everything", "description": "Faster to read; not the real screen."},
        ],
    },
    {
        # The doubled-number case: the agent numbered its own question.
        "body": "3. Should the sandbox delete its temporary directory on Ctrl-C, or leave it for inspection?",
        "options": [
            {"label": "Delete it", "recommended": True},
            {"label": "Leave it and print the path"},
        ],
    },
    {
        # No options at all -- the prose question most of this repository's corpus is.
        "body": "What should the sandbox be called in the docs?",
        "placeholder": "a short name",
    },
    {
        "body": "1) Which of these should the sandbox seed by default?",
        "multi_select": True,
        "options": [
            {"label": "A task under review", "recommended": True},
            {"label": "A task with open questions", "recommended": True},
            {"label": "A held task"},
            {"label": "A draft"},
        ],
    },
]


def seed(manager: Any, *, base: str) -> None:
    """One task per state worth looking at, and nothing else."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Link, LinkRel, Priority

    def make(task_id: str, title: str) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            description=(
                "Seeded so the review panel can be looked at in this state. Nothing "
                "here is real work, and every control on it is safe to press."
            ),
            summary=f"{title}.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.claim_task(task_id, agent="claude")

    make("task-301", "A review request that names three addresses")
    manager.handoff(
        "task-301",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=NAMED_PROMPT.format(base=base),
    )

    make("task-302", "A review with five open questions")
    manager.handoff(
        "task-302",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=(
            "The branch is green and rebased. Five decisions before I can carry on; "
            "the shell below is worth having open while you answer.\n"
            "\n"
            f"Shell to answer against: {base}/app/"
        ),
        questions=QUESTIONS,
    )

    make("task-303", "A review whose addresses are in links[], not in the prose")
    manager.update_task(
        "task-303",
        links=[
            Link(
                url="https://example.test/agentjobs/pull/9",
                rel=LinkRel.PR,
                title="PR #9 -- the shell",
            ),
            Link(url="https://example.test/ci/runs/4821", rel=LinkRel.BUILD, title=None),
            Link(
                url="https://example.test/handbook/reviewing",
                rel=LinkRel.DOC,
                title="How we review",
            ),
        ],
        actor="claude",
    )
    manager.handoff(
        "task-303",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Green on the third run. The pull request and the build are on the record; "
            "the handbook link is there for reference and is not part of this review."
        ),
    )

    make("task-304", "An ordinary review with nothing to go and look at")
    manager.handoff(
        "task-304",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=(
            "Two commits, both in storage.py, both covered. Read the diff and approve "
            "or send it back -- there is nothing to open in a browser."
        ),
    )

    make("task-305", "The same request, written before the link convention")
    manager.handoff(
        "task-305",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=UNNAMED_PROMPT.format(base=base),
    )


def build(root: Path, *, project_id: str, name: str, base: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(sandbox_store(project_root / "tasks")), base=base)
    return project_root


STATES = [
    ("task-301", "named link lines", "the card carries all three; the prose carries none"),
    ("task-302", "five questions", "one wraps, one numbered itself, one has no options"),
    ("task-303", "links[] only", "pr and build hoisted; the doc link stays below"),
    ("task-304", "no addresses", "no card at all -- the state that must stay quiet"),
    ("task-305", "the old way", "one address stuck in a sentence; compare with 301"),
]


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOST
    advertised = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BASE
    base = advertised.rstrip("/") or f"http://{host}:{port}"
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-links-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-links", "Sandbox: review links and questions"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name, base=base),
        project_id=project_id,
        name=name,
    )

    import uvicorn

    from agentjobs.api.main import app

    tasks = f"{base}/app/p/{project_id}/tasks"
    print(f"[review] review-links sandbox at {base}/app/", flush=True)
    print("[review] open it on the phone -- that is the screen this change is for.", flush=True)
    for task_id, state, note in STATES:
        print(f"[review]   {state:<16} {note}", flush=True)
        print(f"[review]     {tasks}/{task_id}", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    print("[review] stop with Ctrl-C; the data is deleted with the process.", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
