"""Stand up the answering form, on its own port, on a phone-shaped screen.

task-017 exists because of one afternoon: task-076 and task-077 both arrived at
`human`/`decision` with four substantive questions in the `ball_prompt`, and answering
them from a phone meant typing four paragraphs with a thumb. The claim this change makes
is that the same four questions are now four taps. That is not a claim a diff can settle,
and it is not one a desktop browser settles either -- so this seeds the states and says
to open it on the phone.

    task-201  the task-077 handoff, verbatim: four questions, one wanting a number
    task-202  one question, no options at all -- the prose question the corpus is full of
    task-203  three questions, two already answered: what a partly-answered task looks like
    task-204  human/review with a question still open, so answering is offered beside Approve
    task-205  human/decision with no questions: the prose box, exactly as before task-017

Every one of them is throwaway. Press anything, including the destructive controls --
nothing here touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops.

    python scripts/answer_questions_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **The questions are on the page when it opens.** No button reveals them, which is
    the change this second pass makes. On `task-205`, where nothing is asked, the
    prose composer is still behind "Answer Questions" exactly as before.
  * **On a phone.** The whole justification is thumb reach. Are the option buttons big
    enough to hit without aiming, and does the page scroll only downwards?
  * **task-201 answered entirely by tapping**, except question 3, which wants a number
    none of the options offer. That is the real case: on 2026-08-18 you took none of
    15 minutes, 4 hours or the hard kill, and typed 60. Try exactly that.
  * **"Something else" is under every question**, including the ones with options. If it
    is ever missing, the form is worse than the prose box it replaced.
  * **Nothing is preselected**, including the option marked Recommended. A recommendation
    that arrived pre-ticked is one you would submit without reading.
  * **Selecting does not submit.** Tap options, then Back out and return: nothing should
    have been recorded until you pressed "Send answers".
  * **task-203** shows only the question nobody has answered. The two with answers
    threaded to them are in the log, not in the form.
  * After submitting, read the log: one handoff, one `answer` entry per question, each
    threaded `re` to its question, and a `ball_prompt` that restates what you were asked
    and what you said -- even if you typed nothing at all.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_PORT = 8898

#: task-077's handoff of 2026-08-18, as it would be written today. Question 3 is the one
#: that made free text a constraint rather than a nicety.
TASK_077: List[Dict[str, Any]] = [
    {
        "body": "Does dispatch drive `claude agents` in session mode, or supervise its own `-p` processes?",
        "options": [
            {
                "label": "Session mode primary, batch retained",
                "description": "`--bg --remote-control`, with `-p` kept as a declared runner mode.",
                "recommended": True,
            },
            {
                "label": "Session-only, delete batch",
                "description": "One code path. Loses the spend ceiling and structured output.",
            },
            {
                "label": "Batch-only",
                "description": "Keeps the spend ceiling. No mid-flight redirect.",
            },
        ],
    },
    {
        "body": "Should runs still be killed when their supervisor restarts?",
        "options": [
            {
                "label": "No -- re-attach to whatever is in the ledger",
                "description": "`claude stop` is an independent kill switch, so the orphan premise is gone.",
                "recommended": True,
            },
            {
                "label": "Keep the rule",
                "description": "Consistent with the original design. Costs a real run per redeploy.",
            },
        ],
    },
    {
        "body": "How long may a session sit idle with an unmoved ball before it counts as stalled?",
        "placeholder": "a number of minutes",
        "options": [
            {"label": "15 minutes", "description": "More false positives if an agent pauses."},
            {"label": "4 hours", "description": "Only catches overnight stalls."},
            {"label": "Keep the 1800s hard kill", "description": "Kills the session outright."},
        ],
    },
    {
        "body": "What happens to task-070 and task-072?",
        "multi_select": True,
        "options": [
            {"label": "Rescope task-070 to batch mode", "recommended": True},
            {"label": "Rescope task-072 to our own ledger", "recommended": True},
            {"label": "Close task-072 as superseded"},
            {"label": "Close both and write one new wrapper task"},
        ],
    },
]


def seed(manager: Any) -> None:
    """One task per state worth looking at, and nothing else."""
    from agentjobs.models_v2 import AnswerDraft, Ball, BallReason, Lifecycle, Priority

    def make(task_id: str, title: str) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            description=(
                "Seeded so the answering form can be looked at in this state. Nothing "
                "here is real work, and every control on it is safe to press."
            ),
            summary=f"{title}.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.claim_task(task_id, agent="claude")

    def ask(task_id: str, prompt: str, questions: List[Dict[str, Any]], reason: Any = None) -> None:
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=reason or BallReason.DECISION,
            ball_prompt=prompt,
            questions=questions,
        )

    make("task-201", "The dispatch session launcher, as it was actually handed off")
    ask(
        "task-201",
        "sc-1 verified against CLI 2.1.228, with commands and output in the log. "
        "Four decisions before I can carry on; the recommendation on each is marked.",
        TASK_077,
    )

    make("task-202", "A plain question, with nothing offered")
    ask(
        "task-202",
        "One thing I cannot read from anywhere.",
        [{"body": "What is the production database called?"}],
    )

    make("task-203", "Partly answered: two down, one to go")
    ask(
        "task-203",
        "Three decisions. Two are already recorded; the third is still open.",
        TASK_077[:3],
    )
    # Answer the first two the way the GUI does, so the state under review is a real
    # one rather than a hand-built imitation of it.
    open_ids = [entry.id for entry in manager.get_task("task-203").open_questions()]
    manager.handoff(
        "task-203",
        actor="Jeff Posey",
        ball=Ball.AGENT,
        ball_reason=BallReason.ANSWER,
        ball_prompt="Two answered; the staleness threshold still needs a number.",
        answers=[
            AnswerDraft(re=open_ids[0], selected=["Session mode primary, batch retained"]),
            AnswerDraft(re=open_ids[1], selected=["No -- re-attach to whatever is in the ledger"]),
        ],
    )
    manager.handoff(
        "task-203",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt="Thanks. Still need the staleness threshold before I can build it.",
    )

    make("task-204", "A branch under review that also left a question open")
    ask(
        "task-204",
        "Branch is green and rebased. One thing I could not settle on my own.",
        [TASK_077[2]],
        reason=BallReason.REVIEW,
    )

    make("task-205", "A decision with no structured questions at all")
    manager.handoff(
        "task-205",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=(
            "Should the notification service be a separate process, or a thread inside "
            "the server? I have no strong view; say which and I will build it."
        ),
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
    seed(TaskManager(sandbox_store(project_root / "tasks")))
    return project_root


STATES = [
    ("task-201", "four questions", "the task-077 handoff; question 3 wants a number"),
    ("task-202", "one, no options", "the prose question the corpus is full of"),
    ("task-203", "partly answered", "only the unanswered one is in the form"),
    ("task-204", "human/review", "Approve stays first; answering joins the extras"),
    ("task-205", "no questions", "the prose box, exactly as before task-017"),
]


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-answer-questions-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-questions", "Sandbox: answering questions"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[review] answering sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print("[review] open it on the phone -- that is the screen this change is for.", flush=True)
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
