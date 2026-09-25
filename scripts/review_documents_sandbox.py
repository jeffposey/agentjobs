"""Stand up "Documents under review" on its own port, with a throwaway repo (task-594).

At a human review gate the review panel now renders each Markdown file in the task's
``deliverables[]`` as it stands at the head of the task's active branch.

    python scripts/review_documents_sandbox.py [port]

What to look at:

  * **task-001** -- at review, its branch adds ``docs/design.md``. The document renders
    with a heading, a table, a code block and links; the branch and short commit it was
    read at are above it. ``src/impl.py`` is listed as a path only. On a phone the
    document starts collapsed; tap its path to open it.
  * **task-002** -- at review with no active branch: the refusal shows in its place.
  * **task-003** -- at review, but the branch does not have the file.
  * **task-004** -- the same deliverables with the ball on the agent: no section at all.

The project root is a real git repository in a temporary directory; ``main`` is
checked out and does not have the document, so what renders came off the branch.
Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME``, deleted when this process stops.
Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 8972
PROJECT_ID = "sandbox-documents"
PROJECT_NAME = "Sandbox: documents under review"
USER = "Jeff Posey"
BRANCH = "feat/task-001-design"

DESIGN = """# Task kinds: design and implementation

The owner reviews a **document**, not a diff. This page is what a design task's
branch holds, rendered as the review panel shows it.

## 1. Decisions

| Candidate | Verdict | Where |
|---|---|---|
| Approve on a design task says it authorises no implementation | **In, reshaped.** | task-001 |
| A plan gate that approves without merging | **In.** New human reason `plan`. | task-001 |
| Dispatch defaults by kind | **Deferred** until there is evidence. | -- |

## 2. The approve payload

- The gate comes from the record, not the click.
- The payload gains one optional field: `gate: "plan" | "final"`.
  1. `final` means review or approval.
  2. `plan` means plan.

> A plan approval is not merge authority anywhere else either.

```python
def gate_for(task):
    return "plan" if task.ball_reason == "plan" else "final"
```

See [the accepted spec](https://example.com/spec), or the sibling
[task-kind notes](../notes.md) (a relative link, shown as text).

![a diagram that is not fetched](https://example.com/diagram.png)
"""


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Sandbox", "-c", "user.email=sandbox@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def seed_repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# Sandbox\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", BRANCH)
    (root / "docs").mkdir()
    (root / "docs" / "design.md").write_text(DESIGN, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "impl.py").write_text("print('not rendered')\n", encoding="utf-8")
    git(root, "add", "docs/design.md", "src/impl.py")
    git(root, "commit", "-q", "-m", "the design")
    git(root, "checkout", "-q", "main")


def seed(manager: Any) -> None:
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle

    deliverables = [
        {"path": "docs/design.md", "note": "the design under review"},
        {"path": "src/impl.py", "note": "a non-Markdown deliverable"},
    ]

    def task(number: int, title: str, branches: list, *, review: bool = True) -> None:
        task_id = f"task-{number:03d}"
        manager.create_task(
            id=task_id,
            title=title,
            summary=f"Seeded for the documents-under-review sandbox: {title}.",
            description="Seeded by scripts/review_documents_sandbox.py. Nothing here is real work.",
            lifecycle=Lifecycle.READY,
            actor="claude",
            category="ux",
        )
        manager.update_task(task_id, actor="claude", deliverables=deliverables, branches=branches)
        manager.claim_task(task_id, agent="claude")
        if review:
            manager.handoff(
                task_id,
                actor="claude",
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="The design is written. Read it below and approve or request changes.",
            )

    active = [{"name": BRANCH, "status": "active"}]
    task(1, "A design at review: the document renders", active)
    task(2, "At review with no active branch", [{"name": BRANCH, "status": "merged"}])
    task(
        3,
        "At review, branch lacks the file",
        [{"name": "main", "status": "active"}],
    )
    task(4, "Ball with the agent: no documents section", active, review=False)


def build(root: Path) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / PROJECT_ID
    project_root.mkdir(parents=True)
    seed_repo(project_root)
    (project_root / ".agentjobs").mkdir()
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=PROJECT_NAME, user=USER), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(sandbox_store(project_root / "tasks", project_id=PROJECT_ID)))
    return project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-documents-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    ProjectRegistry(home).add(build(root), project_id=PROJECT_ID, name=PROJECT_NAME)

    from sandbox_serve import review_base, serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    print(
        f"[review] documents sandbox at {review_base(port)}/app/p/{PROJECT_ID}/tasks/task-001",
        flush=True,
    )
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
